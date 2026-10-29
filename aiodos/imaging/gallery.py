"""Images et vidéos gardées : une ligne par image ou vidéo (table images), le PNG ou le
WebM (ou, pour une création de ComfyUI, add_media : PNG, JPEG, WebP, GIF, MP4) dans le dossier
des créations : celui du réglage images_out_dir, sinon Images/AIODOS
pour AIODOS installé sous Windows (là où on cherche ses images), data/images/out sinon.
Un fichier créé avant un changement de dossier reste trouvé là où il a été écrit.

Les créations audio (voix, musique, effets sonores : audio/, jobs/kinds/audio.py) s'y rangent
aussi (add_audio) : genre « audio », un WAV, sa durée (duration), le texte lu ou la
description (prompt) et les paroles d'une chanson (lyrics) ; ni largeur ni hauteur.

Une demande de plusieurs images forme un lot (« batch ») : même consigne, graines qui se
suivent. Chaque ligne dit aussi quelle machine l'a calculée, et en combien de temps : une
durée finie, entre 0 et MAX_SECONDS, ou rien (elle peut venir d'une autre machine, et un
infini gardé en base ferait tomber toute réponse JSON qui le contient).
"""
import math
import os
import re
import shutil
import sys
from pathlib import Path

from aiodos import db, paths, settings
from aiodos.imaging import catalog
from aiodos.imaging.generate import png_size

_SAFE = re.compile(r"[^0-9a-z]")
MAX_SECONDS = 7 * 86400           # durée de calcul d'une image ou d'une vidéo, au plus


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


def out_dir(create: bool = True) -> Path:
    """Le dossier où vont les nouvelles images et vidéos (créé au besoin : create). Hors des
    données d'AIODOS (Images/AIODOS, le dossier choisi), ce qu'il crée reprend les droits que
    l'utilisateur aurait eus sans AIODOS (paths.for_user) : l'umask 077 de ses données ne
    vaut pas pour une galerie qu'il partage."""
    chosen = str(settings.get("images_out_dir") or "").strip()
    folder = Path(os.path.expandvars(os.path.expanduser(chosen))) if chosen else default_dir()
    if create and not folder.is_dir():
        missing = [folder, *[p for p in folder.parents if not p.exists()]]
        folder.mkdir(parents=True, exist_ok=True)
        for made in reversed(missing):
            paths.for_user(made)
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
    if seconds is not None and not (math.isfinite(seconds) and 0 <= seconds <= MAX_SECONDS):
        seconds = None
    folder = out_dir()
    target = folder / name
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    os.replace(part, target)
    paths.for_user(target)
    with db.transaction() as c:
        image_id = c.execute(
            "INSERT INTO images(batch, model, prompt, negative, seed, width, height, steps, "
            "cfg, file, machine, seconds, created, media, frames, fps, folder) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch, payload["model"], payload["prompt"], payload.get("negative") or "",
             int(payload["seed"]), width, height, int(payload.get("steps") or 0),
             float(payload.get("cfg") or 0), name, machine or None,
             round(seconds, 2) if seconds is not None else None, db.now(),
             "video" if video else "image", frames if video else None,
             fps if video else None, str(folder))).lastrowid
    return get(image_id)


# Les genres qu'une création de ComfyUI peut garder (reconnus par leurs premiers octets,
# engines/media.py), et ce qu'en dit la galerie
MEDIA = {"image/png": "image", "image/jpeg": "image", "image/webp": "image", "image/gif": "image",
         "video/mp4": "video", "video/webm": "video"}
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm",
               ".wav": "audio/wav"}


def add_audio(payload: dict, data: bytes, duration: float, machine: str = "",
              seconds: float | None = None) -> dict:
    """Garde le WAV d'une création audio (jobs/kinds/audio.py) : fichier, puis ligne en base.
    duration : la durée du son (audio/wav.py) ; seconds : le temps de calcul."""
    batch = _SAFE.sub("", str(payload.get("batch") or "lot").lower())[:32] or "lot"
    name = f"{batch}-{int(payload.get('index') or 0):02d}-{int(payload['seed'])}.wav"
    if seconds is not None and not (math.isfinite(seconds) and 0 <= seconds <= MAX_SECONDS):
        seconds = None
    folder = out_dir()
    target = folder / name
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    os.replace(part, target)
    paths.for_user(target)
    text = payload.get("text") or payload.get("prompt") or ""
    with db.transaction() as c:
        image_id = c.execute(
            "INSERT INTO images(batch, model, prompt, negative, seed, width, height, steps, "
            "cfg, file, machine, seconds, created, media, frames, fps, folder, duration, "
            "lyrics) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch, str(payload["model"])[:120], str(text), "", int(payload["seed"]), 0, 0, 0,
             0.0, name, machine or None, round(seconds, 2) if seconds is not None else None,
             db.now(), "audio", None, None, str(folder),
             round(float(duration), 2) if math.isfinite(float(duration)) else None,
             str(payload.get("lyrics") or "") or None)).lastrowid
    return get(image_id)


