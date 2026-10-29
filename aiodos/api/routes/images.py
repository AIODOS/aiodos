"""Images : modèles (état, téléchargement), demandes, galerie (docs/generation-repartie.md).

Une demande devient un travail de la file (jobs/kinds/images.py) : ses images se calculent
ici et chez mes machines qui ont le modèle et prêtent leur puissance. Les PNG ne sortent
que par GET /api/images/{id}/file (jeton exigé, comme toute l'API locale) ; ?size=… : une
vignette (imaging/thumbs.py), pour la conversation et la galerie.
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aiodos import settings
from aiodos.chat import conversations, public_creations
from aiodos.i18n import t
from aiodos.imaging import (catalog, download, gallery, lenders, local_copies, ranking, share,
                            thumbs)
from aiodos.imaging import engine as images_engine
from aiodos.jobs import queue
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.models import URGENT

router = APIRouter(prefix="/api/images")


class DownloadIn(BaseModel):
    source: str | None = None           # une de mes machines qui a le modèle ; vide : Hugging Face


class GenerateIn(BaseModel):
    model: str                          # vide ou « auto » : « Automatique » (imaging/ranking.py)
    prompt: str
    negative: str = ""
    width: int | None = None            # vide : 1024 × 1024 (images), 832 × 480 (vidéos)
    height: int | None = None
    count: int = 1
    seed: int | None = None
    steps: int | None = None
    frames: int | None = None           # vidéos : nombre d'images (4n + 1), 33 par défaut


def _models() -> dict:
    found = images_engine.info()
    others = lenders.helpers()
    offered = lenders.helpers(friends=True)              # amis prêts à calculer, si je l'accepte
    public = public_creations.offerable()                 # le réseau public, pour une conversation
    vram = ranking.vram_here()
    models = []
    for model_id, spec in catalog.MODELS.items():
        # Description, licence, formats et durées : dans la langue de la requête
        models.append({"id": model_id, "label": catalog.label(model_id), "license": spec["license"],
                       "about": catalog.about(model_id), "steps": spec["steps"],
                       "kind": spec.get("kind", "image"), "fps": spec.get("fps"),
                       "sizes": catalog.sizes(model_id), "durations": catalog.durations(model_id),
                       "default_frames": spec.get("default_frames"),
                       "notice": catalog.notice(model_id),
                       "lend_notice": catalog.lend_notice(model_id),
                       # Sa puissance (1 : la plus grande de son genre) et la mémoire de
                       # carte graphique qu'il demande (Mio), celle de ce PC suffit-elle
                       "rank": catalog.rank(model_id), "vram": catalog.vram(model_id),
                       "fits_here": ranking.fits_here(model_id, vram),
                       "negative": spec["negative"], **catalog.state(model_id),
                       "helpers": others.get(model_id, []),
                       "public": model_id in public,
                       "friends": [name for name in offered.get(model_id, [])
                                   if name not in others.get(model_id, [])],
                       "sources": share.sources(model_id),
                       "local": local_copies.available(model_id)})   # déjà sur ce PC
    return {"engine": {"found": found is not None,
                       "version": found["version"] if found else None,
                       "device": (images_engine.best_device(images_engine.devices())
                                  or {}).get("label") if found else None,
                       "loaded": images_engine.loaded()},
            "models": models, "download": download.progress(),
            "from_friends": bool(settings.get("images_from_friends")),
            "vram": vram, "lend_warnings": lend_warnings(found is not None)}


def lend_warnings(engine: bool = True) -> list:
    """[{"model", "label", "text"}] : les modèles de ce PC dont la licence encadre le prêt
    (catalog.lend_notice : MiniMax H3), quand il les prête à des tiers, amis
    (lend_images_friends) ou inconnus (lend_images_public) : l'avertissement à montrer à qui
    prête, dans la langue de la requête."""
    lending = settings.get("lend_images_public") \
        or (settings.get("lend_images_friends") and settings.get("share_enabled"))
    if not engine or not lending:
        return []
    return [{"model": model_id, "label": catalog.label(model_id),
             "text": catalog.lend_notice(model_id)}
            for model_id in catalog.MODELS
            if catalog.MODELS[model_id].get("lend_notice") and catalog.ready(model_id)]


@router.get("/models")
async def models():
    return await asyncio.to_thread(_models)


@router.post("/models/{model_id}/download")
def start_download(model_id: str, request: Request, body: DownloadIn | None = None):
    bus = request.app.state.bus
    try:
        started = download.start(model_id, changed=lambda: bus.publish({"type": "images"}),
                                 source=body.source if body else None)
        catalog.accept(model_id)     # sa licence, lue et acceptée avant (settings-models.js)
        return started
    except catalog.ModelError as exc:
        raise HTTPException(404, str(exc))
    except download.DownloadError as exc:
        raise HTTPException(409, str(exc))


@router.post("/models/{model_id}/license")
def accept_license(model_id: str):
    """La licence restrictive d'un modèle, lue et acceptée dans le menu des modèles avant de le
    faire calculer par une autre machine (model-menu.js) : « Automatique » peut ensuite le
    prendre chez un ami ou un inconnu (imaging/ranking.py)."""
    try:
        catalog.accept(model_id)
    except catalog.ModelError as exc:
        raise HTTPException(404, str(exc))
    return {"model": model_id, "accepted": True}


@router.delete("/models/download")
def cancel_download():
    download.cancel()
    return download.progress()


@router.post("/generate")
async def generate(body: GenerateIn, request: Request):
    asked = body.model_dump()
    if asked["model"] in ("", ranking.AUTO):
        # « Automatique » : le plus puissant que ce PC, mes machines ou mes amis calculent
        # (ni ComfyUI, ni le réseau public : cette API ne crée qu'avec stable-diffusion.cpp
        # ici et chez mes machines) ; une vidéo si elle dit son nombre d'images
        media = "video" if body.frames else "image"
        choice = await asyncio.to_thread(ranking.choose, media, None, comfy=False,
                                         public=False)
        if choice.model is None:
            raise HTTPException(409, t("imaging.nobody_can_compute"))
        asked["model"] = choice.model
    try:
        params = kind.normalize(asked)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    here, others = await asyncio.to_thread(lenders.can_compute, params["model"])
    if not here and not others:
        raise HTTPException(409, t("imaging.nobody_can_compute"))
    executor = request.app.state.executor
    job_id = await asyncio.to_thread(queue.create_job, "generate_images", params, URGENT)
    executor.wake()
    executor.publish_job(job_id, force=True)
    return {"job_id": job_id, "batch": params["batch"], "seed": params["seed"],
            "here": here}


@router.get("")
async def list_images(limit: int = 60, before: int | None = None, batch: str | None = None):
    """La galerie, les plus récentes d'abord ; `batch` : les images d'une demande. Chacune
    dit où est son fichier, et la conversation où elle a été demandée, s'il y en a une."""
    def listed():
        rows = gallery.batch(batch) if batch else gallery.list_images(limit, before)
        talks = conversations.by_batch(row["batch"] for row in rows)
        return [dict(row, path=str(gallery.path_of(row)),
                     conversation_id=talks.get(row["batch"])) for row in rows]
    return await asyncio.to_thread(listed)


@router.get("/{image_id}/file")
def image_file(image_id: int, size: int | None = None):
    row = gallery.get(image_id)
    if row is None or not gallery.path_of(row).is_file():
        raise HTTPException(404, t("imaging.image_not_found"))
    cache = {"Cache-Control": "private, max-age=86400"}
    small = thumbs.thumbnail(row, size) if size and size > 0 else None
    if small is not None:
        return FileResponse(small, media_type="image/webp", headers=cache)
    # Le genre d'après l'extension que la galerie a donnée (une création de ComfyUI peut être
    # un MP4, un WebP…)
    kind = gallery.MEDIA_TYPES.get(gallery.path_of(row).suffix.lower(), "image/png")
    return FileResponse(gallery.path_of(row), media_type=kind, headers=cache)


@router.delete("/{image_id}")
def delete_image(image_id: int, request: Request):
    if not gallery.delete(image_id):
        raise HTTPException(404, t("imaging.image_not_found"))
    thumbs.remove(image_id)
    request.app.state.bus.publish({"type": "images"})
    return {"ok": True}
