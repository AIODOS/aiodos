"""Mes workflows de ComfyUI importés (comfyui-createur.md § 2.2) : un graphe au format API
(« Exporter (API) » dans ComfyUI, ou un graphe de PANDORA), gardé dans le dossier de données
(data/engines/comfyui/workflows/<id>.json), rejoué sur ce PC et sur mes machines seulement :
jamais envoyé ailleurs (un nœud ajouté peut exécuter n'importe quel code chez celui qui
calcule ; jobs/kinds/engine.py : mine_only), jamais au réseau public (les recettes seules).

À l'import (load) :
- le JSON, borné (MAX_BYTES, comme un graphe d'appli), au format API (celui de l'éditeur,
  « nodes » et « links », est refusé avec de quoi faire), vérifié comme un graphe d'appli de
  mes machines (comfy_graph : forme, valeurs interdites ; nœuds d'un service en ligne refusés
  quand les définitions du ComfyUI de ce PC sont connues) ; au moins une sortie enregistrée ;
  pas encore de fichier d'entrée (LoadImage…) : refusé, dit tel quel ;
- ses paramètres repérés d'office (detect) : la description et la description négative
  (le texte des encodeurs qui alimentent l'entrée positive ou négative d'un échantillonneur),
  la graine (remplacée par le jeton @seed : une par création), les pas, le guidage, la taille
  et la durée (latent vide, nœuds vidéo), la cadence ; chacun avec des bornes larges et sa
  valeur du workflow pour défaut, ajustable à chaque création (et la liste elle-même, par
  update) ;
- les entrées de texte des encodeurs (« texts ») sont notées : de quoi revérifier le graphe
  sans le ComfyUI ouvert (seul le caractère nul compte dans une consigne).
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path

from aiodos import paths
from aiodos.engines import comfy_graph, recipes
from aiodos.i18n import t

MAX_BYTES = 2 * 1024 ** 2          # comme un graphe d'appli (jobs/kinds/engine.MAX_GRAPH_BYTES)
MAX_WORKFLOWS = 200
MAX_NAME = 80
ID_BYTES = 6

# Les encodeurs de texte du cœur de ComfyUI, et leurs entrées de consigne
TEXT_NODES = {"CLIPTextEncode": ("text",), "CLIPTextEncodeSDXL": ("text_g", "text_l"),
              "CLIPTextEncodeSDXLRefiner": ("text",), "CLIPTextEncodeFlux": ("clip_l", "t5xxl"),
              "CLIPTextEncodeSD3": ("clip_l", "clip_g", "t5xxl"),
              "CLIPTextEncodeHunyuanDiT": ("bert", "mt5xl"),
              "TextEncodeQwenImageEdit": ("prompt",), "TextEncodeQwenImageEditPlus": ("prompt",),
              "TextEncodeHunyuanVideo_ImageToVideo": ("prompt",)}
# Les échantillonneurs et ce qui en tient lieu : leurs entrées de graine, de pas, de guidage
SAMPLING = {"KSampler": {"seed": "seed", "steps": "steps", "cfg": "cfg"},
            "KSamplerAdvanced": {"seed": "noise_seed", "steps": "steps", "cfg": "cfg"},
            "SamplerCustom": {"seed": "noise_seed", "cfg": "cfg"},
            "RandomNoise": {"seed": "noise_seed"}, "BasicScheduler": {"steps": "steps"},
            "CFGGuider": {"cfg": "cfg"}}
GUIDED = ("KSampler", "KSamplerAdvanced", "SamplerCustom", "CFGGuider")
VIDEO_OUTPUTS = {"SaveVideo", "SaveWEBM"}
FPS_NODES = {"CreateVideo": "fps", "SaveWEBM": "fps", "SaveAnimatedWEBP": "fps",
             "SaveAnimatedPNG": "fps"}
# Bornes larges (le workflow est le mien ; ComfyUI revérifie les siennes)
BOUNDS = {"width": (16, 8192), "height": (16, 8192), "frames": (1, 2000), "steps": (1, 200),
          "cfg": (0.0, 100.0), "fps": (1.0, 120.0)}

_lock = threading.Lock()


class WorkflowError(Exception):
    """Workflow refusé : code stable (workflow_invalid, workflow_too_big, workflow_inputs,
    workflow_unknown, workflow_full, graph_not_api…), message."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def folder() -> Path:
    found = paths.data_dir() / "engines" / "comfyui" / "workflows"
    found.mkdir(parents=True, exist_ok=True)
    return found


