"""Images et vidéos gardées : une ligne par image ou vidéo (table images), le PNG ou le
WebM dans data/images/out/.

Une demande de plusieurs images forme un lot (« batch ») : même consigne, graines qui se
suivent. Chaque ligne dit aussi quelle machine l'a calculée, et en combien de temps.
"""
import os
import re
from pathlib import Path

from aiodos import db, paths
from aiodos.imaging import catalog
from aiodos.imaging.generate import png_size

_SAFE = re.compile(r"[^0-9a-z]")


def out_dir() -> Path:
    folder = paths.data_dir() / "images" / "out"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def add(payload: dict, data: bytes, machine: str = "", seconds: float | None = None,
        frames: int | None = None, fps: int | None = None) -> dict:
    """Garde l'image (PNG) ou la vidéo (WebM) calculée pour une unité : fichier, puis ligne
    en base."""
    video = catalog.is_video(payload["model"])
    if video:
        width, height, ext = int(payload["width"]), int(payload["height"]), "webm"
    else:
        (width, height), ext = png_size(data), "png"
    batch = _SAFE.sub("", str(payload.get("batch") or "lot").lower())[:32] or "lot"
    name = f"{batch}-{int(payload.get('index') or 0):02d}-{int(payload['seed'])}.{ext}"
    target = out_dir() / name
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    os.replace(part, target)
    with db.transaction() as c:
        image_id = c.execute(
            "INSERT INTO images(batch, model, prompt, negative, seed, width, height, steps, "
            "cfg, file, machine, seconds, created, media, frames, fps) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch, payload["model"], payload["prompt"], payload.get("negative") or "",
             int(payload["seed"]), width, height, int(payload.get("steps") or 0),
             float(payload.get("cfg") or 0), name, machine or None,
             round(seconds, 2) if seconds is not None else None, db.now(),
             "video" if video else "image", frames if video else None,
             fps if video else None)).lastrowid
    return get(image_id)


def get(image_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM images WHERE id=?", (int(image_id),))
    return dict(row) if row else None


def list_images(limit: int = 60, before: int | None = None) -> list:
    """Les plus récentes d'abord ; `before` : suite de la liste (id de la dernière vue)."""
    limit = max(1, min(int(limit), 500))
    if before:
        rows = db.query("SELECT * FROM images WHERE id < ? ORDER BY id DESC LIMIT ?",
                        (int(before), limit))
    else:
        rows = db.query("SELECT * FROM images ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def path_of(row: dict) -> Path:
    return out_dir() / Path(row["file"]).name


def delete(image_id: int) -> bool:
    row = get(image_id)
    if row is None:
        return False
    db.execute("DELETE FROM images WHERE id=?", (int(image_id),))
    path_of(row).unlink(missing_ok=True)
    return True
