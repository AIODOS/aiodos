"""Activité en direct avec mes machines appairées, pour la liste « Machines connectées »
(à la façon de la liste des pairs d'un client BitTorrent) : qui est en ligne, par quel
chemin et avec quel temps de réponse, qui prête sa puissance, et ce que chacune fait pour
l'autre en ce moment (unités calculées, conversations servies par un modèle prêté).

Chaque machine joint à ses nouvelles (ping toutes les 15 s, profil) l'état de son partage :
à mes machines, l'état du moment et sa raison (« en pause : sur batterie ») ; à un ami,
seulement s'il est activé, car un ami ne voit pas l'activité de cette machine
(sharing.FRIEND_PROFILE, placement.load_field).
"""
import threading
import time
from collections import Counter
from contextlib import contextmanager

from aiodos import db, settings
from aiodos.jobs.models import RUNNING
from aiodos.network import peers, route, sharing

_lock = threading.Lock()
_heard = {}                  # machine -> {"sharing": …, "rtt_ms": …, "at": …}
_chats = {"for": Counter(),  # conversations d'une machine servies par mes modèles
          "by": Counter()}   # mes conversations servies par les modèles d'une machine


def field(manager, sender) -> dict:
    """{"sharing": …} à joindre aux nouvelles de cette machine (ping, profil)."""
    enabled = bool(settings.get("share_enabled"))
    if sharing.level(sender) != sharing.MINE:
        return {"sharing": {"enabled": enabled}}
    worker = getattr(manager, "worker", None)
    state = getattr(worker, "state", None) or {}
    return {"sharing": {"enabled": enabled, "active": bool(state.get("active")),
                        "detail": str(state.get("detail") or "")}}


def note(node_id: str, reply: dict, rtt_ms: float | None = None):
    """Nouvelles reçues d'une machine appairée : l'état de son partage s'il y est (une
    version précédente n'en dit rien), et le temps de réponse d'un ping."""
    with _lock:
        heard = _heard.setdefault(node_id, {})
        if isinstance(reply.get("sharing"), dict):
            heard["sharing"] = reply["sharing"]
        if rtt_ms is not None:
            heard["rtt_ms"] = round(rtt_ms, 1)
        heard["at"] = time.time()


def forget(node_id: str):
    with _lock:
        _heard.pop(node_id, None)


@contextmanager
def chat(direction: str, node_id: str):
    """Le temps d'une conversation servie pour cette machine (« for ») ou par elle (« by ») ;
    une réponse menée à son terme s'ajoute au compte des échanges (peers.account)."""
    with _lock:
        _chats[direction][node_id] += 1
    try:
        yield
    finally:
        with _lock:
            _chats[direction][node_id] -= 1
            if _chats[direction][node_id] <= 0:
                del _chats[direction][node_id]
    # Seulement sans erreur ni interruption (une exception ne passe pas par ici)
    if direction == "for":
        peers.account(node_id, given_chats=1)
    else:
        peers.account(node_id, received_chats=1)


def _helping_me() -> dict:
    """{machine: [{"title", "units"}]} : les unités qu'elle calcule pour moi en ce moment."""
    out = {}
    for r in db.query("SELECT u.node, j.title, COUNT(*) AS n FROM units u "
                      "JOIN jobs j ON j.id = u.job_id WHERE u.status=? AND u.node IS NOT NULL "
                      "AND u.lease_until IS NOT NULL GROUP BY u.node, u.job_id ORDER BY n DESC",
                      (RUNNING,)):
        out.setdefault(r["node"], []).append({"title": r["title"], "units": r["n"]})
    return out


def _route(peer: dict) -> str | None:
    """Chemin vers une machine en ligne : réseau local, Internet en direct, ou par un relais."""
    if not peer.get("online"):
        return None
    if route.lan_first(peer):
        return "lan"
    inet = peer.get("internet") or {}
    if inet.get("connected"):
        return "direct" if inet.get("path") == "direct" else "relay"
    return "internet"


def annotate(paired: list, working: list | None):
    """Ajoute « live » à chaque machine appairée (après l'état Internet, qu'il utilise).
    `working` : les unités que cette machine calcule pour d'autres (Worker.snapshot)."""
    helping = _helping_me()
    for_them = {}
    for item in working or []:
        for_them.setdefault(item.get("peer"), []).append(item.get("label") or "")
    with _lock:
        heard = {node_id: dict(item) for node_id, item in _heard.items()}
        chats_for, chats_by = dict(_chats["for"]), dict(_chats["by"])
    for peer in paired:
        node_id = peer["node_id"]
        seen = heard.get(node_id, {})
        inet = peer.get("internet") or {}
        peer["live"] = {
            "route": _route(peer),
            "rtt_ms": seen.get("rtt_ms") if peer.get("online") else None,
            "internet_rtt_ms": inet.get("rtt_ms") if inet.get("connected") else None,
            "sharing": seen.get("sharing"),
            "for_me": helping.get(node_id, []),          # elle calcule pour moi
            "for_it": for_them.get(node_id, []),         # je calcule pour elle
            "chats_for_me": chats_by.get(node_id, 0),    # mes conversations, par ses modèles
            "chats_for_it": chats_for.get(node_id, 0),   # les siennes, par mes modèles
        }