# ------------------------------------------------------------------ le repérage
def _literal(graph, node_id, name, kinds):
    value = graph[node_id].get("inputs", {}).get(name)
    if isinstance(value, bool) or not isinstance(value, kinds):
        return None
    return value


def _upstream_texts(graph, start) -> list:
    """Les consignes des encodeurs de texte les plus proches en amont de ce nœud
    ([(nœud, entrée)]), en remontant les liens."""
    seen, frontier = set(), [start]
    while frontier:
        found, following = [], []
        for node_id in frontier:
            if node_id in seen or node_id not in graph:
                continue
            seen.add(node_id)
            cls = graph[node_id]["class_type"]
            if cls in TEXT_NODES:
                found += [(node_id, name) for name in TEXT_NODES[cls]
                          if _literal(graph, node_id, name, str) is not None]
            else:
                following += sorted(comfy_graph.links_of(graph, node_id),
                                    key=comfy_graph._order_key)
        if found:
            return found
        frontier = following
    return []


def _side(graph, node_id, name) -> list:
    value = graph[node_id].get("inputs", {}).get(name)
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) \
            and value[0] in graph:
        return _upstream_texts(graph, value[0])
    return []


def _targets(pairs) -> list:
    out = []
    for node_id, name in pairs:
        target = f"{node_id}.{name}"
        if target not in out:
            out.append(target)
    return out[:8]


def detect(graph: dict) -> tuple:
    """(graphe, paramètres) : la graine remplacée par @seed là où elle est une valeur, et les
    paramètres repérés d'office, au format de ceux des recettes (recipes.check_params)."""
    graph = {k: {**v, "inputs": dict(v.get("inputs", {}))} for k, v in graph.items()}
    order = sorted(graph, key=comfy_graph._order_key)
    positive, negative = [], []
    for node_id in order:
        if graph[node_id]["class_type"] in GUIDED:
            positive += _side(graph, node_id, "positive")
            negative += _side(graph, node_id, "negative")
    negative = [p for p in negative if p not in positive]
    if not positive:                      # aucun échantillonneur lisible : le premier texte
        positive = next(([(n, name)] for n in order if graph[n]["class_type"] in TEXT_NODES
                         for name in TEXT_NODES[graph[n]["class_type"]]
                         if _literal(graph, n, name, str) is not None), [])
    found = {"seed": [], "steps": [], "cfg": [], "width": [], "height": [], "frames": [],
             "fps": []}
    for node_id in order:
        cls = graph[node_id]["class_type"]
        for what, name in SAMPLING.get(cls, {}).items():
            kinds = (int, float) if what == "cfg" else int
            if _literal(graph, node_id, name, kinds) is not None:
                found[what].append((node_id, name))
        loader = cls.endswith(("Loader", "LoaderSimple"))
        if not loader and all(_literal(graph, node_id, n, int) is not None
                              for n in ("width", "height")):
            found["width"].append((node_id, "width"))
            found["height"].append((node_id, "height"))
            if _literal(graph, node_id, "length", int) is not None:
                found["frames"].append((node_id, "length"))
        if cls in FPS_NODES and _literal(graph, node_id, FPS_NODES[cls], (int, float)) is not None:
            found["fps"].append((node_id, FPS_NODES[cls]))
    for node_id, name in found["seed"]:
        graph[node_id]["inputs"][name] = comfy_graph.TOKEN_SEED
    params = []
    for name, pairs in (("prompt", positive), ("negative", negative)):
        first = graph[pairs[0][0]]["inputs"][pairs[0][1]] if pairs else None
        if first is not None and len(first) <= recipes.MAX_TEXT:     # plus long : laissé tel quel
            params.append({"name": name, "type": "text", "max_length": recipes.MAX_TEXT,
                           "default": first, "targets": _targets(pairs)})
    if found["seed"]:
        params.append({"name": "seed", "type": "seed", "targets": _targets(found["seed"])})
    for name in ("width", "height", "frames", "fps", "steps", "cfg"):
        pairs = found[name]
        if not pairs:
            continue
        first = graph[pairs[0][0]]["inputs"][pairs[0][1]]
        low, high = BOUNDS[name]
        kind = "float" if name in ("cfg", "fps") else "int"
        default = float(first) if kind == "float" else int(first)
        if not low <= default <= high:
            continue
        spec = {"name": name, "type": kind, "min": low, "max": high, "default": default,
                "targets": _targets(pairs)}
        if name in ("width", "height") and (default - low) % 8 == 0:
            spec["step"] = 8
        params.append(spec)
    return graph, params


