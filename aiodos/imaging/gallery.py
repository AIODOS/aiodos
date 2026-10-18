"""Images et vidéos gardées : une ligne par image ou vidéo (table images), le PNG ou le
WebM dans le dossier des créations : celui du réglage images_out_dir, sinon Images/AIODOS
pour AIODOS installé sous Windows (là où on cherche ses images), data/images/out sinon.
Un fichier créé avant un changement de dossier reste trouvé là où il a été écrit.

Une demande de plusieurs images forme un lot (« batch ») : même consigne, graines qui se
suivent. Chaque ligne dit aussi quelle machine l'a calculée, et en combien de temps.
"""
import os
import re
import sys
from pathlib import Path

from aiodos import db, paths, settings
from aiodos.imaging import catalog
from aiodos.imaging.generate import png_size

_SAFE = re.compile(r"[^0-9a-z]")


def _pictures() -> Path | None:
    """Le dossier Images de Windows, même déplacé (OneDrive…)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
    pictures = GUID(0x33E28130, 0x4E1E, 0x4676,
                    (ctypes.c_ubyte * 8)(0x83, 0x5A, 0x98, 0x39, 0x5C, 0x3B, 0xC3, 0xBB))
    found = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(pictures), 0, None,
                                                      ctypes.byref(found)) != 0:
            return None
        return Path(found.value)
    except (AttributeError, OSError):
        return None
    finally:
        if found.value is not None:
            ctypes.windll.ole32.CoTaskMemFree(found)


def _internal() -> Path:
    return paths.data_dir() / "images" / "out"


def default_dir() -> Path:
    """Images/AIODOS pour AIODOS installé sous Windows ; data/images/out sinon."""
    if paths.installed_data_dir() is not None:
        pictures = _pictures()
        if pictures is not None:
            return pictures / "AIODOS"
    return _internal()


def out_dir() -> Path:
    """Le dossier où vont les nouvelles images et vidéos."""
    chosen = str(settings.get("images_out_dir") or "").strip()
    folder = Path(os.path.expandvars(os.path.expanduser(chosen))) if chosen else default_dir()
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


def batch(name: str) -> list:
    """Les images d'un lot (une demande), dans l'ordre où elles sont arrivées."""
    return [dict(r) for r in db.query("SELECT * FROM images WHERE batch=? ORDER BY id",
                                      (_SAFE.sub("", str(name).lower())[:32],))]


def typical_seconds(model: str, width: int, height: int, frames: int | None = None) -> float | None:
    """Temps de calcul habituel d'une image ou d'une vidéo de ce modèle à cette taille (et
    cette durée) : la médiane des dix dernières, quelle que soit la machine. None : jamais
    faite."""
    rows = db.query("SELECT seconds FROM images WHERE model=? AND width=? AND height=? "
                    "AND COALESCE(frames, 0)=? AND seconds > 0 ORDER BY id DESC LIMIT 10",
                    (model, int(width or 0), int(height or 0), int(frames or 0)))
    values = sorted(r["seconds"] for r in rows)
    return round(values[len(values) // 2], 1) if values else None


def path_of(row: dict) -> Path:
    """Le fichier d'une image : dans le dossier actuel, ou dans celui où elle a été écrite
    avant un changement de dossier."""
    name = Path(row["file"]).name
    current = out_dir()
    for folder in (current, default_dir(), _internal()):
        if (folder / name).exists():
            return folder / name
    return current / name


def delete(image_id: int) -> bool:
    row = get(image_id)
    if row is None:
        return False
    db.execute("DELETE FROM images WHERE id=?", (int(image_id),))
    path_of(row).unlink(missing_ok=True)
    return True