def _picture_size(path: Path, kind: str) -> tuple | None:
    try:
        if kind == "image/png":
            with open(path, "rb") as f:
                return png_size(f.read(64))
        from PIL import Image
        with Image.open(path) as picture:
            return picture.size
    except Exception:                 # illisible, ou sans Pillow : la taille demandée
        return None


def add_media(meta: dict, source, kind: str, machine: str = "",
              seconds: float | None = None, n: int = 1) -> dict:
    """Garde une création de ComfyUI (jobs/kinds/comfy_create.py) : son fichier (un chemin, ou
    les octets venus d'une de mes machines), d'un genre de MEDIA ; meta : batch, index, model,
    prompt, negative, seed, width, height, steps, cfg, frames, fps. n : son rang parmi les
    sorties de la même unité (un workflow peut en donner plusieurs). ValueError pour un autre
    genre."""
    from aiodos.engines import media as engine_media
    media = MEDIA.get(kind)
    if media is None:
        raise ValueError(kind)
    batch = _SAFE.sub("", str(meta.get("batch") or "lot").lower())[:32] or "lot"
    suffix = f"-{n}" if n > 1 else ""
    name = (f"{batch}-{int(meta.get('index') or 0):02d}-{int(meta.get('seed') or 0)}{suffix}"
            f"{engine_media.extension(kind)}")
    if seconds is not None and not (math.isfinite(seconds) and 0 <= seconds <= MAX_SECONDS):
        seconds = None
    folder = out_dir()
    target = folder / name
    part = target.with_name(target.name + ".part")
    if isinstance(source, (bytes, bytearray)):
        part.write_bytes(bytes(source))
    else:
        shutil.copyfile(source, part)
    os.replace(part, target)
    paths.for_user(target)
    size = _picture_size(target, kind) if media == "image" else None
    width, height = size or (int(meta.get("width") or 0), int(meta.get("height") or 0))
    frames, fps = meta.get("frames"), meta.get("fps")
    with db.transaction() as c:
        image_id = c.execute(
            "INSERT INTO images(batch, model, prompt, negative, seed, width, height, steps, "
            "cfg, file, machine, seconds, created, media, frames, fps, folder) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch, str(meta["model"])[:120], str(meta.get("prompt") or ""),
             str(meta.get("negative") or ""), int(meta.get("seed") or 0), int(width),
             int(height), int(meta.get("steps") or 0), float(meta.get("cfg") or 0), name,
             machine or None, round(seconds, 2) if seconds is not None else None, db.now(),
             media, int(frames) if media == "video" and frames else None,
             round(float(fps)) if media == "video" and fps else None, str(folder))).lastrowid
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
                    "AND COALESCE(frames, 0)=? AND seconds > 0 AND seconds <= ? "
                    "ORDER BY id DESC LIMIT 10",
                    (model, int(width or 0), int(height or 0), int(frames or 0), MAX_SECONDS))
    values = sorted(r["seconds"] for r in rows if math.isfinite(r["seconds"]))
    return round(values[len(values) // 2], 1) if values else None


def path_of(row: dict) -> Path:
    """Le fichier d'une image : dans le dossier où elle a été écrite (colonne folder), sinon
    (images d'avant la migration 13) dans le dossier actuel, celui par défaut ou celui des
    données. Rien n'est créé ici : un dossier choisi sur un disque débranché ne gêne que
    les images qu'il contient."""
    name = Path(row["file"]).name
    folders = [Path(row["folder"])] if row.get("folder") else []
    folders += [out_dir(create=False), default_dir(), _internal()]
    for folder in folders:
        try:
            if (folder / name).exists():
                return folder / name
        except OSError:
            continue
    return folders[0] / name


def delete(image_id: int) -> bool:
    row = get(image_id)
    if row is None:
        return False
    db.execute("DELETE FROM images WHERE id=?", (int(image_id),))
    path_of(row).unlink(missing_ok=True)
    return True
