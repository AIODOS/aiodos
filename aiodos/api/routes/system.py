"""Réglages, flux d'événements, ouverture de liens externes, fenêtre à montrer."""
import asyncio
import secrets
import webbrowser

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from aiodos import instance, settings
from aiodos.api.routes import engines
from aiodos.jobs.kinds import reindex

router = APIRouter(prefix="/api")
# Hors de /api : protégé par sa propre clé (cf. instance.wake_running), pas par le jeton
window_router = APIRouter()

# Options avec lesquelles le compagnon Internet se lance : changées, il repart avec
COMPANION_OPTIONS = {"internet_relays", "internet_directory", "internet_portmap"}
# Ouverture aux autres outils : la passerelle s'ouvre, se ferme ou change de port
GATEWAY_OPTIONS = {"gateway_enabled", "gateway_port", "gateway_apps"}


class UrlIn(BaseModel):
    url: str


@router.get("/settings")
def read_settings():
    return settings.public_view()


@router.put("/settings")
def write_settings(values: dict, request: Request):
    settings.set_many(values)
    request.app.state.bus.publish({"type": "settings"})
    if values.get("auto_update"):          # réactivées : on regarde tout de suite
        request.app.state.updates.check_now()
    if COMPANION_OPTIONS & set(values):
        request.app.state.network.internet.relaunch_from_thread()
    if GATEWAY_OPTIONS & set(values):
        request.app.state.gateway.apply_from_thread()
    if engines.ENGINE_OPTIONS & set(values):     # ComfyUI accordé, déplacé : relu
        engines.forget()
        request.app.state.executor.wake()
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


def _check_show_key(request: Request):
    expected = getattr(request.app.state, "show_key", None)
    given = request.headers.get("x-aiodos-show", "")
    # En octets : un en-tête non ASCII doit être refusé, pas faire échouer la comparaison
    if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(403, "Clé refusée.")


@window_router.post("/instance/show", include_in_schema=False)
def show_window(request: Request):
    """AIODOS relancé alors qu'il tourne déjà (fenêtre cachée dans la zone de
    notification…) : la fenêtre de cette instance revient (cf. instance.wake_running).
    La clé, propre à cette demande, ne donne accès à rien d'autre."""
    _check_show_key(request)
    return {"shown": instance.show_window()}


@window_router.post("/instance/app-request", include_in_schema=False)
async def app_request(request: Request):
    """La demande d'accès d'une appli, transmise par le programme d'aide (« python -m aiodos
    --app-request ») quand la passerelle est fermée : même clé show_key que ci-dessus, que
    seul ce compte lit (gateway/app_connect.py). Elle n'accorde rien : la carte d'accord
    s'affiche, et c'est l'utilisateur qui décide."""
    _check_show_key(request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    from aiodos.gateway import app_connect
    from aiodos.gateway.server import _port
    status, reply = await asyncio.to_thread(app_connect.instance_request, body, _port())
    return JSONResponse(reply, status_code=status)


@router.post("/open_url")
def open_url(body: UrlIn):
    """Ouvre un lien dans le navigateur par défaut (pas dans la fenêtre de l'app)."""
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "Seuls les liens http(s) sont ouverts.")
    webbrowser.open(body.url)
    return {"ok": True}
