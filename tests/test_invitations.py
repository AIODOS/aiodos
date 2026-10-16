"""Invitation à distance (étape 2.4) : les codes d'invitation et de réponse, et ce qu'ils
ouvrent, sans réseau. L'essai avec deux vrais compagnons est dans
tests/test_network_invitation.py."""
import time

import pytest

from aiodos import db
from aiodos.network import invitations, netinfo
from aiodos.network.invitations import InvitationError

KEY_A, KEY_B = "a" * 64, "b" * 64
NODE_A, NODE_B = "1" * 32, "2" * 32


class Machine:
    def __init__(self, node_id, name):
        self.node_id, self._name = node_id, name

    def name(self):
        return self._name


@pytest.fixture
def a(data_dir, monkeypatch):
    """Cette machine (A), compagnon en marche."""
    db.connect()
    monkeypatch.setattr(netinfo, "mine", lambda: {
        "id": KEY_A, "relays": ["https://relais.example/"], "addrs": ["192.0.2.1:4000"]})
    return Machine(NODE_A, "PC de Marc")


def reply_from_b(invite_code, monkeypatch) -> str:
    """Ce que la machine B renverrait : on joue B dans une base à part."""
    saved = netinfo.mine
    monkeypatch.setattr(netinfo, "mine", lambda: {"id": KEY_B, "relays": [], "addrs": ["198.51.100.2:5000"]})
    found = invitations.decode(invite_code)
    code = invitations._code("reply", Machine(NODE_B, "PC de Léa"), invitations._mine(),
                             found["secret"], found["expires"])
    monkeypatch.setattr(netinfo, "mine", saved)
    return code


def test_an_invitation_carries_the_key_and_a_one_time_secret(a):
    created = invitations.create(a)
    found = invitations.decode(created["code"])
    assert found["kind"] == "invite" and found["node_id"] == NODE_A
    assert found["name"] == "PC de Marc" and found["net"]["id"] == KEY_A
    assert found["net"]["relays"] == ["https://relais.example/"]
    assert len(found["secret"]) == 32 and found["expires"] > time.time() + 23 * 3600
    spaced = created["code"][:20] + "\n  " + created["code"][20:]   # collé avec des retours
    assert invitations.decode(spaced)["secret"] == found["secret"]
    assert invitations.snapshot()[0]["role"] == "sent"
    assert "secret" not in invitations.snapshot()[0]


@pytest.mark.parametrize("code", ["", "bonjour", "AIODOS1-", "AIODOS1-!!!!", "AIODOS1-e30",
                                  "AIODOS1-eyJ2IjoyfQ"])
def test_garbage_codes_are_refused(code):
    with pytest.raises(InvitationError):
        invitations.decode(code)


def test_the_invited_machine_admits_the_inviter_and_answers(a, monkeypatch):
    invite = invitations._code("invite", Machine(NODE_B, "PC de Léa"),
                               {"id": KEY_B, "relays": [], "addrs": []}, "c" * 32,
                               time.time() + 3600)
    answer = invitations.accept(a, invite)
    assert answer["name"] == "PC de Léa"
    reply = invitations.decode(answer["reply"])
    assert reply["kind"] == "reply" and reply["secret"] == "c" * 32
    assert reply["node_id"] == NODE_A and reply["net"]["id"] == KEY_A
    assert [r["net_id"] for r in invitations.allowlist_rows()] == [KEY_B]   # B admis ici
    assert invitations.admit(KEY_B, "c" * 32)["node_id"] == NODE_B
    assert invitations.admit(KEY_B, "d" * 32) is None                        # mauvais secret
    assert invitations.admit("e" * 64, "c" * 32) is None                     # autre clé
    assert invitations.accept(a, invite)["reply"]                            # recollée : même réponse


def test_wrong_codes_in_the_wrong_place_are_explained(a, monkeypatch):
    created = invitations.create(a)
    with pytest.raises(InvitationError, match="cette machine-ci"):
        invitations.accept(a, created["code"])                               # sa propre invitation
    reply = reply_from_b(created["code"], monkeypatch)
    with pytest.raises(InvitationError, match="réponse"):
        invitations.accept(a, reply)                                         # une réponse
    with pytest.raises(InvitationError, match="invitation"):
        invitations.answered(a, created["id"], created["code"])              # une invitation
    other = invitations.create(a)
    with pytest.raises(InvitationError, match="ne correspond pas"):
        invitations.answered(a, other["id"], reply)                          # autre invitation
    expired = invitations._code("invite", Machine(NODE_B, "B"), {"id": KEY_B, "relays": [],
                                                                  "addrs": []}, "f" * 32, time.time() - 1)
    with pytest.raises(InvitationError, match="périmée"):
        invitations.accept(a, expired)


def test_the_reply_admits_the_friend_until_the_invitation_is_used(a, monkeypatch):
    created = invitations.create(a)
    assert invitations.allowlist_rows() == []                  # personne tant qu'on n'a pas sa réponse
    target = invitations.answered(a, created["id"], reply_from_b(created["code"], monkeypatch))
    assert target["node_id"] == NODE_B and target["net_id"] == KEY_B
    assert target["hints"] == {"relays": [], "addrs": ["198.51.100.2:5000"]}
    assert [r["net_id"] for r in invitations.allowlist_rows()] == [KEY_B]
    assert invitations.snapshot()[0]["answered"] and invitations.snapshot()[0]["name"] == "PC de Léa"
    invitations.use(target["secret"])
    assert invitations.allowlist_rows() == [] and invitations.snapshot() == []
    assert invitations.admit(KEY_B, target["secret"]) is None                # a déjà servi
    with pytest.raises(InvitationError, match="déjà servi"):
        invitations.answered(a, created["id"], reply_from_b(created["code"], monkeypatch))


def test_cancelled_or_expired_invitations_open_nothing(a, monkeypatch):
    created = invitations.create(a)
    target = invitations.answered(a, created["id"], reply_from_b(created["code"], monkeypatch))
    db.execute("UPDATE invitations SET expires=? WHERE id=?", (time.time() - 1, created["id"]))
    assert invitations.allowlist_rows() == [] and invitations.admit(KEY_B, target["secret"]) is None
    other = invitations.create(a)
    assert invitations.cancel(other["id"]) and invitations.snapshot() == []


def test_no_invitation_without_the_internet_companion(data_dir, monkeypatch):
    db.connect()
    monkeypatch.setattr(netinfo, "mine", lambda: None)
    with pytest.raises(InvitationError, match="Internet"):
        invitations.create(Machine(NODE_A, "A"))
