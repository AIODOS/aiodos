# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""`ai.comfy` : les travaux ComfyUI confiés à AIODOS (docs/api-applications.md § 5.7 ;
conception « partager la puissance », § 6.10).

AIODOS ne remplace pas le ComfyUI de l'appli : il rejoue ses graphes au format API dans le
ComfyUI accordé de ce PC (Réglages → Moteurs des applications ; mes machines suivront), et
garde les fichiers pour elle.

- `tokenize(graph, {nœud: octets})` met les jetons `@in:` dans l'entrée de fichier des nœuds
  de lecture donnés ; rend (copie du graphe, entrées) ;
- `plan(...)` : le pré-vol (rien n'est mis en file) ;
- `submit(...)` : envoie les entrées qui manquent, par empreinte, puis le travail → Job ;
- `run(...)` : submit, wait, download → les chemins des fichiers.

Un fichier ne se donne que par un jeton : `@in:<id>` (une entrée de `inputs`), `@seed` (la
graine de l'élément), `@model:<clé>` (avec `models`). Les recettes (L4), l'object_info et les
gabarits relayés (L2c) ne sont pas encore servis : NotSupported. Tant que `features`
n'annonce pas « engines.comfyui », tout lève NotSupported sans rien essayer : le repli de
l'appli (le ComfyUI de ce PC) se branche sur cette erreur.
"""

import copy
import logging
import re
import tempfile
from dataclasses import dataclass
from dataclasses import field

from . import errors, texts
from .access import normalize_where
from .inputs import FEATURE as INPUTS
from .inputs import MAX_INPUT, Source, as_input, check_input
from .jobs import FEATURE as JOBS
from .jobs import Job

log = logging.getLogger("aiodos_client")

FEATURE = "engines.comfyui"
ENGINE = "comfyui"
TOKEN_IN = "@in:"
# L'entrée de fichier des nœuds de lecture : celle qui prend le jeton @in:
FILE_LOADERS = {"LoadImage": "image", "LoadImageMask": "image", "LoadVideo": "file",
                "LoadAudio": "audio"}
PRIORITIES = ("high", "normal", "low")
SENSITIVE = ("here", "mine")
RETRY_LATER = ("engine_timeout", "no_machine")      # échecs d'unité qu'un autre essai peut lever
_INPUT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class Plan:
    """Le pré-vol d'un travail : peut-il partir (`ok`), où (`where_effective` ; pourquoi pas
    ailleurs : `where_reason`), ce qui manque (`missing` : [{folder, name, machine}]), et
    combien d'unités (`units`)."""
    ok: bool
    units: int | None = None
    where_effective: tuple = ()
    where_reason: str | None = None
    machines: dict = field(default_factory=dict, compare=False, hash=False)
    missing: tuple = field(default=(), compare=False, hash=False)
    refused: dict = field(default_factory=dict, compare=False, hash=False)
    licenses: tuple = field(default=(), compare=False, hash=False)
    estimate: dict | None = field(default=None, compare=False, hash=False)
    raw: dict = field(default_factory=dict, repr=False, compare=False, hash=False)


def plan_from(data, lang="fr") -> Plan:
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise errors.make("protocol", lang)
    units = data.get("units")
    return Plan(ok=data["ok"],
                units=units if isinstance(units, int) and not isinstance(units, bool) else None,
                where_effective=tuple(str(c) for c in data.get("where_effective") or ()),
                where_reason=data.get("where_reason")
                if isinstance(data.get("where_reason"), str) else None,
                machines=dict(data.get("machines") or {}),
                missing=tuple(m for m in data.get("missing") or () if isinstance(m, dict)),
                refused=dict(data.get("refused") or {}),
                licenses=tuple(data.get("licenses") or ()),
                estimate=data.get("estimate") if isinstance(data.get("estimate"), dict) else None,
                raw=dict(data))


def editor_format(graph) -> bool:
    """Un graphe au format de l'éditeur de ComfyUI (« nodes », « links ») plutôt qu'au format
    API (« Exporter (API) »)."""
    return isinstance(graph, dict) and isinstance(graph.get("nodes"), list) and "links" in graph


def _input_id(node_id, taken):
    base = ("node" + _UNSAFE_ID.sub("_", node_id))[:60]
    ident, n = base, 2
    while ident in taken:
        ident, n = f"{base}_{n}", n + 1
    taken.add(ident)
    return ident


def _failure(job, lang):
    """EngineFailed pour un travail sans aucune unité faite, avec l'erreur de ComfyUI."""
    failed = [u.error for u in job.units if u.status == "failed" and isinstance(u.error, dict)]
    reason = next((e.get("message") for e in failed if isinstance(e.get("message"), str)), None)
    if reason is None and isinstance(job.error, dict):
        reason = job.error.get("message") if isinstance(job.error.get("message"), str) else None
    message = texts.error("job_failed", lang)
    if reason:
        message = f"{message} {reason}"
    # À réessayer quand le temps ou les machines ont manqué, pas quand le graphe échoue
    later = bool(failed) and all(e.get("code") in RETRY_LATER for e in failed)
    return errors.make("engine_failed", lang, message=message, hint=texts.hint("job_failed", lang),
                       retryable=later, detail={"job": job.id, "errors": failed})


class Comfy:
    """Les travaux ComfyUI par AIODOS : `ai.comfy`."""

    def __init__(self, client):
        self._client = client

    # --- Pas encore servis ------------------------------------------------------------
    def object_info(self, cls=None):
        """L'object_info du ComfyUI de ce PC, ou de ma machine de référence (L2c) :
        NotSupported (« engines.comfyui.object_info »)."""
        self._client._refuse_unserved("engines.comfyui.object_info")

    def template(self, name):
        """Un gabarit de ComfyUI relayé par AIODOS (L2c) : NotSupported
        (« engines.comfyui.templates »)."""
        self._client._refuse_unserved("engines.comfyui.templates")

    def recipes(self):
        """Les recettes connues d'AIODOS (L4) : NotSupported (« recipes »)."""
        self._client._refuse_unserved("recipes")

    def recipe_for(self, template):
        """La recette qui correspond à un gabarit (L4) : NotSupported (« recipes »)."""
        self._client._refuse_unserved("recipes")

    # --- Jetons -----------------------------------------------------------------------
    def tokenize(self, graph, files):
        """Met les jetons d'entrée dans les nœuds de lecture donnés : `files` vaut
        {identifiant de nœud: octets, chemin ou {"sha256": …}}. Rend (copie du graphe,
        entrées), à donner à submit (`graph=`, `inputs=`) ; le graphe de l'appli n'est pas
        touché. Le jeton va dans l'entrée de fichier du nœud : `image` (LoadImage,
        LoadImageMask), `file` (LoadVideo), `audio` (LoadAudio)."""
        client = self._client
        client._gate(FEATURE)
        if not isinstance(graph, dict):
            raise TypeError("graph : un graphe au format API (dict)")
        if editor_format(graph):
            raise errors.make("graph_not_api", client.lang, detail={"graph": 0})
        if not isinstance(files, dict):
            raise TypeError("files : {identifiant de nœud: octets, chemin ou {\"sha256\": …}}")
        result = copy.deepcopy(graph)
        inputs, taken = {}, set()
        for node_id, value in files.items():
            key = str(node_id)
            node = result.get(key)
            if not isinstance(node, dict):
                raise ValueError(f"tokenize : pas de nœud « {key} » dans le graphe")
            name = FILE_LOADERS.get(node.get("class_type"))
            if name is None:
                raise ValueError(f"tokenize : le nœud « {key} » ({node.get('class_type')}) ne "
                                 "lit pas de fichier (LoadImage, LoadImageMask, LoadVideo, "
                                 "LoadAudio)")
            check_input(value)
            if not isinstance(node.get("inputs"), dict):
                node["inputs"] = {}
            ident = _input_id(key, taken)
            node["inputs"][name] = TOKEN_IN + ident
            inputs[ident] = value
        return result, inputs

    # --- Le corps d'un travail --------------------------------------------------------
    @staticmethod
    def _circles(where, sensitive):
        """Le lieu demandé (une liste de cercles, ou None) ; `sensitive` : ce PC et mes
        machines seulement."""
        circles = normalize_where(where)
        if sensitive:
            circles = [c for c in (circles or SENSITIVE) if c in SENSITIVE]
            if not circles:
                raise ValueError("sensitive : le lieu ne peut être que « here » ou « mine »")
        return circles

    def _place(self, circles, sensitive):
        """L'en-tête X-Aiodos-Where, qui resserre l'accord de l'appli sans jamais l'élargir.
        Pour `sensitive`, ce PC et mes machines dans ce que l'accord permet (WhereDenied, sans
        rien envoyer, s'il n'en reste rien)."""
        if circles is None:
            return {}
        if sensitive:
            granted = self._client._granted()
            if granted is not None:
                circles = [c for c in circles if c in granted]
            if not circles:
                lang = self._client.lang
                raise errors.make("where_denied", lang,
                                  message=texts.error("sensitive_denied", lang),
                                  detail={"allowed": list(granted or ())})
        return {"X-Aiodos-Where": ",".join(circles)}

    def _prepare(self, graph, graphs, items, repeat, inputs, models, priority, label,
                 client_ref, bounds, strict):
        """Le corps d'un travail (sans les entrées, envoyées à part) et les entrées : {id:
        empreinte donnée, ou Source à envoyer}. Les erreurs de l'appelant : TypeError,
        ValueError ; une entrée de plus de 100 Mo : InputTooLarge, avant tout envoi."""
        lang = self._client.lang
        if (graph is None) == (graphs is None):
            raise ValueError("un graphe : « graph », ou « graphs » (64 au plus), pas les deux")
        body = {"engine": ENGINE}
        if graph is not None:
            if not isinstance(graph, dict):
                raise TypeError("graph : un graphe au format API (dict)")
            body["graph"] = graph
        else:
            graphs = list(graphs)
            if not graphs or not all(isinstance(g, dict) for g in graphs):
                raise TypeError("graphs : une liste de graphes au format API (dict)")
            body["graphs"] = graphs
        if label is not None:
            body["label"] = str(label)
        if client_ref is not None:
            if not isinstance(client_ref, str) or not 1 <= len(client_ref) <= 200:
                raise ValueError("client_ref : un texte de 1 à 200 caractères")
            body["client_ref"] = client_ref
        if items is not None and repeat is not None:
            raise ValueError("« items » ou « repeat », pas les deux")
        if items is not None:
            items = list(items)
            if not items or not all(isinstance(item, dict) for item in items):
                raise TypeError("items : une liste d'éléments {graph, label, seed, set}")
            body["items"] = items
        if repeat is not None:
            if not isinstance(repeat, dict):
                raise TypeError("repeat : {count, vary: [{node, input, step}]}")
            body["repeat"] = repeat
        if models != "auto":
            if not isinstance(models, dict):
                raise TypeError("models : « auto », ou {clé: {folder, name}}")
            body["models"] = models
        if priority not in PRIORITIES:
            raise ValueError("priority : « high », « normal » ou « low »")
        if priority != "normal":
            body["priority"] = priority
        if bounds is not None:
            if not isinstance(bounds, dict):
                raise TypeError("bounds : {unit_seconds, output_mb, wait_s}")
            body["bounds"] = bounds
        if strict:
            body["strict"] = True
        wanted = {}
        if inputs:
            if not isinstance(inputs, dict):
                raise TypeError("inputs : {identifiant: octets, chemin ou {\"sha256\": …}}")
            for key, value in inputs.items():
                if not isinstance(key, str) or not _INPUT_ID.match(key):
                    raise ValueError(f"inputs : identifiant invalide {key!r} (A-Z, a-z, 0-9, "
                                     "« _ », « . », « - », 64 au plus)")
                wanted[key] = as_input(value)
            for value in wanted.values():
                if isinstance(value, Source) and value.size > MAX_INPUT:
                    raise errors.make("input_too_large", lang,
                                      detail={"bytes": value.size, "max_bytes": MAX_INPUT})
        return body, wanted

    def _send_inputs(self, wanted, cancel):
        """Envoie les entrées qui manquent (HEAD, puis PUT) → {id: {"sha256": …}}."""
        return {key: {"sha256": value if isinstance(value, str)
                      else self._client.inputs.put(value, cancel=cancel)}
                for key, value in wanted.items()}

    def _ready(self, wanted, *features):
        """La preuve faite et ce qu'il faut annoncé (« inputs » s'il y a des entrées à
        envoyer), avant toute autre route."""
        uploads = (INPUTS,) if any(isinstance(v, Source) for v in wanted.values()) else ()
        self._client._gate(*features, *uploads)

    # --- Pré-vol, dépôt, tout d'un coup ----------------------------------------------
    def plan(self, graph=None, graphs=None, recipe=None, items=None, repeat=None, inputs=None,
             models="auto", where=None, priority="normal", label=None, client_ref=None,
             bounds=None, strict=False, sensitive=False, cancel=None):
        """Le pré-vol d'un travail (le corps de submit) : où il peut tourner, ce qui manque,
        combien d'unités ; rien n'est mis en file (Plan). Les entrées qui manquent sont
        envoyées d'abord : AIODOS les vérifie aussi."""
        if recipe is not None:
            self._client._refuse_unserved("recipes")
        circles = self._circles(where, sensitive)
        body, wanted = self._prepare(graph, graphs, items, repeat, inputs, models, priority,
                                     label, client_ref, bounds, strict)
        self._ready(wanted, FEATURE)
        headers = self._place(circles, sensitive)
        if wanted:
            body["inputs"] = self._send_inputs(wanted, cancel)
        data = self._client._call("POST", "/aiodos/v1/engines/comfyui/plan", body=body,
                                  feature=FEATURE, headers=headers, cancel=cancel)
        return plan_from(data, self._client.lang)

    def submit(self, graph=None, graphs=None, recipe=None, items=None, repeat=None, inputs=None,
               models="auto", where=None, priority="normal", label=None, client_ref=None,
               bounds=None, strict=False, sensitive=False, cancel=None):
        """Dépose un travail ComfyUI et rend son Job (« queued »). Le graphe (`graph`, ou
        `graphs`) au format API, déjà rempli. `inputs` : {id: octets, chemin ou {"sha256"}},
        cités par les jetons `@in:<id>` ; leurs empreintes sont calculées, celles qui
        manquent envoyées (100 Mo au plus chacune). `items` : une unité par élément
        ({graph, label, seed, set: [[nœud, entrée, valeur]]}) ; ou `repeat` ({count, vary}).
        `models` : « auto » (AIODOS relève les noms) ou {clé: {folder, name}} pour les jetons
        `@model:<clé>`. `where` resserre le lieu de calcul (X-Aiodos-Where ; plus large que
        l'accord : WhereDenied) ; `sensitive=True` le limite à ce PC et à mes machines
        (« here », « mine »), dans ce que l'accord permet. `priority` : high, normal ou low.
        Le même `client_ref` rend le même travail (idempotence). Les recettes (`recipe`, L4)
        ne sont pas encore servies : NotSupported."""
        if recipe is not None:
            self._client._refuse_unserved("recipes")
        circles = self._circles(where, sensitive)
        body, wanted = self._prepare(graph, graphs, items, repeat, inputs, models, priority,
                                     label, client_ref, bounds, strict)
        self._ready(wanted, FEATURE, JOBS)
        headers = self._place(circles, sensitive)
        if wanted:
            body["inputs"] = self._send_inputs(wanted, cancel)
        client = self._client
        data = client._call("POST", "/aiodos/v1/jobs", body=body, feature=(FEATURE, JOBS),
                            headers=headers, cancel=cancel)
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            raise errors.make("protocol", client.lang)
        job = Job(client, data["id"], data)
        log.info("Travail ComfyUI %s déposé (%d unités).", job.id, job.progress.total)
        return job

    def run(self, graph=None, graphs=None, recipe=None, items=None, repeat=None, inputs=None,
            models="auto", where=None, priority="normal", label=None, client_ref=None,
            bounds=None, strict=False, sensitive=False, dest_dir=None, stem=None, cancel=None,
            timeout=None, on_file=None):
        """submit(), puis wait(), puis download() : rend les chemins des fichiers
        (Downloads, avec `job`). `dest_dir` absent : un dossier temporaire neuf, à vider par
        l'appli. Lève EngineFailed si aucune unité n'a abouti (l'erreur de ComfyUI dans le
        message et `detail["errors"]`), Cancelled si le travail a été annulé (par l'appli :
        `cancel`, ou dans AIODOS), Timeout au-delà de `timeout` secondes."""
        job = self.submit(graph=graph, graphs=graphs, recipe=recipe, items=items, repeat=repeat,
                          inputs=inputs, models=models, where=where, priority=priority,
                          label=label, client_ref=client_ref, bounds=bounds, strict=strict,
                          sensitive=sensitive, cancel=cancel)
        job.wait(timeout=timeout, cancel=cancel)
        lang = self._client.lang
        if job.status == "cancelled":
            raise errors.make("cancelled", lang, detail={"job": job.id, "by": job.cancelled_by})
        if job.status == "failed":
            raise _failure(job, lang)
        folder = dest_dir if dest_dir is not None else tempfile.mkdtemp(prefix="aiodos-")
        return job.download(folder, stem=stem, on_file=on_file, cancel=cancel)
