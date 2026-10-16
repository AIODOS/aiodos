"""Clés Internet des machines appairées : échange scellé « net/info », liste blanche du
compagnon, et requêtes arrivées par le compagnon (en-tête X-Aiodos-Peer)."""
import json

import pytest
from fastapi.testclient import TestClient

from aiodos.network import envelope, netinfo, peers
from aiodos.network.p2p_app import create_p2p_app

MY_ID, PEER_ID = "a" * 32, "b" * 32
KEY = bytes(range(32))
NET = "d" * 64


class FakeManager:
    node_id = MY_ID
    p2p_port = 47701

    def __init__(self):
        self.guard = envelope.ReplayGuard()
        self.net_changes = 0

    def name(self):
        return "Ici"

    def net_changed(self):
        self.net_changes += 1

    def changed(self):
        pass


@pytest.fixture
def server():
    peers.save(PEER_ID, "Là-bas", KEY, "192.168.1.20", 47701)
    manager = FakeManager()
    return TestClient(create_p2p_app(manager)), manager


def _sealed(client, action, payload, headers=None):
    env = envelope.wrap(KEY, action, PEER_ID, MY_ID, payload)
    response = client.post(f"/p2p/v1/{action}", json=env, headers=headers or {})
    if response.status_code != 200:
        return response.status_code, response.json()
    return 200, envelope.unwrap_reply(KEY, action, MY_ID, PEER_ID, env["ts"], response.json())


def test_clean_keeps_only_well_formed_information():
    info = netinfo.clean({"id": NET.upper(), "relays": ["https://relay.example.org./", "ftp://x",
                                                        "https://a b"],
                          "addrs": ["1.2.3.4:5", "[2001:db8::1]:6", "2001:db8::1:6", "x:1",
                                    "1.2.3.4:70000", 12]})
    assert info == {"id": NET, "relays": ["https://relay.example.org./"],
                    "addrs": ["1.2.3.4:5", "[2001:db8::1]:6"]}
    assert netinfo.clean({"id": "pas une clé"}) is None
    assert netinfo.clean(None) is None


def test_keys_are_exchanged_by_a_sealed_message(server):
    client, manager = server
    status, reply = _sealed(client, "net/info", {"net": {"id": NET, "addrs": ["1.2.3.4:5"]}})
    assert status == 200 and reply == {"net": None}       # pas de compagnon ici
    stored = peers.get(PEER_ID)
    assert stored["net_id"] == NET and stored["net_seen"]
    assert json.loads(stored["net_addrs"]) == {"addrs": ["1.2.3.4:5"], "relays": []}
    assert manager.net_changes == 1
    # Les mêmes infos une seconde fois : rien à changer dans la liste blanche
    _sealed(client, "net/info", {"net": {"id": NET, "addrs": ["1.2.3.4:5"]}})
    assert manager.net_changes == 1
    assert netinfo.allowlist(peers.net_list()) == [
        {"id": NET, "relays": [], "addrs": ["1.2.3.4:5"]}]


def test_requests_through_the_companion(server):
    client, _ = server
    _sealed(client, "net/info", {"net": {"id": NET}})
    peers.touch(PEER_ID, "192.168.1.20")                # adresse connue sur le réseau local
    # Par le compagnon, avec la bonne clé : accepté, et l'adresse 127.0.0.1 n'est pas retenue
    status, _ = _sealed(client, "ping", {}, headers={"X-Aiodos-Peer": NET})
    assert status == 200
    assert peers.get(PEER_ID)["address"] == "192.168.1.20"
    # Une autre clé Internet que celle de la machine qui a scellé : refusé
    status, body = _sealed(client, "ping", {}, headers={"X-Aiodos-Peer": "e" * 64})
    assert status == 403 and "Clé Internet" in body["detail"]
    # Par le réseau local, l'adresse vue reste retenue comme avant
    status, _ = _sealed(client, "ping", {})
    assert status == 200 and peers.get(PEER_ID)["address"] == "testclient"


def test_unknown_machines_cannot_announce_a_key(server):
    client, _ = server
    env = envelope.wrap(bytes(32), "net/info", "intrus", MY_ID, {"net": {"id": NET}})
    assert client.post("/p2p/v1/net/info", json=env).status_code == 403
    assert peers.get(PEER_ID)["net_id"] is None
