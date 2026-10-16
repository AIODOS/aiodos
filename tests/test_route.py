"""Choix du chemin vers une machine appairée : réseau local s'il répond, compagnon sinon.

Pas de vrai réseau ici : requests.post est remplacé par un faux qui simule un réseau
local muet, un compagnon qui répond, une lecture trop longue…
"""
import pytest
import requests
import urllib3

from aiodos import settings
from aiodos.network import client, envelope, route
from aiodos.network.companion import Companion

MY_ID, PEER_ID = "a" * 32, "b" * 32
KEY = bytes(range(32))
NET_ID = "c" * 64


class FakeCompanion:
    running = True
    headers = {"X-Aiodos-Net-Token": "jeton"}

    def proxy_base(self, net_id):
        return f"http://127.0.0.1:47800/peer/{net_id}"


class Reply:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def peer():
    return {"node_id": PEER_ID, "key": KEY, "address": "192.168.1.20", "port": 47701,
            "net_id": NET_ID}


@pytest.fixture
def companion():
    fake = FakeCompanion()
    route.attach(fake)
    yield fake
    route.detach(fake)
    route._lan_down.clear()


def _no_connection():
    return requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(
        None, "/", urllib3.exceptions.NewConnectionError(None, "refusée")))


class FakeNetwork:
    """requests.post simulé : chaque chemin (« lan », « internet ») répond ou échoue."""

    def __init__(self, lan="ok", internet="ok"):
        self.behaviour = {"lan": lan, "internet": internet}
        self.calls = []

    def __call__(self, url, json=None, headers=None, proxies=None, timeout=None, stream=False):
        kind = "internet" if "/peer/" in url else "lan"
        self.calls.append((kind, url, headers, timeout))
        outcome = self.behaviour[kind]
        if outcome == "down":
            raise _no_connection()
        if outcome == "slow":
            raise requests.exceptions.ReadTimeout("trop long")
        action = url.split("/p2p/v1/", 1)[1]
        return Reply(envelope.wrap_reply(KEY, action, PEER_ID, MY_ID, json["ts"],
                                         {"via": kind}))


def test_lan_first_when_it_answers(monkeypatch, peer, companion):
    network = FakeNetwork()
    monkeypatch.setattr(requests, "post", network)
    assert client.call(MY_ID, peer, "ping", {}) == {"via": "lan"}
    kind, url, _, timeout = network.calls[0]
    assert (kind, url) == ("lan", "http://192.168.1.20:47701/p2p/v1/ping")
    assert timeout[0] == route.LAN_CONNECT_TIMEOUT        # un autre chemin attend derrière


def test_companion_when_the_lan_is_silent_and_remembered(monkeypatch, peer, companion):
    network = FakeNetwork(lan="down")
    monkeypatch.setattr(requests, "post", network)
    assert client.call(MY_ID, peer, "ping", {}) == {"via": "internet"}
    assert [c[0] for c in network.calls] == ["lan", "internet"]
    kind, url, headers, timeout = network.calls[1]
    assert url == f"http://127.0.0.1:47800/peer/{NET_ID}/p2p/v1/ping"
    assert headers == FakeCompanion.headers
    assert timeout[1] >= route.INTERNET_MIN_TIMEOUT
    # L'échec du réseau local est retenu : la requête suivante va droit au compagnon
    network.calls.clear()
    assert client.call(MY_ID, peer, "ping", {}) == {"via": "internet"}
    assert [c[0] for c in network.calls] == ["internet"]


def test_lan_is_tried_again_after_a_while(monkeypatch, peer, companion):
    network = FakeNetwork(lan="down")
    monkeypatch.setattr(requests, "post", network)
    client.call(MY_ID, peer, "ping", {})
    assert route.lan_is_down(PEER_ID)
    route._lan_down[PEER_ID] = 0.0                      # la minute d'attente est passée
    network.behaviour["lan"] = "ok"
    network.calls.clear()
    assert client.call(MY_ID, peer, "ping", {}) == {"via": "lan"}


def test_no_second_path_once_the_request_may_have_left(monkeypatch, peer, companion):
    """Lecture trop longue : la requête est peut-être arrivée, pas de nouvel essai."""
    network = FakeNetwork(lan="slow")
    monkeypatch.setattr(requests, "post", network)
    with pytest.raises(client.PeerError):
        client.call(MY_ID, peer, "work/result", {})
    assert [c[0] for c in network.calls] == ["lan"]
    assert not route.lan_is_down(PEER_ID)


def test_forced_routes(monkeypatch, peer, companion):
    network = FakeNetwork()
    monkeypatch.setattr(requests, "post", network)
    settings.set_value("p2p_route", "internet")
    assert client.call(MY_ID, peer, "ping", {}) == {"via": "internet"}
    settings.set_value("p2p_route", "lan")
    network.behaviour["lan"] = "down"
    with pytest.raises(client.PeerError):
        client.call(MY_ID, peer, "ping", {})
    assert all(kind == "lan" for kind, *_ in network.calls[1:])


def test_pairing_always_uses_the_lan(monkeypatch, peer, companion):
    network = FakeNetwork(lan="down")
    monkeypatch.setattr(requests, "post", network)
    with pytest.raises(client.PeerError):
        client.call(MY_ID, peer, "pair/confirm", {"accept": True}, key=KEY)
    assert [c[0] for c in network.calls] == ["lan"]


def test_paths_without_companion_or_key(peer):
    assert [p.kind for p in route.paths_for(peer)] == ["lan"]
    unknown = dict(peer, address=None, port=None)
    assert route.paths_for(unknown) == []
    ipv6 = dict(peer, address="fe80::1")
    assert route.paths_for(ipv6)[0].base == "http://[fe80::1]:47701"


def test_without_its_program_the_companion_leaves_the_lan_alone(monkeypatch, peer, tmp_path):
    """Internet activé mais compagnon pas compilé : AIODOS le dit, et tout continue de
    passer par le réseau local."""
    monkeypatch.setenv("AIODOS_NET_BIN", str(tmp_path / "absent.exe"))
    missing = Companion("127.0.0.1:47701", {"data_dir": tmp_path})
    assert not missing.start()
    assert "introuvable" in missing.error
    route.attach(missing)
    try:
        assert [p.kind for p in route.paths_for(peer)] == ["lan"]
    finally:
        route.detach(missing)
        missing.stop()


def test_companion_errors_are_readable(monkeypatch, peer, companion):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply(
        {"detail": "Machine injoignable par Internet : aucune réponse en 30 s."}, 502))
    settings.set_value("p2p_route", "internet")
    with pytest.raises(client.PeerError, match="aucune réponse en 30 s"):
        client.call(MY_ID, peer, "ping", {})
