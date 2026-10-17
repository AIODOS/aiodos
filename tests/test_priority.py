"""Priorité du travail de fond (node/priority.py) : threads de fond et programmes lancés
par AIODOS abaissés, le reste intact. Sous Windows, vérifié en relisant l'état par l'API
de Windows ; sous Linux et Android, nice simulé."""
import asyncio
import subprocess
import sys
import threading

import pytest

from aiodos.node import priority

windows = pytest.mark.skipif(sys.platform != "win32", reason="API de Windows")


@windows
def test_a_background_thread_is_lowered_and_the_others_stay_normal():
    seen = {}

    def background():
        seen["applied"] = priority.lower_current_thread()
        seen["state"] = priority.thread_state()

    worker = threading.Thread(target=background)
    worker.start()
    worker.join()
    assert seen["applied"] == ["priorité inférieure à la normale", "EcoQoS",
                               "mémoire de priorité inférieure"]
    assert seen["state"] == {"priority": -1, "ecoqos": True, "memory": 4}
    mine = priority.thread_state()                # le thread des tests : rien n'a changé
    assert mine["priority"] == 0 and mine["ecoqos"] is False


@windows
def test_background_work_runs_in_lowered_threads():
    async def main():
        return await priority.run(priority.thread_state)
    assert asyncio.run(main())["priority"] == -1


@windows
def test_a_program_starts_below_normal_then_goes_ecoqos():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             creationflags=priority.CREATION_FLAGS)
    try:
        state = priority.process_state(child.pid)
        assert state == {"priority_class": priority.BELOW_NORMAL_PRIORITY_CLASS, "ecoqos": False}
        assert priority.lower_process(child.pid) == ["priorité inférieure à la normale", "EcoQoS"]
        assert priority.process_state(child.pid)["ecoqos"] is True
    finally:
        child.kill()
        child.wait()


@pytest.fixture
def fake_nice(monkeypatch):
    """os.getpriority / os.setpriority simulés : {thread: nice}."""
    values = {}
    monkeypatch.setattr(priority.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(priority.os, "getpriority", lambda which, who: values.get(who, 0),
                        raising=False)
    monkeypatch.setattr(priority.os, "setpriority",
                        lambda which, who, value: values.__setitem__(who, value), raising=False)
    return values


def test_linux_thread_goes_to_nice_10_never_back_up(fake_nice):
    fake_nice[102] = 15
    assert priority._nice_thread(101) == ["nice 10"] and fake_nice[101] == 10
    assert priority._nice_thread(102) == ["nice 15"] and fake_nice[102] == 15


def test_linux_every_thread_of_a_program_is_lowered(tmp_path, fake_nice):
    for tid in (300, 301, 302):
        (tmp_path / "300" / "task" / str(tid)).mkdir(parents=True)
    assert priority._nice_process(300, proc=tmp_path) == ["nice 10"]
    assert fake_nice == {300: 10, 301: 10, 302: 10}


def test_linux_current_thread(monkeypatch, fake_nice):
    monkeypatch.setattr(priority.sys, "platform", "linux")
    assert priority.lower_current_thread() == ["nice 10"]
    assert fake_nice[threading.get_native_id()] == 10


def test_a_refusal_is_harmless(monkeypatch):
    monkeypatch.setattr(priority.sys, "platform", "linux")
    monkeypatch.setattr(priority.os, "PRIO_PROCESS", 0, raising=False)

    def refuse(*args):
        raise PermissionError("refusé")
    monkeypatch.setattr(priority.os, "getpriority", refuse, raising=False)
    assert priority.lower_current_thread() == []
    assert priority.lower_process(4242) == []


def test_other_systems_are_left_alone(monkeypatch):
    monkeypatch.setattr(priority.sys, "platform", "darwin")
    monkeypatch.setattr(priority.android, "is_android", lambda: False)
    assert priority.lower_current_thread() == []
    assert priority.lower_process(4242) == []
