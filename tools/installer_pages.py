"""Les pages de l'installeur Windows (tools/installer/aiodos.iss) : la charte et les
conditions d'utilisation à accepter, en RTF, et les images de l'assistant, tirées du logo.

Demandé par l'auteur le 27/10/2026 : la charte et les conditions d'utilisation présentes à
l'installation, avec le logo et un texte de présentation (celui-ci est dans aiodos.iss,
[Messages], WelcomeLabel2). Les textes sont ceux du code installé (aiodos/legal/<langue>/),
pris dans les fichiers préparés (stage\\app) : ceux du commit, comme le reste de l'installeur.
"""
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

LANGUAGES = ("fr", "en")
DOCS = ("charter", "terms")
# Taille de la grande image (pages d'accueil et de fin) et de la petite (en haut à droite des
# autres pages), de 100 à 250 % : celles parmi lesquelles Inno Setup 6.6 et plus choisit
LARGE = ((202, 386), (269, 515), (336, 643), (403, 772), (430, 824), (498, 953), (534, 1022))
SMALL = (58, 77, 97, 116, 124, 143, 159)
# Couleurs de l'interface (ui/static/css/app.css : --bg, --panel, --cyan, --text)
BG = (6, 8, 7)
PANEL = (16, 26, 22)
GREEN = (52, 209, 166)
TEXT = (229, 239, 235)
FONT = Path(r"C:\Windows\Fonts\seguisb.ttf")          # Segoe UI Semibold

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"\*(.+?)\*")
_VERSION = re.compile(r"^VERSION\s*=\s*(\d+)", re.M)


def terms_version(app: Path) -> int:
    """VERSION des textes (aiodos/legal/__init__.py du code préparé) : notée dans
    installation.json, elle dit à AIODOS que l'installeur les a fait accepter."""
    found = _VERSION.search((app / "aiodos" / "legal" / "__init__.py").read_text(encoding="utf-8"))
    if not found:
        raise ValueError("VERSION introuvable dans aiodos/legal/__init__.py")
    return int(found.group(1))


# --- La charte et les conditions, en RTF (ce qu'affiche la page de licence d'Inno Setup) ---

