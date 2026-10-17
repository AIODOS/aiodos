"""Séparation (manager.unpair) : l'autre machine est prévenue, sans que ce soit exigé,
qu'on la joigne sur le réseau local ou seulement par Internet (appairée sur invitation,
étape 2.4 : une clé Internet, pas d'adresse). Pas de vrai réseau ici : requests.post et
le compagnon sont remplacés par des faux (essai réel : tests/test_network_invitation.py)."""
import asyncio

import pytest
import requests

from aiodos.network import envelope, peers, route
from aiodos.network.manager import NetworkManager

MY_ID, PEER_ID = "a" * 32, "b" * 32
KEY = bytes(range(32))
NET_ID = "c" * 64


class Bus:
    def publish(self, event):
        pass


class FakeCompanion:
    running = True
    headers = {"X-Aiodos-Net-Token": "jeton"}

    def proxy_base(self, net_id):
        return f"http://127.0.0.1:47800/peer/{net_id}"


class Reply:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def companion():
    fake = FakeCompanion()
    route.attach(fake)
    yield fake
    route.detach(fake)


@pytest.fixture
def sent(monkeypatch):
    """Requêtes parties vers l'autre machine, qui accuse réception de chacune."""
    urls = []

    def fake_post(url, json=None, headers=None, proxies=None, timeout=None, stream=False):
        urls.append(url)
        return Reply(envelope.wrap_reply(KEY, "unpair", PEER_ID, json["from"], json["ts"],
                                         {"ok": True}))
    monkeypatch.setattr(requests, "post", fake_post)
    return urls


def _unpair(node_id):
    asyncio.run(NetworkManager(MY_ID, Bus()).unpair(node_id))


def test_a_machine_paired_by_invitation_is_told_through_the_companion(companion, sent):
    peers.save(PEER_ID, "PC de Léa", KEY, None, None, trust="friend")
    peers.set_net(PEER_ID, NET_ID, {"relays": [], "addrs": []})
    _unpair(PEER_ID)
    assert sent == [f"http://127.0.0.1:47800/peer/{NET_ID}/p2p/v1/unpair"]
    assert peers.get(PEER_ID) is None


def test_a_machine_on_the_lan_is_told_as_before(sent):
    peers.save(PEER_ID, "PC fixe", KEY, "192.168.1.20", 47701)
    _unpair(PEER_ID)
    assert sent == ["http://192.168.1.20:47701/p2p/v1/unpair"]
    assert peers.get(PEER_ID) is None


def test_a_machine_reachable_in_no_way_is_removed_without_a_message(companion, sent):
    peers.save(PEER_ID, "PC sans adresse", KEY, None, None)          # ni adresse, ni clé
    _unpair(PEER_ID)
    assert sent == [] and peers.get(PEER_ID) is None


def test_a_silent_machine_does_not_prevent_the_separation(companion, monkeypatch):
    peers.save(PEER_ID, "PC de Léa", KEY, None, None, trust="friend")
    peers.set_net(PEER_ID, NET_ID, {"relays": [], "addrs": []})

    def silent(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("aucune réponse")
    monkeypatch.setattr(requests, "post", silent)
    _unpair(PEER_ID)
    assert peers.get(PEER_ID) is None
