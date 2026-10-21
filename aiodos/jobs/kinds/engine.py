"""Les travaux de moteur des applications (docs/conception/partage-puissance.md § 2.2, 2.3,
3.4, 3.6) : un travail ComfyUI déposé par une appli (POST /aiodos/v1/jobs) devient des unités
« engine_unit », une par élément (items) ou par variation (repeat), calculées par le ComfyUI
de ce PC, puis par celui de mes machines (livraison suivante).

- normalize : la requête vérifiée avant toute mise en file (forme, jetons, retouches, valeurs
  interdites, nœuds du cloud, modèles, bornes) → les paramètres gardés dans jobs.params,
  chez le propriétaire seulement ;
- split : une unité par élément, au payload petit {engine, api, graph, item, seed, set,
  model} ; « model » vaut comfyui:<empreinte des modèles du graphe> (regroupement de la file) ;
- expand : l'ordre complet, assemblé au moment de calculer (graphe retouché et renuméroté,
  entrées, modèles, contrat, sorties, bornes) ; pour ce PC seulement, ce qui ne le quitte
  jamais (appli, travail, chemins des entrées) ;
- compute : engines/comfyui.run ; assimilate : les fichiers rangés pour l'appli
  (gateway/file_store.py).
"""
import base64
import hashlib
import json
import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from aiodos import db, paths, settings
from aiodos.engines import (comfy_client, comfy_contract, comfy_graph, comfy_instance, comfyui,
                            gpu_share)
from aiodos.jobs import pacing
from aiodos.jobs.models import BACKGROUND, NORMAL, URGENT
from aiodos.jobs.registry import UnitSpec, register_job_kind, register_portable

log = logging.getLogger("aiodos.engines")

KIND = "engine"
TASK = "engine_unit"
ENGINE = "comfyui"
MAX_GRAPHS = 64
MAX_UNITS = 256
MAX_GRAPH_BYTES = 2 * 1024 ** 2
MAX_TOTAL_BYTES = 8 * 1024 ** 2
MAX_LABEL = 200
SEED_MAX = 2 ** 53
FIELDS = {"engine", "label", "client_ref", "where", "priority", "graph", "graphs", "inputs",
          "models", "items", "repeat", "requires", "bounds", "strict", "recipe"}
PRIORITIES = {"high": URGENT, "normal": NORMAL, "low": BACKGROUND}
# (défaut, minimum, maximum)
BOUNDS = {"unit_seconds": (1800, 10, 4 * 3600), "output_mb": (200, 1, 2048),
          "wait_s": (86400, 0, 7 * 86400)}
INPUT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HERE_TTL = 10.0

_params = {}                      # travail -> paramètres (cache de jobs.params)
_params_lock = threading.Lock()
_here = {"at": 0.0, "found": None}


class Invalid(Exception):
    """Travail refusé avant la mise en file : code stable (gateway/errors.py), message,
    détail ; status pour les codes qui n'ont pas le leur (409)."""

    def __init__(self, code: str, message: str, *, param=None, status=None, **detail):
        super().__init__(message)
        self.code, self.param, self.status, self.detail = code, param, status, detail


# ---------------------------------------------------------------- vérifier
def _graphs(body) -> list:
    if "graph" in body and "graphs" in body:
        raise Invalid("invalid_request", "« graph » ou « graphs », pas les deux.", param="graphs")
    if "recipe" in body:
        raise Invalid("not_supported", "Les recettes arriveront avec une version suivante "
                      "d'AIODOS : envoie un graphe au format API.", param="recipe")
    graphs = [body["graph"]] if "graph" in body else body.get("graphs")
    if not isinstance(graphs, list) or not graphs:
        raise Invalid("invalid_request", "Un graphe au moins (« graph » ou « graphs »).",
                      param="graphs")
    if len(graphs) > MAX_GRAPHS:
        raise Invalid("graph_too_large", f"{MAX_GRAPHS} graphes au plus par travail.",
                      param="graphs")
    total = 0
    for i, graph in enumerate(graphs):
        size = len(json.dumps(graph, ensure_ascii=False).encode("utf-8"))
        total += size
        if size > MAX_GRAPH_BYTES or total > MAX_TOTAL_BYTES:
            raise Invalid("graph_too_large", "Graphe trop gros (2 Mo par graphe, 8 Mo en "
                          "tout).", param="graphs", graph=i)
        try:
            comfy_graph.check_shape(graph, "mine")
        except comfy_graph.GraphError as exc:
            raise Invalid(exc.code, str(exc), param="graphs", graph=i, **exc.detail) from exc
    return graphs


