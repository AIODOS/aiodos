"""Les entrées des applications (docs/conception/partage-puissance.md § 5.1, 6.6) : les
fichiers qu'une appli envoie avant son travail (PUT /aiodos/v1/inputs/{sha256}), rangés par
appli et par empreinte. Une appli ne voit jamais les entrées d'une autre (I10) : même
empreinte, deux copies.

- Rangement : data/gateway/inputs/<app_id>/<aa>/<sha256>, une ligne de gateway_inputs ; le
  lien à chaque travail qui la cite, dans job_inputs.
- Réception : octets comptés pendant la lecture (MAX_INPUT), empreinte recalculée, genre
  reconnu par les premiers octets (engines/media.py) : celui que l'appli déclare n'est
  qu'une indication, un genre inconnu est refusé (415).
- Garde : tant qu'un travail non fini la cite, puis KEEP_AFTER ; QUOTA par appli.
"""
import hashlib
import os
import re
import secrets
import shutil
import threading
from pathlib import Path

from aiodos import db, paths
from aiodos.engines import media
from aiodos.gateway import apps
from aiodos.gateway.errors import ApiError

MAX_INPUT = 100 * 1024 ** 2
QUOTA = 10 * 1024 ** 3
KEEP_AFTER = 24 * 3600
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_lock = threading.Lock()


def valid_sha(value) -> bool:
    return isinstance(value, str) and bool(SHA256.match(value))


def _app_dir(app_id: str) -> Path:
    if not apps.valid_id(app_id):
        raise ValueError("identifiant d'appli invalide")
    return paths.gateway_dir("inputs") / app_id


def path_of(app_id: str, sha256: str) -> Path | None:
    """Le fichier de cette entrée de cette appli, s'il est là."""
    if not valid_sha(sha256):
        return None
    row = db.query_one("SELECT 1 FROM gateway_inputs WHERE app_id=? AND sha256=?",
                       (app_id, sha256))
    if row is None:
        return None
    path = _app_dir(app_id) / sha256[:2] / sha256
    return path if path.is_file() else None


def info(app_id: str, sha256: str) -> dict | None:
    row = db.query_one("SELECT sha256, size, type FROM gateway_inputs WHERE app_id=? AND "
                       "sha256=?", (app_id, sha256))
    return dict(row) if row and path_of(app_id, sha256) else None


def used(app_id: str) -> int:
    row = db.query_one("SELECT COALESCE(SUM(size), 0) AS n FROM gateway_inputs WHERE app_id=?",
                       (app_id,))
    return int(row["n"])


async def receive(app_id: str, sha256: str, chunks, declared: str | None = None) -> tuple:
    """Lit le corps (itérateur asynchrone d'octets) et range l'entrée → ({sha256, size,
    type}, créée ?). ApiError : input_hash_mismatch, input_too_large,
    unsupported_media_type, quota_exceeded."""
    if not valid_sha(sha256):
        raise ApiError("invalid_request", "Empreinte attendue : 64 caractères hexadécimaux "
                       "en minuscules (SHA-256).", param="sha256")
    known = info(app_id, sha256)
    if known is not None:
        async for _ in chunks:                  # le corps est lu jusqu'au bout, puis oublié
            pass
        _touch(app_id, sha256)
        return known, False
    folder = paths.gateway_dir("inputs") / "_reception"     # jamais un id d'appli (8 car.)
    folder.mkdir(exist_ok=True)
    part = folder / f"{secrets.token_hex(8)}.part"
    digest, size, head = hashlib.sha256(), 0, b""
    try:
        with open(part, "wb") as out:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_INPUT:
                    raise ApiError("input_too_large", f"Entrée trop lourde : "
                                   f"{MAX_INPUT // 1024 ** 2} Mo au plus.",
                                   detail={"max_bytes": MAX_INPUT})
                if len(head) < media.HEAD:
                    head += chunk[:media.HEAD - len(head)]
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != sha256:
            raise ApiError("input_hash_mismatch", "L'empreinte du fichier reçu ne correspond "
                           "pas à celle de l'adresse.", detail={"sha256": digest.hexdigest()})
        kind = media.sniff(head)
        if kind not in media.INPUT_TYPES:
            raise ApiError("unsupported_media_type", "Genre de fichier non reconnu : images "
                           "(PNG, JPEG, WebP, GIF), vidéos (MP4, WebM) ou sons (WAV, MP3, FLAC, "
                           "OGG).", detail={"declared": declared})
        with _lock:
            if used(app_id) + size > QUOTA:
                raise ApiError("quota_exceeded", "Trop d'entrées gardées pour cette "
                               "application : supprime des travaux finis.",
                               detail={"quota": "inputs", "bytes": QUOTA})
            target = _app_dir(app_id) / sha256[:2] / sha256
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, target)
            now = db.now()
            db.execute("INSERT INTO gateway_inputs(app_id, sha256, size, type, created, used) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT(app_id, sha256) DO UPDATE SET "
                       "used=excluded.used", (app_id, sha256, size, kind, now, now))
        return {"sha256": sha256, "size": size, "type": kind}, True
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


def _touch(app_id: str, sha256: str):
    db.execute("UPDATE gateway_inputs SET used=? WHERE app_id=? AND sha256=?",
               (db.now(), app_id, sha256))


def link(job_id: int, app_id: str, sha256s) -> None:
    """Ces entrées sont citées par ce travail : gardées tant qu'il n'est pas fini."""
    now = db.now()
    with db.transaction() as c:
        for sha256 in set(sha256s):
            c.execute("INSERT OR IGNORE INTO job_inputs(job_id, app_id, sha256) VALUES(?,?,?)",
                      (job_id, app_id, sha256))
            c.execute("UPDATE gateway_inputs SET used=? WHERE app_id=? AND sha256=?",
                      (now, app_id, sha256))


def purge(now: float | None = None) -> int:
    """Efface les entrées qu'aucun travail non fini ne cite depuis KEEP_AFTER → nombre."""
    now = db.now() if now is None else now
    rows = db.query(
        """SELECT i.app_id, i.sha256 FROM gateway_inputs i
           WHERE i.used < ? AND NOT EXISTS (
               SELECT 1 FROM job_inputs l JOIN jobs j ON j.id = l.job_id
               WHERE l.app_id = i.app_id AND l.sha256 = i.sha256
                 AND j.status IN ('queued', 'running', 'paused'))""", (now - KEEP_AFTER,))
    for row in rows:
        forget(row["app_id"], row["sha256"])
    return len(rows)


def forget(app_id: str, sha256: str):
    try:
        (_app_dir(app_id) / sha256[:2] / sha256).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    db.execute("DELETE FROM gateway_inputs WHERE app_id=? AND sha256=?", (app_id, sha256))


def forget_app(app_id: str):
    """L'appli est oubliée : toutes ses entrées avec elle."""
    db.execute("DELETE FROM gateway_inputs WHERE app_id=?", (app_id,))
    try:
        shutil.rmtree(_app_dir(app_id), ignore_errors=True)
    except ValueError:
        pass
