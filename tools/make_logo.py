"""Fabriquer toutes les tailles du logo d'AIODOS à partir d'une image source.

    python tools/make_logo.py [image]      (par défaut : assets/logo-source.webp)

L'emblème est repéré sur son fond uni (une illustration plus large que haute convient),
puis recadré au carré :
- assets/logo.png (1024 px) et assets/logo_128.png : carré aux coins arrondis, l'emblème
  occupe 80 % de la largeur (interface, fenêtre, icône Android d'avant Android 8) ;
- assets/logo.ico (16 à 256 px) : Windows (fenêtre, raccourcis, installeur) ;
- assets/logo-adaptive.png (432 px) : premier plan de l'icône adaptative d'Android, sans
  coins (le lanceur découpe sa forme), l'emblème dans les 58 % du centre, la zone que
  toutes les formes de lanceur gardent (tools/build_android.py).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SOURCE = ASSETS / "logo-source.webp"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
RADIUS = 0.164                    # coins arrondis, comme le logo précédent (168 px sur 1024)


def background(pixels: np.ndarray) -> np.ndarray:
    """Couleur du fond : la médiane des bords de l'image."""
    edges = np.concatenate([pixels[:20].reshape(-1, 3), pixels[-20:].reshape(-1, 3),
                            pixels[:, :20].reshape(-1, 3), pixels[:, -20:].reshape(-1, 3)])
    return np.median(edges, axis=0)


def emblem_box(pixels: np.ndarray, fond: np.ndarray, threshold: int = 40) -> tuple:
    """(gauche, haut, droite, bas) de ce qui se distingue du fond."""
    ys, xs = np.where(np.abs(pixels.astype(int) - fond).sum(axis=2) > threshold)
    if not len(xs):
        raise SystemExit("Aucun emblème distinct du fond dans cette image.")
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def square(image: Image.Image, fond: np.ndarray, box: tuple, fill: float) -> Image.Image:
    """Carré centré sur l'emblème, qui en occupe `fill` de la largeur ; le fond complète
    ce qui dépasse de l'image."""
    left, top, right, bottom = box
    side = round(max(right - left, bottom - top) / fill)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    x0, y0 = round(cx - side / 2), round(cy - side / 2)
    canvas = Image.new("RGB", (side, side), tuple(int(c) for c in fond))
    canvas.paste(image.crop((max(x0, 0), max(y0, 0), min(x0 + side, image.width),
                             min(y0 + side, image.height))), (max(-x0, 0), max(-y0, 0)))
    return canvas


def rounded(image: Image.Image) -> Image.Image:
    """Coins arrondis et lissés (masque dessiné à 4 fois la taille)."""
    big = image.width * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * RADIUS),
                                           fill=255)
    out = image.convert("RGBA")
    out.putalpha(mask.resize(image.size, Image.LANCZOS))
    return out


def make(source: Path) -> list:
    image = Image.open(source).convert("RGB")
    pixels = np.asarray(image)
    fond = background(pixels)
    box = emblem_box(pixels, fond)
    logo = rounded(square(image, fond, box, 0.80).resize((1024, 1024), Image.LANCZOS))
    written = [ASSETS / "logo.png", ASSETS / "logo_128.png", ASSETS / "logo.ico",
               ASSETS / "logo-adaptive.png"]
    logo.save(written[0], optimize=True)
    logo.resize((128, 128), Image.LANCZOS).save(written[1], optimize=True)
    logo.save(written[2], sizes=ICO_SIZES)
    square(image, fond, box, 0.58).resize((432, 432), Image.LANCZOS).save(written[3], optimize=True)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", nargs="?", default=str(SOURCE))
    args = parser.parse_args(argv)
    for path in make(Path(args.source)):
        print(f"{path.relative_to(ROOT)} ({path.stat().st_size / 1e3:.0f} Ko)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
