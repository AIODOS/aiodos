"""Le lanceur, au démarrage : il essaie une nouvelle version dans un processus à part,
la garde si elle se déclare en bonne santé, revient à la précédente sinon. Les
versions lancées ici sont de faux AIODOS (tests/updates_kit.py)."""
import subprocess
import sys
import time

import pytest

from aiodos import instance
from aiodos.updates import launcher, state
from aiodos.updates.store import Store
from tests.updates_kit import installed_fake


@pytest.fixture
def store(data_dir, monkeypatch):
    monkeypatch.setattr(launcher, "developer_checkout", lambda: False)
    return Store()                                   # data/updates du dossier de test


def runs(data_dir) -> list:
    path = data_dir / "runs.txt"
    return path.read_text(encoding="utf-8").split("\n")[:-1] if path.exists() else []


def pending(store, version, attempts=0):
    state.update(store.root, lambda d: d.update(pending={
        "version": version, "attempts": attempts, "previous": "2.0.0", "notes": "Nouveautés"}))


def test_new_version_is_tried_then_kept(store, data_dir):
    installed_fake(store, "2.0.1")
    pending(store, "2.0.1")
    assert launcher.dispatch(["--headless", "--data-dir", "x"], store, "2.0.0") == 0
    assert runs(data_dir) == ["2.0.1 --headless"]       # --data-dir : remplacé par la variable
    data = state.load(store.root)
    assert data["current"] == "2.0.1" and data["pending"] is None
    assert data["notice"] == {"kind": "updated", "version": "2.0.1", "previous": "2.0.0",
                              "notes": "Nouveautés", "at": data["notice"]["at"]}
    assert launcher.dispatch([], store, "2.0.0") == 0   # la suivante fois : sans essai
    assert runs(data_dir)[-1] == "2.0.1 "


def test_version_that_does_not_start_is_retried_once_then_set_aside(store, data_dir):
    installed_fake(store, "2.0.1", "crash")
    pending(store, "2.0.1")
    assert launcher.dispatch([], store, "2.0.0") is None      # la version d'origine tourne
    data = state.load(store.root)
    assert data["pending"]["attempts"] == 1 and data["pending"]["failed_at"]
    assert not data.get("refused")
    assert launcher.dispatch([], store, "2.0.0") is None
    data = state.load(store.root)
    assert data["pending"] is None and "2.0.1" in data["refused"]
    assert data["notice"]["kind"] == "rollback"
    assert launcher.dispatch([], store, "2.0.0") is None
    assert len(runs(data_dir)) == 2                            # plus jamais relancée


def test_previous_version_takes_over_at_once(store, data_dir):
    installed_fake(store, "2.0.1")
    installed_fake(store, "2.0.2", "crash")
    state.update(store.root, lambda d: d.update(current="2.0.1"))
    pending(store, "2.0.2")
    assert launcher.dispatch([], store, "2.0.0") == 0
    assert [line.split()[0] for line in runs(data_dir)] == ["2.0.2", "2.0.1"]
    assert state.load(store.root)["current"] == "2.0.1"


def test_restart_hands_over_to_the_version_installed_meanwhile(store, data_dir):
    installed_fake(store, "2.0.1", "restart")        # pendant qu'elle tourne : 2.0.2 arrive
    installed_fake(store, "2.0.2")
    state.update(store.root, lambda d: d.update(current="2.0.1"))
    assert launcher.dispatch([], store, "2.0.0") == 0
    assert [line.split()[0] for line in runs(data_dir)] == ["2.0.1", "2.0.2"]
    assert state.load(store.root)["current"] == "2.0.2"


def test_version_that_never_gets_ready_is_stopped(store, data_dir, monkeypatch):
    monkeypatch.setattr(launcher, "START_TIMEOUT", 2)
    installed_fake(store, "2.0.1", "hang")
    pending(store, "2.0.1", attempts=1)
    started = time.monotonic()
    assert launcher.dispatch([], store, "2.0.0") is None
    assert time.monotonic() - started < 20
    assert "2.0.1" in state.load(store.root)["refused"]


def test_original_version_runs_itself_when_it_is_the_newest(store, data_dir):
    installed_fake(store, "2.0.1")
    state.update(store.root, lambda d: d.update(current="2.0.1"))
    pending(store, "2.0.2")                              # pas installée : ignorée
    assert launcher.dispatch([], store, "2.0.3") is None
    assert launcher.choose(state.load(store.root), store.installed(), "2.0.0") == [("2.0.1", False)]
    assert runs(data_dir) == []


def test_never_in_a_development_copy_nor_inside_a_launched_version(data_dir, monkeypatch):
    store = Store()
    installed_fake(store, "2.0.1")
    pending(store, "2.0.1")
    assert launcher.developer_checkout()                 # ce dépôt-ci
    assert launcher.dispatch([], store, "2.0.0") is None
    monkeypatch.setattr(launcher, "developer_checkout", lambda: False)
    monkeypatch.setenv(launcher.CHILD_ENV, "1")
    assert launcher.dispatch([], store, "2.0.0") is None
    monkeypatch.delenv(launcher.CHILD_ENV)
    monkeypatch.setenv("AIODOS_NO_UPDATE", "1")
    assert launcher.dispatch([], store, "2.0.0") is None
    assert runs(data_dir) == []


def test_nothing_is_launched_while_aiodos_is_already_open(store, data_dir):
    installed_fake(store, "2.0.1")
    pending(store, "2.0.1")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (data_dir / "instance.json").write_text(f'{{"pid": {other.pid}, "port": 1}}')
        assert instance.running()
        assert launcher.dispatch([], store, "2.0.0") is None   # « déjà ouvert » suit
        assert runs(data_dir) == []
    finally:
        other.kill()
        other.wait()


def test_a_launched_version_declares_itself_healthy(data_dir, monkeypatch):
    ready = data_dir / "ready"
    monkeypatch.setattr(launcher, "HEALTHY_AFTER", 0.05)
    monkeypatch.setenv(launcher.READY_ENV, str(ready))
    launcher.confirm_when_healthy(lambda: True)          # pas lancée par le lanceur : rien
    time.sleep(0.3)
    assert not ready.exists()
    monkeypatch.setenv(launcher.CHILD_ENV, "1")
    launcher.confirm_when_healthy(lambda: False)         # déjà arrêtée : rien non plus
    time.sleep(0.3)
    assert not ready.exists()
    launcher.confirm_when_healthy(lambda: True)
    for _ in range(50):
        if ready.exists():
            break
        time.sleep(0.05)
    assert ready.exists()


def test_data_dir_argument_is_not_passed_on():
    assert launcher.child_argv(["--headless", "--data-dir", "x", "--port", "1",
                                "--data-dir=y"]) == ["--headless", "--port", "1"]
