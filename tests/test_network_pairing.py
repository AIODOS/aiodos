"""Deux vrais nœuds s'appairent, échangent des messages scellés, puis se séparent."""
import base64

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


def test_a_pairing_in_progress_is_neither_replaced_nor_answered_blindly(two_nodes):
    """Engagement avant révélation, demande jamais remplacée, réponse liée à la demande vue,
    et une machine sans engagement (2.0.1) refusée clairement : par le vrai serveur réseau."""
    a, b = two_nodes
    a_id = a.api("GET", "/network")["node_id"]
    b_id = b.api("GET", "/network")["node_id"]
    b.api("POST", "/network/add", {"address": f"127.0.0.1:{a.p2p_port}"})
    started = b.api("POST", "/network/pair", {"node_id": a_id})
    assert started["ref"] and started["address"] == "127.0.0.1" and started["known"] is None
    request = a.wait_for(lambda: a.api("GET", "/network")["pairing"], what="demande reçue")[0]
    assert request["code"] == started["code"] and request["address"] == "127.0.0.1"

    pair_start = f"http://127.0.0.1:{a.p2p_port}/p2p/v1/pair/start"
    # Une autre demande au nom de B (même identifiant) : refusée, la première reste
    commit = base64.b64encode(bytes(32)).decode()          # un engagement bien formé
    again = requests.post(pair_start, json={"pairing": 2, "step": "commit", "node_id": b_id,
                                            "name": "PC fixe", "port": 1, "commit": commit},
                          timeout=5)
    assert again.status_code == 400 and "déjà un appairage en cours" in again.json()["detail"]
    assert a.api("GET", "/network")["pairing"] == [request]
    # Une machine d'avant l'engagement : clé envoyée tout de suite, refus qui dit quoi faire
    old = requests.post(pair_start, json={"node_id": "f" * 32, "name": "Vieux PC", "port": 1,
                                          "pub": "A" * 44}, timeout=5)
    assert old.status_code == 400 and "Mets AIODOS à jour" in old.json()["detail"]

    # La réponse vise la demande montrée : une autre « ref » est refusée
    wrong = a.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": True,
                                                    "ref": "0" * 16}, expect=400)
    assert "a changé" in wrong["detail"]
    b.api("POST", "/network/pair/answer", {"node_id": a_id, "accept": True,
                                           "ref": started["ref"]})
    a.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": True,
                                           "ref": request["ref"]})
    assert _peer_ids(a) == [b_id]
    b.wait_for(lambda: _peer_ids(b) == [a_id], what="appairage côté B")


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