def kind_of(graph: dict) -> str:
    classes = {node["class_type"] for node in graph.values()}
    return "video" if classes & VIDEO_OUTPUTS else "image"


def texts_of(graph: dict, defs: dict | None) -> list:
    """[[classe, entrée]] des consignes de ce graphe : celles des encodeurs connus, et les
    STRING multiline des définitions du ComfyUI de ce PC quand on les a."""
    found = set()
    for node in graph.values():
        cls = node["class_type"]
        for name in TEXT_NODES.get(cls, ()):
            found.add((cls, name))
        for name, spec in comfy_graph.input_specs(defs, cls).items():
            if comfy_graph._is_multiline(spec):
                found.add((cls, name))
    return sorted([cls, name] for cls, name in found)


def text_defs(workflow: dict) -> dict:
    """Les définitions des consignes du workflow (texts), pour comfy_graph.check_values."""
    defs = {}
    for cls, name in workflow.get("texts") or []:
        entry = defs.setdefault(cls, {"input": {"required": {}}})
        entry["input"]["required"][name] = ["STRING", {"multiline": True}]
    return defs


# ------------------------------------------------------------------ l'import
def _unwrap(data):
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict) \
            and all(isinstance(v, dict) for v in data["prompt"].values()):
        return data["prompt"]                 # {"prompt": {…}} : le corps d'un POST /prompt
    return data


