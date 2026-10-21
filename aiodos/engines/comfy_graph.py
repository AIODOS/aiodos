"""Les graphes de ComfyUI au format API, en fonctions pures (docs/conception/partage-puissance.md
§ 2.2, § 3.6 et § 4.3) : vérifier la forme, appliquer les retouches d'une unité, renuméroter,
remplacer les jetons, refuser les valeurs qui feraient lire ou écrire un fichier ou joindre une
adresse, trouver les sorties. Rien ici ne touche au disque ni au réseau : la règle s'applique
deux fois, à la mise en file chez le demandeur, puis chez celui qui calcule, avec ses propres
définitions des nœuds (defs : l'object_info de son ComfyUI, ou None).

Un graphe au format API : {"<id>": {"class_type": "...", "inputs": {...}, "_meta": {...}}}.
Une valeur d'entrée est un texte, un nombre, un booléen, un lien [<id>, <sortie>] ou une liste
de ceux-ci. Les jetons : « @in:<id> » (un fichier d'entrée envoyé à part, la seule façon d'en
donner un), « @model:<clé> » (un modèle résolu par empreinte), « @seed » (la graine de l'unité).
"""
import re
import unicodedata

MAX_NODES = {"mine": 2000, "friends": 500, "public": 500}
MAX_TEXT = 20_000
TOKEN_IN, TOKEN_MODEL, TOKEN_SEED = "@in:", "@model:", "@seed"
# Nœuds qui lisent un fichier d'entrée : leur entrée de fichier doit être un jeton @in:
FILE_LOADERS = {"LoadImage": "image", "LoadImageMask": "image", "LoadVideo": "file",
                "LoadAudio": "audio"}
