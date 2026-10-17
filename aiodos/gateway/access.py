"""Clé d'accès des autres outils (Open WebUI, Continue, assistants MCP…).

Contrairement au jeton de l'interface, tiré à chaque lancement (api/security.py), elle
reste la même d'un lancement à l'autre : on la copie une fois dans l'outil. AIODOS la
crée (256 bits tirés au hasard) ; elle se renouvelle dans les Réglages, et l'ancienne
cesse aussitôt de marcher. Gardée dans la base mais hors des réglages ordinaires
(settings.DEFAULTS) : PUT /api/settings ne peut ni la lire ni la remplacer.
"""
import json
import secrets

from aiodos import db

SETTING = "gateway_key"
PREFIX = "aiodos-"


def _stored() -> str | None:
    row = db.query_one("SELECT value FROM settings WHERE key=?", (SETTING,))
    return json.loads(row["value"]) if row else None


def key() -> str:
    """La clé, créée au premier besoin."""
    current = _stored()
    if current:
        return current
    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
               (SETTING, json.dumps(_new())))
    return _stored()


def existing() -> str | None:
    """La clé si elle a déjà été créée (pont stdio : il ne la crée pas)."""
    return _stored()


def renew() -> str:
    new = _new()
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (SETTING, json.dumps(new)))
    return new


def check(given) -> bool:
    if not given:
        return False
    return secrets.compare_digest(str(given).encode("utf-8", "replace"), key().encode("utf-8"))


def _new() -> str:
    return PREFIX + secrets.token_urlsafe(32)
