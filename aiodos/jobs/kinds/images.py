"""Travail « images » : chaque image demandée est une unité indépendante (modèle, consigne,
graine, réglages → PNG). Elle se calcule ici, ou chez une de mes machines qui a le même
modèle, aux mêmes empreintes (network/work.py), puis l'image n'est gardée qu'ici
(imaging/gallery.py).

Jamais confiée à un ami, ni calculée pour un ami (mine_only) : la consigne peut être
personnelle. Une machine qui reçoit un ordre d'image en revérifie les réglages (checked).
Le calcul suit la cadence du travail de fond (jobs/pacing.py) : il attend la fin d'une
conversation servie par cette machine.
"""
import base64
import secrets

from aiodos import settings
from aiodos.imaging import catalog, gallery, generate
from aiodos.imaging import engine as images_engine
from aiodos.jobs import pacing
from aiodos.jobs.registry import UnitSpec, register_job_kind, register_portable

MAX_COUNT = 16
MAX_PROMPT = 2000
MAX_PNG = 32 * 1024 ** 2
SIDE = (256, 2048)                # côtés : multiples de 64 entre ces bornes
MAX_PIXELS = 1536 * 1536
SEED_SPACE = 2 ** 31


def normalize(params: dict) -> dict:
    """Demande vérifiée et complétée (graine tirée au hasard, nombre de pas du modèle) ;
    ValueError avec un message pour l'utilisateur sinon."""
    model_id = str(params.get("model") or "")
    if model_id not in catalog.MODELS:
        raise ValueError("Modèle d'images inconnu.")
    spec = catalog.model(model_id)
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Décris l'image à créer.")
    if len(prompt) > MAX_PROMPT:
        raise ValueError(f"Description trop longue ({MAX_PROMPT} caractères au plus).")
    try:
        width, height = int(params.get("width") or 1024), int(params.get("height") or 1024)
        count = int(params.get("count") or 1)
        steps = int(params.get("steps") or spec["steps"])
        seed = params.get("seed")
        seed = secrets.randbelow(SEED_SPACE) if seed in (None, "") else int(seed) % SEED_SPACE
    except (TypeError, ValueError) as exc:
        raise ValueError("Taille, nombre, pas ou graine invalides.") from exc
    for side in (width, height):
        if side % 64 or not SIDE[0] <= side <= SIDE[1]:
            raise ValueError(f"Côtés en multiples de 64, de {SIDE[0]} à {SIDE[1]} pixels.")
    if width * height > MAX_PIXELS:
        raise ValueError("Image trop grande (1536 × 1536 pixels au plus).")
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"De 1 à {MAX_COUNT} images à la fois.")
    if not 1 <= steps <= 100:
        raise ValueError("De 1 à 100 pas.")
    negative = str(params.get("negative") or "").strip()[:MAX_PROMPT] if spec["negative"] else ""
    return {"model": model_id, "prompt": prompt, "negative": negative, "width": width,
            "height": height, "steps": steps, "cfg": spec["cfg"], "seed": seed,
            "count": count, "batch": secrets.token_hex(6)}


def _short(text: str, n: int = 50) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


def split_generate(params):
    common = {k: params[k] for k in ("model", "prompt", "negative", "width", "height",
                                     "steps", "cfg", "batch")}
    units = [UnitSpec("generate_image", "gpu",
                      {**common, "seed": (params["seed"] + i) % SEED_SPACE, "index": i})
             for i in range(params["count"])]
    many = f" ×{params['count']}" if params["count"] > 1 else ""
    return f"Images : « {_short(params['prompt'])} »{many}", units, {"batch": params["batch"]}


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
    qu'une demande faite ici) ; ValueError sinon."""
    params = normalize({**payload, "count": 1})
    if params["model"] != payload.get("model") or params["seed"] != int(payload["seed"]):
        raise ValueError("ordre d'image incohérent")
    return {**payload, "width": params["width"], "height": params["height"],
            "steps": params["steps"], "cfg": params["cfg"], "negative": params["negative"]}


def compute_image(payload, file, options, stop=None):
    payload = checked(payload)
    # Une unité d'ici n'a pas de signal d'arrêt : AIODOS qui s'arrête interrompt le
    # chargement ou l'image (l'unité est rendue à la file, sans compter d'essai)
    halt = stop if stop is not None else pacing.stopping()
    made = pacing.paced(lambda: generate.image(payload, halt), stop)
    return {"png": base64.b64encode(made["png"]).decode("ascii"),
            "seconds": round(made["seconds"], 2),
            "machine": str(settings.get("node_name") or settings.machine_name())[:80]}


def _decoded(payload, output) -> bytes:
    try:
        data = base64.b64decode(str(output.get("png") or ""), validate=True)
    except ValueError as exc:
        raise ValueError("image illisible") from exc
    if len(data) > MAX_PNG:
        raise ValueError("image trop lourde")
    if generate.png_size(data) != (payload["width"], payload["height"]):
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
                      machine=str(output.get("machine") or "")[:80], seconds=seconds)
    return {"image_id": row["id"], "seed": row["seed"], "machine": row["machine"]}


def finalize_images(job_id, params, units):
    done = [u for u in units if u["status"] == "done"]
    return {"images": len(done), "batch": params.get("batch"),
            "machines": sorted({u["result"].get("machine") or "?" for u in done})}


register_portable("generate_image", compute_image, assimilate_image,
                  requires=requires_image, validate=check_image, here=runnable_here,
                  mine_only=True)
register_job_kind("generate_images", split_generate, finalize_images)
