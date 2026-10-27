"""La charte d'utilisation et les conditions d'utilisation d'AIODOS (legal/<langue>/*.md).

Demandées par l'auteur le 27/10/2026 : acceptées à l'installation (page de l'installeur
Windows, tools/installer/aiodos.iss ; à la première ouverture de l'interface ailleurs, appli
Android comprise) et lisibles dans l'appli (Réglages → À propos). VERSION change avec le fond
des textes : une nouvelle version se fait accepter de nouveau.

L'installeur Windows note dans installation.json la version qu'il a fait accepter ; elle est
reportée dans les réglages, que lisent aussi les versions lancées par le lanceur des mises à
jour, qui n'ont pas installation.json à côté d'elles.
"""
import json
from pathlib import Path

from aiodos import i18n, paths, settings

VERSION = 2
FOLDER = Path(__file__).resolve().parent
DOCS = ("charter", "terms")              # dans l'ordre où on les lit


def path(doc: str, lang: str) -> Path:
    if doc not in DOCS:
        raise KeyError(doc)
    return FOLDER / (lang if lang in i18n.LANGUAGES else i18n.FALLBACK) / f"{doc}.md"


def text(doc: str, lang: str) -> str:
    return path(doc, lang).read_text(encoding="utf-8")


def split(doc: str, lang: str) -> tuple:
    """(titre, reste du texte) : le titre est la première ligne, « # Titre »."""
    first, _, rest = text(doc, lang).partition("\n")
    return first.lstrip("#").strip(), rest.strip()


def unwrap(markdown: str) -> str:
    """Les lignes repliées des paragraphes et des puces, rejointes : le rendu de l'interface
    (render.py) garde chaque retour à la ligne, comme il le faut pour les réponses."""
    out = []
    for line in markdown.splitlines():
        text = line.strip()
        starts = not text or text.startswith(("#", "- "))
        if out and out[-1] and not starts and not out[-1].startswith("#"):
            out[-1] += " " + text
        else:
            out.append(text)
    return "\n".join(out)


def installer_version() -> int:
    """La version que l'installeur Windows a fait accepter (installation.json), sinon 0."""
    try:
        mark = json.loads(paths.INSTALLED_MARK.read_text(encoding="utf-8"))
        return max(0, int(mark.get("terms") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def accepted() -> int:
    """La dernière version acceptée, dans l'appli ou par l'installeur (alors reportée dans
    les réglages), 0 si aucune."""
    done = int(settings.get("terms_accepted") or 0)
    by_installer = installer_version()
    if by_installer > done:
        settings.set_value("terms_accepted", by_installer)
        done = by_installer
    return done


def accept(version: int):
    """Acceptées dans l'appli : seulement la version montrée, qui doit être l'actuelle."""
    if version != VERSION:
        raise ValueError(version)
    settings.set_value("terms_accepted", VERSION)
