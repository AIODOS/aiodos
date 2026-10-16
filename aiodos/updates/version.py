"""Numéros de version : « 2.0.1 », ou « 2.1.0-dev » pour le code en cours d'écriture,
qui précède la version 2.1.0. Un numéro sert aussi de nom de dossier
(data/updates/versions/2.0.1) : seuls des chiffres, des points et « -dev » sont admis."""
import re

_PATTERN = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-dev(\d{0,4}))?")


def key(text) -> tuple:
    """Clé de comparaison : 2.0.0-dev < 2.0.0-dev2 < 2.0.0 < 2.0.1. ValueError si illisible."""
    found = _PATTERN.fullmatch(text) if isinstance(text, str) else None
    if not found:
        raise ValueError(f"Numéro de version illisible : {text!r}")
    major, minor, patch, dev = found.groups()
    return int(major), int(minor), int(patch), int(dev is None), int(dev or 0)


def valid(text) -> bool:
    return isinstance(text, str) and _PATTERN.fullmatch(text) is not None


def newer(a, b) -> bool:
    """`a` est-elle plus récente que `b` ? Un numéro illisible ne l'est jamais ;
    tout numéro lisible l'est plus que rien (`b` absent ou illisible)."""
    if not valid(a):
        return False
    if not valid(b):
        return True
    return key(a) > key(b)


def newest(*versions):
    """La plus récente des versions lisibles, ou None."""
    readable = [v for v in versions if valid(v)]
    return max(readable, key=key) if readable else None
