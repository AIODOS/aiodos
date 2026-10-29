"""Les recettes de ComfyUI (docs/conception/partage-puissance.md § 3.9, comfyui-createur.md
§ 2.2) : quelques workflows éprouvés, image et vidéo, tirés des gabarits officiels de ComfyUI
(paquet comfyui-workflow-templates, licence MIT, cité dans NOTICE), livrés avec AIODOS dans
son catalogue (engines/catalogue.py). Fonctions pures : rien ici ne touche au disque ni au
réseau.

Une recette est un objet JSON :
- id (« comfy-templates/image_sdxl_simple »), version, kind (image | video), title et about
  ({"fr", "en"}), source (gabarit d'origine, version du paquet, empreinte de son JSON) ;
- graph : le graphe au format API, avec ses jetons : « @model:<clé> » à la place de chaque
  modèle, « @seed » à la place de la graine (comfy_graph.py) ;
- params : [{name, type (text | int | float | seed), default, min, max, step, max_length,
  targets: ["<nœud>.<entrée>"]}] : ce que l'utilisateur règle, et où chaque valeur va ;
- models : [{key, folder, name, sha256, size, license, license_url, url}] : chaque modèle par
  son empreinte (null tant que l'auteur ne l'a pas hachée : tools/comfy_recipes.py hash) ;
- contract : {"comfyui", "core", "classes": {classe: empreinte | null}} (comfy_contract.py ;
  null : à calculer par tools/comfy_recipes.py contract contre un ComfyUI de référence) ;
- outputs : [{node, kind}], inputs (genres et nombre ; aucun pour l'instant), vram_mib,
  typical_seconds ({"<Go de la carte>": secondes}), verify (seuil SSIM, règles de contenu).

Le graphe est toujours construit à partir de la recette (build), jamais reçu : seules les
entrées que nomment ses params changent, à des valeurs revérifiées contre leurs bornes
(values) ; aucun nœud n'est ajouté, retiré ni relié autrement. Les classes doivent être des
nœuds de ComfyUI (core, comfy_contract.is_core) : aucun nœud ajouté, aucun service en ligne.
"""
import copy
import hashlib
import json
import math
import re

from aiodos import i18n
from aiodos.engines import comfy_contract, comfy_graph
from aiodos.i18n import t

FORMAT = 1
KINDS = ("image", "video")
PARAM_TYPES = ("text", "int", "float", "seed")
MAX_TEXT = 2000                   # comme une description d'image de sd.cpp (jobs/kinds/images.py)
SEED_MAX = 2 ** 53
MAX_PARAMS = 32
MAX_MODELS = 16
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?")
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
SHA256 = re.compile(r"[0-9a-f]{64}")
# Les dossiers de modèles de ComfyUI (engines/comfy_instance.py), et les extensions des modèles
FOLDERS = frozenset({"checkpoints", "vae", "loras", "clip", "text_encoders", "diffusion_models",
                     "unet", "upscale_models", "embeddings", "controlnet", "clip_vision",
                     "style_models", "model_patches", "audio_encoders", "latent_upscale_models"})


