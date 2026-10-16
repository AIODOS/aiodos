"""Service de mise à jour : une machine appairée annonce une version plus récente, on
la reçoit, on l'installe, et on redémarre dessus seulement quand la machine est libre.
Le transport est remplacé par un appel direct à la machine qui transmet."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aiodos import db
from aiodos.api.server import create_app
from aiodos.network import peers
from aiodos.updates import launcher, p2p, restart, service, state
from aiodos.updates.store import Store
from tests.updates_kit import fake_code, new_key, publish, zip_folder

PEER = "f" * 32


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Cette machine en version 2.0.0, et une machine appairée, en ligne, qui transmet
    la 2.0.1."""
    private, trusted = new_key()
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", trusted)
    monkeypatch.setattr(service, "__version__", "2.0.0")
    monkeypatch.setattr(launcher, "developer_checkout", lambda: False)
    restarts = []
    monkeypatch.setattr(restart, "request", lambda: restarts.append(True))
    lender = Store(tmp_path / "lender")
    publish(lender, zip_folder(fake_code(tmp_path / "code", "2.0.1"), tmp_path / "a.zip"),
            "2.0.1", private, trusted, notes="Plus rapide")
    monkeypatch.setattr(p2p, "_store", lambda: lender)
    monkeypatch.setattr(p2p.client, "call", lambda my_id, peer, action, payload, key=None,
                        timeout=10: p2p.HANDLERS[action](None, my_id, payload))
    db.connect()
    peers.save(PEER, "PC de Léa", b"k" * 32, "127.0.0.1", 47701)
    peers.set_profile(PEER, {"version": "2.0.1", "updates": {"offer": "2.0.1"}})
    executor = SimpleNamespace(interactive=0, busy={"embed": 0, "gpu": 0}, remote={})
    updates = service.UpdateService(Bus(), executor, SimpleNamespace(node_id="moi"))
    return SimpleNamespace(updates=updates, executor=executor, restarts=restarts,
                           lender=lender, private=private, trusted=trusted)


def test_newer_version_is_received_installed_then_restarted_into(world):
    world.updates.tick()
    store = world.updates.store
    assert store.offer() == "2.0.1"                       # gardée, et transmise à son tour
    assert store.installed() == ["2.0.1"]
    pending = state.load(store.root)["pending"]
    assert pending == {"version": "2.0.1", "attempts": 0, "previous": "2.0.0",
                       "notes": "Plus rapide"}
    assert world.restarts == [True]
    status = world.updates.status()
    assert status["ready"] == "2.0.1" and status["offer"] == "2.0.1" and not status["error"]


def test_nothing_happens_when_automatic_updates_are_off(world):
    from aiodos import settings
    settings.set_value("auto_update", False)
    world.updates.tick()
    assert world.updates.store.offer() is None and world.restarts == []


def test_restart_waits_until_the_machine_is_free(world):
    world.executor.busy["embed"] = 1                     # une unité en cours
    world.updates.tick()
    assert world.updates.store.installed() == ["2.0.1"] and world.restarts == []
    world.executor.busy["embed"] = 0
    world.executor.interactive = 1                        # une conversation
    world.updates.tick()
    assert world.restarts == []
    world.executor.interactive = 0
    world.updates.tick()
    assert world.restarts == [True]


def test_with_a_window_the_user_must_be_away(world, monkeypatch):
    from aiodos.node import identity
    world.updates.windowed = True
    for idle, expected in ((30, []), (None, []), (3600, [True])):
        monkeypatch.setattr(identity, "idle_seconds", lambda idle=idle: idle)
        world.updates.tick()
        assert world.restarts == expected
    monkeypatch.setattr(identity, "idle_seconds", lambda: None)
    assert world.updates.status()["restart_when"] == "next_start"


def test_restarts_are_limited(world):
    for _ in range(5):
        world.updates.tick()
    assert len(world.restarts) == service.MAX_RESTARTS_PER_HOUR


def test_older_or_equal_versions_are_never_taken(world, tmp_path):
    peers.set_profile(PEER, {"updates": {"offer": "1.9.0"}})
    world.updates.tick()
    assert world.updates.store.offer() is None
    service.__version__ = "2.0.1"                         # restauré par monkeypatch
    peers.set_profile(PEER, {"updates": {"offer": "2.0.1"}})
    world.updates.tick()
    assert world.updates.store.offer() is None and world.restarts == []


def test_failed_install_is_remembered_and_not_retried_at_once(world, tmp_path):
    broken = fake_code(tmp_path / "broken", "2.0.2", server="import paquet_absent_ici\n")
    publish(world.lender, zip_folder(broken, tmp_path / "b.zip"), "2.0.2", world.private,
            world.trusted)
    peers.set_profile(PEER, {"updates": {"offer": "2.0.2"}})
    world.updates.tick()
    data = state.load(world.updates.store.root)
    assert data["refused"]["2.0.2"]["retry_after"] > data["refused"]["2.0.2"]["at"]
    assert "paquet_absent_ici" in world.updates.status()["error"]
    assert world.updates.store.installed() == [] and world.restarts == []


def test_unreachable_machine_is_not_asked_again_at_once(world, monkeypatch):
    asked = []

    def down(*args, **kwargs):
        asked.append(True)
        raise p2p.PeerError("Machine injoignable.")
    monkeypatch.setattr(p2p.client, "call", down)
    world.updates.tick()
    world.updates.tick()
    assert len(asked) == 1
    assert "injoignable" in world.updates.status()["error"]


def test_development_copy_relays_but_never_installs(world, monkeypatch):
    monkeypatch.setattr(launcher, "developer_checkout", lambda: True)
    world.updates.tick()
    assert world.updates.store.offer() == "2.0.1"
    assert world.updates.store.installed() == [] and world.restarts == []
    assert world.updates.status()["developer"] and not world.updates.ready()


def test_api_reports_the_state_and_clears_the_notice(data_dir):
    with TestClient(create_app("jeton", {"testserver"})) as client:
        headers = {"X-Aiodos-Token": "jeton"}
        status = client.get("/api/updates", headers=headers).json()
        assert status["version"] and status["auto"] and status["developer"]
        assert client.post("/api/updates/restart", headers=headers).status_code == 409
        state.update(Store().root, lambda d: d.update(notice={"kind": "updated",
                                                              "version": "9.9.9"}))
        assert client.get("/api/updates", headers=headers).json()["notice"]["version"] == "9.9.9"
        assert client.post("/api/updates/notice/seen", headers=headers).json() == {"ok": True}
        assert client.get("/api/updates", headers=headers).json()["notice"] is None
        node = client.get("/api/node", headers=headers).json()
        assert node["updates"] == {"offer": None}
