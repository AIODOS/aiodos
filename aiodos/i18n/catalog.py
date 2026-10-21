"""Les fichiers de textes : aiodos/i18n/<langue>/<fonctionnalité>.json, lus une fois.

Chaque fichier est un objet JSON plat : {"clé": "texte"}. La clé complète d'un texte
commence par le nom de son fichier (« empty » dans chat.json : « chat.empty »). Un texte
peut porter des paramètres, {nom}, et avoir deux formes selon un nombre :
{"one": "{n} image", "other": "{n} images"}, choisie par le paramètre n. « {{ » et « }} »
donnent une accolade.
"""
import json
import re
import threading
from pathlib import Path

LANGUAGES = ("fr", "en")
SOURCE = "fr"                 # la langue de départ, celle où les textes s'écrivent d'abord
FALLBACK = "en"               # système ni en français ni en anglais (choix du 21/10/2026)
ROOT = Path(__file__).resolve().parent
FIELD = re.compile(r"\{\{|\}\}|\{(\w+)\}")

_lock = threading.Lock()
_loaded = {}


def texts(lang: str) -> dict:
    """{clé complète: texte ou formes} de cette langue (lus une fois par processus)."""
    with _lock:
        if lang not in _loaded:
            _loaded[lang] = read(ROOT / lang)
        return _loaded[lang]


def read(folder: Path) -> dict:
    found = {}
    for path in sorted(folder.glob("*.json")):
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            found[f"{path.stem}.{key}"] = value
    return found


def exists(key: str) -> bool:
    return key in texts(SOURCE)


def lookup(lang: str, key: str):
    """Le texte de cette clé dans cette langue ; sinon en français, puis en anglais (un
    texte manquant est une erreur que les tests relèvent), puis la clé elle-même."""
    for candidate in (lang, SOURCE, FALLBACK):
        if candidate in LANGUAGES:
            value = texts(candidate).get(key)
            if value is not None:
                return value
    return key


def form(lang: str, n) -> str:
    """« one » ou « other » : en français, 0 et 1 sont au singulier ; en anglais, 1 seul."""
    try:
        n = abs(float(n))
    except (TypeError, ValueError):
        return "other"
    if lang == "fr":
        return "one" if n < 2 else "other"
    return "one" if n == 1 else "other"


def fill(value, params: dict, lang: str) -> str:
    """Le texte prêt à afficher : la forme qui convient au nombre n, les paramètres posés.
    Un paramètre absent reste tel quel ({nom}) plutôt que de faire échouer l'affichage."""
    if isinstance(value, dict):
        value = value.get(form(lang, params.get("n")), value.get("other", ""))
    text = str(value)
    if "{" not in text and "}" not in text:
        return text

    def put(match):
        name = match.group(1)
        if name is None:
            return match.group(0)[0]
        return str(params[name]) if name in params else match.group(0)
    return FIELD.sub(put, text)
