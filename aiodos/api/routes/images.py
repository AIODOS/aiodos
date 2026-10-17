"""Images : modèles (état, téléchargement), demandes, galerie (docs/generation-repartie.md).

Une demande devient un travail de la file (jobs/kinds/images.py) : ses images se calculent
ici et chez mes machines qui ont le modèle et prêtent leur puissance. Les PNG ne sortent
que par GET /api/images/{id}/file (jeton exigé, comme toute l'API locale).
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aiodos import settings
from aiodos.chat import conversations
from aiodos.imaging import catalog, download, gallery, lenders, local_copies, share
from aiodos.imaging import engine as images_engine
from aiodos.jobs import queue
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.models import URGENT

router = APIRouter(prefix="/api/images")


class DownloadIn(BaseModel):
    source: str | None = None           # une de mes machines qui a le modèle ; vide : Hugging Face


class GenerateIn(BaseModel):
    model: str
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
    models = []
    for model_id, spec in catalog.MODELS.items():
        models.append({"id": model_id, "label": spec["label"], "license": spec["license"],
                       "about": spec["about"], "steps": spec["steps"],
                       "kind": spec.get("kind", "image"), "fps": spec.get("fps"),
                       "sizes": spec["sizes"], "durations": spec.get("durations") or [],
                       "default_frames": spec.get("default_frames"),
                       "notice": spec.get("notice"),
                       "negative": spec["negative"], **catalog.state(model_id),
                       "helpers": others.get(model_id, []),
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
            "from_friends": bool(settings.get("images_from_friends"))}


@router.get("/models")
async def models():
    return await asyncio.to_thread(_models)


@router.post("/models/{model_id}/download")
def start_download(model_id: str, request: Request, body: DownloadIn | None = None):
    bus = request.app.state.bus
    try:
        return download.start(model_id, changed=lambda: bus.publish({"type": "images"}),
                              source=body.source if body else None)
    except catalog.ModelError as exc:
        raise HTTPException(404, str(exc))
    except download.DownloadError as exc:
        raise HTTPException(409, str(exc))


@router.delete("/models/download")
def cancel_download():
    download.cancel()
    return download.progress()


@router.post("/generate")
async def generate(body: GenerateIn, request: Request):
    try:
        params = kind.normalize(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    here, others = await asyncio.to_thread(lenders.can_compute, params["model"])
    if not here and not others:
        raise HTTPException(409, "Ni cette machine ni mes machines en ligne ne peuvent "
                                 "calculer ce modèle : télécharge-le d'abord.")
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
def image_file(image_id: int):
    row = gallery.get(image_id)
    if row is None or not gallery.path_of(row).is_file():
        raise HTTPException(404, "Image introuvable.")
    video = gallery.path_of(row).suffix == ".webm"
    return FileResponse(gallery.path_of(row), media_type="video/webm" if video else "image/png",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/{image_id}")
def delete_image(image_id: int, request: Request):
    if not gallery.delete(image_id):
        raise HTTPException(404, "Image introuvable.")
    request.app.state.bus.publish({"type": "images"})
    return {"ok": True}
