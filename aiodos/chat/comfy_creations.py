"""Créer avec ComfyUI dans la conversation (docs/conception/comfyui-createur.md § 3.2) : les
recettes du catalogue (engines/recipes.py) et mes workflows importés (engines/comfy_workflows.py)
que ce PC ou une de mes machines peut calculer en ce moment sont proposés dans le mode « Créer »
comme des modèles, sous un identifiant « comfyui/<recette> » ou « comfyui/workflow/<numéro> »
(celui que la galerie garde, jobs/kinds/comfy_create.py). Leurs formats et leurs durées viennent
des bornes de leurs paramètres (largeur, hauteur, nombre d'images).

Une création demandée ainsi devient un travail « comfy_create », comme depuis la vue Images et
vidéos : calculé ici ou chez mes machines seulement (jamais un ami ni un inconnu : le réseau
public ne prendra que les recettes signées, plus tard). La description du message est celle de
la recette (son paramètre « prompt ») ; une image jointe ne part pas dans une recette.
"""
import math

from aiodos import i18n
from aiodos.engines import catalogue, comfy_workflows, recipes
from aiodos.i18n import t

PREFIX = "comfyui/"
WORKFLOW = "comfyui/workflow/"
# Durées proposées d'une vidéo, en secondes (comme celles du catalogue de sd.cpp : 1, 2, 3, 5 s)
SECONDS = (1, 2, 3, 5)
# Un format carré par défaut : aussi un portrait et un paysage de même surface (9:7, comme
# les formats habituels de SDXL : 896 × 1152, 1152 × 896)
RATIO = 9 / 7


class Unavailable(Exception):
    """Ni ce PC ni mes machines ne peuvent calculer cette recette (ou elle n'existe plus) :
    status 404 (disparue) ou 409 (nulle part)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def is_comfy(model_id) -> bool:
    return isinstance(model_id, str) and model_id.startswith(PREFIX)


def _body(model_id: str) -> dict:
    if model_id.startswith(WORKFLOW):
        return {"workflow": model_id[len(WORKFLOW):]}
    return {"recipe": model_id[len(PREFIX):]}


def _owner(model_id: str) -> dict:
    """La recette ou le workflow de cet identifiant ; Unavailable (404) s'il n'existe plus."""
    body = _body(model_id)
    try:
        if "workflow" in body:
            return comfy_workflows.get(body["workflow"])
        return catalogue.recipe(body["recipe"], public=False)
    except (catalogue.CatalogueError, comfy_workflows.WorkflowError) as exc:
        raise Unavailable(t("chat_comfy.gone"), 404) from exc


def _owners():
    """(identifiant, recette ou workflow) : les recettes du catalogue, puis mes workflows."""
    for recipe in catalogue.read().recipes.values():
        yield f"{PREFIX}{recipe['id']}", recipe
    for listed in comfy_workflows.listing():
        try:
            yield f"{WORKFLOW}{listed['id']}", comfy_workflows.get(listed["id"])
        except comfy_workflows.WorkflowError:
            continue


def _name(owner: dict) -> str:
    if "title" in owner:                      # une recette : son titre dans ma langue
        return recipes.localized(owner["title"])
    return str(owner.get("name") or "")


def label(owner: dict) -> str:
    return t("chat_comfy.label", name=_name(owner))


# ------------------------------------------------------------ formats et durées
def _bounded(spec: dict, value: float) -> int:
    """La valeur entière la plus proche dans les bornes du paramètre, sur son pas."""
    low, high, step = spec["min"], spec["max"], spec.get("step") or 1
    top = low + (high - low) // step * step
    return int(min(max(low + round((value - low) / step) * step, low), top))


def _numeric(owner: dict, name: str) -> dict | None:
    spec = recipes.param(owner, name)
    return spec if spec is not None and spec["type"] == "int" else None


def sizes(owner: dict) -> list:
    """[[largeur, hauteur, libellé]] : le format par défaut de la recette, puis son inverse
    (ou, pour un carré, un portrait et un paysage de même surface) et un carré de même
    surface ; [] si elle n'a pas de largeur et de hauteur réglables."""
    w_spec, h_spec = _numeric(owner, "width"), _numeric(owner, "height")
    if w_spec is None or h_spec is None:
        return []
    width, height = w_spec["default"], h_spec["default"]
    side = math.sqrt(width * height)
    wanted = [(width, height)]
    if width == height:
        wide, narrow = side * math.sqrt(RATIO), side / math.sqrt(RATIO)
        wanted += [(narrow, wide), (wide, narrow)]
    else:
        wanted += [(height, width), (side, side)]
    found = []
    for w, h in wanted:
        pair = (_bounded(w_spec, w), _bounded(h_spec, h))
        if pair in [tuple(f[:2]) for f in found]:
            continue
        shape = "square" if pair[0] == pair[1] else "portrait" if pair[1] > pair[0] else "landscape"
        # imaging_models.size_square, size_portrait, size_landscape
        found.append([*pair, t(f"imaging_models.size_{shape}", width=pair[0], height=pair[1])])
    return found


