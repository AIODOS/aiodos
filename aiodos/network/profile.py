"""Profil reçu d'une machine appairée (réponses à « profile » et à « ping ») : typé et
borné avant d'être gardé, affiché ou utilisé.

L'enveloppe scellée prouve QUI répond (la clé de la paire), pas CE qui est répondu : un ami,
ou une de mes machines compromise, peut y mettre n'importe quel JSON. Faille du 20/10/2026 :
un objet à la place d'une liste ({"length": "<img onerror=…>"}) devenait du HTML dans la vue
Réseau, et un NaN faisait tomber GET /api/network et GET /api/models (Starlette refuse les
nombres non finis). Ici, chaque clé connue a son type et ses bornes ; ce qui ne les respecte
pas est abandonné, jamais réinjecté, et ce qui n'est pas décrit ne passe pas. Dans les
chaînes, caractères de contrôle (C0 et C1), caractères invisibles de mise en forme (U+202E
qui retourne le texte, U+200B, U+2066 à U+2069…), séparateurs de ligne et chevrons deviennent
des espaces, et elles sont coupées : un ami ne maquille pas le nom de sa machine ou d'un
modèle dans les listes. Les listes sont tronquées. Rien n'est ajouté : un profil réduit (vu
par un ami) ou venu d'une version précédente le reste.

Une clé ajoutée au profil (node/identity.profile, network/p2p_app._profile) doit être
décrite dans SCHEMA, sinon les autres machines ne la voient pas : tests/test_securite_reseau.py
le vérifie sur un vrai profil.
"""
import itertools
import math
import re
import unicodedata

NAME = 60                         # nom d'une machine : comme à l'appairage (pairing.py)
BIG = 2 ** 62                     # octets, au-delà un nombre n'a plus de sens
MAX_MODELS = 300
_DROP = object()                  # valeur abandonnée
_UNSAFE = re.compile(r"[\x00-\x1f\x7f<>]")
_ID = re.compile(r"[0-9a-f]{32}")  # identifiant de machine (même forme que peers.valid_id)
# Un texte à traduire chez qui le lit (i18n.lazy) : la clé d'un catalogue, quelques paramètres
_KEY = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+")
_PARAM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
LAZY_PARAMS = 12
# Hors ASCII : contrôles C1 (Cc), mise en forme invisible (Cf : U+202E, U+200B, U+2066…),
# demi-codets isolés (Cs : illisibles en UTF-8, ils feraient échouer l'écriture en base) et
# séparateurs de ligne ou de paragraphe (Zl, Zp)
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


# ------------------------------------------------------------------- types
def _visible(text: str) -> str:
    """Le texte, chaque caractère de contrôle, invisible ou chevron remplacé par une espace
    (la longueur ne change pas)."""
    text = _UNSAFE.sub(" ", text)
    if text.isascii():
        return text
    return "".join(" " if unicodedata.category(ch) in _HIDDEN else ch for ch in text)


def _text(limit):
    def check(value):
        if not isinstance(value, str):
            return _DROP
        return _visible(value[:limit])
    return check


def _integer(low, high):
    """Un entier dans ses bornes ; un flottant entier (1.0) est accepté, un booléen non."""
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _DROP
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                return _DROP
            value = int(value)
        return value if low <= value <= high else _DROP
    return check


def _number(low, high):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _DROP
        if isinstance(value, float) and not math.isfinite(value):
            return _DROP
        return value if low <= value <= high else _DROP
    return check


def _flag(value):
    return value if isinstance(value, bool) else _DROP


def _optional(check):
    """None accepté : une valeur inconnue (charge d'un téléphone, version d'Ollama…)."""
    return lambda value: None if value is None else check(value)


def _node_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else _DROP


def _lazy(prefix: str):
    """Un texte à traduire chez qui le lit, [clé, {paramètres}] (i18n.lazy) : une clé de la
    forme de celles des catalogues, qui commence par `prefix` (seuls les textes que l'autre
    machine a des raisons d'envoyer), LAZY_PARAMS paramètres au plus, textes (nettoyés,
    coupés) ou nombres finis ; les autres paramètres sont abandonnés."""
    def check(value):
        if not isinstance(value, list) or len(value) != 2:
            return _DROP
        key, params = value
        if not isinstance(key, str) or len(key) > 100 or not _KEY.fullmatch(key) \
                or not key.startswith(prefix) or not isinstance(params, dict):
            return _DROP
        out = {}
        for name, item in itertools.islice(params.items(), LAZY_PARAMS):
            if not isinstance(name, str) or not _PARAM.fullmatch(name) or isinstance(item, bool):
                continue
            if isinstance(item, str):
                out[name] = _visible(item[:300])
            elif isinstance(item, (int, float)) and math.isfinite(item) and abs(item) <= BIG:
                out[name] = item
        return [key, out]
    return check


