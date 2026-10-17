"""Réciprocité souple (network/lend_queue.py) : mes modèles servent une conversation
empruntée à la fois ; celle de la machine qui m'a le plus donné passe d'abord, personne
n'est refusé."""
import threading
import time

import pytest

from aiodos.network import lend_queue, peers

GENEROUS, TAKER, NEW = "machine-genereuse", "machine-qui-prend", "machine-nouvelle"


@pytest.fixture(autouse=True)
def three_machines():
    for node_id, name in ((GENEROUS, "Généreuse"), (TAKER, "Qui prend"), (NEW, "Nouvelle")):
        peers.save(node_id, name, b"k" * 32, "127.0.0.1", 1, trust="friend")
    peers.account(GENEROUS, received_s=120.0, received_chats=2)     # elle m'a donné
    peers.account(TAKER, given_chats=5)                             # je lui ai donné


def test_balance_counts_compute_and_conversations():
    assert lend_queue.balance(GENEROUS) == 120 + 2 * lend_queue.CHAT_AS_SECONDS
    assert lend_queue.balance(TAKER) == -5 * lend_queue.CHAT_AS_SECONDS
    assert lend_queue.balance(NEW) == 0


def test_the_machine_that_gave_most_is_served_first():
    served, ready = [], threading.Event()
    first = lend_queue.turn(NEW)
    first.__enter__()                                   # une conversation en cours

    def wait_turn(node_id):
        with lend_queue.turn(node_id):
            served.append(node_id)

    threads = [threading.Thread(target=wait_turn, args=(n,)) for n in (TAKER, GENEROUS)]
    for thread in threads:                               # la moins généreuse arrive d'abord
        thread.start()
        time.sleep(0.1)
    deadline = time.time() + 5
    while lend_queue.waiting() < 2 and time.time() < deadline:
        time.sleep(0.02)
    first.__exit__(None, None, None)
    for thread in threads:
        thread.join(timeout=5)
    assert served == [GENEROUS, TAKER]


def test_waiting_too_long_or_leaving_frees_the_place():
    busy = lend_queue.turn(NEW)
    busy.__enter__()
    with pytest.raises(lend_queue.Busy):
        with lend_queue.turn(TAKER, max_wait=0.2):
            pass
    stop = threading.Event()
    stop.set()
    with pytest.raises(lend_queue.Cancelled):
        with lend_queue.turn(TAKER, stop=stop):
            pass
    assert lend_queue.waiting() == 0
    busy.__exit__(None, None, None)
    with lend_queue.turn(TAKER, max_wait=1):            # la place est libre
        pass
