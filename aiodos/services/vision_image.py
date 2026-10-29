"""Une image de bibliothèque prête pour le modèle de vision (lecture, description).

Le moteur intégré (llama.cpp, mtmd) décode l'image lui-même avec stb_image : JPEG et PNG,
BMP, GIF, TGA, PSD ; pas WebP, AVIF, TIFF, ni HEIC. Une image qu'il ne sait pas lire donne
« Failed to load image or audio file » (deux fichiers ainsi en échec sur le PC de l'auteur, le
29/10/2026). Ollama ne lit pas tout non plus. D'où, avant l'envoi (prepared) :
- un JPEG ou un PNG ordinaire (couleurs ou gris sur 8 bits, MAX_SIDE pixels au plus de côté)
  part tel quel ;
- toute autre image que Pillow sait lire (WebP, AVIF, TIFF, BMP, GIF, PNG 16 bits, JPEG CMYK
  ou très grand…) est d'abord remise à l'endroit (EXIF), en couleurs sur 8 bits, ramenée à
  MAX_SIDE pixels de côté au plus, et réécrite en JPEG ; le modèle ne lit pas mieux plus grand
  (llama.cpp réduit lui-même l'image à quelques millions de pixels) ;
- une image que Pillow ne lit pas (HEIC sans son greffon, fichier abîmé ou tronqué) lève
  Unreadable, avec sa raison : l'unité est sautée avec cette raison (jobs/kinds/index.py), au
  lieu d'un échec obscur ; de même si le moteur la refuse quand même (refused).
L'image est entièrement décodée ici (Image.load) : un fichier tronqué se voit avant d'occuper
le modèle de vision.
"""
import io
import logging
from pathlib import Path

from aiodos.i18n import t

log = logging.getLogger("aiodos.media")

MAX_SIDE = 4096                       # pixels du plus grand côté, au plus
QUALITY = 92                          # JPEG réécrit : le texte d'une page reste net
PLAIN_FORMATS = {"JPEG", "PNG"}       # lus par tous les moteurs
PLAIN_MODES = {"RGB", "L"}            # 8 bits par couleur, sans transparence
ORIENTATION = 0x0112                  # étiquette EXIF de l'orientation
# Ce que répond llama-server quand il ne sait pas lire l'image (mtmd_helper_bitmap_init)
ENGINE_REFUSAL = "failed to load image"     # i18n: tel quel (motif de détection)


class Unreadable(ValueError):
    """Image que ni AIODOS ni le modèle de vision ne savent lire ; le message dit pourquoi."""


def _pillow():
    from PIL import Image, ImageOps
    return Image, ImageOps


def _format_name(path: Path) -> str:
    return (path.suffix.lstrip(".") or "?").upper()


def prepared(path) -> bytes:
    """Les octets à envoyer au modèle de vision pour cette image ; Unreadable si elle ne se lit
    pas (format non pris en charge, fichier abîmé)."""
    path = Path(path)
    data = path.read_bytes()
    try:
        Image, ImageOps = _pillow()
    except ImportError:                        # nœud sans Pillow : envoyée telle quelle
        return data
    try:
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()                     # décodée en entier : tronquée, elle échoue ici
            # Une photo de téléphone tournée par son EXIF : stb_image l'ignorerait, et le
            # texte se lirait couché
            upright = picture.getexif().get(ORIENTATION, 1) in (None, 1)
            if (picture.format in PLAIN_FORMATS and picture.mode in PLAIN_MODES and upright
                    and max(picture.size) <= MAX_SIDE):
                return data
            return _rewritten(picture, ImageOps)
    except Image.UnidentifiedImageError:
        raise Unreadable(t("media.image_format_unread", format=_format_name(path))) from None
    except Image.DecompressionBombError:
        raise Unreadable(t("media.image_too_big")) from None
    except (OSError, ValueError, SyntaxError) as exc:   # tronquée, abîmée
        log.info("Image %s illisible : %s", path.name, exc)
        raise Unreadable(t("media.image_damaged")) from None


def _rewritten(picture, ImageOps) -> bytes:
    """En JPEG, à l'endroit, en couleurs sur 8 bits (transparence posée sur du blanc, 16 bits
    ramenés à 8), MAX_SIDE pixels de côté au plus."""
    from PIL import Image
    if getattr(picture, "n_frames", 1) > 1:
        picture.seek(0)                        # GIF ou WebP animé : la première image
    picture = ImageOps.exif_transpose(picture)
    if picture.mode.startswith("I") or picture.mode == "F":     # 16 ou 32 bits par point
        picture = picture.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if picture.mode in ("RGBA", "LA", "PA") or (picture.mode == "P"
                                                and "transparency" in picture.info):
        rgba = picture.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        picture = flat
    elif picture.mode != "L":
        picture = picture.convert("RGB")
    picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=QUALITY)
    return out.getvalue()


def refused(exc) -> bool:
    """Cette erreur du moteur dit-elle qu'il n'a pas su lire l'image ?"""
    return ENGINE_REFUSAL in str(exc).lower()