def _inputs(body, app_id) -> dict:
    from aiodos.gateway import input_store
    given = body.get("inputs") or {}
    if not isinstance(given, dict):
        raise Invalid("invalid_request", "« inputs » : {identifiant: {sha256}}.", param="inputs")
    out = {}
    for key, value in given.items():
        sha = value.get("sha256") if isinstance(value, dict) else None
        if not INPUT_ID.match(str(key)) or not input_store.valid_sha(sha):
            raise Invalid("invalid_request", "Entrée illisible : {identifiant: {\"sha256\": "
                          "…}}.", param="inputs", input=str(key))
        known = input_store.info(app_id, sha)
        if known is None:
            raise Invalid("input_missing", f"Entrée « {key} » jamais envoyée : PUT "
                          f"/aiodos/v1/inputs/{sha} d'abord.", param="inputs", input=key)
        out[key] = {"sha256": sha, "size": known["size"], "type": known["type"]}
    return out


def _items(body, graphs) -> list:
    if "items" in body and "repeat" in body:
        raise Invalid("invalid_request", "« items » ou « repeat », pas les deux.", param="items")
    if "repeat" in body:
        return _repeat(body["repeat"], graphs)
    items = body.get("items")
    if items is None:
        return [{"graph": i, "label": None, "seed": None, "set": []} for i in range(len(graphs))]
    if not isinstance(items, list) or not items:
        raise Invalid("invalid_request", "« items » : une liste d'éléments.", param="items")
    if len(items) > MAX_UNITS:
        raise Invalid("invalid_request", f"{MAX_UNITS} unités au plus par travail.",
                      param="items")
    out = []
    for n, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - {"graph", "label", "seed", "set"}:
            raise Invalid("invalid_request", "Élément illisible : {graph, label, seed, set}.",
                          param="items", item=n)
        index = item.get("graph", 0)
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(graphs):
            raise Invalid("invalid_request", "« graph » : l'indice d'un des graphes.",
                          param="items", item=n)
        out.append({"graph": index, "label": _label(item.get("label"), f"items[{n}].label"),
                    "seed": _seed(item.get("seed"), n), "set": item.get("set") or []})
    return out


def _repeat(repeat, graphs) -> list:
    if not isinstance(repeat, dict) or set(repeat) - {"count", "vary", "graph"}:
        raise Invalid("invalid_request", "« repeat » : {count, vary, graph}.", param="repeat")
    count, index = repeat.get("count"), repeat.get("graph", 0)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_UNITS:
        raise Invalid("invalid_request", f"« repeat.count » : de 1 à {MAX_UNITS}.",
                      param="repeat")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(graphs):
        raise Invalid("invalid_request", "« repeat.graph » : l'indice d'un des graphes.",
                      param="repeat")
    vary = repeat.get("vary") or []
    if not isinstance(vary, list):
        raise Invalid("invalid_request", "« repeat.vary » : [{node, input, step}].",
                      param="repeat")
    graph = graphs[index]
    for v in vary:
        node, name, step = (v.get("node"), v.get("input"), v.get("step")) if isinstance(v, dict) \
            else (None, None, None)
        current = (graph.get(node) or {}).get("inputs", {}).get(name) if isinstance(node, str) \
            else None
        if not isinstance(step, (int, float)) or isinstance(step, bool) or isinstance(
                current, bool) or not isinstance(current, (int, float)):
            raise Invalid("patch_invalid", "« vary » : un nombre déjà présent dans le graphe, "
                          "et un pas.", param="repeat", node=node, input=name)
    return [{"graph": index, "label": None, "seed": None,
             "set": [[v["node"], v["input"], graph[v["node"]]["inputs"][v["input"]] + i * v["step"]]
                     for v in vary]} for i in range(count)]