def escape(text: str) -> str:
    """Du texte en RTF : \\, { et } échappés, tout caractère hors ASCII en \\uN? (en paire
    de substitution au-delà du plan de base). Le fichier ne contient que de l'ASCII."""
    out = []
    for ch in text:
        code = ord(ch)
        if ch in "\\{}":
            out.append("\\" + ch)
        elif code < 128:
            out.append(ch)
        else:
            units = [code] if code < 0x10000 else [
                0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
            out.extend(f"\\u{u - 65536 if u > 32767 else u}?" for u in units)
    return "".join(out)


def inline(text: str) -> str:
    """**gras** et *italique*, les seuls styles des textes."""
    text = _BOLD.sub(r"{\\b \1}", escape(text))
    return _ITALIC.sub(r"{\\i \1}", text)


def blocks(markdown: str) -> list:
    """Les blocs du Markdown des textes : (« h1 » / « h2 », titre), (« p », paragraphe),
    (« li », élément de liste), les lignes repliées rejointes."""
    out, current = [], None
    for line in markdown.splitlines():
        text = line.strip()
        if not text:
            current = None
        elif text.startswith("#"):
            level = len(text) - len(text.lstrip("#"))
            out.append([f"h{level}", text[level:].strip()])
            current = None
        elif text.startswith("- "):
            current = ["li", text[2:].strip()]
            out.append(current)
        elif current is not None:
            current[1] += " " + text
        else:
            current = ["p", text]
            out.append(current)
    return [tuple(b) for b in out]


def rtf(texts: list) -> str:
    """Les textes Markdown, l'un après l'autre, en un document RTF : titres en vert (la
    couleur d'AIODOS, gardée par le style sombre d'Inno Setup), puces, Segoe UI 9 pt."""
    parts = ["{\\rtf1\\ansi\\ansicpg1252\\deff0\\uc1"
             "{\\fonttbl{\\f0\\fswiss\\fcharset0 Segoe UI;}}"
             "{\\colortbl ;\\red52\\green209\\blue166;}"
             "\\viewkind4\\f0\\fs18\n"]
    for i, markdown in enumerate(texts):
        for kind, text in blocks(markdown):
            body = inline(text)
            if kind == "h1":
                before = "\\sb480" if i else "\\sb0"
                parts.append(f"\\pard{before}\\sa160\\cf1\\b\\fs28 {body}\\b0\\fs18\\cf0\\par\n")
            elif kind in ("h2", "h3"):
                parts.append(f"\\pard\\sb200\\sa80\\b\\fs20 {body}\\b0\\fs18\\par\n")
            elif kind == "li":
                parts.append(f"\\pard\\fi-220\\li440\\sa60\\tx440 \\u8226?\\tab {body}\\par\n")
            else:
                parts.append(f"\\pard\\sa120 {body}\\par\n")
    parts.append("}\n")
    return "".join(parts)


# --- Les images de l'assistant ---

def _font(size: int):
    try:
        return ImageFont.truetype(str(FONT), size)
    except OSError:
        return ImageFont.load_default(size)


def _spaced(draw, center: float, top: float, text: str, font, spacing: float):
    """Un mot aux lettres espacées, centré sur `center`."""
    widths = [font.getlength(ch) for ch in text]
    x = center - (sum(widths) + spacing * (len(text) - 1)) / 2
    for ch, width in zip(text, widths):
        draw.text((x, top), ch, font=font, fill=TEXT, anchor="lt")
        x += width + spacing


def large(logo: Image.Image, size: tuple) -> Image.Image:
    """Grande image : le logo dans un halo vert, sur le noir d'AIODOS, et son nom dessous."""
    w, h = size
    shade = Image.linear_gradient("L").resize(size)              # noir en haut, blanc en bas
    image = Image.composite(Image.new("RGB", size, BG), Image.new("RGB", size, PANEL), shade)
    cx, cy = w / 2, h * 0.40
    glow = Image.new("L", size, 0)
    r = w * 0.46
    ImageDraw.Draw(glow).ellipse((cx - r, cy - r, cx + r, cy + r), fill=80)
    glow = glow.filter(ImageFilter.GaussianBlur(w * 0.14))
    image = Image.composite(Image.new("RGB", size, GREEN), image, glow)
    side = round(w * 0.58)
    emblem = logo.resize((side, side), Image.LANCZOS)
    image.paste(emblem, (round(cx - side / 2), round(cy - side / 2)), emblem)
    _spaced(ImageDraw.Draw(image), cx, cy + side / 2 + w * 0.11, "AIODOS",
            _font(round(w * 0.12)), w * 0.022)
    return image


def small(logo: Image.Image, side: int) -> Image.Image:
    """Petite image : le logo seul, sur fond transparent."""
    return logo.resize((side, side), Image.LANCZOS)


def prepare(stage: Path, logo: Path) -> dict:
    """stage\\setup\\ : legal-<langue>.rtf, et les images (wizard-*, small-*). → les chemins
    à passer à Inno Setup : {"legal": {langue: chemin}, "large": [...], "small": [...]}."""
    out = stage / "setup"
    out.mkdir(parents=True, exist_ok=True)
    folder = stage / "app" / "aiodos" / "legal"
    legal = {}
    for lang in LANGUAGES:
        texts = [(folder / lang / f"{doc}.md").read_text(encoding="utf-8") for doc in DOCS]
        legal[lang] = out / f"legal-{lang}.rtf"
        legal[lang].write_text(rtf(texts), encoding="ascii", newline="\n")
    emblem = Image.open(logo).convert("RGBA")
    larges, smalls = [], []
    for size in LARGE:
        larges.append(out / f"wizard-{size[0]}.png")
        large(emblem, size).save(larges[-1])
    for side in SMALL:
        smalls.append(out / f"small-{side}.png")
        small(emblem, side).save(smalls[-1])
    return {"legal": legal, "large": larges, "small": smalls}