# Nœuds de sortie connus sans définitions (avec elles : output_node)
OUTPUT_NODES = {"SaveImage", "SaveAnimatedWEBP", "SaveAnimatedPNG", "SaveVideo", "SaveWEBM",
                "SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "SaveGLB"}
# Toujours retirés : ils écrivent dans le dossier temporaire de ComfyUI
PREVIEWS = {"PreviewImage", "PreviewAny", "PreviewAudio"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
_DRIVE = re.compile(r"^[A-Za-z]:")
_ANNOTATION = re.compile(r"\[\s*(output|input|temp)\s*\]\s*$", re.IGNORECASE)
_MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")


class GraphError(Exception):
    """Graphe refusé : code stable (graph_not_api, graph_invalid, patch_invalid,
    forbidden_value, forbidden_node, input_missing) et détail (nœud, entrée, raison)."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------- forme
def is_editor_format(graph) -> bool:
    """Le format de l'éditeur (« nodes » et « links ») au lieu du format API."""
    return isinstance(graph, dict) and isinstance(graph.get("nodes"), list) \
        and "links" in graph


def _is_link(value, graph) -> bool:
    return (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
            and isinstance(value[1], int) and not isinstance(value[1], bool)
            and value[0] in graph)


def check_shape(graph, circle: str = "mine"):
    """La forme : un objet de nœuds, chacun {class_type, inputs[, _meta]}, des liens vers des
    nœuds qui existent, aucun cycle, des tailles bornées. Lève GraphError."""
    if is_editor_format(graph):
        raise GraphError("graph_not_api", "Graphe au format de l'éditeur : exporte-le au format "
                         "API (« Exporter (API) » dans ComfyUI).")
    if not isinstance(graph, dict) or not graph:
        raise GraphError("graph_invalid", "Graphe vide ou illisible.")
    if len(graph) > MAX_NODES.get(circle, MAX_NODES["friends"]):
        raise GraphError("graph_invalid", f"Trop de nœuds ({len(graph)}).", nodes=len(graph))
    for node_id, node in graph.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise GraphError("graph_invalid", "Nœud illisible.", node=str(node_id))
        if not isinstance(node.get("class_type"), str) or not node["class_type"]:
            raise GraphError("graph_invalid", "Nœud sans class_type.", node=node_id)
        if not isinstance(node.get("inputs", {}), dict):
            raise GraphError("graph_invalid", "« inputs » doit être un objet.", node=node_id)
        extra = set(node) - {"class_type", "inputs", "_meta", "is_changed"}
        if extra:
            raise GraphError("graph_invalid", f"Champ inconnu dans un nœud : {sorted(extra)[0]}.",
                             node=node_id)
        for name, value in node.get("inputs", {}).items():
            _check_value_shape(graph, node_id, name, value)
    _check_cycles(graph)


def _check_value_shape(graph, node_id, name, value, depth=0):
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return
    if isinstance(value, str):
        if len(value) > MAX_TEXT:
            raise GraphError("graph_invalid", "Texte trop long.", node=node_id, input=name)
        return
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int) \
                and not isinstance(value[1], bool):
            if value[0] not in graph:
                raise GraphError("graph_invalid", f"Lien vers un nœud absent : {value[0]}.",
                                 node=node_id, input=name)
            return
        if depth > 2:
            raise GraphError("graph_invalid", "Valeur trop imbriquée.", node=node_id, input=name)
        for item in value:
            _check_value_shape(graph, node_id, name, item, depth + 1)
        return
    raise GraphError("graph_invalid", "Valeur d'entrée illisible.", node=node_id, input=name)


def links_of(graph, node_id) -> set:
    """Les nœuds dont ce nœud lit une sortie."""
    found = set()

    def walk(value):
        if _is_link(value, graph):
            found.add(value[0])
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for value in graph[node_id].get("inputs", {}).values():
        walk(value)
    return found


def _check_cycles(graph):
    state = {}

    def visit(start):
        stack = [(start, iter(sorted(links_of(graph, start))))]
        state[start] = 1
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2
                stack.pop()
            elif state.get(child) == 1:
                raise GraphError("graph_invalid", "Le graphe forme un cycle.", node=child)
            elif child not in state:
                state[child] = 1
                stack.append((child, iter(sorted(links_of(graph, child)))))
    for node_id in graph:
        if node_id not in state:
            visit(node_id)


# --------------------------------------------------------------- retouches
def apply_set(graph, patches) -> dict:
    """Les retouches d'une unité ([[nœud, entrée, valeur], …]) : chacune remplace une valeur
    littérale déjà présente, du même type JSON, jamais un lien ni un class_type. → une copie."""
    out = {k: {**v, "inputs": dict(v.get("inputs", {}))} for k, v in graph.items()}
    for patch in patches or []:
        if not (isinstance(patch, list) and len(patch) == 3 and isinstance(patch[0], str)
                and isinstance(patch[1], str)):
            raise GraphError("patch_invalid", "Retouche illisible : [nœud, entrée, valeur].")
        node_id, name, value = patch
        node = out.get(node_id)
        if node is None or name not in node["inputs"]:
            raise GraphError("patch_invalid", "La retouche vise un nœud ou une entrée absents.",
                             node=node_id, input=name)
        current = node["inputs"][name]
        if _is_link(current, out) or isinstance(current, (list, dict)):
            raise GraphError("patch_invalid", "Une retouche ne remplace pas un lien.",
                             node=node_id, input=name)
        if _kind(current) != _kind(value):
            raise GraphError("patch_invalid", "La retouche change le type de la valeur.",
                             node=node_id, input=name)
        _check_value_shape(out, node_id, name, value)
        node["inputs"][name] = value
    return out


def _kind(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


# ----------------------------------------------------------- renumérotation
def _order_key(node_id: str):
    return (0, int(node_id), node_id) if node_id.isdigit() else (1, 0, node_id)


def renumber(graph) -> tuple:
    """Nœuds renumérotés « 1 », « 2 »… dans l'ordre des identifiants d'origine, liens compris,
    et « _meta » remplacé par {} (ComfyUI le lit : il garde des titres). → (graphe, {ancien:
    nouveau})."""
    mapping = {old: str(i) for i, old in enumerate(sorted(graph, key=_order_key), 1)}

    def rewrite(value):
        if _is_link(value, graph):
            return [mapping[value[0]], value[1]]
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        return value
    out = {}
    for old, node in graph.items():
        out[mapping[old]] = {"class_type": node["class_type"],
                             "inputs": {k: rewrite(v) for k, v in node.get("inputs", {}).items()},
                             "_meta": {}}
    return dict(sorted(out.items(), key=lambda kv: int(kv[0]))), mapping


def without_previews(graph) -> dict:
    """Sans les nœuds d'aperçu, qui écrivent dans le dossier temporaire de ComfyUI."""
    return {k: v for k, v in graph.items() if v["class_type"] not in PREVIEWS}


# ------------------------------------------------------------------- jetons
def tokens(graph) -> dict:
    """{"inputs": {ids}, "models": {clés}, "seed": bool} : les jetons employés."""
    found = {"inputs": set(), "models": set(), "seed": False}

    def walk(value):
        if isinstance(value, str):
            if value.startswith(TOKEN_IN):
                found["inputs"].add(value[len(TOKEN_IN):])
            elif value.startswith(TOKEN_MODEL):
                found["models"].add(value[len(TOKEN_MODEL):])
            elif value == TOKEN_SEED:
                found["seed"] = True
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for node in graph.values():
        for value in node.get("inputs", {}).values():
            walk(value)
    return found


def replace_tokens(graph, inputs: dict, models: dict, seed=None) -> dict:
    """Jetons remplacés : @in:<id> → inputs[id] (nom du fichier chez celui qui calcule),
    @model:<clé> → models[clé] (nom local), @seed → seed. Un jeton sans valeur : GraphError."""
    def swap(value, node_id, name):
        if isinstance(value, str):
            if value.startswith(TOKEN_IN):
                key = value[len(TOKEN_IN):]
                if key not in inputs:
                    raise GraphError("input_missing", f"Entrée absente : {key}.", node=node_id,
                                     input=name)
                return inputs[key]
            if value.startswith(TOKEN_MODEL):
                key = value[len(TOKEN_MODEL):]
                if key not in models:
                    raise GraphError("model_unresolved", f"Modèle non résolu : {key}.",
                                     node=node_id, input=name)
                return models[key]
            if value == TOKEN_SEED:
                if seed is None:
                    raise GraphError("graph_invalid", "Jeton @seed sans graine.", node=node_id,
                                     input=name)
                return seed
            return value
        if isinstance(value, list) and not (len(value) == 2 and isinstance(value[1], int)
                                            and value[0] in graph):
            return [swap(v, node_id, name) for v in value]
        return value
    return {node_id: {**node, "inputs": {k: swap(v, node_id, k)
                                         for k, v in node.get("inputs", {}).items()}}
            for node_id, node in graph.items()}


# -------------------------------------------------------- valeurs interdites
def forbidden(value: str) -> str | None:
    """Pourquoi ce texte est refusé (chemin, adresse, annotation, nom réservé…), ou None
    (§ 4.3 de la conception)."""
    if "\x00" in value:
        return "nul"
    text = value.strip()
    if _ANNOTATION.search(text):
        return "annotation"
    lowered = text.lower()
    if "://" in lowered or lowered.startswith(("file:", "http", "ftp", "www.")):
        return "url"
    if lowered.startswith("data:"):
        return "data_url"
    if text.startswith(("/", "\\", "~")) or _DRIVE.match(text):
        return "absolute_path"
    decoded = re.sub(r"%2e", ".", text, flags=re.IGNORECASE).replace("\\", "/")
    parts = decoded.split("/")
    if any(p == ".." for p in parts):
        return "parent_path"
    stem = unicodedata.normalize("NFC", parts[-1]).split(".")[0].strip().lower()
    if stem in _RESERVED:
        return "reserved_name"
    return None


def input_specs(defs, class_type) -> dict:
    node = (defs or {}).get(class_type) if isinstance(defs, dict) else None
    inputs = node.get("input") if isinstance(node, dict) else None
    flat = {}
    for group in ("required", "optional"):
        for name, spec in ((inputs or {}).get(group) or {}).items():
            flat[name] = spec
    return flat


def _is_multiline(spec) -> bool:
    return (isinstance(spec, list) and len(spec) >= 2 and spec[0] == "STRING"
            and isinstance(spec[1], dict) and spec[1].get("multiline") is True)


def is_file_list(spec) -> bool:
    """Une liste de choix qui énumère des fichiers (modèles, images d'entrée)."""
    if not isinstance(spec, list) or not spec:
        return False
    choices = spec[0]
    if isinstance(choices, list) and choices and all(isinstance(c, str) for c in choices):
        return any(c.lower().endswith(_MODEL_EXT) for c in choices)
    if choices == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        options = spec[1]
        return bool(options.get("image_upload") or options.get("image_folder")
                    or any(str(c).lower().endswith(_MODEL_EXT) for c in options.get("options") or []))
    return False


def check_values(graph, defs=None):
    """Aucune valeur ne fait lire ou écrire un fichier, ni joindre une adresse (§ 4.3). Les
    consignes (STRING multiline des définitions) ne sont contrôlées que pour le caractère nul ;
    les listes de modèles admettent un chemin relatif sans « .. » ; l'entrée de fichier des
    nœuds de lecture doit être un jeton @in:. Lève GraphError(forbidden_value)."""
    for node_id, node in graph.items():
        specs = input_specs(defs, node["class_type"])
        loader = FILE_LOADERS.get(node["class_type"])
        for name, value in node.get("inputs", {}).items():
            if name == "filename_prefix":
                continue                           # réécrit au départ (set_prefix)
            if loader == name:
                if not (isinstance(value, str) and value.startswith(TOKEN_IN)):
                    raise GraphError("forbidden_value", "Un fichier d'entrée se donne par un "
                                     "jeton @in:.", node=node_id, input=name, reason="file_input")
                continue
            for text in _texts(value):
                if text.startswith((TOKEN_IN, TOKEN_MODEL)) or text == TOKEN_SEED:
                    continue
                spec = specs.get(name)
                if _is_multiline(spec):
                    if "\x00" in text:
                        raise GraphError("forbidden_value", "Caractère nul.", node=node_id,
                                         input=name, reason="nul")
                    continue
                # Un modèle se nomme par un chemin relatif (« sdxl/base.safetensors ») :
                # forbidden le laisse passer, pas un chemin absolu ni « .. »
                reason = forbidden(text)
                if reason:
                    raise GraphError("forbidden_value", "Valeur refusée (chemin, adresse ou "
                                     "annotation de dossier).", node=node_id, input=name,
                                     reason=reason)


def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


# ------------------------------------------------------------------ sorties
def outputs(graph, defs=None) -> list:
    """Les nœuds de sortie (output_node des définitions, sinon les « Save… » connus)."""
    found = []
    for node_id, node in graph.items():
        spec = (defs or {}).get(node["class_type"]) if isinstance(defs, dict) else None
        if isinstance(spec, dict) and spec.get("output_node") is True:
            found.append(node_id)
        elif spec is None and node["class_type"] in OUTPUT_NODES:
            found.append(node_id)
    return sorted(found, key=_order_key)


def set_prefix(graph, output_ids, prefix: str) -> dict:
    """Chaque nœud de sortie écrit sous « prefix » (aiodos/<run_uuid>/out) : un préfixe neuf à
    chaque remise, qui fait aussi qu'un graphe déjà calculé produit bien ses fichiers malgré
    le cache des nœuds."""
    out = {k: {**v, "inputs": dict(v.get("inputs", {}))} for k, v in graph.items()}
    for node_id in output_ids:
        if node_id in out:
            out[node_id]["inputs"]["filename_prefix"] = prefix
    return out


def cloud_nodes(graph, defs) -> list:
    """Les nœuds qui appellent un service en ligne (api_node, comfy_api_nodes) : refusés
    partout."""
    found = []
    for node_id, node in graph.items():
        spec = (defs or {}).get(node["class_type"]) if isinstance(defs, dict) else None
        if isinstance(spec, dict) and (spec.get("api_node") is True or str(
                spec.get("python_module") or "").startswith("comfy_api_nodes")):
            found.append(node_id)
    return found
