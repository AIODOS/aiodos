"""POST /aiodos/v1/open : l'appli fait revenir la fenêtre d'AIODOS sur une page
(docs/api-applications.md § 5.3), par exemple pour « L'installer dans AIODOS » : c'est là que
l'utilisateur choisit, voit la licence et accepte. La passerelle ne télécharge ni n'installe
jamais rien. Une fois par 10 s et par appli au plus."""
import asyncio
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aiodos import instance
from aiodos.gateway import access_flow, errors
from aiodos.gateway.errors import ApiError

router = APIRouter(prefix="/aiodos/v1")
EVERY = 10
# Page demandée → vue de l'interface (ui/static/js/app.js)
PAGES = {"applications": "settings", "models": "settings", "jobs": "jobs", "gallery": "images"}

_lock = threading.Lock()
_last = {}                  # appli -> instant monotonic du dernier appel


@router.post("/open")
async def open_page(request: Request):
    lang = errors.lang_of(request.headers)
    try:
        body = await request.json()
    except ValueError:
        body = None
    page = body.get("page") if isinstance(body, dict) else None
    if page not in PAGES:
        return errors.response(request.url.path, ApiError(
            "invalid_request", "« page » : applications, models, jobs ou gallery.",
            param="page"), lang)
    context = request.state.aiodos_app
    now = time.monotonic()
    with _lock:
        if now - _last.get(context.id, -EVERY) < EVERY:
            return errors.response(request.url.path, ApiError("rate_limited", retry_after=EVERY),
                                   lang)
        _last[context.id] = now
    access_flow.publish({"type": "show_view", "view": PAGES[page], "page": page,
                         "model": body.get("model") if isinstance(body.get("model"), str)
                         else None})
    shown = await asyncio.to_thread(instance.show_window)
    return JSONResponse({"opened": bool(shown)}, status_code=202)


def forget():
    with _lock:
        _last.clear()
