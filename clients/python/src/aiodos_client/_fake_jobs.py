# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Les travaux ComfyUI du faux AIODOS (mêlés à FakeAiodos, aiodos_client.fake) : moteurs,
entrées, travaux, événements et fichiers, en mémoire, comme les sert la passerelle
(docs/api-applications.md § 5.7, clients/contract/jobs.json).

- Les entrées sont rangées par appli : une appli ne voit jamais celles d'une autre, ni ses
  travaux, ni ses fichiers (404).
- Un travail avance seul : ses unités passent une à une, `job_delay` secondes chacune (0,05 s
  par défaut) ; avec `job_delay=None`, il n'avance que par advance(). Chaque unité faite rend
  un petit PNG (png()), sauf celles que fail_unit() fait échouer.
- Les événements (SSE) : « job » (l'état complet, puis à chaque changement), « file » (un
  nouveau fichier), « end » ; « : ping » toutes les `ping_every` secondes.
- Les fichiers se servent par plages (Range) ; servis jusqu'au dernier octet, ils sont
  récupérés (`fetched`).
- Les commandes : pause (après l'unité en cours, ou tout de suite), reprise, annulation,
  relance des unités en échec, oubli ; le même client_ref (ou Idempotency-Key) rend le même
  travail.
- Le lieu, comme la passerelle : X-Aiodos-Where resserre l'accord de la clé, puis seuls
  restent les cercles servis aux travaux ComfyUI (`comfy_circles` : ce PC et mes machines) ;
  un « where » dans le corps ne peut que resserrer ce lieu-là. Les unités se calculent sur la
  première machine permise qui a ComfyUI et tous les modèles : ce PC, puis mes machines
  (`comfy_peers`).
- Pour les essais : `comfyui` (ComfyUI accordé et ouvert sur ce PC, ou non), `comfy_models`
  (ses modèles), `comfy_peers` (mes autres machines qui ont ComfyUI : [{"name", "models"}]),
  job_delay (lenteur), fail_unit() (une unité en échec, no_machine compris), cut_events() (le
  flux se coupe), cut_content() (un téléchargement se coupe).
"""

import hashlib
import json
import re
import secrets
import struct
import time
import zlib
from urllib.parse import unquote

from .inputs import sniff

JOB_FEATURES = ("engines", "engines.comfyui", "jobs", "jobs.pause", "inputs", "files")
SCOPE = "engines:comfyui"
PREFIXES = ("/aiodos/v1/engines", "/aiodos/v1/inputs/", "/aiodos/v1/jobs", "/aiodos/v1/files/")
MAX_INPUT = 100 * 1024 * 1024
INPUT_QUOTA = 10 * 1024 ** 3
MAX_ACTIVE = 50
MAX_GRAPHS = 64
MAX_UNITS = 256
COMFY_VERSION = "0.35.1"
KEEP_DAYS = 7
FIELDS = {"engine", "label", "client_ref", "where", "priority", "graph", "graphs", "inputs",
          "models", "items", "repeat", "requires", "bounds", "strict", "recipe"}
INPUT_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "video/mp4", "video/webm",
               "audio/wav", "audio/mpeg", "audio/flac", "audio/ogg"}
LOADERS = {"LoadImage": "image", "LoadImageMask": "image", "LoadVideo": "file",
           "LoadAudio": "audio"}
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
CIRCLES = ("here", "mine", "friends", "public")
FINISHED = ("completed", "failed", "cancelled")
RETRYABLE = {"model_unavailable", "engine_unavailable", "quota_exceeded"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
INPUT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_JOB = re.compile(r"^/aiodos/v1/jobs/([^/]+)(?:/(events|files|pause|resume|cancel|retry))?$")
_FILE = re.compile(r"^/aiodos/v1/files/([^/]+)(/content)?$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_ANNOTATION = re.compile(r"\[\s*(output|input|temp)\s*\]\s*$", re.IGNORECASE)
MODELS = ({"folder": "checkpoints", "name": "sd_xl_base_1.0.safetensors"},)


def png(width=8, height=8, color=(200, 120, 40)) -> bytes:
    """Un vrai PNG d'une seule couleur (RVB, 8 bits), sans bibliothèque : « {png} » du contrat
    (clients/contract/README.md) avec les valeurs par défaut."""
    line = b"\x00" + bytes(color) * width          # filtre 0, puis les pixels de la ligne

    def block(tag, payload):
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + block(b"IHDR", header)
            + block(b"IDAT", zlib.compress(line * height)) + block(b"IEND", b""))


def _b64id(prefix, size):
    return prefix + secrets.token_urlsafe(size)


class _Refused(Exception):
    """Une demande refusée, avec son code stable (comme gateway/errors.ApiError)."""

    def __init__(self, code, message, status=None, param=None, **detail):
        super().__init__(message)
        self.code, self.message, self.status, self.param, self.detail = (code, message, status,
                                                                          param, detail)


def _is_link(value):
    return (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
            and isinstance(value[1], int) and not isinstance(value[1], bool))


def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list) and not _is_link(value):
        for item in value:
            yield from _texts(item)


def _forbidden(text):
    """Pourquoi une valeur est refusée (chemin, adresse, annotation), ou None."""
    lowered = text.strip().lower()
    if "\x00" in text:
        return "nul"
    if _ANNOTATION.search(text.strip()):
        return "annotation"
    if "://" in lowered or lowered.startswith(("file:", "http", "ftp", "www.")):
        return "url"
    if lowered.startswith("data:"):
        return "data_url"
    if lowered.startswith(("/", "\\")) or _DRIVE.match(lowered):
        return "absolute_path"
    if ".." in re.split(r"[\\/]", lowered):
        return "parent_path"
    return None


class FakeJobs:
    """Les travaux ComfyUI de FakeAiodos (voir l'en-tête du module) ; s'appuie sur ce que
    FakeAiodos fournit : _lock, _keyed, _error, _send, _send_json, _chunk, machine,
    features, stream_closed."""

    def _init_jobs(self, job_delay=0.05, ping_every=5.0):
        self.job_delay = job_delay
        self.ping_every = ping_every
        self.comfyui = True
        self.comfy_models = [dict(m) for m in MODELS]
        self.comfy_circles = ("here", "mine")      # servis aux travaux ComfyUI
        self.comfy_peers = []       # mes autres machines qui ont ComfyUI : [{"name", "models"}]
        self._inputs = {}           # appli -> {sha256: {"size", "type", "data"}}
        self._jobs = {}             # identifiant -> travail (interne)
        self._files = {}            # identifiant -> fichier (interne)
        self._next_failures = []    # [(unité, code, message)] du prochain travail
        self._event_cuts = []       # nombre d'événements avant la coupure des prochains flux
        self._content_cuts = []     # octets avant la coupure des prochains contenus
        self._seq = 0               # ordre de dépôt (les plus récents d'abord)
        self.last_job_id = None

    # --- Pour les essais -----------------------------------------------------------------
    def advance(self, job_id=None, steps=1) -> dict:
        """Fait avancer un travail (le dernier déposé, par défaut) : à chaque pas, l'unité en
        cours finit (son fichier arrive) et la suivante commence ; sans unité en cours, la
        suivante commence. Rend l'objet travail."""
        with self._lock:
            job = self._jobs[job_id or self.last_job_id]
            for _ in range(steps):
                running = _running(job)
                if running is not None:
                    self._finish(job, running, time.monotonic())
                if not job["paused"] and not job["cancelled"]:
                    queued = _queued(job)
                    if queued is not None:
                        self._start(job, queued, time.monotonic())
            return self._public(job)

    def fail_unit(self, index, code="engine_error", message=None, job_id=None):
        """L'unité `index` échoue au lieu de rendre son fichier (`code` : engine_error,
        engine_timeout, output_invalid, output_too_large, no_machine) : celle de ce travail,
        ou du prochain déposé."""
        message = message or "KSampler : CUDA out of memory (nœud 3)"
        with self._lock:
            if job_id is None:
                self._next_failures.append((index, code, message))
            else:
                self._jobs[job_id]["failures"][index] = (code, message)

    def cut_events(self, after=1, times=1):
        """Les `times` prochains flux d'événements se coupent après `after` événements."""
        with self._lock:
            self._event_cuts.extend([after] * times)

    def cut_content(self, after=16, times=1):
        """Les `times` prochains contenus de fichier se coupent après `after` octets."""
        with self._lock:
            self._content_cuts.extend([after] * times)

    def job_record(self, job_id=None) -> dict:
        """L'objet travail tel que le servirait la passerelle (le dernier déposé, par
        défaut)."""
        with self._lock:
            job = self._jobs[job_id or self.last_job_id]
            self._tick(job)
            return self._public(job)

    def inputs_of(self, key) -> dict:
        """Les entrées reçues de l'appli de cette clé : {sha256: {"size", "type"}}."""
        with self._lock:
            record = self._keys.get(key)
            found = self._inputs.get(record["id"], {}) if record else {}
            return {sha: {"size": v["size"], "type": v["type"]} for sha, v in found.items()}

    # --- Les routes ------------------------------------------------------------------------
    def _jobs_route(self, h, method, path, query, body, raw, key):
        if path == "/aiodos/v1/engines":
            feature = "engines"
        elif path.startswith("/aiodos/v1/engines/"):
            feature = "engines.comfyui"
        elif path.startswith("/aiodos/v1/inputs/"):
            feature = "inputs"
        elif path.startswith("/aiodos/v1/files/"):
            feature = "files"
        else:
            feature = "jobs.pause" if path.endswith(("/pause", "/resume")) else "jobs"
        if feature not in self.features:
            return self._error(h, path, "not_supported",
                               message="Cette version d'AIODOS ne sert pas cette route.")
        record = self._keyed(h, path, key, SCOPE)
        if record is None:
            return None
        app = record["id"]
        try:
            return self._dispatch(h, method, path, query, body, raw, record, app)
        except _Refused as exc:
            return self._error(h, path, exc.code, status=exc.status, message=exc.message,
                               param=exc.param, detail=exc.detail, hint=FAKE_HINTS.get(exc.code),
                               retryable=True if exc.code in RETRYABLE else None)

    def _dispatch(self, h, method, path, query, body, raw, record, app):
        if path == "/aiodos/v1/engines" and method == "GET":
            where = self._where(h, record, {})
            return self._send_json(h, 200, {"object": "list", "engines": [self._engine(where)]})
        if path == "/aiodos/v1/engines/comfyui/plan" and method == "POST":
            return self._send_json(h, 200, self._plan(h, record, body))
        if path.startswith("/aiodos/v1/inputs/"):
            return self._input(h, method, app, unquote(path[len("/aiodos/v1/inputs/"):]), raw)
        if path == "/aiodos/v1/jobs":
            if method == "POST":
                return self._submit(h, record, body)
            if method == "GET":
                return self._send_json(h, 200, {"object": "list",
                                                "jobs": self._list(app, query)})
        if path in ("/aiodos/v1/jobs/pause", "/aiodos/v1/jobs/resume") and method == "POST":
            return self._all(h, app, path.rsplit("/", 1)[1], body)
        found = _JOB.match(path)
        if found:
            return self._job_route(h, method, app, unquote(found.group(1)), found.group(2),
                                   query, body)
        found = _FILE.match(path)
        if found:
            return self._file_route(h, method, app, unquote(found.group(1)),
                                    bool(found.group(2)))
        return self._error(h, path, "not_supported",
                           message="Cette version d'AIODOS ne sert pas cette route.")

    def _not_allowed(self, h):
        return self._send_json(h, 405, {"detail": "Method Not Allowed"})

    # --- Moteurs, pré-vol ------------------------------------------------------------------
    def _machines(self, where):
        """Les machines permises qui ont ComfyUI accordé : [{"name", "place", "models"}],
        ce PC d'abord."""
        found = []
        if "here" in where and self.comfyui:
            found.append({"name": self.machine, "place": "here", "models": self.comfy_models})
        if "mine" in where:
            found += [{"name": str(p.get("name") or "Portable"), "place": "mine",
                       "models": list(p.get("models", MODELS))} for p in self.comfy_peers]
        return found

    def _engine(self, where):
        on = bool(self.comfyui) and "here" in where
        machines = self._machines(where)
        models = {}
        for machine in machines:
            for model in machine["models"]:
                key = (model.get("folder"), model["name"].replace("\\", "/"))
                entry = models.setdefault(key, {
                    "folder": key[0], "name": key[1], "sha256": None, "size": None,
                    "where": {"here": False, "mine": [] if "mine" in where else None,
                              "friends": None, "public": None}, "license": None})
                if machine["place"] == "here":
                    entry["where"]["here"] = True
                elif machine["name"] not in entry["where"]["mine"]:
                    entry["where"]["mine"].append(machine["name"])
        state = ({"enabled": True, "version": COMFY_VERSION, "vram_mib": 8187} if on
                 else {"enabled": bool(self.comfyui), "version": None, "vram_mib": None})
        return {"id": "comfyui", "label": "ComfyUI",
                "here": state if "here" in where else None,
                "machines": {"here": 1 if on else 0,
                             "mine": sum(1 for m in machines if m["place"] == "mine")
                             if "mine" in where else None,
                             "friends": None, "public": None},
                "models": [models[k] for k in sorted(models, key=lambda k: (k[0] or "", k[1]))],
                "object_info": {"available": on, "machine": self.machine if on else None,
                                "digest": "c0a1f3e29b7d4e51" if on else None},
                "recipes": 0}

    def _plan(self, h, record, body):
        where = self._where(h, record, {})          # comme la passerelle : l'accord, l'en-tête
        checked = self._check(record["id"], body)
        machines = self._machines(where)
        gaps = [(self._missing(checked, m["models"]), m) for m in machines]
        able = [m for missing, m in gaps if not missing]
        reason = None if able else ("aucune machine permise n'a ComfyUI accordé à AIODOS"
                                    if not machines else "il manque des modèles")
        return {"ok": bool(able),
                "where_effective": [c for c in CIRCLES if any(m["place"] == c for m in able)],
                "where_reason": reason,
                "machines": {"here": any(m["place"] == "here" for m in able),
                             "mine": sum(1 for m in able if m["place"] == "mine")
                             if "mine" in where else None,
                             "friends": None, "public": None},
                "missing": [{**x, "machine": m["name"]} for missing, m in gaps for x in missing],
                "refused": {}, "licenses": [], "units": len(checked["items"]), "estimate": None}

    def _where(self, h, record, body):
        """Le lieu effectif, comme la passerelle : X-Aiodos-Where resserre l'accord (plus
        large : where_denied), puis seuls restent les cercles servis aux travaux ComfyUI
        (`comfy_circles` : ce PC et mes machines) ; un « where » dans le corps ne peut que
        resserrer ce lieu-là (plus large : where_denied)."""
        granted = list(record["where"])
        asked = [c.strip() for c in (h.headers.get("X-Aiodos-Where") or "").split(",")
                 if c.strip()]
        if asked:
            wider = [c for c in asked if c not in granted]
            if wider:
                raise _Refused("where_denied", "Lieu de calcul refusé : cette application n'a "
                               f"pas accès à « {wider[0]} ».", allowed=granted)
            granted = [c for c in granted if c in asked]
        allowed = [c for c in granted if c in self.comfy_circles]
        given = body.get("where") if isinstance(body, dict) else None
        if isinstance(given, str):
            given = [c.strip() for c in given.split(",") if c.strip()]
        if given:
            if any(c not in allowed for c in given):
                raise _Refused("where_denied", "Lieu de calcul refusé : plus large que l'accès "
                               "de cette application.", allowed=allowed)
            allowed = [c for c in allowed if c in given]
        return allowed

    # --- Vérifier un travail -----------------------------------------------------------
    def _check(self, app, body):
        """Le travail vérifié comme le fait AIODOS avant toute mise en file → {graphs,
        items, inputs, models} ; _Refused sinon."""
        if not isinstance(body, dict):
            raise _Refused("invalid_request", "Corps JSON attendu : un objet.")
        unknown = sorted(set(body) - FIELDS)
        if unknown:
            raise _Refused("invalid_request", f"Champ inconnu : « {unknown[0]} ».",
                           param=unknown[0])
        if body.get("engine", "comfyui") != "comfyui":
            raise _Refused("engine_not_found", f"Moteur inconnu : « {body.get('engine')} » "
                           "(connu : comfyui).", param="engine")
        graphs = self._graphs(body)
        inputs = self._declared_inputs(app, body)
        items = self._items(body, graphs)
        models = body.get("models", "auto")
        if models != "auto" and not isinstance(models, dict):
            raise _Refused("invalid_request", "« models » : \"auto\" ou {clé: {folder, name}}.",
                           param="models")
        for i, graph in enumerate(graphs):
            for node in graph.values():
                for value in node["inputs"].values():
                    for text in _texts(value):
                        if text.startswith("@in:") and text[4:] not in inputs:
                            raise _Refused("input_missing", f"Jeton @in:{text[4:]} sans entrée "
                                           "déclarée dans « inputs ».", param="inputs",
                                           graph=i, input=text[4:])
                        if text.startswith("@model:") and (models == "auto"
                                                           or text[7:] not in models):
                            raise _Refused("model_unresolved", "Des jetons @model: demandent "
                                           "« models ».", status=409, param="models", graph=i)
            if not any(str(n["class_type"]).startswith("Save") for n in graph.values()):
                raise _Refused("graph_invalid", "Ce graphe n'a aucun nœud de sortie "
                               "(SaveImage…).", param="graphs", graph=i)
        for n, item in enumerate(items):
            patched = self._patched(graphs[item["graph"]], item["set"], n)
            for node_id, node in patched.items():
                loader = LOADERS.get(node["class_type"])
                for name, value in node["inputs"].items():
                    if name == loader:
                        if not (isinstance(value, str) and value.startswith("@in:")):
                            raise _Refused("forbidden_value", "Un fichier d'entrée se donne par "
                                           "un jeton @in:.", param="items", item=n,
                                           node=node_id, input=name, reason="file_input")
                        continue
                    if name == "filename_prefix":
                        continue
                    for text in _texts(value):
                        if text.startswith(("@in:", "@model:")) or text == "@seed":
                            continue
                        reason = _forbidden(text)
                        if reason:
                            raise _Refused("forbidden_value", "Valeur refusée (chemin, adresse "
                                           "ou annotation de dossier).", param="items", item=n,
                                           node=node_id, input=name, reason=reason)
        if body.get("priority", "normal") not in ("high", "normal", "low"):
            raise _Refused("invalid_request", "« priority » : high, normal ou low.",
                           param="priority")
        for name in ("label", "client_ref"):
            value = body.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > 200
                                      or (name == "client_ref" and not value)):
                raise _Refused("invalid_request", f"« {name} » : un texte de 200 caractères "
                               "au plus.", param=name)
        bounds = body.get("bounds")
        if bounds is not None and (not isinstance(bounds, dict)
                                   or set(bounds) - {"unit_seconds", "output_mb", "wait_s"}):
            raise _Refused("invalid_request", "« bounds » : {unit_seconds, output_mb, wait_s}.",
                           param="bounds")
        return {"graphs": graphs, "items": items, "inputs": inputs, "models": models}

    def _graphs(self, body):
        if "graph" in body and "graphs" in body:
            raise _Refused("invalid_request", "« graph » ou « graphs », pas les deux.",
                           param="graphs")
        if "recipe" in body:
            raise _Refused("not_supported", "Les recettes arriveront avec une version suivante "
                           "d'AIODOS : envoie un graphe au format API.", param="recipe")
        graphs = [body["graph"]] if "graph" in body else body.get("graphs")
        if not isinstance(graphs, list) or not graphs:
            raise _Refused("invalid_request", "Un graphe au moins (« graph » ou « graphs »).",
                           param="graphs")
        if len(graphs) > MAX_GRAPHS:
            raise _Refused("graph_too_large", f"{MAX_GRAPHS} graphes au plus par travail.",
                           param="graphs")
        for i, graph in enumerate(graphs):
            if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) \
                    and "links" in graph:
                raise _Refused("graph_not_api", "Graphe au format de l'éditeur : exporte-le au "
                               "format API (« Exporter (API) » dans ComfyUI).", param="graphs",
                               graph=i)
            if not isinstance(graph, dict) or not graph:
                raise _Refused("graph_invalid", "Graphe vide ou illisible.", param="graphs",
                               graph=i)
            for node_id, node in graph.items():
                if (not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                        or not isinstance(node.get("inputs", {}), dict)):
                    raise _Refused("graph_invalid", "Nœud illisible.", param="graphs", graph=i,
                                   node=str(node_id))
                for value in node.get("inputs", {}).values():
                    if _is_link(value) and value[0] not in graph:
                        raise _Refused("graph_invalid", f"Lien vers un nœud absent "
                                       f"({value[0]}).", param="graphs", graph=i,
                                       node=str(node_id))
            for node in graph.values():
                node.setdefault("inputs", {})
        return graphs

    def _declared_inputs(self, app, body):
        given = body.get("inputs") or {}
        if not isinstance(given, dict):
            raise _Refused("invalid_request", "« inputs » : {identifiant: {sha256}}.",
                           param="inputs")
        known = self._inputs.get(app, {})
        for name, value in given.items():
            sha = value.get("sha256") if isinstance(value, dict) else None
            if not INPUT_ID.match(str(name)) or not isinstance(sha, str) \
                    or not SHA256.match(sha):
                raise _Refused("invalid_request", "Entrée illisible : {identifiant: "
                               "{\"sha256\": …}}.", param="inputs", input=str(name))
            if sha not in known:
                raise _Refused("input_missing", f"Entrée « {name} » jamais envoyée : PUT "
                               f"/aiodos/v1/inputs/{sha} d'abord.", param="inputs", input=name)
        return {name: value["sha256"] for name, value in given.items()}

    def _items(self, body, graphs):
        if "items" in body and "repeat" in body:
            raise _Refused("invalid_request", "« items » ou « repeat », pas les deux.",
                           param="items")
        if "repeat" in body:
            repeat = body["repeat"]
            count = repeat.get("count") if isinstance(repeat, dict) else None
            if not isinstance(count, int) or isinstance(count, bool) \
                    or not 1 <= count <= MAX_UNITS:
                raise _Refused("invalid_request", f"« repeat.count » : de 1 à {MAX_UNITS}.",
                               param="repeat")
            index = repeat.get("graph", 0)
            graph = graphs[index] if isinstance(index, int) and 0 <= index < len(graphs) else None
            if graph is None:
                raise _Refused("invalid_request", "« repeat.graph » : l'indice d'un des "
                               "graphes.", param="repeat")
            vary = repeat.get("vary") or []
            for v in vary:
                current = (graph.get(v.get("node")) or {}).get("inputs", {}).get(
                    v.get("input")) if isinstance(v, dict) else None
                if isinstance(current, bool) or not isinstance(current, (int, float)) \
                        or not isinstance(v.get("step"), (int, float)):
                    raise _Refused("patch_invalid", "« vary » : un nombre déjà présent dans le "
                                   "graphe, et un pas.", param="repeat")
            return [{"graph": index, "label": None, "seed": None,
                     "set": [[v["node"], v["input"],
                              graph[v["node"]]["inputs"][v["input"]] + i * v["step"]]
                             for v in vary]} for i in range(count)]
        items = body.get("items")
        if items is None:
            return [{"graph": i, "label": None, "seed": None, "set": []}
                    for i in range(len(graphs))]
        if not isinstance(items, list) or not items or len(items) > MAX_UNITS:
            raise _Refused("invalid_request", f"« items » : de 1 à {MAX_UNITS} éléments.",
                           param="items")
        out = []
        for n, item in enumerate(items):
            if not isinstance(item, dict) or set(item) - {"graph", "label", "seed", "set"}:
                raise _Refused("invalid_request", "Élément illisible : {graph, label, seed, "
                               "set}.", param="items", item=n)
            index = item.get("graph", 0)
            if not isinstance(index, int) or isinstance(index, bool) \
                    or not 0 <= index < len(graphs):
                raise _Refused("invalid_request", "« graph » : l'indice d'un des graphes.",
                               param="items", item=n)
            seed = item.get("seed")
            if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)
                                     or seed < 0):
                raise _Refused("invalid_request", "« seed » : un entier positif.",
                               param="items", item=n)
            out.append({"graph": index, "label": item.get("label"), "seed": seed,
                        "set": item.get("set") or []})
        return out

    def _patched(self, graph, patches, n):
        result = {k: {**v, "inputs": dict(v["inputs"])} for k, v in graph.items()}
        if not isinstance(patches, list):
            raise _Refused("patch_invalid", "« set » : [[nœud, entrée, valeur]].",
                           param="items", item=n)
        for patch in patches:
            if not isinstance(patch, list) or len(patch) != 3:
                raise _Refused("patch_invalid", "« set » : [[nœud, entrée, valeur]].",
                               param="items", item=n)
            node, name, value = patch
            current = (result.get(node) or {}).get("inputs", {}).get(name, _ABSENT)
            if current is _ABSENT or _is_link(current) or type(current) is not type(value):
                raise _Refused("patch_invalid", "Une retouche remplace une valeur déjà "
                               "présente, du même type, jamais un lien.", param="items",
                               item=n, node=node, input=name)
            result[node]["inputs"][name] = value
        return result

    def _missing(self, checked, models):
        """Les modèles du travail qu'une machine (ses `models`) n'a pas : [{folder, name}]."""
        have = {(m.get("folder"), m["name"].replace("\\", "/")) for m in models}
        names = {name for _, name in have}
        wanted = []
        if isinstance(checked["models"], dict):
            for value in checked["models"].values():
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    wanted.append((value.get("folder"), value["name"]))
        else:
            for graph in checked["graphs"]:
                for node in graph.values():
                    loader = LOADERS.get(node["class_type"])
                    for name, value in node["inputs"].items():
                        if name != loader and isinstance(value, str) \
                                and value.lower().endswith(MODEL_EXT):
                            folders = [f for f, n in have if n == value.replace("\\", "/")]
                            wanted.append((folders[0] if folders else None, value))
        missing = []
        for folder, name in wanted:
            key = name.replace("\\", "/")
            found = (folder, key) in have if folder else key in names
            entry = {"folder": folder, "name": name}
            if not found and entry not in missing:
                missing.append(entry)
        return missing

    # --- Les entrées -----------------------------------------------------------------------
    def _input(self, h, method, app, sha, raw):
        if method not in ("HEAD", "PUT"):
            return self._not_allowed(h)
        if not SHA256.match(sha):
            raise _Refused("invalid_request", "Empreinte attendue : 64 caractères hexadécimaux "
                           "en minuscules (SHA-256).", param="sha256")
        with self._lock:
            known = self._inputs.get(app, {}).get(sha)
        if method == "HEAD":
            if known is None:
                return self._send(h, 404, b"", "application/json")
            return self._send(h, 200, b"", "application/json",
                              headers={"X-Aiodos-Size": str(known["size"]),
                                       "X-Aiodos-Type": known["type"]})
        if known is not None:
            return self._send_json(h, 200, {"sha256": sha, "size": known["size"],
                                            "type": known["type"]})
        if len(raw) > MAX_INPUT:
            raise _Refused("input_too_large", "Entrée trop lourde : 100 Mo au plus.",
                           max_bytes=MAX_INPUT)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != sha:
            raise _Refused("input_hash_mismatch", "L'empreinte du fichier reçu ne correspond "
                           "pas à celle de l'adresse.", sha256=digest)
        kind = sniff(raw[:16])
        if kind not in INPUT_TYPES:
            raise _Refused("unsupported_media_type", "Genre de fichier non reconnu : images "
                           "(PNG, JPEG, WebP, GIF), vidéos (MP4, WebM) ou sons (WAV, MP3, FLAC, "
                           "OGG).", declared=h.headers.get("Content-Type"))
        with self._lock:
            store = self._inputs.setdefault(app, {})
            if sum(v["size"] for v in store.values()) + len(raw) > INPUT_QUOTA:
                raise _Refused("quota_exceeded", "Trop d'entrées gardées pour cette "
                               "application : supprime des travaux finis.", quota="inputs")
            store[sha] = {"size": len(raw), "type": kind, "data": raw}
        return self._send_json(h, 201, {"sha256": sha, "size": len(raw), "type": kind})

    # --- Déposer, lister -------------------------------------------------------------------
    def _submit(self, h, record, body):
        app = record["id"]
        ref = body.get("client_ref") if isinstance(body, dict) else None
        ref = ref or h.headers.get("Idempotency-Key")
        with self._lock:
            old = next((j for j in self._jobs.values()
                        if ref and j["app"] == app and j["client_ref"] == ref), None)
            if old is not None:
                self._tick(old)
                return self._send_json(h, 200, self._public(old))
        where = self._where(h, record, body)
        checked = self._check(app, body)
        with self._lock:
            active = [j for j in self._jobs.values()
                      if j["app"] == app and _status(j) not in FINISHED]
            if len(active) >= MAX_ACTIVE:
                raise _Refused("quota_exceeded", f"{MAX_ACTIVE} travaux actifs au plus par "
                               "application.", quota="active_jobs", max=MAX_ACTIVE)
        machines = self._machines(where)
        if not machines:
            raise _Refused("engine_unavailable", "Aucune machine permise n'a ComfyUI accordé à "
                           "AIODOS (Réglages → Moteurs des applications, sur ce PC ou sur une "
                           "de mes machines).", status=409, engine="comfyui", where=where)
        gaps = [(self._missing(checked, m["models"]), m) for m in machines]
        able = [m for missing, m in gaps if not missing]
        if not able:
            missing, best = min(gaps, key=lambda gap: len(gap[0]))
            raise _Refused("model_unavailable", f"Il manque des modèles à « {best['name']} » : "
                           + ", ".join(m["name"] for m in missing[:5]) + ".", models=missing,
                           machine=best["name"])
        now = time.monotonic()
        job = {"id": _b64id("job_", 12), "app": app, "label": body.get("label"),
               "client_ref": ref, "priority": body.get("priority", "normal"),
               "where": where, "runner": {"name": able[0]["name"], "place": able[0]["place"]},
               "graphs": checked["graphs"], "created": int(time.time()),
               "started": None, "finished": None, "clock": now, "paused": False,
               "pause_mode": None, "cancelled": False, "cancelled_by": None, "cancel_at": None,
               "failures": {}, "units": []}
        for index, item in enumerate(checked["items"]):
            job["units"].append({"index": index, "label": item["label"], "graph": item["graph"],
                                 "seed": item["seed"], "status": "queued", "t0": None,
                                 "files": [], "seconds": None, "error": None})
        with self._lock:
            for index, code, message in self._next_failures:
                job["failures"][index] = (code, message)
            self._next_failures.clear()
            self._seq += 1
            job["seq"] = self._seq
            self._jobs[job["id"]] = job
            self.last_job_id = job["id"]
            public = self._public(job)
        return self._send_json(h, 202, public, headers={"Location": f"/aiodos/v1/jobs/{job['id']}"})

    def _list(self, app, query):
        active = (query.get("active") or [None])[0]
        ref = (query.get("client_ref") or [None])[0]
        try:
            limit = max(1, min(200, int((query.get("limit") or ["50"])[0])))
        except ValueError:
            limit = 50
        with self._lock:
            jobs = [j for j in self._jobs.values() if j["app"] == app]
            for job in jobs:
                self._tick(job)
            if active in ("1", "true"):
                jobs = [j for j in jobs if _status(j) not in FINISHED]
            elif active in ("0", "false"):
                jobs = [j for j in jobs if _status(j) in FINISHED]
            if ref:
                jobs = [j for j in jobs if j["client_ref"] == ref]
            jobs.sort(key=lambda j: j["seq"], reverse=True)
            return [self._public(j) for j in jobs[:limit]]

    def _all(self, h, app, action, body):
        when = (body or {}).get("when", "after_current") if isinstance(body, dict) \
            else "after_current"
        if when not in ("after_current", "now"):
            raise _Refused("invalid_request", "« when » : after_current ou now.", param="when")
        count = 0
        with self._lock:
            for job in self._jobs.values():
                if job["app"] != app:
                    continue
                self._tick(job)
                status = _status(job)
                if action == "pause" and status in ("queued", "in_progress"):
                    self._pause(job, when)
                    count += 1
                elif action == "resume" and job["paused"] and status not in FINISHED:
                    self._resume(job)
                    count += 1
        key = "paused" if action == "pause" else "resumed"
        return self._send_json(h, 200, {key: count})

    # --- Un travail ------------------------------------------------------------------------
    def _job_route(self, h, method, app, job_id, action, query, body):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["app"] != app:
                raise _Refused("job_not_found", "Travail introuvable.")
            self._tick(job)
        if action is None:
            if method == "GET":
                with self._lock:
                    return self._send_json(h, 200, self._public(job))
            if method == "DELETE":
                return self._forget(h, job, (query.get("files") or ["keep"])[0])
            return self._not_allowed(h)
        if action == "events" and method == "GET":
            return self._events(h, app, job_id)
        if action == "files" and method == "GET":
            unfetched = (query.get("unfetched") or [""])[0] in ("1", "true")
            with self._lock:
                files = [self._file_public(self._files[f]) for f in self._job_files(job)
                         if not (unfetched and self._files[f]["fetched_at"])]
            return self._send_json(h, 200, {"object": "list", "files": files})
        if method != "POST" or action in ("events", "files"):
            return self._not_allowed(h)
        with self._lock:
            status = _status(job)
            if action == "pause":
                when = (body or {}).get("when", "after_current") if isinstance(body, dict) \
                    else "after_current"
                if when not in ("after_current", "now"):
                    raise _Refused("invalid_request", "« when » : after_current ou now.",
                                   param="when")
                if status in FINISHED:
                    raise _Refused("job_not_pausable", "Ce travail est déjà fini.")
                self._pause(job, when)
            elif action == "resume":
                if status in FINISHED:
                    raise _Refused("job_not_pausable", "Ce travail est déjà fini.")
                self._resume(job)
            elif action == "cancel":
                if status in FINISHED:
                    raise _Refused("job_not_cancellable", "Ce travail est déjà fini.")
                self._cancel(job)
            else:
                self._retry(job)
            return self._send_json(h, 202, self._public(job))

    def _forget(self, h, job, mode):
        with self._lock:
            if _status(job) not in FINISHED:
                raise _Refused("job_not_cancellable", "Annule le travail avant de l'oublier.")
            if mode == "delete":
                for file_id in self._job_files(job):
                    self._files.pop(file_id, None)
            self._jobs.pop(job["id"], None)
        return self._send_json(h, 200, {"id": job["id"], "deleted": True})

    # --- L'avancement ----------------------------------------------------------------------
    def _tick(self, job):
        """Le travail mis à l'heure : les unités finies pendant `job_delay` secondes chacune,
        une à la fois (rien sans job_delay)."""
        delay = self.job_delay
        now = time.monotonic()
        while True:
            running = _running(job)
            if running is not None:
                if job["cancelled"]:
                    if job["cancel_at"] is not None and now < job["cancel_at"]:
                        return
                    self._finish(job, running, now)
                    continue
                if delay is None or running["t0"] + delay > now:
                    return
                self._finish(job, running, running["t0"] + delay)
                continue
            if delay is None or job["paused"] or job["cancelled"]:
                return
            queued = _queued(job)
            if queued is None:
                return
            self._start(job, queued, job["clock"])

    def _start(self, job, unit, at):
        unit.update(status="running", t0=at)
        if job["started"] is None:
            job["started"] = int(time.time())

    def _finish(self, job, unit, at):
        """L'unité en cours finit : faite (son PNG), en échec (fail_unit), ou annulée."""
        job["clock"] = at
        unit["seconds"] = round(max(0.0, at - (unit["t0"] or at)), 1)
        if job["cancelled"]:
            unit.update(status="cancelled", seconds=None)
        elif unit["index"] in job["failures"]:
            code, message = job["failures"][unit["index"]]
            unit.update(status="failed", error={"code": code, "message": message})
        else:
            unit["status"] = "done"
            unit["files"].append(self._output(job, unit))
        if _queued(job) is None and _running(job) is None and job["finished"] is None:
            job["finished"] = int(time.time())

    def _output(self, job, unit):
        seed = f"{job['id']}:{unit['index']}:{unit['seed']}".encode("utf-8")
        data = png(8, 8, tuple(hashlib.sha256(seed).digest()[:3]))
        graph = job["graphs"][unit["graph"]]
        node = next((n for n, v in graph.items() if str(v["class_type"]).startswith("Save")),
                    "9")
        file_id = _b64id("file_", 16)
        self._files[file_id] = {"id": file_id, "job": job["id"], "app": job["app"],
                                "unit": unit["index"], "node": node, "type": "image/png",
                                "data": data, "sha256": hashlib.sha256(data).hexdigest(),
                                "machine": job["runner"]["name"],
                                "place": job["runner"]["place"],
                                "created": int(time.time()), "served": 0, "fetched_at": None}
        return file_id

    def _pause(self, job, when):
        job.update(paused=True, pause_mode=when)
        running = _running(job)
        if when == "now" and running is not None:       # rendue sans compter d'essai
            running.update(status="queued", t0=None)

    def _resume(self, job):
        job.update(paused=False, pause_mode=None, clock=time.monotonic())

    def _cancel(self, job):
        now = time.monotonic()
        job.update(cancelled=True, cancelled_by=job["cancelled_by"] or "app", paused=False,
                   pause_mode=None, cancel_at=now + min(self.job_delay or 0.05, 0.05))
        for unit in job["units"]:
            if unit["status"] == "queued":
                unit["status"] = "cancelled"
        if _running(job) is None and job["finished"] is None:
            job["finished"] = int(time.time())

    def _retry(self, job):
        again = [u for u in job["units"] if u["status"] == "failed"]
        for unit in again:
            job["failures"].pop(unit["index"], None)
            unit.update(status="queued", error=None, seconds=None, t0=None)
        if again:
            job.update(finished=None, clock=time.monotonic())

    # --- L'objet travail -----------------------------------------------------------------
    def _job_files(self, job):
        return [f for u in job["units"] for f in u["files"] if f in self._files]

    def _file_public(self, f):
        fetched = f["fetched_at"]
        return {"id": f["id"], "unit": f["unit"], "node": f["node"], "type": f["type"],
                "bytes": len(f["data"]), "sha256": f["sha256"], "width": 8, "height": 8,
                "machine": f["machine"], "place": f["place"],
                "engine": f"comfyui {COMFY_VERSION}",
                "verified": "none", "fetched": fetched is not None,
                "expires_at": int(fetched + KEEP_DAYS * 86400) if fetched else None,
                "created_at": f["created"]}

    def _public(self, job):
        status = _status(job)
        runner = job["runner"]
        units, counts = [], {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        for unit in job["units"]:
            counts[unit["status"]] += 1
            busy = unit["status"] in ("running", "done")
            entry = {"index": unit["index"], "label": unit["label"],
                     "status": {"running": "in_progress", "done": "completed"}.get(
                         unit["status"], unit["status"]),
                     "place": runner["place"] if busy else None,
                     "machine": runner["name"] if busy else None,
                     "files": [f for f in unit["files"] if f in self._files],
                     "seconds": unit["seconds"] if unit["status"] == "done" else None,
                     "stage": "running" if unit["status"] == "running" else (
                         "waiting_machine" if unit["status"] == "queued" else None)}
            if unit["status"] == "failed":
                entry["error"] = dict(unit["error"])
            units.append(entry)
        total, done, failed = len(units), counts["done"], counts["failed"]
        running = counts["running"]
        machines = ([{"name": runner["name"], "place": runner["place"], "running": running,
                      "done": done}] if running or done else [])
        message = f"{done} sur {total}"
        if status == "completed":
            message += " : fini"
        elif status in ("paused", "pausing"):
            message += " · en pause"
        elif status in ("cancelled", "cancelling"):
            message += " · annulé"
        elif running:
            message += f" · {running} en cours sur 1 machine"
        stage = "done" if status in FINISHED else ("running" if running else "queued")
        elapsed = int(time.time() - job["started"]) if job["started"] else 0
        return {"id": job["id"], "object": "aiodos.job", "engine": "comfyui",
                "label": job["label"], "client_ref": job["client_ref"], "status": status,
                "pause": ({"when": job["pause_mode"], "pending": running} if job["paused"]
                          else None),
                "priority": job["priority"], "where": list(job["where"]),
                "where_effective": list(job["where"]), "where_reason": None,
                "progress": {"total": total, "done": done, "running": running,
                             "queued": counts["queued"], "failed": failed,
                             "percent": int(100 * (done + failed) / total) if total else 100,
                             "estimated": True, "elapsed_s": elapsed, "eta_s": None,
                             "stage": stage, "machines": machines, "message": message},
                "waiting": None, "units": units,
                "files": [self._file_public(self._files[f]) for f in self._job_files(job)],
                "error": ({"code": "all_failed", "message": "Aucune unité n'a abouti."}
                          if status == "failed" else None),
                "cancelled_by": job["cancelled_by"], "created_at": job["created"],
                "started_at": job["started"], "completed_at": job["finished"]
                if status in FINISHED else None}

    # --- Les événements --------------------------------------------------------------------
    def _events(self, h, app, job_id):
        with self._lock:
            cut = self._event_cuts.pop(0) if self._event_cuts else None
        h.send_response(200)
        for name, value in (("X-Aiodos-Api", "1"), ("Content-Type", "text/event-stream"),
                            ("Cache-Control", "no-store"), ("Transfer-Encoding", "chunked"),
                            ("Connection", "close")):
            h.send_header(name, value)
        h.end_headers()
        h.close_connection = True
        server = h.server
        last, seen, sent, first = None, set(), 0, True
        pinged = time.monotonic()
        deadline = pinged + 120.0
        try:
            while self._server is server and time.monotonic() < deadline:
                events = []
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None:
                        events.append(("end", {"id": job_id, "status": "deleted"}))
                    else:
                        self._tick(job)
                        obj = self._public(job)
                        for f in obj["files"]:
                            if f["id"] not in seen:
                                seen.add(f["id"])
                                if not first:
                                    events.append(("file", f))
                        text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
                        if text != last:
                            last = text
                            events.append(("job", obj))
                        if obj["status"] in FINISHED:
                            events.append(("end", {"id": job_id, "status": obj["status"]}))
                first = False
                for name, data in events:
                    line = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    self._chunk(h, line.encode("utf-8"))
                    sent += 1
                    if cut is not None and sent >= cut:
                        return                      # coupé : sans le morceau final
                    pinged = time.monotonic()
                    if name == "end":
                        h.wfile.write(b"0\r\n\r\n")
                        return
                if time.monotonic() - pinged >= self.ping_every:
                    self._chunk(h, b": ping\n\n")
                    pinged = time.monotonic()
                time.sleep(0.02)
        except OSError:
            self.stream_closed.set()

    # --- Les fichiers ----------------------------------------------------------------------
    def _file_route(self, h, method, app, file_id, content):
        with self._lock:
            found = self._files.get(file_id)
            if found is None or found["app"] != app:
                raise _Refused("file_not_found", "Fichier introuvable.")
            public = self._file_public(found)
        if content:
            if method != "GET":
                return self._not_allowed(h)
            return self._content(h, found)
        if method == "GET":
            return self._send_json(h, 200, public)
        if method == "DELETE":
            with self._lock:
                self._files.pop(file_id, None)
            return self._send_json(h, 200, {"id": file_id, "deleted": True})
        return self._not_allowed(h)

    def _content(self, h, found):
        data = found["data"]
        size = len(data)
        keep = "pending" if found["fetched_at"] is None else str(
            int(found["fetched_at"] + KEEP_DAYS * 86400))
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff", "X-Aiodos-Keep-Until": keep,
                   "ETag": f'"{found["sha256"]}"'}
        span = _span(h.headers.get("Range"), size)
        if span is False:
            headers["Content-Range"] = f"bytes */{size}"
            return self._send(h, 416, b"", found["type"], headers=headers)
        start, end = span if span else (0, size - 1)
        if span:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        body = data[start:end + 1]
        with self._lock:
            cut = self._content_cuts.pop(0) if self._content_cuts else None
        if cut is not None and cut < len(body):
            h.send_response(206 if span else 200)
            h.send_header("X-Aiodos-Api", "1")
            h.send_header("Content-Type", found["type"])
            h.send_header("Content-Length", str(len(body)))
            h.send_header("Connection", "close")
            for name, value in headers.items():
                h.send_header(name, value)
            h.end_headers()
            h.wfile.write(body[:cut])               # puis la connexion se ferme
            h.close_connection = True
            return None
        with self._lock:                # noté avant l'envoi : l'essai qui suit le voit déjà
            found["served"] += len(body)
            if found["fetched_at"] is None and end + 1 >= size and found["served"] >= size:
                found["fetched_at"] = time.time()
        return self._send(h, 206 if span else 200, body, found["type"], headers=headers)


