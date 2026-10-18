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
FIXE, LEA, NEW, OTHER = "f" * 32, "a" * 32, "1" * 32, "2" * 32


class Manager:
    node_id = "e" * 32

    def __init__(self):
        self.events = []
        self.presented = []
        self.removed = []
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

    def present_later(self, friend_id, only=None, delay=0):
        self.presented.append(friend_id)

    def remove_now(self, node_id):
        self.removed.append(node_id)
        peers.remove(node_id)


@pytest.fixture
def mine(monkeypatch):
    db.connect()
    manager = Manager()
    manager.group = group.Group(manager)
    monkeypatch.setattr(group.Group, "_background", staticmethod(lambda f, *a: f(*a)))
    peers.save(FIXE, "PC fixe", b"f" * 32, "127.0.0.1", 1, sharing.MINE)
    peers.save(LEA, "PC de Léa", b"a" * 32, "127.0.0.1", 2, sharing.FRIEND)
    return manager


CARD = {"node_id": NEW, "name": "Machine inconnue", "address": "127.0.0.1", "port": 9}


def _pub() -> str:
    return envelope.b64e(crypto.new_exchange()[1])


def _calls(monkeypatch, reply=None) -> list:
    sent = []
    monkeypatch.setattr(client, "call", lambda my_id, peer, action, payload, *a, **k:
                        sent.append((peer["node_id"], action, payload)) or (reply or {"ok": True}))
    return sent


def test_only_my_machines_introduce_and_never_a_friend(mine):
    """Une machine d'ami ne me présente rien (pas d'amis d'amis) ; une de mes machines,
    oui ; une machine déjà connue n'est pas présentée de nouveau."""
    with pytest.raises(PeerError, match="une de mes machines"):
        mine.group.introduced(LEA, {"intro": "x", "machine": CARD})
    assert "pub" in mine.group.introduced(FIXE, {"intro": "x", "machine": CARD})
    assert mine.group.introduced(FIXE, {"intro": "y", "machine": {
        **CARD, "node_id": LEA}}) == {"known": True}


def test_a_crafted_card_is_refused(mine, monkeypatch):
    """L'identifiant sert à composer des chemins (API, interface) : seul un identifiant de
    machine passe ; l'adresse ne peut être qu'une adresse IP, jamais un chemin."""
    for node_id in ("../../collections/3#", "PC", "f" * 31, "F" * 32, None, 42):
        with pytest.raises(PeerError, match="illisible"):
            mine.group.proposed(LEA, {"intro": "x", "machine": {**CARD, "node_id": node_id},
                                      "pub": _pub()})
    tricky = {**CARD, "address": "192.168.1.1/cgi-bin/reboot?", "port": 80}
    assert group._read_card(tricky)["address"] is None and group._read_card(tricky)["port"] is None
    assert group._read_card({**CARD, "address": "::1"})["address"] == "::1"
    for pub in (None, "", "AAAA", envelope.b64e(b"x" * 31)):
        with pytest.raises(PeerError, match="illisible"):
            mine.group.proposed(LEA, {"intro": "x", "machine": CARD, "pub": pub})
    # Une clé nulle : l'échange échoue, la présentation est refusée, rien n'est gardé
    sent = _calls(monkeypatch)
    mine.group.proposed(LEA, {"intro": "z", "machine": CARD, "pub": envelope.b64e(bytes(32))})
    mine.group.answer("z", True)
    assert peers.trust_of(NEW) is None and sent[-1][2]["accept"] is False


def test_a_key_relayed_by_another_machine_is_refused(mine):
    mine.group.introduced(FIXE, {"intro": "x", "machine": CARD})
    with pytest.raises(PeerError, match="inconnue ou expirée"):
        mine.group.keyed(LEA, {"intro": "x", "pub": _pub()})
    mine.group.keyed(FIXE, {"intro": "x", "pub": _pub()})       # toujours valable
    assert peers.trust_of(NEW) == sharing.FRIEND