def _fps(owner: dict) -> float:
    spec = recipes.param(owner, "fps")
    return float(spec["default"]) if spec is not None and spec["type"] in ("int", "float") else 16.0


def durations(owner: dict) -> list:
    """[[nombre d'images, libellé]] d'une vidéo : environ 1, 2, 3 et 5 s, et la durée par
    défaut de la recette ; [] sans nombre d'images réglable."""
    spec = _numeric(owner, "frames")
    if spec is None:
        return []
    fps = _fps(owner)
    counts = sorted({_bounded(spec, s * fps + 1) for s in SECONDS} | {int(spec["default"])})
    out = []
    for n in counts:
        seconds = max(n - 1, 1) / fps
        digits = 0 if float(round(seconds, 1)).is_integer() else 1
        out.append([n, t("format.seconds", n=i18n.number(round(seconds, digits), digits))])
    return out


def _frames_for(owner: dict, seconds) -> int | None:
    found = durations(owner)
    if not seconds or not found:
        return None
    fps = _fps(owner)
    return min((n for n, _ in found), key=lambda n: abs((n - 1) / fps - float(seconds)))


def _size_for(owner: dict, shape) -> tuple | None:
    test = {"portrait": lambda w, h: h > w, "landscape": lambda w, h: w > h,
            "square": lambda w, h: w == h}.get(shape or "")
    if test is None:
        return None
    return next(((w, h) for w, h, _ in sizes(owner) if test(w, h)), None)


# ---------------------------------------------------------------- les choix
def _where(model_id: str, inventory) -> dict | None:
    """{"here", "machines"} pour cette recette à ses réglages par défaut ; None si ComfyUI ne
    peut pas la calculer (un nœud absent du ComfyUI de ce PC…)."""
    from aiodos.jobs.kinds import comfy_create
    try:
        params = comfy_create.normalize(_body(model_id), inventory)
    except comfy_create.CreateError:
        return None
    return comfy_create.places(params, inventory)


def choices(media_kind: str | None = None) -> list:
    """Les recettes et mes workflows que ce PC ou une de mes machines peut calculer en ce
    moment, comme des modèles du mode « Créer » (les champs de GET /api/images/models que
    l'interface lit, plus « engine » et « here »)."""
    from aiodos.jobs.kinds import comfy_create
    inventory = comfy_create.inventory_here()
    out = []
    for model_id, owner in _owners():
        kind = owner.get("kind", "image")
        if media_kind and kind != media_kind:
            continue
        where = _where(model_id, inventory)
        if where is None or not (where["here"] or where["machines"]):
            continue
        frames = _numeric(owner, "frames")
        out.append({"id": model_id, "label": label(owner), "kind": kind, "engine": "comfyui",
                    "here": bool(where["here"]), "helpers": where["machines"],
                    "state": "ready" if where["here"] else "absent", "public": False,
                    "sizes": sizes(owner), "durations": durations(owner) if kind == "video" else [],
                    "default_frames": frames["default"] if frames and kind == "video" else None})
    return out


