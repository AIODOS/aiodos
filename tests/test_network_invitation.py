"""Invitation à distance (étape 2.4), avec deux vrais nœuds et leurs compagnons en essai
local (--internet-local) : ils ne se connaissent pas sur le réseau local (pas de
découverte, pas d'ajout par adresse), et s'appairent uniquement par Internet, sur
invitation ; la séparation passe aussi par Internet. Il faut le compagnon compilé."""
import pytest

from aiodos.network import companion
from tests.nodes import Node, pair_by_invitation

pytestmark = pytest.mark.skipif(companion.find_binary() is None,
                                reason="compagnon aiodos-net non compilé (voir compagnon/README.md)")


@pytest.fixture
def two_strangers(tmp_path):
    a = Node(tmp_path / "a", "PC de Marc", ["--internet-local"]).start()
    b = Node(tmp_path / "b", "PC de Léa", ["--internet-local"]).start()
    yield a, b
    a.stop()
    b.stop()


def _ready(node):
    return node.wait_for(lambda: (s := node.api("GET", "/network/internet"))["running"] and s,
                         what="compagnon prêt")


def _peers(node):
    return node.api("GET", "/network")["peers"]


def _seen_by_companion(node, other) -> dict:
    """Ce que le compagnon de `node` sait de la machine `other` (liste blanche, connexion)."""
    key = other.api("GET", "/network/internet")["id"]
    known = node.api("GET", "/network/internet")["companion"]["peers"]
    return next((p for p in known if p["id"] == key), {})


def test_a_friend_far_away_pairs_by_invitation_only(two_strangers):
    a, b = two_strangers
    _ready(a), _ready(b)
    a_id, b_id = a.node_id, b.node_id

    invitation = a.api("POST", "/network/invitations")
    assert invitation["code"].startswith("AIODOS1-")
    assert a.api("GET", "/network")["invitations"][0]["answered"] is False
    # Collée de travers : expliqué, rien ne bouge
    assert "réponse" not in b.api("POST", "/network/invitations/accept",
                                  {"code": "AIODOS1-abc"}, expect=400)["detail"].lower()
    reply = b.api("POST", "/network/invitations/accept", {"code": invitation["code"]})
    assert reply["name"] == "PC de Marc"

    started = a.api("POST", f"/network/invitations/{invitation['id']}/reply",
                    {"code": reply["reply"]})
    assert started["node_id"] == b_id and len(started["code"]) == 6
    pending = b.wait_for(lambda: b.api("GET", "/network")["pairing"], what="demande chez B")
    assert pending[0]["node_id"] == a_id and pending[0]["code"] == started["code"]

    b.api("POST", "/network/pair/answer", {"node_id": a_id, "accept": True, "trust": "friend"})
    a.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": True, "trust": "friend"})
    for node, other_id in ((a, b_id), (b, a_id)):
        peer = node.wait_for(lambda: [p for p in _peers(node) if p["node_id"] == other_id
                                      and p["online"]], timeout=40, what="appairés")[0]
        assert peer["trust"] == "friend" and peer["net_id"] and not peer["address"]
        assert node.api("GET", "/network")["invitations"] == []            # a servi
    # Plus tard, les nouvelles passent par Internet : profil reçu par le compagnon
    profile = a.wait_for(lambda: next(p for p in _peers(a) if p["node_id"] == b_id).get("profile"),
                         timeout=40, what="profil de B par Internet")
    assert profile["name"] == "PC de Léa" and "memory" not in profile       # B voit A en ami

    # Une invitation déjà utilisée n'ouvre plus rien
    assert b.api("POST", "/network/invitations/accept", {"code": invitation["code"]},
                 expect=400)["detail"].startswith("Cette invitation a déjà servi")


def test_a_friend_far_away_is_told_of_the_separation(two_strangers):
    """A ne connaît de B que sa clé Internet (pas d'adresse sur le réseau local) : elle la
    prévient quand même de la séparation, par les compagnons, et chacune retire l'autre
    de la liste blanche du sien."""
    a, b = two_strangers
    _, b_id = pair_by_invitation(a, b)
    assert not next(p for p in _peers(a) if p["node_id"] == b_id)["address"]
    for node, other in ((a, b), (b, a)):
        node.wait_for(lambda: _seen_by_companion(node, other).get("allowed"),
                      what="admise dans la liste blanche")

    a.api("DELETE", f"/network/peers/{b_id}")
    assert _peers(a) == []
    b.wait_for(lambda: _peers(b) == [], what="séparation prévenue chez B")
    for node, other in ((a, b), (b, a)):
        node.wait_for(lambda: not _seen_by_companion(node, other).get("allowed"),
                      what="retirée de la liste blanche")
