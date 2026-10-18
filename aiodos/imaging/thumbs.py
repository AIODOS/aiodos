"""Vignettes des images créées.

La conversation et la galerie montrent les images en quelques centaines de pixels : leur
PNG entier (1 024 × 1 024, 1 à 2 Mo) ne part que pour la visionneuse et l'enregistrement.
Une vignette est un WebP de SIZES pixels au plus de côté, faite à la première demande et
gardée dans data/images/vignettes (jamais dans le dossier des créations, que
l'utilisateur voit). Son nom porte celui du fichier d'origine : un numéro d'image repris
après une suppression ne ressert pas l'ancienne.
"""
import hashlib
import os
import threading
from pathlib import Path

from aiodos import paths
from aiodos.imaging import gallery

SIZES = (384, 768)
QUALITY = 82
_guard = threading.Lock()
_making = {}                      # vignette en cours → son verrou (plusieurs à la fois)


def folder() -> Path:
    return paths.data_dir() / "images" / "vignettes"


def size_for(wanted: int) -> int:
    """La plus petite taille gardée qui couvre `wanted` (la plus grande au-delà)."""
    return next((s for s in SIZES if s >= int(wanted)), SIZES[-1])


def _name(row: dict, size: int) -> str:
    tag = hashlib.sha1(str(row["file"]).encode("utf-8")).hexdigest()[:10]
    return f"{int(row['id'])}-{size}-{tag}.webp"


def thumbnail(row: dict, wanted: int) -> Path | None:
    """Le fichier de la vignette (fait au besoin) ; None pour une vidéo, ou si l'image ne
    se lit pas (le fichier entier sert alors)."""
    if row.get("media") == "video":
        return None
    size = size_for(wanted)
    target = folder() / _name(row, size)
    if target.is_file():
        return target
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:              # sans Pillow (Termux sans le paquet) : l'image entière
        return None
    with _guard:
        lock = _making.setdefault(target.name, threading.Lock())
    with lock:                       # deux demandes de la même vignette : faite une fois
        try:
            if target.is_file():
                return target
            with Image.open(gallery.path_of(row)) as image:
                image = image.convert("RGB")
                image.thumbnail((size, size), Image.LANCZOS)
                target.parent.mkdir(parents=True, exist_ok=True)
                part = target.with_name(f"{target.name}.{threading.get_ident()}.part")
                image.save(part, "WEBP", quality=QUALITY, method=4)
            os.replace(part, target)
        except (OSError, UnidentifiedImageError, ValueError):
            return None
        finally:
            with _guard:
                _making.pop(target.name, None)
    return target


def remove(image_id: int):
    """Les vignettes d'une image supprimée."""
    try:
        for path in folder().glob(f"{int(image_id)}-*.webp"):
            path.unlink(missing_ok=True)
    except OSError:
        pass
