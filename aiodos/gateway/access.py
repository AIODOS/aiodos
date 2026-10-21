"""Clé générale des autres outils (Open WebUI, Continue, assistants MCP…).

Contrairement au jeton de l'interface, tiré à chaque lancement (api/security.py), elle
reste la même d'un lancement à l'autre : on la copie une fois dans l'outil. AIODOS la
crée (256 bits tirés au hasard) quand on ouvre AIODOS aux autres outils (gateway_enabled),
plus d'office ; elle se renouvelle dans les Réglages, et l'ancienne cesse aussitôt de
marcher. Gardée dans la base mais hors des réglages ordinaires (settings.DEFAULTS) :
PUT /api/settings ne peut ni la lire ni la remplacer. Protégée par DPAPI sous Windows
(node/vault.py) : tout de suite pour une clé créée par cette version, une fois la version
jugée saine pour une clé d'avant (settings.protect_secrets, PROTECTED_ELSEWHERE).

Les applications, elles, ont chacune leur clé (gateway/apps.py).
"""
import json
import logging
import secrets

from aiodos import db
from aiodos.node import vault

log = logging.getLogger("aiodos.gateway")

SETTING = "gateway_key"
PREFIX = "aiodos-"


def _stored() -> str | None:
    row = db.query_one("SELECT value FROM settings WHERE key=?", (SETTING,))
    if not row:
        return None
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return vault.unprotect_text(value)
    except vault.Unreadable as exc:            # base venue d'un autre compte : à renouveler
        log.warning("Clé générale de la passerelle illisible ici (%s) : à renouveler", exc)
        return None


def key() -> str:
    """La clé, créée au premier besoin."""
    current = _stored()
    if current:
        return current
    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
               (SETTING, json.dumps(vault.protect_text(_new()))))
    return _stored() or renew()              # clé illisible ici (autre compte) : remplacée


def existing() -> str | None:
    """La clé si elle a déjà été créée (pont stdio, état de la passerelle : ils ne la
    créent pas)."""
    return _stored()


def renew() -> str:
    new = _new()
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (SETTING, json.dumps(vault.protect_text(new))))
    return new


def check(given) -> bool:
    if not given:
        return False
    current = _stored()
    if not current:
        return False
    return secrets.compare_digest(str(given).encode("utf-8", "replace"), current.encode("utf-8"))


def _new() -> str:
    return PREFIX + secrets.token_urlsafe(32)