def _object(fields, required=()):
    """Seulement les clés décrites ; une clé requise absente ou vide abandonne l'objet."""
    def check(value):
        if not isinstance(value, dict):
            return _DROP
        out = {}
        for key, field in fields.items():
            if key in value:
                cleaned = field(value[key])
                if cleaned is not _DROP:
                    out[key] = cleaned
        if any(out.get(key) in (None, "") for key in required):
            return _DROP
        return out
    return check


def _items(check_item, limit):
    def check(value):
        if not isinstance(value, list):
            return _DROP
        out = []
        for item in value[:limit]:
            cleaned = check_item(item)
            if cleaned is not _DROP:
                out.append(cleaned)
        return out
    return check


def _mapping(check_key, check_value, limit):
    def check(value):
        if not isinstance(value, dict):
            return _DROP
        out = {}
        for key, item in itertools.islice(value.items(), limit):
            key, item = check_key(key), check_value(item)
            if key is not _DROP and key and item is not _DROP:
                out[key] = item
        return out
    return check


# ------------------------------------------------------------------ profil
_MODEL = _object({                # un modèle d'Ollama ou de llama.cpp (identity._models)
    "name": _text(200), "digest": _text(100), "size": _integer(0, BIG),
    "family": _text(80), "parameters": _text(40), "quantization": _text(40),
    "caps": _items(_text(40), 16), "fits_vram": _flag, "loaded": _flag,
}, required=("name",))

SCHEMA = _object({
    "node_id": _node_id,
    "name": _text(NAME),
    "version": _text(40),
    "os": _text(120),
    "cpu": _object({"name": _text(200), "threads": _optional(_integer(0, 1 << 20))}),
    "memory": _object({"total": _integer(0, BIG), "available": _integer(0, BIG)}),
    "gpus": _items(_object({"name": _text(200), "vram_total": _integer(0, BIG),
                            "vram_used": _integer(0, BIG),
                            "utilization": _integer(0, 100)}), 16),
    "on_ac_power": _optional(_flag),
    "power": _object({"plugged": _optional(_flag), "level": _optional(_number(0, 100)),
                      "temperature": _optional(_number(-100, 200)), "mobile": _flag,
                      "source": _optional(_text(20))}),
    "idle_seconds": _optional(_number(0, 1e10)),
    "ollama": _object({"version": _optional(_text(40)), "models": _items(_MODEL, MAX_MODELS),
                       "engine": _text(20)}),
    "lends_models": _flag,
    "stream_alive": _flag,
    "cluster": _object({"lends": _flag, "version": _optional(_text(80)),
                        "device": _optional(_text(200)), "free_mib": _integer(0, 1 << 40)}),
    "images": _object({"engine": _optional(_text(80)), "models": _items(_text(100), 200),
                       "shares": _flag}),
    "updates": _object({"offer": _optional(_text(40)),
                        "files": _mapping(_text(40), _items(_text(60), 32), 64)}),
    "consult": _object({"open": _integer(0, 10_000)}),          # remote_search.MAX_OPEN
    "load": _object({"chats": _integer(0, 1000), "units": _integer(0, 1000)}),
    # L'état de son partage (activity.field) : detail, dans la langue de cette machine-là ;
    # detail_t, le même à traduire ici (i18n.lazy ; une version d'avant n'en dit rien)
    "sharing": _object({"enabled": _flag, "active": _flag, "detail": _text(300),
                        "detail_t": _lazy("worker.")}),
    # Le ComfyUI accordé d'une de mes machines (engines/lending.summary)
    "engines": _object({"comfyui": _object({
        "version": _optional(_text(40)), "vram_mib": _optional(_integer(0, 1 << 30)),
        "models": _mapping(_text(60), _items(_text(300), 2000), 64)})}),
})


def clean(value) -> dict | None:
    """Le profil (ou les nouvelles d'un ping) réduit à ce qui a la forme attendue ; None
    s'il n'est même pas un objet."""
    cleaned = SCHEMA(value)
    return None if cleaned is _DROP else cleaned


def lazy(value, prefix: str) -> list | None:
    """Un texte à traduire reçu d'une autre machine ([clé, {paramètres}], i18n.lazy), typé et
    borné, sa clé commençant par `prefix` (_lazy) ; None sinon."""
    cleaned = _lazy(prefix)(value)
    return None if cleaned is _DROP else cleaned


def name(value) -> str | None:
    """Nom d'une machine reçu d'elle (profil, ping, balise) : nettoyé, sur une ligne ; None
    s'il est absent ou vide (on garde alors celui qu'on a)."""
    if not isinstance(value, str):
        return None
    return " ".join(_visible(value[:4 * NAME]).split())[:NAME] or None