def test_a_friend_is_presented_only_by_a_machine_that_trusts_her_as_one(mine):
    with pytest.raises(PeerError, match="la machine d'un ami"):
        mine.group.present(FIXE)
    peers.set_aside(LEA, "essai")
    with pytest.raises(PeerError):
        mine.group.present(LEA)                  # mise de côté : rien de plus


def test_a_presentation_waits_for_an_answer_and_can_be_accepted_for_good(mine, monkeypatch):
    reply = mine.group.proposed(LEA, {"intro": "i1", "machine": CARD, "pub": _pub()})
    assert reply == {"pending": True}
    assert mine.events[-1]["type"] == "group_request"
    assert [o["machine"] for o in mine.group.offers()] == ["Machine inconnue"]
    sent = _calls(monkeypatch)
    mine.group.answer("i1", True, always=True)
    assert sent[0][:2] == (LEA, "group/answer") and sent[0][2]["accept"] is True
    # Au niveau donné à la machine qui présente (une amie), et présentée par elle
    assert peers.trust_of(NEW) == sharing.FRIEND and group.group_ok(LEA)
    assert group.introduced_by(NEW) == LEA
    # « Toujours » vaut aussi pour mes autres machines, et la nouvelle rencontre les miennes
    assert (FIXE, "group/ok", {"node_ids": [LEA, NEW]}) in sent
    assert mine.presented == [NEW]
    # Désormais acceptée d'office
    second = {**CARD, "node_id": OTHER, "name": "Autre machine"}
    assert mine.group.proposed(LEA, {"intro": "i2", "machine": second,
                                     "pub": _pub()})["accept"]


def test_always_accept_has_a_daily_limit(mine):
    """« Toujours accepter » ne laisse pas une machine en faire entrer sans fin : au-delà
    de 8 par jour, on demande de nouveau."""
    group.set_group_ok(LEA, True)
    for i in range(group.AUTO_LIMIT):
        card = {**CARD, "node_id": f"{i + 1:032x}", "name": f"Machine {i}"}
        assert mine.group.proposed(LEA, {"intro": f"i{i}", "machine": card, "pub": _pub()})["accept"]
    card = {**CARD, "node_id": "9" * 32, "name": "Une de trop"}
    assert mine.group.proposed(LEA, {"intro": "trop", "machine": card, "pub": _pub()}) == {
        "pending": True}


def test_one_machine_cannot_fill_the_questions(mine):
    for i in range(group.MAX_OFFERS_EACH):
        card = {**CARD, "node_id": f"{i + 1:032x}"}
        assert mine.group.proposed(LEA, {"intro": f"i{i}", "machine": card, "pub": _pub()})["pending"]
    with pytest.raises(PeerError, match="Trop de présentations"):
        mine.group.proposed(LEA, {"intro": "x", "machine": {**CARD, "node_id": "9" * 32},
                                  "pub": _pub()})


def test_a_refusal_or_a_removal_is_not_undone_by_a_presentation(mine, monkeypatch):
    """Refusée ici, retirée ici (même « seulement de cette machine »), ou retirée sur une
    autre de mes machines : aucune présentation ne la ramène ; un appairage par code, si."""
    mine.group.proposed(LEA, {"intro": "i1", "machine": CARD, "pub": _pub()})
    _calls(monkeypatch, reply={"ok": False})
    mine.group.answer("i1", False)
    assert group.declined(NEW)
    assert mine.group.proposed(LEA, {"intro": "i2", "machine": CARD, "pub": _pub()}) == {
        "accept": False}
    assert mine.group.introduced(FIXE, {"intro": "i3", "machine": CARD}) == {"declined": True}
    # Retirée sur une autre de mes machines : ici aussi, et pour de bon
    group.removed(mine, FIXE, {"node_id": LEA})
    assert mine.removed == [LEA] and group.declined(LEA)
    group.undecline(NEW)                         # nouvel appairage par code
    assert "pub" in mine.group.introduced(FIXE, {"intro": "i4", "machine": CARD})


