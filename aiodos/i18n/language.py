"""La langue des textes : celle de la requête en cours, sinon celle du nœud.

Requête : l'interface envoie la sienne (Accept-Language, ou lang= pour le flux
d'événements, cf. middleware.py). Nœud (travail de fond, icône près de l'horloge, ligne de
commande, écran d'attente) : le réglage « language » s'il désigne une langue, sinon celle
du système (Windows, Android, variables LANGUAGE/LC_ALL/LANG), sinon l'anglais.
AIODOS_LANGUAGE remplace la langue du système (essais automatiques, nœud sans fenêtre).
"""
import contextvars
import locale
import logging
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager

from aiodos.i18n.catalog import FALLBACK, LANGUAGES

log = logging.getLogger("aiodos")

_request = contextvars.ContextVar("aiodos_language", default=None)
_lock = threading.Lock()
_node = {"value": None, "system": None}
_listeners = []


def current() -> str:
    return _request.get() or node()


def node() -> str:
    """La langue du nœud hors de toute requête (gardée jusqu'au prochain changement)."""
    value = _node["value"]
    if value is None:
        value = chosen() or system() or FALLBACK
        _node["value"] = value
    return value


def chosen() -> str | None:
    """La langue choisie dans les Réglages, ou None (« auto » : celle du système)."""
    value = _setting()
    return value if value in LANGUAGES else None


def supported(tag) -> str | None:
    """« fr-FR », « en_US.UTF-8 », « EN »… → « fr », « en », ou None."""
    primary = str(tag or "").strip().replace("_", "-").split("-")[0].split(".")[0].lower()
    return primary if primary in LANGUAGES else None


def first_supported(tags) -> str | None:
    for tag in tags:
        found = supported(tag)
        if found:
            return found
    return None


def from_header(header: str | None) -> str | None:
    """La langue préférée d'un en-tête Accept-Language (« fr-FR,fr;q=0.9,en;q=0.8 »)."""
    ranked = []
    for position, part in enumerate(str(header or "").split(",")[:20]):
        tag, _, weight = part.strip().partition(";")
        quality = 1.0
        if weight.strip().startswith("q="):
            try:
                quality = float(weight.strip()[2:])
            except ValueError:
                quality = 0.0
        if tag and quality > 0:
            ranked.append((-quality, position, tag))
    return first_supported(tag for _, _, tag in sorted(ranked))


@contextmanager
def using(lang):
    """Les textes produits dans ce bloc sont dans cette langue (si elle est connue)."""
    token = _request.set(lang if lang in LANGUAGES else None)
    try:
        yield
    finally:
        _request.reset(token)


def set_request(lang):
    """Pour le middleware : → jeton à rendre à reset_request."""
    return _request.set(lang if lang in LANGUAGES else None)


def reset_request(token):
    _request.reset(token)


def changed():
    """Le réglage a changé : la langue du nœud est recalculée, et ceux qui l'affichent hors
    de l'interface (l'appli Android) sont prévenus."""
    _node["value"] = None
    lang = node()
    for listener in list(_listeners):
        try:
            listener(lang)
        except Exception:
            log.exception("Langue : un abonné n'a pas suivi le changement")


def on_change(listener):
    """listener(langue), appelé quand le réglage de langue change."""
    _listeners.append(listener)


def forget():
    """La langue du nœud sera relue (une autre base : essais automatiques)."""
    _node["value"] = None


def system() -> str | None:
    """La langue du système, si c'est le français ou l'anglais (calculée une fois)."""
    forced = supported(os.environ.get("AIODOS_LANGUAGE"))
    if forced:
        return forced
    with _lock:
        if _node["system"] is None:
            try:
                _node["system"] = first_supported(_system_tags()) or ""
            except Exception:
                log.exception("Langue du système illisible")
                _node["system"] = ""
        return _node["system"] or None


def _system_tags() -> list:
    tags = []
    if sys.platform == "win32":
        tags += _windows_languages()
    android = _android_language()
    if android:
        tags.append(android)
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        tags += [part for part in value.split(":") if part and part not in ("C", "POSIX")]
    try:
        current_locale = locale.getlocale()[0]
    except ValueError:
        current_locale = None
    if current_locale:
        tags.append(current_locale)
    return tags


def _windows_languages() -> list:
    """Les langues d'affichage de Windows, dans l'ordre de l'utilisateur."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    count, size = wintypes.ULONG(0), wintypes.ULONG(0)
    MUI_LANGUAGE_NAME = 0x8
    if kernel.GetUserPreferredUILanguages(MUI_LANGUAGE_NAME, ctypes.byref(count), None,
                                          ctypes.byref(size)) and size.value:
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel.GetUserPreferredUILanguages(MUI_LANGUAGE_NAME, ctypes.byref(count), buffer,
                                              ctypes.byref(size)):
            return [part for part in buffer[:size.value].split("\0") if part]
    langid = kernel.GetUserDefaultUILanguage()
    name = locale.windows_locale.get(langid)
    return [name] if name else []


def _android_language() -> str | None:
    """Dans l'appli Android : la langue du téléphone (Chaquopy relie Python à Java)."""
    if os.environ.get("AIODOS_ANDROID_APP") != "1":
        return None
    try:
        from java import jclass
        return str(jclass("java.util.Locale").getDefault().toLanguageTag())
    except Exception:
        return None


def _setting():
    """Le réglage « language », sans jamais créer ni migrer de base : avant que le nœud
    ouvre la sienne (ligne de commande, écran d'attente), une lecture seule si elle existe."""
    from aiodos import db, settings
    if db.is_open():
        try:
            return settings.get("language")
        except Exception:
            return None
    return _peek()


def _peek():
    import json
    from aiodos import paths
    path = paths.data_path() / "aiodos.db"
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='language'").fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
