"""Audio : modèles (état, téléchargement, licence) pour Réglages → Modèles et le menu des
modèles (docs/conception/audio.md). Les créations passent par la conversation
(POST /api/chat/create, chat/audio_creations.py) ; leurs WAV sont dans la galerie, servis par
GET /api/images/{id}/file comme les images et les vidéos."""
import asyncio

from fastapi import APIRouter, HTTPException, Request

from aiodos import settings
from aiodos.audio import catalog, download, files, lenders, ranking
from aiodos.audio import engine as audio_engine

router = APIRouter(prefix="/api/audio")


def _models() -> dict:
    found = audio_engine.info()
    others = lenders.helpers()
    offered = lenders.helpers(friends=True)
    from aiodos.imaging.ranking import vram_here
    vram = vram_here()
    language = ranking.language_of()
    models = []
    for genre in catalog.GENRES:
        for model_id in ranking.candidates(genre):
            spec = catalog.model(model_id)
            # Description, licence, voix et durées : dans la langue de la requête
            models.append({"id": model_id, "label": catalog.label(model_id), "genre": genre,
                           "kind": "audio", "license": spec["license"],
                           "about": catalog.about(model_id), "notice": catalog.notice(model_id),
                           "rank": catalog.rank(model_id), "vram": catalog.vram(model_id),
                           "fits_here": ranking.fits_here(model_id, vram),
                           "languages": catalog.languages(model_id),
                           "voices": catalog.voices(model_id, language),
                           "seconds": catalog.seconds(model_id),
                           "default_seconds": spec.get("default_seconds"),
                           "lyrics": spec.get("lyrics"),
                           "experimental": bool(spec.get("experimental")),
                           **files.state(model_id),
                           "helpers": others.get(model_id, []),
                           "friends": [name for name in offered.get(model_id, [])
                                       if name not in others.get(model_id, [])]})
    best = audio_engine.best_device(audio_engine.devices()) if found else None
    return {"engine": {"found": found is not None,
                       "version": found["version"] if found else None,
                       "device": best["label"] if best else None},
            "models": models, "download": download.progress(),
            "from_friends": bool(settings.get("images_from_friends")), "vram": vram}


@router.get("/models")
async def models():
    return await asyncio.to_thread(_models)


@router.post("/models/{model_id}/download")
def start_download(model_id: str, request: Request):
    bus = request.app.state.bus
    try:
        started = download.start(model_id, changed=lambda: bus.publish({"type": "images"}))
        catalog.accept(model_id)     # sa licence, lue et acceptée avant (settings-models.js)
        return started
    except catalog.ModelError as exc:
        raise HTTPException(404, str(exc))
    except download.DownloadError as exc:
        raise HTTPException(409, str(exc))


@router.post("/models/{model_id}/license")
def accept_license(model_id: str):
    """La licence restrictive d'un modèle, lue et acceptée dans le menu des modèles avant de
    le faire calculer par une autre machine : « Automatique » peut ensuite le prendre chez un
    ami (audio/ranking.py)."""
    try:
        catalog.accept(model_id)
    except catalog.ModelError as exc:
        raise HTTPException(404, str(exc))
    return {"model": model_id, "accepted": True}


@router.delete("/models/download")
def cancel_download():
    download.cancel()
    return download.progress()
