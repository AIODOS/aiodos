"""Pièces jointes d'une conversation : une image de référence, une vidéo, un son, un document
ou un texte joint à un message. Elles restent sur ce PC (data/attachments/<id>/), liées au
message qui les a envoyées ; ce qu'on en lit (document, image décrite, audio ou vidéo
transcrits) est lu une fois, puis gardé avec elles.

- Envoi : POST /api/attachments (api/routes/attachments.py), avant le message. Une pièce
  jointe qu'aucun message n'a prise, ou dont le message a disparu (conversation supprimée,
  « Modifier »), s'efface (purge).
- Pour la réponse : le texte de chaque pièce jointe accompagne la question, borné (context) ;
  une image va telle quelle à un modèle de cette machine qui voit les images (pipeline), et
  décrite à tous les autres. Elle suit la conversation comme le texte du message : chez la
  machine qui répond, le temps de la réponse.
- Pour une création : une image jointe est l'image de départ (reference_png).
"""
import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from aiodos import db, paths, settings
from aiodos.i18n import t
from aiodos.services import extract

log = logging.getLogger("aiodos")

MAX_BYTES = 512 * 1024 ** 2          # une vidéo de quelques minutes
MAX_PER_MESSAGE = 10
TEXT_PER_ATTACHMENT = 12_000         # caractères lus par pièce jointe pour la question
TEXT_PER_QUESTION = 24_000           # et pour toutes celles de la question
TEXT_IN_HISTORY = 3_000              # celles des messages précédents, par pièce jointe
UNLINKED_DAYS = 1
KINDS = {"image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
         "video": {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"},
         "audio": {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".opus", ".aac"}}
_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


class AttachmentError(Exception):
    """Une pièce jointe refusée (message lisible) ; status : le code HTTP."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def kind_of(name: str) -> str:
    """image | video | audio | document | text | other, d'après l'extension."""
    ext = Path(name).suffix.lower()
    for kind, exts in KINDS.items():
        if ext in exts:
            return kind
    family = extract.classify(name)
    return family if family in ("document", "text") else "other"


def safe_name(name: str) -> str:
    """Le nom gardé : sans chemin ni caractère interdit, 120 caractères au plus."""
    base = _UNSAFE.sub("_", Path(str(name or "")).name).strip(" .") or "fichier"
    stem, ext = os.path.splitext(base)
    return stem[:120 - len(ext[:16])] + ext[:16]


def folder() -> Path:
    return paths.data_dir() / "attachments"


def path_of(row) -> Path:
    return folder() / row["id"] / row["name"]


async def receive(name: str, stream, mime: str = "") -> dict:
    """Range un fichier reçu (le flux d'octets de la requête) → sa fiche publique.
    AttachmentError s'il est vide ou trop gros : rien n'est gardé."""
    folder().mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".reception-", dir=folder())
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(handle, "wb") as out:
            async for chunk in stream:
                size += len(chunk)
                if size > MAX_BYTES:
                    raise AttachmentError(t("attachments.too_big", size=MAX_BYTES // 1024 ** 2),
                                          status=413)
                digest.update(chunk)
                await asyncio.to_thread(out.write, chunk)
        if not size:
            raise AttachmentError(t("attachments.empty"))
        return await asyncio.to_thread(_register, name, Path(temp), size, digest.hexdigest(), mime)
    finally:
        Path(temp).unlink(missing_ok=True)             # rangée : plus rien à effacer


def _register(name: str, received: Path, size: int, sha256: str, mime: str) -> dict:
    att_id = "att_" + secrets.token_urlsafe(12)
    name = safe_name(name)
    target = folder() / att_id / name
    target.parent.mkdir()
    shutil.move(str(received), target)
    db.execute("INSERT INTO attachments(id, message_id, name, kind, type, size, sha256, created) "
               "VALUES(?, NULL, ?, ?, ?, ?, ?, ?)",
               (att_id, name, kind_of(name), (mime or "application/octet-stream")[:100], size,
                sha256, db.now()))
    return public(get(att_id))


def get(att_id: str):
    return db.query_one("SELECT * FROM attachments WHERE id=?", (str(att_id),))


def public(row) -> dict:
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "type": row["type"],
            "size": row["size"], "url": f"/api/attachments/{row['id']}"}


def pending(ids) -> list:
    """Les pièces jointes envoyées avec un message, encore libres, dans l'ordre donné
    (inconnues ou déjà prises : laissées de côté)."""
    rows = []
    for att_id in list(dict.fromkeys(str(i) for i in ids or []))[:MAX_PER_MESSAGE]:
        row = get(att_id)
        if row is not None and row["message_id"] is None and path_of(row).is_file():
            rows.append(row)
    return rows


def link(rows, message_id: int):
    """Liées au message qui les envoie (dans la transaction qui l'enregistre)."""
    for row in rows:
        db.execute("UPDATE attachments SET message_id=? WHERE id=? AND message_id IS NULL",
                   (message_id, row["id"]))


def of_messages(message_ids) -> dict:
    """{message_id: [lignes]} des pièces jointes de ces messages, dans leur ordre d'envoi."""
    ids = [int(i) for i in message_ids]
    found = {}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        for row in db.query(f"SELECT * FROM attachments WHERE message_id IN "
                            f"({','.join('?' * len(part))}) ORDER BY created, rowid", part):
            found.setdefault(row["message_id"], []).append(row)
    return found


def in_conversation(conv_id) -> bool:
    """Un message de cette conversation porte-t-il une pièce jointe ? (une telle conversation
    ne part jamais chez un inconnu du réseau public : chat/public_consent.py)"""
    return db.query_one(
        "SELECT 1 FROM attachments a JOIN messages m ON m.id = a.message_id "
        "WHERE m.conversation_id=? LIMIT 1", (conv_id,)) is not None


def delete(att_id: str) -> bool:
    """Retire une pièce jointe pas encore envoyée (la croix de la zone de saisie)."""
    row = get(att_id)
    if row is None or row["message_id"] is not None:
        return False
    _remove(row)
    return True


def _remove(row):
    shutil.rmtree(folder() / row["id"], ignore_errors=True)
    db.execute("DELETE FROM attachments WHERE id=?", (row["id"],))


def purge(now: float | None = None) -> int:
    """Efface celles qu'aucun message n'a prises depuis UNLINKED_DAYS, et celles dont le
    message n'existe plus. → nombre effacé."""
    now = db.now() if now is None else now
    rows = db.query("SELECT a.* FROM attachments a LEFT JOIN messages m ON m.id = a.message_id "
                    "WHERE (a.message_id IS NULL AND a.created < ?) "
                    "OR (a.message_id IS NOT NULL AND m.id IS NULL)",
                    (now - UNLINKED_DAYS * 86400,))
    for row in rows:
        _remove(row)
    return len(rows)


async def loop(every: float = 3600):
    """Au démarrage, puis toutes les heures : purge."""
    while True:
        try:
            gone = await asyncio.to_thread(purge)
            if gone:
                log.info("Pièces jointes effacées : %s", gone)
        except Exception:
            log.exception("Ménage des pièces jointes : échec")
        await asyncio.sleep(every)


# ------------------------------------------------------------------- lecture
def _vision():
    """La lecture d'une image (texte et description), si le réglage le permet."""
    if not settings.get("media_vision"):
        return None
    from aiodos.services import media
    model, mode = settings.get("vision_model"), settings.get("media_vision_mode")
    return lambda p: media.vision_extract(p, model, mode)


def _transcriber(stop=None):
    if not settings.get("media_transcribe"):
        return None
    from aiodos.services import media
    return lambda p: media.transcribe(p, settings.get("whisper_model"), stop=stop)


def text_of(row, stop=None) -> str:
    """Ce qu'on lit de la pièce jointe (lu une fois, gardé) ; « » si rien. Une image n'est
    décrite que si un modèle de vision est réglé ; l'audio et la vidéo transcrits que si
    Whisper est là."""
    if row["text_state"] is not None:
        return row["text"] or ""
    path = path_of(row)
    try:
        if row["kind"] == "image":
            vision = _vision()
            text = vision(path) if vision else None
        elif row["kind"] in ("video", "audio"):
            transcribe = _transcriber(stop)
            text = transcribe(path) if transcribe else None
        elif row["kind"] in ("document", "text"):
            text = extract.extract_text(path)
        else:
            text = extract.extract_text(path) if extract.looks_like_text(path) else None
        state = "ok" if text and text.strip() else "empty"
    except Exception as exc:             # un fichier illisible ne bloque pas la réponse
        log.warning("Pièce jointe %s illisible : %s", row["name"], exc)
        text, state = None, f"error:{str(exc)[:200]}"
    if stop is not None and stop.is_set():
        return text or ""                # interrompue : rien de gardé, relue la prochaine fois
    db.execute("UPDATE attachments SET text=?, text_state=? WHERE id=?",
               (text, state, row["id"]))
    return text or ""


# Ce que le modèle lit d'une pièce jointe (attachments_prompts.json, dans la langue de la
# requête) selon sa sorte : (avec son texte, rien à en lire)
_BLOCKS = {"image": ("attachments_prompts.image", "attachments_prompts.image_empty"),
           "video": ("attachments_prompts.video", "attachments_prompts.video_empty"),
           "audio": ("attachments_prompts.audio", "attachments_prompts.audio_empty")}
_FILE_BLOCK = ("attachments_prompts.file", "attachments_prompts.file_empty")


def _block(row, text: str, limit: int) -> str:
    cut = len(text) > limit
    body = text[:limit].rstrip() + ("\n" + t("attachments_prompts.cut") if cut else "")
    full, empty = _BLOCKS.get(row["kind"], _FILE_BLOCK)
    return t(full, name=row["name"], body=body) if body.strip() \
        else t(empty, name=row["name"])


def question_blocks(rows, stop=None, skip_text_of=(), status=None) -> str:
    """Le texte des pièces jointes d'une question, borné (TEXT_PER_QUESTION). skip_text_of :
    les identifiants dont le texte n'est pas utile (une image vue telle quelle par le
    modèle) ; status : status(texte) pendant une lecture longue."""
    blocks, left = [], TEXT_PER_QUESTION
    for row in rows:
        if stop is not None and stop.is_set():
            break
        if row["id"] in skip_text_of:
            blocks.append(t("attachments_prompts.image_seen", name=row["name"]))
            continue
        if status is not None and row["text_state"] is None:
            status(t("attachments.reading", name=row["name"]))
        text = text_of(row, stop)
        blocks.append(_block(row, text, max(0, min(TEXT_PER_ATTACHMENT, left))))
        left -= min(len(text), TEXT_PER_ATTACHMENT)
    return "\n\n".join(blocks)


def images_b64(rows, max_side: int = 2048) -> list:
    """Les images jointes en base64, pour un modèle qui les voit. Autre chose qu'un PNG ou un
    JPEG (WebP, GIF, BMP), ou une très grande image, est remis en JPEG : tous les moteurs ne
    lisent pas les autres formats, et le modèle ne lirait pas mieux une image plus grande."""
    found = []
    for row in rows:
        data = path_of(row).read_bytes()
        plain = data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8"
        try:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(data)) as picture:
                if not plain or max(picture.size) > max_side:
                    picture = ImageOps.exif_transpose(picture).convert("RGB")
                    picture.thumbnail((max_side, max_side))
                    out = io.BytesIO()
                    picture.save(out, "JPEG", quality=90)
                    data = out.getvalue()
        except Exception as exc:          # illisible ici : envoyée telle quelle
            log.info("Image jointe %s laissée telle quelle : %s", row["name"], exc)
        found.append(base64.b64encode(data).decode("ascii"))
    return found


def history_blocks(rows) -> str:
    """Le texte déjà lu des pièces jointes d'un message précédent, plus court ; rien n'est lu
    de nouveau ici."""
    return "\n\n".join(_block(row, row["text"] or "", TEXT_IN_HISTORY) for row in rows)


# --------------------------------------------------------------- créations
def reference_png(row, sizes, multiple: int) -> Path:
    """L'image jointe, recadrée au centre au format du modèle le plus proche du sien et
    enregistrée en PNG à côté d'elle : l'image de départ d'une création. sizes : [(largeur,
    hauteur), …] du modèle. AttachmentError si ce n'est pas une image lisible."""
    if row["kind"] != "image":
        raise AttachmentError(t("attachments.reference_not_image"))
    from PIL import Image, ImageOps
    try:
        with Image.open(path_of(row)) as source:
            source = ImageOps.exif_transpose(source).convert("RGB")
            ratio = source.width / source.height
            width, height = min(sizes, key=lambda s: abs(s[0] / s[1] - ratio))
            width, height = width - width % multiple, height - height % multiple
            fitted = ImageOps.fit(source, (width, height), Image.LANCZOS)
    except (OSError, ValueError) as exc:
        raise AttachmentError(t("attachments.reference_unreadable", name=row["name"])) from exc
    target = path_of(row).parent / f"depart-{width}x{height}.png"
    fitted.save(target, "PNG")
    return target
