"""Travail « images » : chaque image ou vidéo demandée est une unité indépendante (modèle,
consigne, graine, réglages → PNG ou WebM). Elle se calcule ici, ou chez une de mes
machines qui a le même modèle, aux mêmes empreintes (network/work.py), puis n'est gardée
qu'ici (imaging/gallery.py).

Pas confiée à un ami ni calculée pour un ami (mine_only), sauf accord des deux côtés :
le propriétaire autorise ses amis à calculer ses images (réglage images_from_friends), et
l'ami leur prête sa carte graphique (lend_images_friends). La consigne peut être
personnelle : elle passe alors chez l'ami le temps du calcul, sans y être gardée. Une machine qui reçoit un ordre d'image en revérifie les réglages (checked).
Le calcul suit la cadence du travail de fond (jobs/pacing.py) : il attend la fin d'une
conversation servie par cette machine.
"""
import base64
import secrets
from pathlib import Path

from aiodos import settings
from aiodos.imaging import catalog, gallery, generate
from aiodos.imaging import engine as images_engine
from aiodos.jobs import pacing
from aiodos.jobs.registry import UnitSpec, register_job_kind, register_portable

MAX_COUNT = 16
MAX_PROMPT = 2000
MAX_PNG = 32 * 1024 ** 2
MAX_WEBM = 512 * 1024 ** 2
SEED_SPACE = 2 ** 31              # tailles, durées : règles de chaque modèle (catalog.py)
STRENGTH = 0.75                   # retouche : part de l'image de départ redessinée (sd.cpp)


def normalize(params: dict) -> dict:
    """Demande vérifiée et complétée (graine tirée au hasard, nombre de pas du modèle) ;
    ValueError avec un message pour l'utilisateur sinon."""
    model_id = str(params.get("model") or "")
    if model_id not in catalog.MODELS:
        raise ValueError("Modèle d'images inconnu.")
    spec = catalog.model(model_id)
    video = catalog.is_video(model_id)
    what = "vidéo" if video else "image"
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError(f"Décris la {what} à créer.")
    if len(prompt) > MAX_PROMPT:
        raise ValueError(f"Description trop longue ({MAX_PROMPT} caractères au plus).")
    default_w, default_h = spec["sizes"][0][:2]
    try:
        width = int(params.get("width") or default_w)
        height = int(params.get("height") or default_h)
        count = int(params.get("count") or 1)
        steps = int(params.get("steps") or spec["steps"])
        frames = int(params.get("frames") or spec["default_frames"]) if video else None
        seed = params.get("seed")
        seed = secrets.randbelow(SEED_SPACE) if seed in (None, "") else int(seed) % SEED_SPACE
    except (TypeError, ValueError) as exc:
        raise ValueError("Taille, nombre, durée, pas ou graine invalides.") from exc
    multiple, (low, high) = spec["multiple"], spec["side"]
    for side in (width, height):
        if side % multiple or not low <= side <= high:
            raise ValueError(f"Côtés en multiples de {multiple}, de {low} à {high} pixels.")
    if width * height > spec["max_pixels"]:
        raise ValueError(f"{what.capitalize()} trop grande pour ce modèle.")
    if video:
        step, base = spec["frame_step"], spec["frame_base"]
        if not base <= frames <= spec["max_frames"] or (frames - base) % step:
            raise ValueError(f"Durée : de {base} à {spec['max_frames']} images, en "
                             f"{step} × k + {base}.")
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"De 1 à {MAX_COUNT} {what}s à la fois.")
    if not 1 <= steps <= 100:
        raise ValueError("De 1 à 100 pas.")
    negative = str(params.get("negative") or "").strip()[:MAX_PROMPT] if spec["negative"] else ""
    found = {"model": model_id, "prompt": prompt, "negative": negative, "width": width,
             "height": height, "steps": steps, "cfg": spec["cfg"], "seed": seed,
             "count": count, "batch": secrets.token_hex(6)}
    if video:
        found["frames"] = frames
    if params.get("init_path"):       # retouche : l'image de départ donne la taille
        found.update(_start_from(params["init_path"], params.get("strength"), video, spec))
    return found


def _start_from(path, strength, video, spec) -> dict:
    """Une image de départ (retouche) : un PNG de cette machine, au format du modèle, et la
    force de la retouche ; ValueError sinon."""
    if video:
        raise ValueError("Une vidéo ne part pas d'une image.")
    try:
        data = Path(str(path)).read_bytes()
        width, height = generate.png_size(data)
    except (OSError, ValueError) as exc:
        raise ValueError("Image de départ illisible.") from exc
    if len(data) > MAX_PNG or width % spec["multiple"] or height % spec["multiple"] \
            or width * height > spec["max_pixels"]:
        raise ValueError("Image de départ d'un format que ce modèle ne prend pas.")
    try:
        strength = float(STRENGTH if strength in (None, "") else strength)
    except (TypeError, ValueError) as exc:
        raise ValueError("Force de retouche invalide.") from exc
    if not 0.05 <= strength <= 1.0:
        raise ValueError("Force de retouche : de 0,05 à 1.")
    return {"init_path": str(path), "strength": strength, "width": width, "height": height}


def _short(text: str, n: int = 50) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


