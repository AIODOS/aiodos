"""Les fichiers produits pour une appli (docs/conception/partage-puissance.md § 5.3, 6.8) :
GET /aiodos/v1/files/{id} (la fiche), /content (les octets, avec reprise par Range),
DELETE. Une appli ne voit que ses fichiers (I10). Un fichier servi jusqu'à son dernier octet
est récupéré : sa garde de 7 jours commence (gateway/file_store.py).
"""
import asyncio
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from aiodos.gateway import errors, file_store
from aiodos.gateway.errors import ApiError
from aiodos.i18n import t

router = APIRouter(prefix="/aiodos/v1")
CHUNK = 256 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _fail(request: Request, exc: ApiError):
    return errors.response(request.url.path, exc, errors.lang_of(request.headers))


async def _own(request: Request, file_id: str):
    context = request.state.aiodos_app
    return await asyncio.to_thread(file_store.get, context.id, file_id)


@router.get("/files/{file_id}")
async def file_info(file_id: str, request: Request):
    row = await _own(request, file_id)
    if row is None:
        return _fail(request, ApiError("file_not_found"))
    return JSONResponse(file_store.public(row))


def _span(header: str | None, size: int):
    """(début, fin) d'une seule plage, None pour tout, ou False si elle est hors du fichier."""
    if not header:
        return None
    found = _RANGE.match(header.strip())
    if not found or (not found.group(1) and not found.group(2)):
        return None
    if found.group(1):
        start = int(found.group(1))
        end = min(int(found.group(2)), size - 1) if found.group(2) else size - 1
    else:
        start, end = max(0, size - int(found.group(2))), size - 1
    if start >= size or end < start:
        return False
    return start, end


@router.get("/files/{file_id}/content")
async def file_content(file_id: str, request: Request):
    row = await _own(request, file_id)
    if row is None:
        return _fail(request, ApiError("file_not_found"))
    return serve(row, request)


def serve(row, request: Request):
    """Les octets d'un fichier (tout, ou une plage), comptés pour sa récupération ; aussi
    par un lien (gateway/links_api.py)."""
    path = file_store.path_of(row)
    if not path.is_file():
        return _fail(request, ApiError("file_not_found", t("gateway_files.file_gone")))
    size = int(row["bytes"])
    span = _span(request.headers.get("range"), size)
    keep = "pending" if row["fetched_at"] is None else str(
        int(row["fetched_at"] + file_store.OUT_DAYS * 86400))
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store",
               "X-Content-Type-Options": "nosniff", "X-Aiodos-Keep-Until": keep,
               "ETag": f'"{row["sha256"]}"'}
    if span is False:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    start, end = span if span else (0, size - 1)
    length = end - start + 1
    headers["Content-Length"] = str(length)
    if span:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    async def stream():
        # Les octets remis comptent même si la réponse est coupée : la plage reprise ensuite
        # complète le compte, et le fichier est bien récupéré. Un bloc compte dès qu'il part
        # (l'appli a pu en recevoir une partie) ; le compte est écrit sans attendre, car une
        # attente serait annulée avec la réponse.
        sent = 0
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while sent < length:
                    block = f.read(min(CHUNK, length - sent))
                    if not block:
                        break
                    sent += len(block)
                    yield block
        finally:
            if sent:
                file_store.served(row["id"], start, start + sent - 1)
    return StreamingResponse(stream(), status_code=206 if span else 200,
                             media_type=row["type"], headers=headers)


@router.delete("/files/{file_id}")
async def file_delete(file_id: str, request: Request):
    context = request.state.aiodos_app
    gone = await asyncio.to_thread(file_store.delete, context.id, file_id)
    if not gone:
        return _fail(request, ApiError("file_not_found"))
    return JSONResponse({"id": file_id, "deleted": True})