_ABSENT = object()

FAKE_HINTS = {
    "graph_not_api": "Exporte le graphe au format API dans ComfyUI.",
    "graph_invalid": "Vérifie les liens, les nœuds et les sorties du graphe.",
    "patch_invalid": "Une retouche remplace une valeur déjà présente, du même type, jamais un "
                     "lien.",
    "forbidden_value": "Un fichier se donne par un jeton @in:, un modèle par son nom.",
    "input_missing": "Envoie l'entrée (PUT /aiodos/v1/inputs/{sha256}) et déclare-la dans "
                     "« inputs ».",
    "input_hash_mismatch": "Calcule le SHA-256 des octets envoyés.",
    "input_too_large": "100 Mo au plus.",
    "model_unavailable": "Installe les modèles qui manquent (detail.models), ou élargis le lieu "
                         "de calcul.",
    "model_unresolved": "Déclare le modèle dans « models » : {folder, name}.",
    "quota_exceeded": "Récupère ou supprime des fichiers et des travaux finis.",
    "engine_unavailable": "Réessaie dans quelques secondes.",
    "job_not_cancellable": "",
    "job_not_pausable": "",
    "job_not_found": "",
    "file_not_found": "",
}


def _running(job):
    return next((u for u in job["units"] if u["status"] == "running"), None)


def _queued(job):
    return next((u for u in job["units"] if u["status"] == "queued"), None)


def _status(job):
    units = job["units"]
    running = any(u["status"] == "running" for u in units)
    if job["cancelled"]:
        return "cancelling" if running else "cancelled"
    if job["paused"]:
        return "pausing" if running else "paused"
    if all(u["status"] in ("done", "failed", "cancelled") for u in units):
        return "completed" if any(u["status"] == "done" for u in units) else "failed"
    if running or any(u["status"] in ("done", "failed") for u in units):
        return "in_progress"
    return "queued"


def _span(header, size):
    """(début, fin) d'une seule plage, None pour tout, False si elle est hors du fichier."""
    found = _RANGE.match((header or "").strip())
    if not found or (not found.group(1) and not found.group(2)):
        return None
    if found.group(1):
        start = int(found.group(1))
        end = min(int(found.group(2)), size - 1) if found.group(2) else size - 1
    else:
        start, end = max(0, size - int(found.group(2))), size - 1
    if start >= size or end < start:
        return False
    return start, end