def test_nothing_is_kept_when_the_presentation_does_not_finish(mine, monkeypatch):
    """La machine présentée n'a pas terminé (injoignable) : l'ami ne garde pas une paire
    à moitié faite, le dit, et prévient l'autre côté au cas où sa réponse se serait perdue."""
    mine.group.proposed(LEA, {"intro": "i1", "machine": CARD, "pub": _pub()})
    sent = _calls(monkeypatch, reply={"ok": False})
    with pytest.raises(PeerError, match="n'a pas abouti"):
        mine.group.answer("i1", True)
    assert peers.trust_of(NEW) is None and mine.presented == []
    assert (NEW, "unpair", {}) in sent


def test_a_pair_kept_at_once_is_undone_when_the_presenter_cancels(mine, monkeypatch):
    group.set_group_ok(LEA, True)
    assert mine.group.proposed(LEA, {"intro": "i1", "machine": CARD, "pub": _pub()})["accept"]
    assert peers.trust_of(NEW) == sharing.FRIEND
    _calls(monkeypatch)
    mine.group.cancelled(FIXE, {"intro": "i1"})             # pas celle qui présentait
    assert peers.trust_of(NEW) == sharing.FRIEND
    mine.group.cancelled(LEA, {"intro": "i1"})
    assert peers.trust_of(NEW) is None


@pytest.mark.parametrize("first", ["introduce", "propose"])
def test_crossing_presentations_of_the_same_pair_keep_the_same_one(mine, first):
    """La même paire présentée de chaque côté à la fois : quel que soit l'ordre d'arrivée,
    c'est la présentation de la machine au plus petit identifiant qui continue (celle de
    Léa, « aaa… » < « fff… ») ; l'autre machine de la paire applique la même règle."""
    by_mine = lambda: mine.group.introduced(FIXE, {"intro": "b", "machine": CARD})
    by_friend = lambda: mine.group.proposed(LEA, {"intro": "a", "machine": CARD,
                                                  "pub": _pub()})
    if first == "introduce":
        assert "pub" in by_mine()
        assert by_friend() == {"pending": True}      # celle de Léa l'emporte
    else:
        assert by_friend() == {"pending": True}
        assert by_mine() == {"busy": True}
    assert [o["intro"] for o in mine.group.offers()] == ["a"]
    with pytest.raises(PeerError):
        mine.group.keyed(FIXE, {"intro": "b", "pub": _pub()})


def test_a_known_pair_is_never_replaced(mine):
    with pytest.raises(PeerError, match="déjà appairée"):
        mine.group._store({"node_id": LEA, "name": "PC de Léa"}, b"x" * 32,
                          sharing.FRIEND, FIXE)
    assert peers.get(LEA)["key"] == b"a" * 32


def test_always_from_another_of_my_machines(mine, monkeypatch):
    """« Toujours accepter ce groupe » dit sur une autre de mes machines vaut ici, pour les
    questions déjà affichées aussi ; jamais demandé par une machine d'ami."""
    mine.group.proposed(LEA, {"intro": "i1", "machine": CARD, "pub": _pub()})
    with pytest.raises(PeerError):
        group.group_accepted(mine, LEA, {"node_ids": [LEA]})
    sent = _calls(monkeypatch)
    group.group_accepted(mine, FIXE, {"node_ids": [LEA, FIXE]})
    assert group.group_ok(LEA) and not group.group_ok(FIXE)
    assert sent[0][:2] == (LEA, "group/answer") and peers.trust_of(NEW)
    assert mine.group.offers() == []


def test_a_removal_my_machine_missed_is_told_when_it_comes_back(mine, monkeypatch):
    def offline(my_id, peer, action, payload, *a, **k):
        raise PeerError("injoignable")
    monkeypatch.setattr(client, "call", offline)
    assert mine.group.spread("group/remove", {"node_id": LEA}) == ["PC fixe"]
    sent = _calls(monkeypatch)
    mine.group.flush(FIXE)
    assert sent == [(FIXE, "group/remove", {"node_id": LEA})]
    mine.group.flush(FIXE)                       # dit une fois
    assert len(sent) == 1
