"""Réglages, flux d'événements et ouverture de liens externes."""
import webbrowser

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from coeur import settings
from coeur.jobs.kinds import reindex

router = APIRouter(prefix="/api")


class UrlIn(BaseModel):
    url: str


@router.get("/settings")
def read_settings():
    return settings.public_view()


@router.put("/settings")
def write_settings(values: dict, request: Request):
    settings.set_many(values)
    request.app.state.bus.publish({"type": "settings"})
    job_id = reindex.schedule()            # nouveau modèle d'embedding : tout recalculer
    if job_id:
        executor = request.app.state.executor
        executor.wake()
        executor.publish_job(job_id, force=True)
    return settings.public_view()


@router.get("/events")
async def events(request: Request):
    return StreamingResponse(request.app.state.bus.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.post("/open_url")
def open_url(body: UrlIn):
    """Ouvre un lien dans le navigateur par défaut (pas dans la fenêtre de l'app)."""
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "Seuls les liens http(s) sont ouverts.")
    webbrowser.open(body.url)
    return {"ok": True}
