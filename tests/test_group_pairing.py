"""Appairage par groupe (network/group.py) : un ami appairé avec une de mes machines
rencontre mes autres machines, avec son accord, sans code à comparer ; le niveau de
confiance ne monte jamais ; la machine qui présente ne connaît pas la clé de la nouvelle
paire ; retirer un ami le retire de toutes mes machines. De vrais nœuds sur cette machine
(réseau local simulé sur 127.0.0.1), puis les règles une à une, sans réseau."""
import os
import sqlite3

import pytest

from aiodos import db
from aiodos.network import client, crypto, envelope, group, peers, sharing
from aiodos.network.client import PeerError
from aiodos.node import vault
from tests.nodes import Node, pair


def _peers(node) -> dict:
    return {p["node_id"]: p for p in node.api("GET", "/network")["peers"]}


def _keys(node) -> list:
    """Clés des paires de ce nœud, lues dans sa base (même compte Windows : DPAPI)."""
    with sqlite3.connect(os.path.join(node.data_dir, "aiodos.db")) as conn:
        return [vault.unprotect(row[0]) for row in conn.execute("SELECT key FROM peers")]


def _offer(node, machine):
    """La présentation de `machine` affichée sur ce nœud, quand elle arrive."""
    return node.wait_for(lambda: next((o for o in node.api("GET", "/network")["introductions"]
                                       if o["machine"] == machine), None),
                         timeout=30, what=f"présentation de « {machine} »")


def _paired(node, other_id, trust="friend"):
    return node.wait_for(lambda: (_peers(node).get(other_id) or {}).get("trust") == trust,
                         timeout=30, what="nouvelle paire")


@pytest.fixture
def nodes(tmp_path):
    started = []

    def start(*names):
        for name in names:
            started.append(Node(tmp_path / str(len(started)), name).start())
        return started[-len(names):]
    yield start
    for node in started:
        node.stop()


def test_a_friend_meets_my_other_machines_with_her_consent(nodes):
    p, f, a = nodes("Portable", "PC fixe", "PC de Léa")
    _, f_id = pair(p, f)                                          # deux de mes machines
    p_id, a_id = pair(p, a, a_trusts="friend", b_trusts="friend")  # Léa, une amie
    # P la présente d'office à F ; chez Léa, la première fois, on lui demande
    offer = _offer(a, "PC fixe")
    assert offer["presenter"] == "Portable"
    a.api("POST", "/network/introductions/answer",
          {"intro": offer["intro"], "accept": True, "always": True})
    _paired(f, a_id)
    # Le niveau ne monte jamais : pour F, Léa est une amie ; pour Léa, F comme P
    assert _peers(f)[a_id]["introduced_by"] == p_id
    seen = _peers(a)[f_id]
    assert seen["trust"] == "friend" and seen["introduced_by"] == p_id
    # La nouvelle paire marche (F joint Léa avec la clé tirée de l'échange)…
    f.api("POST", f"/network/peers/{a_id}/refresh")
    assert f.wait_for(lambda: _peers(f)[a_id]["online"], what="Léa jointe par F")
    # … et P ne la connaît pas
    fa_key = next(k for k in _keys(f) if k not in _keys(p))
    assert fa_key in _keys(a) and fa_key not in _keys(p)
    assert a.api("GET", "/network")["introductions"] == []
    # Retirer Léa de F la retire aussi de P (mes autres machines), et Léa nous oublie
    f.api("DELETE", f"/network/peers/{a_id}")
    p.wait_for(lambda: a_id not in _peers(p), timeout=30, what="Léa retirée de P")
    a.wait_for(lambda: not {p_id, f_id} & set(_peers(a)), timeout=30,
               what="P et F retirés chez Léa")