def first(media_kind: str) -> dict | None:
    """La première recette (ou le premier workflow) de ce genre qui peut se calculer : son
    choix (id, label…), ou None."""
    found = choices(media_kind)
    return found[0] if found else None


def ready(model_id: str) -> str:
    """Le libellé de cette recette si ce PC ou une de mes machines peut la calculer en ce
    moment ; Unavailable sinon (404 : elle n'existe plus)."""
    from aiodos.jobs.kinds import comfy_create
    owner = _owner(model_id)
    where = _where(model_id, comfy_create.inventory_here())
    if where is None or not (where["here"] or where["machines"]):
        raise Unavailable(t("chat_comfy.nowhere", name=_name(owner)))
    return label(owner)


# ---------------------------------------------------------------- la demande
def prepare(model_id: str, request: dict, found: dict, prompt: str) -> dict:
    """Les paramètres du travail comfy_create d'une création de la conversation : la
    description (déjà enrichie), le format choisi ou suggéré par la phrase, la durée, le nombre
    et la graine, revérifiés contre les bornes de la recette (jobs/kinds/comfy_create.py).
    ValueError si la demande ne convient pas à la recette ; Unavailable si rien ne peut la
    calculer. params["creation"] porte le libellé montré dans la conversation."""
    from aiodos.jobs.kinds import comfy_create
    owner = _owner(model_id)
    names = {p["name"] for p in owner["params"]}
    if "prompt" not in names:
        raise ValueError(t("chat_comfy.no_prompt", name=_name(owner)))
    limit = recipes.param(owner, "prompt").get("max_length", recipes.MAX_TEXT)
    given = {"prompt": str(prompt or "")[:limit]}
    if request.get("negative") and "negative" in names:
        given["negative"] = str(request["negative"])
    chosen = (request["width"], request["height"]) \
        if request.get("width") and request.get("height") else None
    size = _size_for(owner, request.get("shape") or (None if chosen else found.get("shape"))) \
        or chosen
    if size and {"width", "height"} <= names:
        given["width"], given["height"] = size
    if owner.get("kind") == "video" and "frames" in names:
        frames = request.get("frames") or _frames_for(owner, request.get("seconds")
                                                      or found.get("seconds"))
        if frames:
            given["frames"] = frames
    if request.get("seed") is not None and "seed" in names:
        given["seed"] = request["seed"]
    body = {**_body(model_id), "params": given,
            "count": request.get("count") or found.get("count") or 1}
    inventory = comfy_create.inventory_here()
    try:
        params = comfy_create.normalize(body, inventory)
        where = comfy_create.places(params, inventory)
    except comfy_create.CreateError as exc:
        if exc.status == 404:
            raise Unavailable(str(exc), 404) from exc
        raise ValueError(str(exc)) from exc
    if not where["here"] and not where["machines"]:
        from aiodos import settings
        raise Unavailable(t("comfy_create.nowhere" if settings.get("engines_comfyui_enabled")
                            else "comfy_create.not_granted"))
    params["creation"]["label"] = label(owner)
    return params


def media(params: dict, job_id: int) -> dict:
    """La création telle que la réponse de l'assistant la garde (colonne media), comme celles
    de sd.cpp (chat/creations.accept), avec « engine »."""
    creation = params["creation"]
    out = {"kind": creation["kind"], "model": creation["model"],
           "label": creation.get("label") or creation["name"],
           "prompt": creation.get("prompt") or "", "count": creation["count"],
           "batch": creation["batch"], "job_id": job_id, "width": creation.get("width"),
           "height": creation.get("height"), "seed": creation["seed"], "engine": "comfyui"}
    if creation.get("frames"):
        out["frames"] = creation["frames"]
    return out
