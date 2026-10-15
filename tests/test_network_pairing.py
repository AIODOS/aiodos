"""Deux vrais nœuds s'appairent, échangent des messages scellés, puis se séparent."""
import pytest
import requests

from tests.nodes import Node


@pytest.fixture
def two_nodes(tmp_path):
    a = Node(tmp_path / "a", "PC portable").start()
    b = Node(tmp_path / "b", "PC fixe").start()
    yield a, b
    a.stop()
    b.stop()


def _peer_ids(node):
    return [p["node_id"] for p in node.api("GET", "/network")["peers"]]


def test_pairing_requires_both_sides_and_matching_codes(two_nodes):
    a, b = two_nodes
    a_id = a.api("GET", "/network")["node_id"]
    b_id = b.api("GET", "/network")["node_id"]

    # B trouve A par son adresse puis lance l'appairage
    info = b.api("POST", "/network/add", {"address": f"127.0.0.1:{a.p2p_port}"})
    assert info["node_id"] == a_id and info["name"] == "PC portable"
    code = b.api("POST", "/network/pair", {"node_id": a_id})["code"]

    # A voit la demande, avec le même code
    request = a.wait_for(lambda: a.api("GET", "/network")["pairing"], what="demande reçue")[0]
    assert request["node_id"] == b_id and request["code"] == code and request["role"] == "responder"

    # Accepter d'un seul côté ne suffit pas
    b.api("POST", "/network/pair/answer", {"node_id": a_id, "accept": True})
    assert _peer_ids(a) == [] and _peer_ids(b) == []

    a.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": True})
    assert _peer_ids(a) == [b_id]
    b.wait_for(lambda: _peer_ids(b) == [a_id], what="appairage côté B")

    # Message scellé : B demande le profil de A
    snapshot = b.api("POST", f"/network/peers/{a_id}/refresh")
    peer = snapshot["peers"][0]
    assert peer["online"] and peer["profile"]["node_id"] == a_id

    # Une machine non appairée est refusée par le serveur réseau
    intrus = requests.post(f"http://127.0.0.1:{a.p2p_port}/p2p/v1/profile",
                           json={"from": "intrus", "to": a_id, "ts": 0, "box": "AAAA"}, timeout=5)
    assert intrus.status_code == 403

    # Séparation : B oublie A et le prévient
    b.api("DELETE", f"/network/peers/{a_id}")
    assert _peer_ids(b) == []
    a.wait_for(lambda: _peer_ids(a) == [], what="séparation côté A")


def test_refusal_on_one_side_cancels_pairing(two_nodes):
    a, b = two_nodes
    a_id = a.api("GET", "/network")["node_id"]
    b_id = b.api("GET", "/network")["node_id"]
    b.api("POST", "/network/add", {"address": f"127.0.0.1:{a.p2p_port}"})
    b.api("POST", "/network/pair", {"node_id": a_id})
    a.wait_for(lambda: a.api("GET", "/network")["pairing"], what="demande reçue")
    a.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": False})
    assert a.api("GET", "/network")["pairing"] == []
    b.wait_for(lambda: b.api("GET", "/network")["pairing"] == [], what="refus transmis")
    assert _peer_ids(a) == [] and _peer_ids(b) == []