def test_her_phone_meets_my_other_machines_too(nodes):
    """Dans les deux sens, de proche en proche : Léa a deux machines, moi aussi ; après un
    seul appairage (P–A) et une réponse de chaque côté, les quatre se connaissent."""
    p, f, a, t = nodes("Portable", "PC fixe", "PC de Léa", "Téléphone de Léa")
    _, f_id = pair(p, f)
    _, t_id = pair(a, t)
    pair(p, a, a_trusts="friend", b_trusts="friend")
    for node, machine in ((a, "PC fixe"), (p, "Téléphone de Léa")):
        offer = _offer(node, machine)
        node.api("POST", "/network/introductions/answer",
                 {"intro": offer["intro"], "accept": True, "always": True})
    # Le PC fixe et le téléphone de Léa se rencontrent sans autre question
    _paired(f, t_id)
    _paired(t, f_id)
    f.api("POST", f"/network/peers/{t_id}/refresh")
    assert f.wait_for(lambda: _peers(f)[t_id]["online"], what="téléphone joint par F")
    for node in (p, f, a, t):
        assert node.wait_for(lambda: node.api("GET", "/network")["introductions"] == [],
                             timeout=20, what="plus de question")


# ---------------------------------------------------------------- les règles
class Manager:
    node_id = "moi"

    def __init__(self):
        self.events = []
        self.presented = []
        self.group = None

    def name(self):
        return "Portable"

    def publish(self, event):
        self.events.append(event)

    def changed(self):
        pass

    def net_changed(self, removed=None):
        pass

    def on_paired(self, node_id):
        pass

    def present_later(self, friend_id, only=None):
        self.presented.append(friend_id)


@pytest.fixture
def mine(monkeypatch):
    db.connect()
    manager = Manager()
    manager.group = group.Group(manager)
    monkeypatch.setattr(group.Group, "_background", staticmethod(lambda f, *a: f(*a)))
    peers.save("pc-fixe", "PC fixe", b"f" * 32, "127.0.0.1", 1, sharing.MINE)
    peers.save("lea", "PC de Léa", b"a" * 32, "127.0.0.1", 2, sharing.FRIEND)
    return manager


CARD = {"node_id": "inconnu", "name": "Machine inconnue", "address": "127.0.0.1", "port": 9}


def _pub() -> str:
    return envelope.b64e(crypto.new_exchange()[1])


def _calls(monkeypatch, reply=None) -> list:
    sent = []
    monkeypatch.setattr(client, "call", lambda my_id, peer, action, payload, **k:
                        sent.append((peer["node_id"], action, payload)) or (reply or {"ok": True}))
    return sent


def test_only_my_machines_introduce_and_never_a_friend(mine):
    """Une machine d'ami ne me présente rien (pas d'amis d'amis) ; une de mes machines,
    oui ; une machine déjà connue n'est pas présentée de nouveau."""
    with pytest.raises(PeerError, match="une de mes machines"):
        mine.group.introduced("lea", {"intro": "x", "machine": CARD})
    assert "pub" in mine.group.introduced("pc-fixe", {"intro": "x", "machine": CARD})
    assert mine.group.introduced("pc-fixe", {"intro": "y", "machine": {
        **CARD, "node_id": "lea"}}) == {"known": True}


def test_a_key_relayed_by_another_machine_is_refused(mine):
    mine.group.introduced("pc-fixe", {"intro": "x", "machine": CARD})
    with pytest.raises(PeerError, match="inconnue ou expirée"):
        mine.group.keyed("lea", {"intro": "x", "pub": _pub()})
    mine.group.keyed("pc-fixe", {"intro": "x", "pub": _pub()})       # toujours valable
    assert peers.trust_of("inconnu") == sharing.FRIEND


def test_a_friend_is_presented_only_by_a_machine_that_trusts_her_as_one(mine):
    with pytest.raises(PeerError, match="la machine d'un ami"):
        mine.group.present("pc-fixe")
    peers.set_aside("lea", "essai")
    with pytest.raises(PeerError):
        mine.group.present("lea")                 # mise de côté : rien de plus


