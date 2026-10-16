"""Choix du chemin vers une machine appairée : réseau local s'il répond, compagnon sinon.

Le réseau local d'abord : le plus rapide, et rien ne sort de la maison. Quand il ne
répond pas (machine partie en 4G, chez un ami…), la requête passe par le compagnon
aiodos-net (Internet, en direct ou par un relais) avec la même enveloppe scellée : si
elle était quand même arrivée, l'autre machine la refuserait comme rejouée, rien n'est
traité deux fois. Seul un échec avant l'envoi (connexion impossible) fait changer de
chemin ; il est retenu une minute, pour ne pas payer son délai à chaque requête.

Le réglage p2p_route force un chemin : « lan » (réseau local seulement) ou « internet »
(compagnon seulement, pour l'essayer). Un appairage en cours passe par le réseau local,
ou par le compagnon s'il a commencé sur invitation (étape 2.4, cf. invitations.py).
"""
import threading
import time
from dataclasses import dataclass, field

from aiodos import settings

MODES = ("auto", "lan", "internet")
LAN_RETRY_AFTER = 60       # après un échec du réseau local, on passe par Internet pendant 1 min
LAN_CONNECT_TIMEOUT = 3    # sur un réseau local, une connexion s'établit en quelques ms
INTERNET_MIN_TIMEOUT = 30  # première connexion par un relais en 4G : plusieurs secondes
                           # (le compagnon abandonne au bout de 20 s, avec un message)
BEST_EFFORT_BELOW = 10     # délai plus court demandé : appel « sans l'exiger », pas rallongé
NO_PROXY = {"http": None, "https": None}    # le compagnon est sur 127.0.0.1 : jamais de mandataire

_lock = threading.Lock()
_companion = None          # compagnon actif (aiodos.network.companion.Companion)
_lan_down = {}             # machine -> instant (monotonic) jusqu'auquel éviter le réseau local


@dataclass(frozen=True)
class Path:
    kind: str                          # « lan » ou « internet »
    base: str                          # début des URL, sans /p2p/…
    headers: dict = field(default_factory=dict)
    proxies: dict | None = None

    @property
    def label(self) -> str:
        return "réseau local" if self.kind == "lan" else "Internet"


def attach(companion):
    global _companion
    with _lock:
        _companion = companion


def detach(companion=None):
    global _companion
    with _lock:
        if companion is None or _companion is companion:
            _companion = None


def companion():
    return _companion


def mode() -> str:
    value = str(settings.get("p2p_route") or "auto")
    return value if value in MODES else "auto"


def _host(address: str) -> str:
    return f"[{address}]" if ":" in address and not address.startswith("[") else address


def paths_for(peer: dict, pairing: bool = False) -> list:
    """Chemins à essayer, dans l'ordre, pour joindre cette machine."""
    lan = None
    if peer.get("address") and peer.get("port"):
        lan = Path("lan", f"http://{_host(str(peer['address']))}:{int(peer['port'])}")
    net = None
    comp = _companion
    if comp is not None and comp.running and peer.get("net_id"):
        net = Path("internet", comp.proxy_base(peer["net_id"]), comp.headers, NO_PROXY)
    if pairing:                           # le chemin par lequel l'appairage a commencé
        return [lan] if lan else [net] if net else []
    chosen = mode()
    if chosen == "lan":
        return [lan] if lan else []
    if chosen == "internet":
        return [net] if net else []
    if lan and net:
        return [net] if lan_is_down(peer.get("node_id")) else [lan, net]
    return [p for p in (lan, net) if p]


def lan_failed(node_id):
    with _lock:
        _lan_down[node_id] = time.monotonic() + LAN_RETRY_AFTER


def lan_ok(node_id):
    with _lock:
        _lan_down.pop(node_id, None)


def lan_is_down(node_id) -> bool:
    with _lock:
        until = _lan_down.get(node_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            del _lan_down[node_id]
            return False
        return True


def timeouts(path: Path, timeout: float, fallback: bool) -> tuple:
    """(connexion, lecture) pour ce chemin : connexion courte au réseau local quand un
    autre chemin attend derrière ; lecture plus longue par Internet (relais, 4G), sauf
    pour un appel qui demande lui-même un délai court (prévenir sans l'exiger)."""
    if path.kind == "internet":
        if timeout < BEST_EFFORT_BELOW:
            return 5, timeout
        return 5, max(timeout, INTERNET_MIN_TIMEOUT)
    if fallback:
        return min(LAN_CONNECT_TIMEOUT, timeout), timeout
    return timeout, timeout