def _label(value, param) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_LABEL:
        raise Invalid("invalid_request", f"Libellé : un texte de {MAX_LABEL} caractères au "
                      "plus.", param=param)
    return value.strip() or None


def _seed(value, n):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < SEED_MAX:
        raise Invalid("invalid_request", "« seed » : un entier positif.", param="items", item=n)
    return value


def _bounds(given) -> dict:
    if given is None:
        given = {}
    if not isinstance(given, dict) or set(given) - set(BOUNDS):
        raise Invalid("invalid_request", "« bounds » : {unit_seconds, output_mb, wait_s}.",
                      param="bounds")
    out = {}
    for name, (default, low, high) in BOUNDS.items():
        value = given.get(name, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise Invalid("invalid_request", f"« bounds.{name} » : un nombre.", param="bounds")
        out[name] = min(high, max(low, value))
    return out


def _models(body, graphs, inventory) -> tuple:
    """Les modèles employés, par jeton @model:<n> → (graphes jetonnés, {n: {folder, name,
    size}}). « auto » : les valeurs des listes de fichiers de modèles (d'après les
    définitions de ce ComfyUI), sinon celles qui finissent par une extension de modèle."""
    given = body.get("models", "auto")
    models, numbered = {}, {}

    def number(folder, name) -> str:
        key = (folder or "", name.replace("\\", "/"))
        if key not in numbered:
            numbered[key] = str(len(numbered))
            models[numbered[key]] = {"folder": folder, "name": key[1],
                                     "size": _size(inventory, folder, key[1])}
        return numbered[key]

    if isinstance(given, dict):
        declared = {}
        for key, value in given.items():
            if not isinstance(value, dict) or not isinstance(value.get("folder"), str) \
                    or not isinstance(value.get("name"), str) or not value["name"]:
                raise Invalid("model_unresolved", "Modèle déclaré sans dossier ni nom : "
                              "{\"folder\", \"name\"} (l'empreinte seule viendra plus tard).",
                              param="models", status=409, model=str(key))
            reason = comfy_graph.forbidden(value["name"])
            if reason:
                raise Invalid("forbidden_value", "Nom de modèle refusé.", param="models",
                              model=str(key), reason=reason)
            declared[str(key)] = number(value["folder"], value["name"])
        out = []
        for i, graph in enumerate(graphs):
            used = comfy_graph.tokens(graph)["models"]
            missing = sorted(used - set(declared))
            if missing:
                raise Invalid("model_unresolved", f"Jeton @model:{missing[0]} sans modèle "
                              "déclaré dans « models ».", param="models", status=409,
                              graph=i, model=missing[0])
            out.append(_rename_tokens(graph, declared))
        return out, models
    if given != "auto":
        raise Invalid("invalid_request", "« models » : \"auto\" ou {clé: {folder, name}}.",
                      param="models")
    defs = (inventory or {}).get("defs") or {}
    out = []
    for i, graph in enumerate(graphs):
        if comfy_graph.tokens(graph)["models"]:
            raise Invalid("model_unresolved", "Des jetons @model: demandent « models ».",
                          param="models", status=409, graph=i)
        tokenized = {}
        for node_id, node in graph.items():
            specs = comfy_graph.input_specs(defs, node["class_type"])
            loader = comfy_graph.FILE_LOADERS.get(node["class_type"])
            inputs = dict(node.get("inputs", {}))
            for name, value in inputs.items():
                if name == loader or not isinstance(value, str) or value.startswith("@"):
                    continue
                folder = _model_folder(inventory, specs.get(name), value)
                if folder is False:
                    continue
                inputs[name] = comfy_graph.TOKEN_MODEL + number(folder, value)
            tokenized[node_id] = {**node, "inputs": inputs}
        out.append(tokenized)
    return out, models


def _model_folder(inventory, spec, value):
    """Le dossier d'un nom de modèle (None : inconnu, la machine qui calcule cherchera
    dans tous les siens), ou False si ce n'est pas un modèle."""
    lowered = value.lower()
    if not lowered.endswith(comfy_graph._MODEL_EXT):
        return False
    if spec is not None and not comfy_graph.is_file_list(spec):
        return False
    folders = comfyui.folders_of((inventory or {}).get("models") or {}, value)
    if len(folders) > 1:
        raise Invalid("model_ambiguous", f"« {value} » est dans plusieurs dossiers de "
                      f"modèles ({', '.join(folders)}) : déclare-le dans « models ».",
                      param="models", status=409, candidates=folders)
    return folders[0] if folders else None


def _rename_tokens(graph, declared) -> dict:
    def swap(value):
        if isinstance(value, str) and value.startswith(comfy_graph.TOKEN_MODEL):
            return comfy_graph.TOKEN_MODEL + declared[value[len(comfy_graph.TOKEN_MODEL):]]
        if isinstance(value, list):
            return [swap(v) for v in value]
        return value
    return {n: {**node, "inputs": {k: swap(v) for k, v in node.get("inputs", {}).items()}}
            for n, node in graph.items()}


def _size(inventory, folder, name):
    if not inventory:
        return None
    found = comfyui.local_name(inventory.get("models") or {}, folder, name)
    if found is None:
        return None
    where = folder or (comfyui.folders_of(inventory["models"], found) or [""])[0]
    return comfyui._size_of(comfyui._model_roots(inventory.get("argv")), where, found) \
        if where else None


def model_key(models: list) -> str:
    """comfyui:<16 hex> : les modèles d'une unité (dossier, nom, taille), triés."""
    text = json.dumps(sorted([m.get("folder") or "", m["name"], m.get("size") or 0]
                             for m in models), ensure_ascii=False)
    return f"{ENGINE}:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize(body, *, app_id: str, where: list, inventory: dict | None) -> dict:
    """La requête vérifiée → les paramètres du travail ; Invalid sinon."""
    if not isinstance(body, dict):
        raise Invalid("invalid_request", "Corps JSON attendu : un objet.")
    unknown = sorted(set(body) - FIELDS)
    if unknown:
        raise Invalid("invalid_request", f"Champ inconnu : « {unknown[0]} ».", param=unknown[0])
    if body.get("engine", ENGINE) != ENGINE:
        raise Invalid("engine_not_found", f"Moteur inconnu : « {body.get('engine')} » "
                      "(connu : comfyui).", param="engine")
    graphs = _graphs(body)
    inputs = _inputs(body, app_id)
    items = _items(body, graphs)
    graphs, models = _models(body, graphs, inventory)
    defs = (inventory or {}).get("defs")
    used_inputs = set()
    for i, graph in enumerate(graphs):
        cloud = comfy_graph.cloud_nodes(graph, defs) if defs else []
        if cloud:
            raise Invalid("forbidden_node", "Nœud d'un service en ligne : refusé.",
                          param="graphs", graph=i, node=cloud[0],
                          **{"class": graph[cloud[0]]["class_type"]})
        tokens = comfy_graph.tokens(graph)
        missing = sorted(tokens["inputs"] - set(inputs))
        if missing:
            raise Invalid("input_missing", f"Jeton @in:{missing[0]} sans entrée déclarée dans "
                          "« inputs ».", param="inputs", graph=i, input=missing[0])
        used_inputs |= tokens["inputs"]
    for n, item in enumerate(items):
        graph = graphs[item["graph"]]
        try:
            patched = comfy_graph.apply_set(graph, item["set"])
            comfy_graph.check_values(patched, defs)
        except comfy_graph.GraphError as exc:
            raise Invalid(exc.code, str(exc), param="items", item=n, **exc.detail) from exc
        if comfy_graph.tokens(graph)["seed"] and item["seed"] is None:
            item["seed"] = secrets.randbelow(2 ** 32)
    # Les aperçus (PreviewImage…) sont retirés au calcul : ils ne comptent pas comme sorties
    outputs = [[n for n in comfy_graph.outputs(g, defs)
                if g[n]["class_type"] not in comfy_graph.PREVIEWS] for g in graphs]
    for i, found in enumerate(outputs):
        if not found:
            raise Invalid("graph_invalid", "Ce graphe n'enregistre aucun fichier (SaveImage…) : "
                          "les aperçus ne sont pas gardés.", param="graphs", graph=i)
    priority = body.get("priority", "normal")
    if priority not in PRIORITIES:
        raise Invalid("invalid_request", "« priority » : high, normal ou low.", param="priority")
    label = _label(body.get("label"), "label")
    client_ref = body.get("client_ref")
    if client_ref is not None and (not isinstance(client_ref, str) or not client_ref
                                   or len(client_ref) > MAX_LABEL):
        raise Invalid("invalid_request", "« client_ref » : un texte de 200 caractères au plus.",
                      param="client_ref")
    keys, used_models = {}, set()  # par graphe (clé en texte : jobs.params est du JSON)
    for i, graph in enumerate(graphs):
        used = comfy_graph.tokens(graph)["models"]
        used_models |= used
        keys[str(i)] = model_key([models[n] for n in sorted(used)])
    models = {n: m for n, m in models.items() if n in used_models}   # déclarés, pas employés
    contract = {}
    if inventory:
        for i, graph in enumerate(graphs):
            classes = sorted({node["class_type"] for node in graph.values()})
            contract[str(i)] = {"comfyui": inventory.get("version"), "core": inventory.get("core"),
                                "classes": {c: inventory["classes"][c] for c in classes
                                            if c in inventory.get("classes", {})}}
    return {"engine": ENGINE, "api": comfyui.API, "app_id": app_id, "graphs": graphs,
            "inputs": {k: v for k, v in inputs.items() if k in used_inputs},
            "models": models, "items": items, "outputs": outputs, "model_keys": keys,
            "contract": contract, "bounds": _bounds(body.get("bounds")),
            "strict": bool(body.get("strict", False)), "where": list(where),
            "priority": priority, "label": label, "client_ref": client_ref,
            "requires": body.get("requires") if isinstance(body.get("requires"), dict) else {}}


def priority_of(params) -> int:
    return PRIORITIES.get(params.get("priority"), NORMAL)


def missing_here(params, inventory) -> list:
    """Les modèles du travail que le ComfyUI de ce PC n'a pas : [{folder, name}]."""
    have = (inventory or {}).get("models") or {}
    return [{"folder": m.get("folder"), "name": m["name"]} for m in params["models"].values()
            if comfyui.local_name(have, m.get("folder"), m["name"]) is None]


# ---------------------------------------------------------------- découper
def split(params):
    units = []
    for n, item in enumerate(params["items"]):
        units.append(UnitSpec(TASK, "gpu", {
            "engine": ENGINE, "api": params["api"], "graph": item["graph"], "item": n,
            "seed": item["seed"], "set": item["set"],
            "model": params["model_keys"][str(item["graph"])]}))
    title = params.get("label") or "Travail ComfyUI"
    return title, units, {}


def params_of(job_id: int) -> dict:
    with _params_lock:
        found = _params.get(job_id)
    if found is not None:
        return found
    row = db.query_one("SELECT params FROM jobs WHERE id=?", (job_id,))
    if row is None:
        raise ValueError(f"travail {job_id} introuvable")
    found = json.loads(row["params"])
    with _params_lock:
        if len(_params) > 64:
            _params.clear()
        _params[job_id] = found
    return found


def forget_params(job_id=None):
    with _params_lock:
        if job_id is None:
            _params.clear()
        else:
            _params.pop(job_id, None)


def expand(unit, here: bool = True, fresh: bool = True) -> dict:
    """L'ordre complet d'une unité (§ 2.2), tiré de jobs.params. Pour ce PC (here), il
    porte aussi « local » : l'appli, le travail, l'unité et les chemins des entrées, qui ne
    partent jamais ailleurs. fresh : une remise, qui tire un nouveau run_uuid ; sinon celui
    de la remise en cours (résultat revenu d'une autre machine)."""
    params = params_of(unit["job_id"])
    payload = unit["payload"] if isinstance(unit["payload"], dict) else json.loads(unit["payload"])
    item = params["items"][payload["item"]]
    graph = comfy_graph.apply_set(params["graphs"][item["graph"]], item["set"])
    graph, mapping = comfy_graph.renumber(graph)
    used = comfy_graph.tokens(graph)
    run_id = _run_id(unit) if fresh else _current_run_id(unit)
    contract = (params.get("contract") or {}).get(str(item["graph"])) or {}
    order = {"engine": ENGINE, "api": params["api"], "run_uuid": run_id, "graph": graph,
             "seed": item["seed"],
             "inputs": [{"slot": key, **params["inputs"][key]} for key in sorted(used["inputs"])],
             "models": [{"token": comfy_graph.TOKEN_MODEL + n, **params["models"][n]}
                        for n in sorted(used["models"], key=int)],
             "contract": contract, "outputs": [mapping[n] for n in params["outputs"][item["graph"]]],
             "bounds": {k: params["bounds"][k] for k in ("unit_seconds", "output_mb")},
             "strict": params.get("strict", False)}
    if here:
        from aiodos.gateway import input_store
        files = {}
        for entry in order["inputs"]:
            path = input_store.path_of(params["app_id"], entry["sha256"])
            if path is None:
                raise RuntimeError(f"Entrée « {entry['slot']} » introuvable sur ce PC.")
            files[entry["slot"]] = str(path)
        order["local"] = {"app_id": params["app_id"], "job_id": unit["job_id"],
                          "unit_id": unit["id"], "item": payload["item"],
                          "label": item.get("label"), "files": files}
    return order


def _run_id(unit) -> str:
    """Tiré à chaque remise (expand), gardé dans units.run_uuid : le prompt_id de ComfyUI,
    et le nom du dossier de calcul de l'unité."""
    run_id = comfy_client.new_run_id()
    db.execute("UPDATE units SET run_uuid=? WHERE id=?", (run_id, unit["id"]))
    return run_id


def _current_run_id(unit) -> str:
    row = db.query_one("SELECT run_uuid FROM units WHERE id=?", (unit["id"],))
    return (row["run_uuid"] if row and row["run_uuid"] else None) or _run_id(unit)


# ----------------------------------------------------------------- calculer
def instance_here():
    """Le ComfyUI de ce PC s'il est accordé et ouvert : celui de l'utilisateur, ou celui
    qu'AIODOS a lancé (engines/comfy_instance.py). Gardé HERE_TTL s : la file le demande
    souvent."""
    now = time.monotonic()
    if now - _here["at"] < HERE_TTL:
        return _here["found"]
    found = comfyui.instance() or (comfy_instance.current() if comfyui.enabled() else None)
    _here.update(at=now, found=found)
    return found


def forget_here():
    _here.update(at=0.0, found=None)


def runnable_here(model) -> bool:
    """L'exécuteur de ce PC prend-il les unités de ces modèles ? ComfyUI accordé, ouvert (ou
    qu'AIODOS peut lancer : ses modèles lus sur le disque), et tous les modèles de l'unité
    présents."""
    found = instance_here()
    if found is None and not comfy_instance.launchable():
        return False
    needs = _needs(model)
    if needs is None:
        return False
    if found is not None:
        try:
            have = comfyui.inventory(found.url).get("models") or {}
        except comfy_client.Unreachable:
            return False
    else:
        have = comfy_instance.disk_models()
    return all(comfyui.local_name(have, m.get("folder"), m["name"]) for m in needs)


def _needs(model) -> list | None:
    """Les modèles d'une clé comfyui:<…>, d'après les travaux en cours qui la citent."""
    for row in db.query("SELECT id, params FROM jobs WHERE kind=? AND status IN "
                        "('queued', 'running', 'paused')", (KIND,)):
        params = params_of(row["id"])
        for index, key in params["model_keys"].items():
            if key == model:
                graph = params["graphs"][int(index)]
                return [params["models"][n] for n in comfy_graph.tokens(graph)["models"]]
    return None


def requires(unit, options) -> dict:
    """Ce qu'il faut à une autre machine pour calculer cette unité (network/work.can_serve,
    engines/lending.can_serve) : ComfyUI à la même version d'unité, les classes du graphe,
    les modèles par dossier et par nom (mes machines seulement en L2b)."""
    params = params_of(unit["job_id"])
    payload = unit["payload"] if isinstance(unit["payload"], dict) else json.loads(unit["payload"])
    item = params["items"][payload["item"]]
    graph = params["graphs"][item["graph"]]
    used = comfy_graph.tokens(graph)["models"]
    return {"engine": {"name": ENGINE, "api": params["api"],
                       "classes": sorted({n["class_type"] for n in graph.values()
                                          if n["class_type"] not in comfy_graph.PREVIEWS}),
                       "models": [{"folder": params["models"][n].get("folder"),
                                   "name": params["models"][n]["name"]} for n in sorted(used)]}}


def compute(order, file, options, stop=None):
    """Joue l'unité dans le ComfyUI de ce PC (engines/comfyui.run) → les fichiers produits.
    Pour ce PC (l'ordre porte « local »), leurs chemins dans le dossier de calcul ; pour une
    autre machine (ses entrées reçues dans « files »), leurs octets, qui repartent avec le
    résultat (work/result)."""
    from aiodos.jobs import queue
    halt = stop if stop is not None else pacing.stopping()
    local = order.get("local") or {}
    unit_id = local.get("unit_id")
    remote = not local

    def progress(stage, **info):
        if unit_id:
            queue.set_progress(unit_id, {"stage": stage, "at": db.now()})
        else:                               # repart avec le prochain renouvellement du bail
            with _stages_lock:
                _stages[order["run_uuid"]] = stage
    try:
        return _compute(order, halt, progress, local, remote)
    finally:
        with _stages_lock:
            _stages.pop(order.get("run_uuid"), None)


_stages = {}                      # run_uuid -> étape d'une unité calculée ici pour une autre
_stages_lock = threading.Lock()
STAGES = ("sending_inputs", "waiting_engine", "running", "receiving", "checking")


def stage_of(run_id) -> str | None:
    """L'étape d'une unité calculée ici pour une autre machine (network/worker.py la joint
    au renouvellement de son bail)."""
    with _stages_lock:
        return _stages.get(run_id)


def _compute(order, halt, progress, local, remote):
    from aiodos.jobs.registry import Interrupted
    pacing.wait_turn(halt, True)            # les conversations d'abord
    gpu_share.before_unit()                 # la mémoire vidéo qu'AIODOS tient sans s'en servir
    found = instance_here()
    if found is None and comfy_instance.launchable():
        progress("waiting_engine")          # ComfyUI se lance sans fenêtre (L2c)
        try:
            found = comfy_instance.ensure(stop=halt)
        except comfy_instance.Unavailable as exc:
            raise Interrupted(str(exc)) from exc
        forget_here()
        try:
            comfy_instance.save_inventory(comfyui.inventory(found.url))
        except comfy_client.Unreachable:
            pass
    if found is None:
        raise Interrupted("ComfyUI n'est pas ouvert sur ce PC.")
    folder = paths.engine_runs_dir() / order["run_uuid"]
    began = time.monotonic()
    files = (order.get("files") if remote else local.get("files")) or {}
    try:
        outputs = comfyui.run(order, files, folder, halt, progress, url=found.url)
    except comfyui.Refused as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError(f"Refusé par ce ComfyUI ({exc.reason}) : {exc}") from exc
    except comfyui.Failed as exc:
        shutil.rmtree(folder, ignore_errors=True)
        where = f" (nœud {exc.node})" if exc.node else ""
        raise RuntimeError(f"{exc.code} : {exc}{where}") from exc
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        comfy_instance.touch()              # l'instance d'AIODOS reste prête un moment
    machine = str(settings.get("node_name") or settings.machine_name())[:80]
    version = (comfyui.inventory(found.url) or {}).get("version")
    progress("checking")
    produced = [{"path": str(o.path), "node": o.node, "type": o.type, "size": o.size,
                 "sha256": o.sha256} for o in outputs]
    if remote:                              # les octets repartent chez le propriétaire
        for entry in produced:
            path = Path(entry.pop("path"))
            entry["name"] = path.name.split("-", 1)[-1]
            entry["data"] = base64.b64encode(path.read_bytes()).decode("ascii")
        shutil.rmtree(folder, ignore_errors=True)
    return {"files": produced, "machine": machine,
            "engine": f"comfyui {version}" if version else "comfyui",
            "seconds": round(time.monotonic() - began, 2)}


MAX_FILES = 64


def validate(order, output):
    """Le résultat d'une autre machine, contrôlé avant d'entrer (chez le propriétaire) : des
    fichiers en nombre et en poids bornés, leurs octets à l'empreinte annoncée, d'un genre
    reconnu. ValueError sinon (le résultat est refusé, l'unité échoue)."""
    from aiodos.engines import media
    files = output.get("files") if isinstance(output, dict) else None
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError("résultat sans fichiers, ou trop de fichiers")
    limit = int(float((order.get("bounds") or {}).get("output_mb") or 200) * 1024 ** 2)
    total, outputs = 0, set(order.get("outputs") or [])
    for f in files:
        if not isinstance(f, dict) or "data" not in f:
            raise ValueError("fichier du résultat illisible")
        try:
            data = base64.b64decode(str(f["data"]), validate=True)
        except ValueError as exc:
            raise ValueError("fichier du résultat illisible") from exc
        total += len(data)
        if total > limit:
            raise ValueError("résultat plus lourd que permis")
        if len(data) != f.get("size") or hashlib.sha256(data).hexdigest() != f.get("sha256"):
            raise ValueError("fichier du résultat incomplet ou modifié")
        if str(f.get("node")) not in outputs:
            raise ValueError("fichier d'un nœud qui n'est pas une sortie de l'unité")
        if media.sniff(data[:media.HEAD]) is None:
            raise ValueError("fichier du résultat d'un genre inconnu")


def _safe_name(name) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or ""))[:80].strip("._")
    return text or "sortie"


