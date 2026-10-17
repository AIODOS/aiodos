"""Images : modèles (état, téléchargement), demandes, galerie (docs/generation-repartie.md).

Une demande devient un travail de la file (jobs/kinds/images.py) : ses images se calculent
ici et chez mes machines qui ont le modèle et prêtent leur puissance. Les PNG ne sortent
que par GET /api/images/{id}/file (jeton exigé, comme toute l'API locale).
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aiodos.imaging import catalog, download, gallery, local_copies, share
from aiodos.imaging import engine as images_engine
from aiodos.jobs import queue
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.models import URGENT
from aiodos.network import peers, sharing

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


def helpers() -> dict:
    """{modèle : [mes machines en ligne qui l'ont et prêtent leur puissance]}."""
    found = {}
    for peer in peers.list_peers():
        if not peer["online"] or sharing.level(peer["node_id"]) != sharing.MINE:
            continue
        images = (peer.get("profile") or {}).get("images") or {}
        if not images.get("engine") or not images.get("shares"):
            continue
        for model_id in images.get("models") or []:
            found.setdefault(model_id, []).append(peer["name"])
    return found


def _models() -> dict:
    found = images_engine.info()
    others = helpers()
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
                       "sources": share.sources(model_id),
                       "local": local_copies.available(model_id)})   # déjà sur ce PC
    return {"engine": {"found": found is not None,
                       "version": found["version"] if found else None,
                       "device": (images_engine.best_device(images_engine.devices())
                                  or {}).get("label") if found else None,
                       "loaded": images_engine.loaded()},
            "models": models, "download": download.progress()}


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
    here = await asyncio.to_thread(lambda: images_engine.folder() is not None
                                   and catalog.ready(params["model"]))
    if not here and not (await asyncio.to_thread(helpers)).get(params["model"]):
        raise HTTPException(409, "Ni cette machine ni mes machines en ligne ne peuvent "
                                 "calculer ce modèle : télécharge-le d'abord.")
    executor = request.app.state.executor
    job_id = await asyncio.to_thread(queue.create_job, "generate_images", params, URGENT)
    executor.wake()
    executor.publish_job(job_id, force=True)
    return {"job_id": job_id, "batch": params["batch"], "seed": params["seed"],
            "here": here}


@router.get("")
async def list_images(limit: int = 60, before: int | None = None):
    return await asyncio.to_thread(gallery.list_images, limit, before)


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
