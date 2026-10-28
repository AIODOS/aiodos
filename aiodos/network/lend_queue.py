"""File d'attente des conversations que mes modèles servent pour d'autres machines
(lending.py) : une à la fois, la carte graphique n'en répond qu'une ; quand plusieurs
attendent, celles de mes machines passent d'abord, puis celle de la machine qui m'a le plus
donné, puis la plus ancienne.

Réciprocité souple (décision de l'auteur, 18/10/2026) : personne n'est refusé, ceux qui
participent passent devant. Ce qu'une machine m'a donné se lit dans le compte des échanges
(peers.account) : son temps de calcul pour moi et les conversations auxquelles ses modèles
ont répondu pour moi, moins ce que je lui ai donné. Le solde compté est borné (BALANCE_CAP) :
un gros contributeur passe devant, sans pouvoir écarter les autres indéfiniment.

Une conversation qui attend reçoit un signe de vie toutes les quelques secondes (wait,
tick) : la machine qui la demande peut partir (Cancelled ici), et l'attente se termine en
« occupée » (Busy, MAX_WAIT_S) avant que sa propre patience (10 minutes) ne s'épuise.

Le réseau public (public_lending.py, 28/10/2026) vient après mes machines et mes amis
(PUBLIC, rang le plus bas) et n'attend jamais : sa conversation prend la place tout de suite
(take_now) quand personne n'est servi ni n'attend, sinon elle est refusée (« occupée »). Une
conversation de mes machines ou de mes amis qui arrive pendant la sienne l'arrête (elle
regarde waiting()).
"""
import heapq
import itertools
import threading
import time
from contextlib import contextmanager

from aiodos.network import peers, sharing

CHAT_AS_SECONDS = 30            # une conversation servie compte comme 30 s de calcul
BALANCE_CAP = 1800              # solde compté, au plus, dans un sens comme dans l'autre
MAX_WAIT_S = 540                # avant les 600 s d'attente de lecture de la machine qui demande
AT_ONCE = 1
# Rang des conversations d'inconnus du réseau public : jamais un identifiant de machine
PUBLIC = "réseau public"        # i18n: tel quel (jamais montré)

_cond = threading.Condition()
_serving = 0
_waiting = []                   # tas de (clé, rang d'arrivée, ticket)
_order = itertools.count()


class Busy(Exception):
    pass


class Cancelled(Exception):
    pass


class Ticket:
    """Une place dans la file (enter), servie (wait), puis rendue (leave)."""
    __slots__ = ("entry", "deadline", "serving")


def balance(node_id: str) -> float:
    """Ce que cette machine m'a donné, moins ce que je lui ai donné (en secondes de calcul)."""
    row = next((p for p in peers.list_peers() if p["node_id"] == node_id), None) or {}
    gave_me = row.get("received_s", 0) + CHAT_AS_SECONDS * row.get("received_chats", 0)
    got = row.get("given_s", 0) + CHAT_AS_SECONDS * row.get("given_chats", 0)
    return float(gave_me - got)


def _key(node_id: str) -> tuple:
    """Mes machines d'abord (la réciprocité vaut entre amis), puis le solde, borné ; le
    réseau public en dernier."""
    if node_id == PUBLIC:
        return (2, 0.0)
    clamped = max(-BALANCE_CAP, min(BALANCE_CAP, balance(node_id)))
    return (0 if sharing.level(node_id) == sharing.MINE else 1, -clamped)


def waiting() -> int:
    with _cond:
        return len(_waiting)


def idle() -> bool:
    """Personne n'est servi, personne n'attend."""
    with _cond:
        return _serving == 0 and not _waiting


def take_now(node_id: str) -> Ticket | None:
    """La place servie tout de suite, sans attendre : seulement si personne n'est servi ni
    n'attend (le réseau public passe après tout le monde) ; None sinon. Rendue par leave."""
    global _serving
    key = _key(node_id)
    with _cond:
        if _serving >= AT_ONCE or _waiting:
            return None
        ticket = Ticket()
        ticket.entry = (key, next(_order), ticket)
        ticket.deadline = time.monotonic()
        ticket.serving = True
        _serving += 1
        return ticket


def enter(node_id: str, max_wait: float = MAX_WAIT_S) -> Ticket:
    ticket = Ticket()
    ticket.entry = (_key(node_id), next(_order), ticket)
    ticket.deadline = time.monotonic() + max_wait
    ticket.serving = False
    with _cond:
        heapq.heappush(_waiting, ticket.entry)
    return ticket


def _drop(ticket: Ticket):
    """Retire de la file une place qui n'est pas servie (sous _cond)."""
    try:
        _waiting.remove(ticket.entry)
        heapq.heapify(_waiting)
    except ValueError:
        pass
    _cond.notify_all()


def wait(ticket: Ticket, stop=None, tick: float | None = None) -> bool:
    """Attend son tour : True quand il est venu (la place est prise) ; False au bout de
    `tick` secondes sans y être (le temps d'un signe de vie). Busy si l'attente dépasse le
    délai, Cancelled si la machine est partie (stop) : la place est alors rendue."""
    global _serving
    end = None if tick is None else time.monotonic() + tick
    with _cond:
        while not (_serving < AT_ONCE and _waiting and _waiting[0][2] is ticket):
            if stop is not None and stop.is_set():
                _drop(ticket)
                raise Cancelled()
            now = time.monotonic()
            remaining = ticket.deadline - now
            if remaining <= 0:
                _drop(ticket)
                raise Busy()
            if end is not None and now >= end:
                return False
            _cond.wait(timeout=min(1.0, remaining, end - now if end is not None else 1.0))
        heapq.heappop(_waiting)
        _serving += 1
        ticket.serving = True
        return True


def position(ticket: Ticket) -> int:
    """Combien passent avant elle (0 : la prochaine)."""
    with _cond:
        ahead = sorted(_waiting)
        try:
            return ahead.index(ticket.entry)
        except ValueError:
            return 0


def leave(ticket: Ticket):
    """Rend la place, servie ou non (toujours appelé, même après une erreur)."""
    global _serving
    with _cond:
        if ticket.serving:
            ticket.serving = False
            _serving -= 1
            _cond.notify_all()
        else:
            _drop(ticket)


@contextmanager
def turn(node_id: str, stop=None, max_wait: float = MAX_WAIT_S):
    """Le temps de servir une conversation de cette machine, à son tour. Busy si l'attente
    dépasse max_wait, Cancelled si elle a abandonné (stop)."""
    ticket = enter(node_id, max_wait)
    try:
        wait(ticket, stop)
        yield
    finally:
        leave(ticket)