def assimilate(order, output) -> dict:
    """Les fichiers produits, rangés pour l'appli (chez le propriétaire, ce PC) : depuis le
    dossier de calcul (calculés ici), ou depuis leurs octets (une de mes machines)."""
    from aiodos.engines import media
    from aiodos.gateway import file_store
    local = order.get("local") or {}
    job = db.query_one("SELECT public_id FROM jobs WHERE id=?", (local["job_id"],))
    folder = paths.engine_runs_dir() / order["run_uuid"]
    place = "here"
    ids = []
    for n, f in enumerate(output.get("files") or [], 1):
        if "data" in f:                       # calculé par une de mes machines
            place = "mine"
            data = base64.b64decode(str(f["data"]))
            folder.mkdir(parents=True, exist_ok=True)
            kind = media.sniff(data[:media.HEAD]) or "application/octet-stream"
            path = folder / f"{n:03d}-{_safe_name(f.get('name'))}"
            path.write_bytes(data)
        else:
            path, kind = Path(f["path"]), str(f["type"])
            if not path.is_file() or path.stat().st_size != int(f["size"]):
                raise ValueError("sortie absente ou tronquée")
        row = file_store.add(local["app_id"], local["job_id"], job["public_id"], local["item"],
                             path, node=str(f["node"]), kind=kind, size=int(f["size"]),
                             sha256=str(f["sha256"]),
                             machine=str(output.get("machine") or "")[:80] or None,
                             place=place, engine=str(output.get("engine") or "")[:80] or None)
        ids.append(row["id"])
    shutil.rmtree(folder, ignore_errors=True)
    return {"files": ids, "machine": output.get("machine"), "place": place,
            "seconds": output.get("seconds"), "engine": output.get("engine")}


def finalize(job_id, params, units) -> dict:
    forget_params(job_id)
    done = [u for u in units if u["status"] == "done"]
    return {"files": sum(len((u["result"] or {}).get("files") or []) for u in done),
            "units_done": len(done)}


register_portable(TASK, compute, assimilate, requires=requires, validate=validate,
                  here=runnable_here, mine_only=True, expand=expand)
register_job_kind(KIND, split, finalize)
