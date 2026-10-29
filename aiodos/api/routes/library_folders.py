"""Les dossiers suivis des bibliothèques (knowledge/folders.py) : leur liste, « Relire »,
« Retirer », leurs fichiers non lus ; et l'état du modèle d'embeddings (knowledge/
embed_model.py) que la bibliothèque montre quand il manque ou qu'il a été pris d'office."""
import asyncio

from fastapi import APIRouter, HTTPException, Request

from aiodos.knowledge import embed_model, folders

router = APIRouter(prefix="/api")


def _known(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.get("/library_folders")
async def list_folders(collection_id: int | None = None):
    return {"folders": await asyncio.to_thread(folders.list_folders, collection_id)}


@router.post("/library_folders/{folder_id}/rescan")
async def rescan(folder_id: int, request: Request):
    """« Relire » : les fichiers nouveaux ou modifiés, et ceux en échec ou annulés."""
    found = await asyncio.to_thread(_known, folders.scan, folder_id, folders.RETRY_ALL,
                                    folders.NORMAL)
    executor = request.app.state.executor
    if found.get("job_id") and not found.get("busy"):
        executor.wake()
        executor.publish_job(found["job_id"], force=True)
    request.app.state.bus.publish({"type": "knowledge"})
    return found


@router.delete("/library_folders/{folder_id}")
async def remove(folder_id: int, request: Request):
    removed = await asyncio.to_thread(_known, folders.remove, folder_id)
    request.app.state.executor.wake()
    request.app.state.bus.publish({"type": "knowledge"})
    request.app.state.bus.publish({"type": "jobs_reset"})
    return {"removed": removed}


@router.get("/library_folders/{folder_id}/problems")
async def problems(folder_id: int):
    return {"files": await asyncio.to_thread(_known, folders.problems, folder_id)}


@router.get("/knowledge/embed")
async def embed_status():
    return await asyncio.to_thread(embed_model.status)


@router.delete("/knowledge/embed/notice")
def dismiss_notice(request: Request):
    embed_model.dismiss()
    request.app.state.bus.publish({"type": "knowledge"})
    return {"ok": True}