class RecipeError(Exception):
    """Recette refusée ou paramètre hors bornes : code stable (recipe_invalid, recipe_unknown,
    recipe_unsigned, param_invalid, contract), message, détail."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _invalid(recipe, what: str):
    rid = recipe.get("id") if isinstance(recipe, dict) else None
    return RecipeError("recipe_invalid", t("comfy_recipes.invalid", id=str(rid or "?")[:80],
                                           what=what), what=what)


# ------------------------------------------------------------------- la forme
def _target(graph, target):
    """(nœud, entrée) d'une cible « <nœud>.<entrée> » qui désigne une valeur littérale du
    graphe ; None sinon (nœud absent, entrée absente, lien)."""
    if not isinstance(target, str):
        return None
    node, _, name = target.partition(".")
    inputs = (graph.get(node) or {}).get("inputs") if isinstance(graph.get(node), dict) else None
    if not name or not isinstance(inputs, dict) or name not in inputs:
        return None
    value = inputs[name]
    if isinstance(value, list):                  # un lien, ou une liste : jamais réglé
        return None
    return node, name


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _check_param(recipe, graph, n, param):
    where = f"params[{n}]"
    if not isinstance(param, dict) or not isinstance(param.get("name"), str) \
            or not NAME.fullmatch(param["name"]) or param.get("type") not in PARAM_TYPES:
        raise _invalid(recipe, where)
    targets = param.get("targets")
    if not isinstance(targets, list) or not targets or len(targets) > 8:
        raise _invalid(recipe, f"{where}.targets")
    kind = param["type"]
    for target in targets:
        found = _target(graph, target)
        if found is None:
            raise _invalid(recipe, f"{where}.targets")
        current = graph[found[0]]["inputs"][found[1]]
        if kind == "seed":
            if current != comfy_graph.TOKEN_SEED:
                raise _invalid(recipe, f"{where}.targets")
        elif kind == "text":
            # Jamais un jeton (un modèle, une entrée) ni le préfixe des sorties (réécrit)
            if not isinstance(current, str) or current.startswith("@") \
                    or found[1] == "filename_prefix":
                raise _invalid(recipe, f"{where}.targets")
        elif not _number(current):
            raise _invalid(recipe, f"{where}.targets")
    if kind == "text":
        limit = param.get("max_length", MAX_TEXT)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_TEXT \
                or not isinstance(param.get("default", ""), str) \
                or len(param.get("default", "")) > limit:
            raise _invalid(recipe, f"{where}.max_length")
    elif kind in ("int", "float"):
        low, high, default = param.get("min"), param.get("max"), param.get("default")
        if not all(_number(v) for v in (low, high, default)) or not low <= default <= high:
            raise _invalid(recipe, f"{where}.bounds")
        if kind == "int" and not all(isinstance(v, int) for v in (low, high, default)):
            raise _invalid(recipe, f"{where}.bounds")
        step = param.get("step")
        if step is not None and (not _number(step) or step <= 0
                                 or (kind == "int" and not isinstance(step, int))
                                 or not _on_step(default, low, step)):
            raise _invalid(recipe, f"{where}.step")


def _on_step(value, low, step) -> bool:
    if step is None:
        return True
    ratio = (value - low) / step
    return abs(ratio - round(ratio)) < 1e-9


def check_params(owner: dict, graph: dict, params):
    """Les paramètres d'une recette (ou d'un workflow importé, comfy_workflows.py) : chacun
    nommé une fois, d'un type connu, ses cibles des valeurs littérales du graphe du même type
    (jamais un lien, un jeton de modèle ni le préfixe des sorties), son défaut dans ses
    bornes ; RecipeError sinon."""
    if not isinstance(params, list) or len(params) > MAX_PARAMS:
        raise _invalid(owner, "params")
    names = set()
    for n, param in enumerate(params):
        _check_param(owner, graph, n, param)
        if param["name"] in names:
            raise _invalid(owner, f"params[{n}].name")
        names.add(param["name"])


def _check_model(recipe, n, model):
    where = f"models[{n}]"
    if not isinstance(model, dict) or not isinstance(model.get("key"), str) \
            or not KEY.fullmatch(model["key"]) or model.get("folder") not in FOLDERS:
        raise _invalid(recipe, where)
    name = model.get("name")
    if not isinstance(name, str) or not name.lower().endswith(comfy_graph._MODEL_EXT) \
            or comfy_graph.forbidden(name) or len(name) > 200:
        raise _invalid(recipe, f"{where}.name")
    sha, size = model.get("sha256"), model.get("size")
    if sha is not None and (not isinstance(sha, str) or not SHA256.fullmatch(sha)):
        raise _invalid(recipe, f"{where}.sha256")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        raise _invalid(recipe, f"{where}.size")
    if not isinstance(model.get("license"), str) or not model["license"].strip():
        raise _invalid(recipe, f"{where}.license")       # une licence inconnue se dit « inconnue »
    url = model.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
        raise _invalid(recipe, f"{where}.url")


def check(recipe: dict) -> dict:
    """La recette telle qu'elle doit être (forme du § 3.9) ; RecipeError sinon. Le graphe passe
    les contrôles des graphes reçus d'un inconnu (forme, valeurs interdites), sans aperçu ni
    lecture de fichier ; chaque jeton de modèle est déclaré, chaque modèle employé."""
    if not isinstance(recipe, dict) or not isinstance(recipe.get("id"), str) \
            or not ID.fullmatch(recipe["id"]):
        raise _invalid(recipe, "id")
    if not isinstance(recipe.get("version"), str) or not 0 < len(recipe["version"]) <= 40:
        raise _invalid(recipe, "version")
    if recipe.get("kind") not in KINDS:
        raise _invalid(recipe, "kind")
    for field in ("title", "about"):
        texts = recipe.get(field)
        if not isinstance(texts, dict) or not all(isinstance(texts.get(lang), str)
                                                  for lang in i18n.LANGUAGES):
            raise _invalid(recipe, field)
    if not isinstance(recipe.get("source"), dict):
        raise _invalid(recipe, "source")
    graph = recipe.get("graph")
    try:
        comfy_graph.check_shape(graph, "public")
    except comfy_graph.GraphError as exc:
        raise _invalid(recipe, f"graph ({exc.code})") from exc
    classes = {node["class_type"] for node in graph.values()}
    if classes & comfy_graph.PREVIEWS or classes & set(comfy_graph.FILE_LOADERS):
        raise _invalid(recipe, "graph (preview, file)")
    check_params(recipe, graph, recipe.get("params"))
    try:                              # les consignes : seulement le caractère nul et la longueur
        comfy_graph.check_values(graph, text_defs(recipe))
    except comfy_graph.GraphError as exc:
        raise _invalid(recipe, f"graph ({exc.code})") from exc
    models = recipe.get("models")
    if not isinstance(models, list) or len(models) > MAX_MODELS:
        raise _invalid(recipe, "models")
    keys = set()
    for n, model in enumerate(models):
        _check_model(recipe, n, model)
        if model["key"] in keys:
            raise _invalid(recipe, f"models[{n}].key")
        keys.add(model["key"])
    if comfy_graph.tokens(graph)["models"] != keys:
        raise _invalid(recipe, "models (tokens)")
    if comfy_graph.tokens(graph)["inputs"]:
        raise _invalid(recipe, "graph (@in)")       # aucune entrée pour l'instant
    contract = recipe.get("contract")
    if not isinstance(contract, dict) or not isinstance(contract.get("classes"), dict) \
            or set(contract["classes"]) != classes:
        raise _invalid(recipe, "contract")
    outputs = recipe.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise _invalid(recipe, "outputs")
    for out in outputs:
        node = graph.get(out.get("node")) if isinstance(out, dict) else None
        if node is None or node["class_type"] not in comfy_graph.OUTPUT_NODES \
                or "filename_prefix" not in node.get("inputs", {}) or out.get("kind") not in KINDS:
            raise _invalid(recipe, "outputs")
    if recipe.get("inputs", []) != []:
        raise _invalid(recipe, "inputs")
    if not isinstance(recipe.get("vram_mib"), int) or recipe["vram_mib"] <= 0:
        raise _invalid(recipe, "vram_mib")
    typical = recipe.get("typical_seconds")
    if not isinstance(typical, dict) or not all(_number(v) and v > 0 for v in typical.values()):
        raise _invalid(recipe, "typical_seconds")
    return recipe


def digest(recipe: dict) -> str:
    """L'empreinte de la recette (JSON canonique) : deux machines au même digest rejouent la
    même recette (§ 3.9, « digest égal »)."""
    text = json.dumps(recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------------------------------------------------------------- les valeurs
def param(recipe: dict, name: str) -> dict | None:
    return next((p for p in recipe["params"] if p["name"] == name), None)


def default(spec: dict):
    if spec["type"] == "seed":
        return None
    if spec["type"] == "text":
        return spec.get("default", "")
    return spec["default"]


def _refused(spec, key, **params):
    return RecipeError("param_invalid", t(key, name=spec["name"], **params), param=spec["name"])


def value(spec: dict, given):
    """Une valeur revérifiée contre les bornes de son paramètre ; RecipeError sinon."""
    kind = spec["type"]
    if kind == "text":
        if not isinstance(given, str):
            raise _refused(spec, "comfy_recipes.param_text")
        text = given.strip()
        if "\x00" in text or len(text) > spec.get("max_length", MAX_TEXT):
            raise _refused(spec, "comfy_recipes.param_too_long",
                           max=spec.get("max_length", MAX_TEXT))
        return text
    if kind == "seed":
        if not isinstance(given, int) or isinstance(given, bool) or not 0 <= given < SEED_MAX:
            raise _refused(spec, "comfy_recipes.param_seed")
        return given
    if isinstance(given, bool) or not isinstance(given, (int, float)) or not math.isfinite(given):
        raise _refused(spec, "comfy_recipes.param_number")
    if kind == "int":
        if isinstance(given, float):
            if not given.is_integer():
                raise _refused(spec, "comfy_recipes.param_integer")
            given = int(given)
    else:
        given = float(given)
    if not spec["min"] <= given <= spec["max"]:
        raise _refused(spec, "comfy_recipes.param_range", low=spec["min"], high=spec["max"])
    if not _on_step(given, spec["min"], spec.get("step")):
        raise _refused(spec, "comfy_recipes.param_step", step=spec["step"], low=spec["min"])
    return given


def values(recipe: dict, given: dict | None) -> dict:
    """{nom: valeur} de tous les paramètres : ceux donnés, revérifiés ; les autres, à leur
    défaut. Un nom inconnu est refusé. La graine (type seed) n'y est que si elle est donnée :
    sinon chaque unité tire la sienne."""
    given = given or {}
    if not isinstance(given, dict):
        raise RecipeError("param_invalid", t("comfy_recipes.params_shape"))
    unknown = sorted(set(given) - {p["name"] for p in recipe["params"]})
    if unknown:
        raise RecipeError("param_invalid", t("comfy_recipes.param_unknown", name=unknown[0][:40]),
                          param=unknown[0][:40])
    out = {}
    for spec in recipe["params"]:
        if spec["name"] in given and given[spec["name"]] not in (None, ""):
            out[spec["name"]] = value(spec, given[spec["name"]])
        elif spec["type"] != "seed":
            out[spec["name"]] = default(spec)
    return out


def build(recipe: dict, chosen: dict) -> dict:
    """Le graphe de la recette, chaque paramètre posé à sa valeur (déjà revérifiée par
    values) ; la graine reste le jeton @seed (posée par unité). Rien d'autre ne change : mêmes
    nœuds, mêmes classes, mêmes liens."""
    graph = copy.deepcopy(recipe["graph"])
    for spec in recipe["params"]:
        if spec["type"] == "seed" or spec["name"] not in chosen:
            continue
        for target in spec["targets"]:
            node, name = _target(recipe["graph"], target)
            graph[node]["inputs"][name] = chosen[spec["name"]]
    return graph


def declared_models(recipe: dict) -> dict:
    """{clé: {folder, name}} : les modèles de la recette, pour la file (jobs/kinds/engine.py)."""
    return {m["key"]: {"folder": m["folder"], "name": m["name"]} for m in recipe["models"]}


def text_defs(recipe: dict) -> dict:
    """Les définitions des entrées de texte de la recette (des consignes, multiline) : de quoi
    revérifier le graphe quand le ComfyUI de ce PC n'est pas ouvert (comfy_graph.check_values
    ne regarde dans une consigne que le caractère nul et la longueur)."""
    defs = {}
    for spec in recipe["params"]:
        if spec["type"] != "text":
            continue
        for target in spec["targets"]:
            node, name = _target(recipe["graph"], target)
            cls = recipe["graph"][node]["class_type"]
            entry = defs.setdefault(cls, {"input": {"required": {}}})
            entry["input"]["required"][name] = ["STRING", {"multiline": True}]
    return defs


# ------------------------------------------------------------- le contrat
def not_core(recipe: dict, defs: dict) -> list:
    """Les classes de la recette qui ne sont pas des nœuds de ComfyUI ici, ou qui diffèrent de
    son contrat : [(classe, raison)] ; vide si elle peut se rejouer avec ces définitions
    (l'object_info du ComfyUI qui calcule). Raisons : missing (classe absente), custom (nœud
    ajouté ou service en ligne), different (empreinte autre que celle du contrat)."""
    found = []
    contract = recipe.get("contract") or {}
    expected = contract.get("classes") or {}
    for cls in sorted({node["class_type"] for node in recipe["graph"].values()}):
        definition = defs.get(cls) if isinstance(defs, dict) else None
        if not isinstance(definition, dict):
            found.append((cls, "missing"))
        elif not comfy_contract.is_core(definition):
            found.append((cls, "custom"))
        elif expected.get(cls) and expected[cls] != comfy_contract.fingerprint(definition):
            found.append((cls, "different"))
    return found


def check_here(recipe: dict, defs: dict):
    """RecipeError(contract) si la recette ne se rejoue pas avec ces définitions."""
    wrong = not_core(recipe, defs)
    if wrong:
        cls, reason = wrong[0]
        raise RecipeError("contract", t(f"comfy_recipes.class_{reason}", node=cls),
                          node=cls, reason=reason)


# ------------------------------------------------------------------ l'affichage
def localized(texts: dict) -> str:
    lang = i18n.current()
    return str(texts.get(lang) or texts.get(i18n.SOURCE) or "")


def summary(recipe: dict) -> dict:
    """Ce qu'en voit l'interface, dans la langue de la requête (sans le graphe)."""
    return {"id": recipe["id"], "version": recipe["version"], "kind": recipe["kind"],
            "title": localized(recipe["title"]), "about": localized(recipe["about"]),
            "source": recipe["source"],
            "params": [{k: v for k, v in p.items() if k != "targets"} for p in recipe["params"]],
            "models": [{k: m.get(k) for k in ("key", "folder", "name", "sha256", "size",
                                              "license", "license_url", "url")}
                       for m in recipe["models"]],
            "vram_mib": recipe["vram_mib"], "typical_seconds": recipe["typical_seconds"],
            "digest": digest(recipe)}
