"""Les liens vers un fichier produit (docs/conception/partage-puissance.md § 6.8) : une adresse
qu'une appli donne à du code qui ne sait que lire une URL (requests.get), sans sa clé.

- POST /aiodos/v1/files/{id}/link {"ttl", "uses"} (avec la clé de l'appli, sur ses fichiers
  seulement) → {"url", "expires_at", "uses"} : TTL secondes (MAX_TTL au plus), USES lectures
  (MAX_USES au plus ; une plage compte pour une lecture).
- GET /aiodos/v1/links/{jeton} : le fichier, sans clé. Le jeton est tiré au hasard (256 bits)
  et gardé en mémoire seulement : un lien meurt avec AIODOS, à son échéance, ou à sa dernière
  lecture. Comme le reste de la passerelle : 127.0.0.1, aucune page web (Origin, Fetch
  Metadata : web_guard.py), Cache-Control no-store.
"""
import asyncio
import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aiodos.gateway import errors, file_store, files_api
from aiodos.gateway.errors import ApiError

router = APIRouter(prefix="/aiodos/v1")
MAX_TTL = 3600
MAX_USES = 5
MAX_LINKS = 1000

_lock = threading.Lock()
_links = {}                 # jeton -> {"file_id", "app_id", "expires", "uses"}


def _fail(request: Request, exc: ApiError):
    return errors.response(request.url.path, exc, errors.lang_of(request.headers))


def _number(value, default, low, high, name):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ApiError("invalid_request", f"« {name} » : un entier de {low} à {high}.",
                       param=name)
    return value


def _forget_old(now):
    for token in [t for t, link in _links.items() if link["expires"] <= now or link["uses"] <= 0]:
        _links.pop(token, None)


@router.post("/files/{file_id}/link")
async def make_link(file_id: str, request: Request):
    context = request.state.aiodos_app
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        ttl = _number(body.get("ttl"), 900, 1, MAX_TTL, "ttl")
        uses = _number(body.get("uses"), 3, 1, MAX_USES, "uses")
    except ApiError as exc:
        return _fail(request, exc)
    row = await asyncio.to_thread(file_store.get, context.id, file_id)
    if row is None:
        return _fail(request, ApiError("file_not_found"))
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _lock:
        _forget_old(now)
        if len(_links) >= MAX_LINKS:
            return _fail(request, ApiError("quota_exceeded", "Trop de liens ouverts : attends "
                                           "qu'ils expirent.", detail={"quota": "links"}))
        _links[token] = {"file_id": row["id"], "app_id": context.id, "expires": now + ttl,
                         "uses": uses}
    host = request.headers.get("host") or "127.0.0.1"
    return JSONResponse({"url": f"http://{host}/aiodos/v1/links/{token}",
                         "expires_at": int(now + ttl), "uses": uses})


@router.get("/links/{token}")
async def follow_link(token: str, request: Request):
    now = time.time()
    with _lock:
        link = _links.get(token)
        if link is None or link["expires"] <= now or link["uses"] <= 0:
            _links.pop(token, None)
            link = None
        else:
            link["uses"] -= 1
    if link is None:
        return _fail(request, ApiError("file_not_found", "Lien inconnu, expiré ou déjà lu."))
    row = await asyncio.to_thread(file_store.get, link["app_id"], link["file_id"])
    if row is None:
        return _fail(request, ApiError("file_not_found"))
    return files_api.serve(row, request)


def forget():
    with _lock:
        _links.clear()