def load(raw: bytes, name: str | None = None, defs: dict | None = None) -> dict:
    """Le workflow vérifié et ses paramètres repérés (pas encore gardé : add) ;
    WorkflowError sinon. defs : l'object_info du ComfyUI de ce PC, s'il est ouvert."""
    if len(raw) > MAX_BYTES:
        raise WorkflowError("workflow_too_big", t("comfy_workflows.too_big",
                                                  mb=MAX_BYTES // 1024 ** 2))
    try:
        graph = _unwrap(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise WorkflowError("workflow_invalid", t("comfy_workflows.not_json")) from exc
    try:
        comfy_graph.check_shape(graph, "mine")
    except comfy_graph.GraphError as exc:
        raise WorkflowError(exc.code, str(exc), **exc.detail) from exc
    loaders = sorted({n["class_type"] for n in graph.values()} & set(comfy_graph.FILE_LOADERS))
    if loaders:
        raise WorkflowError("workflow_inputs", t("comfy_workflows.reads_files", node=loaders[0]))
    if comfy_graph.tokens(graph)["models"] or comfy_graph.tokens(graph)["inputs"]:
        raise WorkflowError("workflow_invalid", t("comfy_workflows.has_tokens"))
    cloud = comfy_graph.cloud_nodes(graph, defs) if defs else []
    if cloud:
        raise WorkflowError("forbidden_node", t("comfy.cloud_node"), node=cloud[0])
    texts = texts_of(graph, defs)
    holder = {"texts": texts}
    try:
        comfy_graph.check_values(graph, defs or text_defs(holder))
    except comfy_graph.GraphError as exc:
        raise WorkflowError(exc.code, str(exc), **exc.detail) from exc
    graph, params = detect(graph)
    outputs = [n for n in comfy_graph.outputs(graph, defs or None)
               if graph[n]["class_type"] not in comfy_graph.PREVIEWS]
    if not outputs:
        raise WorkflowError("workflow_invalid", t("engine_jobs.no_saved_output"))
    title = " ".join(str(name or "").split())[:MAX_NAME] or t("comfy_workflows.default_name")
    workflow = {"name": title, "graph": graph, "params": params, "texts": texts,
                "kind": kind_of(graph), "nodes": len(graph),
                "classes": sorted({n["class_type"] for n in graph.values()})}
    check(workflow)
    return workflow


def check(workflow: dict):
    """Ses paramètres revérifiés contre son graphe (au chargement, après update)."""
    try:
        recipes.check_params({"id": workflow.get("id") or workflow.get("name")},
                             workflow["graph"], workflow["params"])
    except recipes.RecipeError as exc:
        raise WorkflowError("workflow_invalid", str(exc)) from exc


# ------------------------------------------------------------------ la garde
def _path(workflow_id: str) -> Path:
    if not isinstance(workflow_id, str) or len(workflow_id) != 2 * ID_BYTES \
            or any(c not in "0123456789abcdef" for c in workflow_id):
        raise WorkflowError("workflow_unknown", t("comfy_workflows.unknown"))
    return folder() / f"{workflow_id}.json"


def _write(workflow: dict):
    target = _path(workflow["id"])
    part = target.with_name(target.name + ".part")
    part.write_bytes((json.dumps(workflow, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(part, target)


def add(workflow: dict) -> dict:
    with _lock:
        if len(list(folder().glob("*.json"))) >= MAX_WORKFLOWS:
            raise WorkflowError("workflow_full", t("comfy_workflows.full", n=MAX_WORKFLOWS))
        workflow = {"id": secrets.token_hex(ID_BYTES), "imported": time.time(), **workflow}
        _write(workflow)
    return workflow


def get(workflow_id: str) -> dict:
    path = _path(workflow_id)
    try:
        workflow = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise WorkflowError("workflow_unknown", t("comfy_workflows.unknown")) from exc
    check(workflow)
    return workflow


def listing() -> list:
    """Mes workflows, les plus récents d'abord (sans leur graphe)."""
    found = []
    for path in folder().glob("*.json"):
        try:
            workflow = get(path.stem)
        except WorkflowError:
            continue
        found.append(summary(workflow))
    return sorted(found, key=lambda w: -float(w.get("imported") or 0))


def update(workflow_id: str, name=None, params=None) -> dict:
    """Son nom, ou ses paramètres corrigés à la main (revérifiés contre son graphe)."""
    with _lock:
        workflow = get(workflow_id)
        if name is not None:
            title = " ".join(str(name).split())[:MAX_NAME]
            if not title:
                raise WorkflowError("workflow_invalid", t("comfy_workflows.name_empty"))
            workflow["name"] = title
        if params is not None:
            workflow["params"] = params
            check(workflow)
        _write(workflow)
    return workflow


def delete(workflow_id: str) -> bool:
    path = _path(workflow_id)
    with _lock:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


def summary(workflow: dict) -> dict:
    return {"id": workflow["id"], "name": workflow["name"], "kind": workflow.get("kind", "image"),
            "imported": workflow.get("imported"), "nodes": workflow.get("nodes"),
            "classes": workflow.get("classes") or [], "params": workflow["params"]}
