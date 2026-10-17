"""File d'attente des conversations que mes modèles servent pour d'autres machines
(lending.py) : une à la fois, la carte graphique n'en répond qu'une ; quand plusieurs
attendent, celle de la machine qui m'a le plus donné passe d'abord, puis la plus ancienne.

Réciprocité souple (décision de l'auteur, 18/10/2026) : personne n'est refusé, ceux qui
participent passent devant. Ce qu'une machine m'a donné se lit dans le compte des échanges
(peers.account) : son temps de calcul pour moi et les conversations auxquelles ses modèles
ont répondu pour moi, moins ce que je lui ai donné.
"""
import heapq
import itertools
import threading
import time
from contextlib import contextmanager

from aiodos.network import peers

CHAT_AS_SECONDS = 30            # une conversation servie compte comme 30 s de calcul
MAX_WAIT_S = 600
AT_ONCE = 1

_cond = threading.Condition()
_serving = 0
_waiting = []                   # tas de (-solde, rang d'arrivée, jeton)
_order = itertools.count()


class Busy(Exception):
    pass


class Cancelled(Exception):
    pass


def balance(node_id: str) -> float:
    """Ce que cette machine m'a donné, moins ce que je lui ai donné (en secondes de calcul)."""
    row = next((p for p in peers.list_peers() if p["node_id"] == node_id), None) or {}
    gave_me = row.get("received_s", 0) + CHAT_AS_SECONDS * row.get("received_chats", 0)
    got = row.get("given_s", 0) + CHAT_AS_SECONDS * row.get("given_chats", 0)
    return float(gave_me - got)


def waiting() -> int:
    with _cond:
        return len(_waiting)


@contextmanager
def turn(node_id: str, stop=None, max_wait: float = MAX_WAIT_S):
    """Le temps de servir une conversation de cette machine, à son tour. Busy si l'attente
    dépasse max_wait, Cancelled si elle a abandonné (stop)."""
    global _serving
    ticket = object()
    entry = (-balance(node_id), next(_order), ticket)
    deadline = time.monotonic() + max_wait
    with _cond:
        heapq.heappush(_waiting, entry)
        try:
            while not (_serving < AT_ONCE and _waiting[0][2] is ticket):
                if stop is not None and stop.is_set():
                    raise Cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Busy()
                _cond.wait(timeout=min(1.0, remaining))
        except BaseException:
            _waiting.remove(entry)
            heapq.heapify(_waiting)
            _cond.notify_all()
            raise
        heapq.heappop(_waiting)
        _serving += 1
    try:
        yield
    finally:
        with _cond:
            _serving -= 1
            _cond.notify_all()
