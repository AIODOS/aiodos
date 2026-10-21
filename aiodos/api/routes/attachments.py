"""Pièces jointes de la conversation (chat/attachments.py) : envoyées avant le message
(POST, le fichier tel quel, son nom dans X-File-Name), relues par l'interface (GET), retirées
avant l'envoi (DELETE).

Seules les images, vidéos et sons reconnus par leur extension s'affichent dans la page, avec
le type qui leur correspond ; tout le reste (document, page HTML, SVG…) se télécharge, sous un
type neutre : un fichier joint ne s'exécute jamais dans l'interface."""
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from aiodos.chat import attachments
from aiodos.i18n import t

router = APIRouter(prefix="/api/attachments")
SHOWN = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".gif": "image/gif", ".bmp": "image/bmp", ".mp4": "video/mp4", ".m4v": "video/mp4",
         ".webm": "video/webm", ".mov": "video/quicktime", ".mp3": "audio/mpeg",
         ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".m4a": "audio/mp4",
         ".aac": "audio/aac", ".flac": "audio/flac"}


@router.post("")
async def upload(request: Request):
    name = unquote(request.headers.get("x-file-name") or "") or "fichier"
    try:
        return await attachments.receive(name, request.stream(),
                                         request.headers.get("content-type", ""))
    except attachments.AttachmentError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/{att_id}")
def read(att_id: str):
    row = attachments.get(att_id)
    path = attachments.path_of(row) if row is not None else None
    if path is None or not path.is_file():
        raise HTTPException(404, t("attachments.not_found"))
    shown = SHOWN.get(Path(row["name"]).suffix.lower())
    if shown:
        return FileResponse(path, media_type=shown, headers={"Cache-Control": "private, max-age=3600"})
    return FileResponse(path, media_type="application/octet-stream", filename=row["name"])


@router.delete("/{att_id}")
def remove(att_id: str):
    if not attachments.delete(att_id):
        raise HTTPException(404, t("attachments.not_found_or_sent"))
    return {"deleted": True}
