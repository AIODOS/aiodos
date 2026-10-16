"""Clés Internet des machines appairées (compagnon aiodos-net).

Chaque machine dont le compagnon tourne a une clé publique, son adresse sur Internet,
et des indices pour la joindre (relais, adresses directes). Les machines appairées se
les échangent par un message scellé avec la clé de la paire (action « net/info ») :
seule la vraie machine peut annoncer sa clé, et chacune met la clé de l'autre dans la
liste blanche de son compagnon. L'échange a lieu sur le chemin habituel (le réseau
local, à l'appairage), puis toutes les 5 minutes et dès que les infos changent.

Une clé qui change (dossier de données perdu, compagnon réinstallé) ne passe que par le
réseau local : par Internet, le compagnon de l'autre machine ne connaît que l'ancienne
et refuse la nouvelle dès la poignée de main.
"""
import ipaddress
import json
import re

from aiodos.network import peers, route

EXCHANGE_EVERY = 300           # secondes entre deux échanges avec une même machine
MAX_HINTS = 8
_KEY = re.compile(r"^[0-9a-f]{64}$")


def mine() -> dict | None:
    """Infos Internet de cette machine, si son compagnon tourne."""
    comp = route.companion()
    if comp is None or not comp.running:
        return None
    return comp.info()


def _address(text) -> str | None:
    """« 1.2.3.4:5678 » ou « [2001:db8::1]:5678 », sinon None."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    try:
        ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
    except ValueError:
        return None
    if ":" in host and not host.startswith("["):
        return None
    return text


def _relay(text) -> str | None:
    if not isinstance(text, str) or len(text) > 200 or any(c.isspace() for c in text):
        return None
    return text if text.startswith(("https://", "http://")) else None


def clean(info) -> dict | None:
    """Infos reçues d'une autre machine, vérifiées : une clé de 64 caractères
    hexadécimaux, et quelques indices bien formés (les autres sont ignorés)."""
    if not isinstance(info, dict):
        return None
    key = str(info.get("id") or "").strip().lower()
    if not _KEY.match(key):
        return None
    relays = [r for r in map(_relay, info.get("relays") or []) if r][:MAX_HINTS]
    addrs = [a for a in map(_address, info.get("addrs") or []) if a][:MAX_HINTS]
    return {"id": key, "relays": relays, "addrs": addrs}


def remember(node_id, info) -> bool:
    """Enregistre les infos Internet d'une machine appairée ; vrai si elles ont changé."""
    cleaned = clean(info)
    if cleaned is None:
        return False
    return peers.set_net(node_id, cleaned["id"],
                         {"relays": cleaned["relays"], "addrs": cleaned["addrs"]})


def handle(manager, sender, payload):
    """Action scellée « net/info » : l'appelant donne ses infos, on rend les nôtres."""
    if remember(sender, (payload or {}).get("net")):
        manager.net_changed()
    return {"net": mine()}


def allowlist(rows) -> list:
    """Liste blanche du compagnon : les machines appairées dont on connaît la clé."""
    entries = []
    for row in rows:
        if not row.get("net_id"):
            continue
        try:
            hints = json.loads(row.get("net_addrs") or "{}")
        except ValueError:
            hints = {}
        entries.append({"id": row["net_id"], "relays": list(hints.get("relays") or []),
                        "addrs": list(hints.get("addrs") or [])})
    return entries
