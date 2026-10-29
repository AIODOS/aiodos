"""Activité en direct avec mes machines appairées, pour la liste « Machines connectées »
(à la façon de la liste des pairs d'un client BitTorrent) : qui est en ligne, par quel
chemin et avec quel temps de réponse, qui prête sa puissance, et ce que chacune fait pour
l'autre en ce moment (unités calculées, conversations servies par un modèle prêté).

Chaque machine joint à ses nouvelles (ping toutes les 15 s, profil) l'état de son partage :
à mes machines, l'état du moment et sa raison (« en pause : sur batterie ») ; à un ami,
seulement s'il est activé, car un ami ne voit pas l'activité de cette machine
(sharing.FRIEND_PROFILE, placement.load_field).

Le chantier en direct (essaim.md § 13, étape 7) y ajoute les doublons de fin de partie
qu'une machine calcule pour moi (jobs/endgame.py), ses places et combien sont prises
(network/places_seen.py), et ce que cette machine fait avec le réseau public (public_live) :
les conversations qu'elle lui sert, celles qu'elle lui fait calculer, par leur modèle et
leur durée seulement, jamais leur texte ni une clé.

À mes machines seulement, les nouvelles disent aussi la clé de cette machine sur le réseau
public (« public_key », quand elle l'a rejoint) : la liste unique des machines
(machines_view.py) y reconnaît une de mes machines vue aussi dans l'annuaire, et ne la
montre qu'une fois. Jamais à un ami : sa clé publique ne relie pas un inconnu à une machine
appairée (docs/conception/reseau-public-calcul.md). Gardée en mémoire, jamais en base.
"""
import threading
import time
from collections import Counter
from contextlib import contextmanager

from aiodos import db, settings
from aiodos.jobs.models import RUNNING
from aiodos.network import peers, places_seen, profile, public_lending, route, sharing

_lock = threading.Lock()
_heard = {}                  # machine -> {"sharing": …, "rtt_ms": …, "at": …}
_chats = {"for": Counter(),  # conversations d'une machine servies par mes modèles
          "by": Counter()}   # mes conversations servies par les modèles d'une machine


def field(manager, sender) -> dict:
    """{"sharing": …} à joindre aux nouvelles de cette machine (ping, profil). Sa raison :
    detail, dans la langue de cette machine (pour une version d'avant), et detail_t, la même
    à traduire chez celle qui la lit (i18n.lazy, worker.sharing_state)."""
    enabled = bool(settings.get("share_enabled"))
    if sharing.level(sender) != sharing.MINE:
        return {"sharing": {"enabled": enabled}}
    worker = getattr(manager, "worker", None)
    state = getattr(worker, "state", None) or {}
    found = {"enabled": enabled, "active": bool(state.get("active")),
             "detail": str(state.get("detail") or "")}
    if isinstance(state.get("detail_t"), list):
        found["detail_t"] = state["detail_t"]
    out = {"sharing": found}
    from aiodos.network import public          # à la demande : il importe public_lending
    key = profile.public_key(public.seen()[0])  # réseau public rejoint : sa clé (cf. le début)
    if key:
        out["public_key"] = key
    return out


def note(node_id: str, reply: dict, rtt_ms: float | None = None):
    """Nouvelles reçues d'une machine appairée : l'état de son partage s'il y est (une
    version précédente n'en dit rien), le temps de réponse d'un ping, et sa clé du réseau
    public (une de mes machines qui l'a rejoint ; absente : oubliée)."""
    with _lock:
        heard = _heard.setdefault(node_id, {})
        if isinstance(reply.get("sharing"), dict):
            heard["sharing"] = reply["sharing"]
        key = profile.public_key(reply.get("public_key"))
        if key:
            heard["public_key"] = key
        else:
            heard.pop("public_key", None)
        if rtt_ms is not None:
            heard["rtt_ms"] = round(rtt_ms, 1)
        heard["at"] = time.time()


def public_keys() -> dict:
    """{machine appairée : sa clé du réseau public}, telle qu'elle l'a dite (note), pour la
    liste unique des machines (machines_view.py). Mes machines seulement (field)."""
    with _lock:
        return {node_id: heard["public_key"] for node_id, heard in _heard.items()
                if heard.get("public_key")}


def forget(node_id: str):
    with _lock:
        _heard.pop(node_id, None)
    places_seen.forget(node_id)


@contextmanager
def chat(direction: str, node_id: str, stop=None):
    """Le temps d'une conversation servie pour cette machine (« for ») ou par elle (« by ») ;
    une réponse menée à son terme s'ajoute au compte des échanges (peers.account). Arrêtée
    en route (stop levé : bouton Stop, machine partie), elle ne compte pas."""
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
    if stop is not None and stop.is_set():
        return
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


def _doubling_for_me() -> dict:
    """{machine: [{"title", "units"}]} : les doublons de fin de partie qu'elle calcule pour
    moi en ce moment (jobs/endgame.py ; ceux d'ici n'ont pas de bail)."""
    out = {}
    for r in db.query("SELECT uc.node, j.title, COUNT(*) AS n FROM unit_copies uc "
                      "JOIN units u ON u.id = uc.unit_id JOIN jobs j ON j.id = u.job_id "
                      "WHERE u.status=? AND uc.lease_until IS NOT NULL "
                      "GROUP BY uc.node, u.job_id ORDER BY n DESC", (RUNNING,)):
        out.setdefault(r["node"], []).append({"title": r["title"], "units": r["n"]})
    return out


# ------------------------------------------------------------- réseau public
def _asked_public() -> dict:
    """{modèle : réponses en cours} : mes conversations qu'une machine du réseau public
    calcule en ce moment, d'après le compte de placement (placement.sending : le modèle exact
    de chaque envoi, même chez un prêteur qui en annonce plusieurs ; lu, jamais changé)."""
    from aiodos.network import placement, public_chat     # à la demande : ils importent celui-ci
    asking = {}
    for target, models in placement.sending().items():
        if not (isinstance(target, str) and target.startswith(public_chat.PREFIX)):
            continue
        for model, n in models.items():
            parsed = public_chat.parse(model)
            if parsed is not None and n > 0:
                asking[parsed[1]] = asking.get(parsed[1], 0) + n
    return asking


def public_live() -> dict:
    """Ce que cette machine fait avec le réseau public en ce moment, pour « Machines
    connectées » : « serving », les conversations qu'elle sert à des inconnus
    (public_lending.serving : modèle, début) ; « asking », celles qu'elle leur fait
    calculer (modèle, nombre). Jamais le texte, jamais une clé."""
    return {"serving": public_lending.serving(),
            "asking": [{"model": model, "n": n} for model, n in sorted(_asked_public().items())]}


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
    helping, doubling = _helping_me(), _doubling_for_me()
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
            "copies_for_me": doubling.get(node_id, []),  # … et des doublons de fin de partie
            "places": places_seen.get(node_id),          # ses places, dites en demandant du travail
            "for_it": for_them.get(node_id, []),         # je calcule pour elle
            "chats_for_me": chats_by.get(node_id, 0),    # mes conversations, par ses modèles
            "chats_for_it": chats_for.get(node_id, 0),   # les siennes, par mes modèles
        }