def test_a_presentation_waits_for_an_answer_and_can_be_accepted_for_good(mine, monkeypatch):
    reply = mine.group.proposed("lea", {"intro": "i1", "machine": CARD, "pub": _pub()})
    assert reply == {"pending": True}
    assert mine.events[-1]["type"] == "group_request"
    assert [o["machine"] for o in mine.group.offers()] == ["Machine inconnue"]
    sent = _calls(monkeypatch)
    mine.group.answer("i1", True, always=True)
    assert sent[0][:2] == ("lea", "group/answer") and sent[0][2]["accept"] is True
    # Au niveau donné à la machine qui présente (une amie), et présentée par elle
    assert peers.trust_of("inconnu") == sharing.FRIEND and group.group_ok("lea")
    assert group.introduced_by("inconnu") == "lea"
    # « Toujours » vaut aussi pour mes autres machines, et la nouvelle rencontre les miennes
    assert ("pc-fixe", "group/ok", {"node_ids": ["lea", "inconnu"]}) in sent
    assert mine.presented == ["inconnu"]
    # Désormais acceptée d'office
    second = {**CARD, "node_id": "autre", "name": "Autre machine"}
    assert mine.group.proposed("lea", {"intro": "i2", "machine": second,
                                       "pub": _pub()})["accept"]


def test_nothing_is_kept_when_the_presentation_does_not_finish(mine, monkeypatch):
    """La machine présentée n'a pas terminé (injoignable) : l'ami ne garde pas une paire
    à moitié faite, et le dit."""
    mine.group.proposed("lea", {"intro": "i1", "machine": CARD, "pub": _pub()})
    _calls(monkeypatch, reply={"ok": False})
    with pytest.raises(PeerError, match="n'a pas abouti"):
        mine.group.answer("i1", True)
    assert peers.trust_of("inconnu") is None and mine.presented == []


def test_a_pair_kept_at_once_is_undone_when_the_presenter_cancels(mine):
    group.set_group_ok("lea", True)
    assert mine.group.proposed("lea", {"intro": "i1", "machine": CARD, "pub": _pub()})["accept"]
    assert peers.trust_of("inconnu") == sharing.FRIEND
    mine.group.cancelled("pc-fixe", {"intro": "i1"})             # pas celle qui présentait
    assert peers.trust_of("inconnu") == sharing.FRIEND
    mine.group.cancelled("lea", {"intro": "i1"})
    assert peers.trust_of("inconnu") is None


@pytest.mark.parametrize("first", ["introduce", "propose"])
def test_crossing_presentations_of_the_same_pair_keep_the_same_one(mine, first):
    """La même paire présentée de chaque côté à la fois : quel que soit l'ordre d'arrivée,
    c'est la présentation de la machine au plus petit identifiant qui continue (« lea » <
    « pc-fixe ») ; l'autre machine de la paire applique la même règle."""
    by_mine = lambda: mine.group.introduced("pc-fixe", {"intro": "b", "machine": CARD})
    by_friend = lambda: mine.group.proposed("lea", {"intro": "a", "machine": CARD,
                                                    "pub": _pub()})
    if first == "introduce":
        assert "pub" in by_mine()
        assert by_friend() == {"pending": True}      # celle de « lea » l'emporte
    else:
        assert by_friend() == {"pending": True}
        assert by_mine() == {"busy": True}
    assert [o["intro"] for o in mine.group.offers()] == ["a"]
    with pytest.raises(PeerError):
        mine.group.keyed("pc-fixe", {"intro": "b", "pub": _pub()})


def test_a_known_pair_is_never_replaced(mine):
    with pytest.raises(PeerError, match="déjà appairée"):
        mine.group._store({"node_id": "lea", "name": "PC de Léa"}, b"x" * 32,
                          sharing.FRIEND, "pc-fixe")
    assert peers.get("lea")["key"] == b"a" * 32


def test_always_from_another_of_my_machines(mine, monkeypatch):
    """« Toujours accepter ce groupe » dit sur une autre de mes machines vaut ici, pour les
    questions déjà affichées aussi ; jamais demandé par une machine d'ami."""
    mine.group.proposed("lea", {"intro": "i1", "machine": CARD, "pub": _pub()})
    with pytest.raises(PeerError):
        group.group_accepted(mine, "lea", {"node_ids": ["lea"]})
    sent = _calls(monkeypatch)
    group.group_accepted(mine, "pc-fixe", {"node_ids": ["lea", "pc-fixe"]})
    assert group.group_ok("lea") and not group.group_ok("pc-fixe")
    assert sent[0][:2] == ("lea", "group/answer") and peers.trust_of("inconnu")
    assert mine.group.offers() == []