def split_generate(params):
    common = {k: params[k] for k in ("model", "prompt", "negative", "width", "height",
                                     "steps", "cfg", "batch", "frames", "init_path",
                                     "strength") if k in params}
    units = [UnitSpec("generate_image", "gpu",
                      {**common, "seed": (params["seed"] + i) % SEED_SPACE, "index": i})
             for i in range(params["count"])]
    many = f" ×{params['count']}" if params["count"] > 1 else ""
    what = "Vidéos" if catalog.is_video(params["model"]) else "Images"
    return f"{what} : « {_short(params['prompt'])} »{many}", units, {"batch": params["batch"]}


def runnable_here(model) -> bool:
    """Sans stable-diffusion.cpp ou sans le modèle complet ici, l'image attend en file
    qu'une de mes machines qui les a la demande."""
    return images_engine.folder() is not None and catalog.ready(str(model or ""))


def requires_image(unit, options) -> dict:
    """Ce qu'il faut à une machine : stable-diffusion.cpp, et les fichiers du modèle aux
    mêmes empreintes."""
    model_id = unit["payload"]["model"]
    return {"images": {"model": model_id, "files": catalog.hashes(model_id)}}


def checked(payload) -> dict:
    """Les réglages d'une image reçue en ordre de calcul, revérifiés ici (mêmes bornes
    qu'une demande faite ici) ; ValueError sinon. L'image de départ d'une retouche arrive à
    part (le fichier de l'unité) : sa force est revérifiée ici."""
    params = normalize({k: v for k, v in payload.items() if k != "init_path"} | {"count": 1})
    if payload.get("strength") is not None:
        strength = float(payload["strength"])
        if not 0.05 <= strength <= 1.0:
            raise ValueError("ordre d'image incohérent")
        params["strength"] = strength
    if params["model"] != payload.get("model") or params["seed"] != int(payload["seed"]):
        raise ValueError("ordre d'image incohérent")
    found = {**payload, "width": params["width"], "height": params["height"],
             "steps": params["steps"], "cfg": params["cfg"], "negative": params["negative"]}
    if "strength" in params:
        found["strength"] = params["strength"]
    if "frames" in params:
        found["frames"] = params["frames"]
    return found


def compute_image(payload, file, options, stop=None):
    payload = checked(payload)
    # Une unité d'ici n'a pas de signal d'arrêt : AIODOS qui s'arrête interrompt le
    # chargement ou l'image (l'unité est rendue à la file, sans compter d'essai)
    halt = stop if stop is not None else pacing.stopping()
    machine = str(settings.get("node_name") or settings.machine_name())[:80]
    if catalog.is_video(payload["model"]):
        made = pacing.paced(lambda: generate.video(payload, halt), stop)
        return {"webm": base64.b64encode(made["webm"]).decode("ascii"),
                "frames": made["frames"], "fps": made["fps"],
                "seconds": round(made["seconds"], 2), "machine": machine}
    if file:                          # retouche : l'image de départ (d'ici, ou reçue)
        start = Path(file).read_bytes()
        if generate.png_size(start) != (payload["width"], payload["height"]):
            raise ValueError("image de départ d'une autre taille que celle demandée")
        payload = {**payload, "init_png": start}
    made = pacing.paced(lambda: generate.image(payload, halt), stop)
    return {"png": base64.b64encode(made["png"]).decode("ascii"),
            "seconds": round(made["seconds"], 2), "machine": machine}


def _decoded(payload, output) -> bytes:
    """Le PNG ou le WebM d'une sortie, contrôlé (signature, poids, taille d'une image)."""
    video = catalog.is_video(payload["model"])
    what = "vidéo" if video else "image"
    try:
        data = base64.b64decode(str(output.get("webm" if video else "png") or ""),
                                validate=True)
    except ValueError as exc:
        raise ValueError(f"{what} illisible") from exc
    if len(data) > (MAX_WEBM if video else MAX_PNG):
        raise ValueError(f"{what} trop lourde")
    if video:
        if not generate.is_webm(data):
            raise ValueError("ce n'est pas une vidéo WebM")
    elif generate.png_size(data) != (payload["width"], payload["height"]):
        raise ValueError("image d'une autre taille que celle demandée")
    return data


def check_image(payload, output):
    """Contrôle d'une image calculée ailleurs, avant de la garder."""
    _decoded(payload, output)


def assimilate_image(payload, output):
    try:
        seconds = float(output.get("seconds"))
    except (TypeError, ValueError):
        seconds = None
    row = gallery.add(payload, _decoded(payload, output),
                      machine=str(output.get("machine") or "")[:80], seconds=seconds,
                      frames=_small_int(output.get("frames")), fps=_small_int(output.get("fps")))
    return {"image_id": row["id"], "seed": row["seed"], "machine": row["machine"]}


def _small_int(value) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 0 < value < 100000 else None


def finalize_images(job_id, params, units):
    done = [u for u in units if u["status"] == "done"]
    return {"images": len(done), "batch": params.get("batch"),
            "machines": sorted({u["result"].get("machine") or "?" for u in done})}


register_portable("generate_image", compute_image, assimilate_image, file_key="init_path",
                  requires=requires_image, validate=check_image, here=runnable_here,
                  mine_only=True, friends_out="images_from_friends",
                  friends_in="lend_images_friends",
                  shared_keys=("model", "prompt", "negative", "width", "height", "steps",
                               "cfg", "frames", "seed", "strength"))
register_job_kind("generate_images", split_generate, finalize_images)
