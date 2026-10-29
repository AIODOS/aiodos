"""Programmes lancés par AIODOS arrêtés avec lui, même s'il est tué (node/children.py) :
sous Windows, l'objet de travail arrête l'enfant quand son parent disparaît."""
import subprocess
import sys
import time

import pytest

from aiodos.node import children
from tests.nodes import APP_ROOT

PARENT = r'''
import subprocess, sys, time
from aiodos.node import children
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, children.bind(child.pid), flush=True)
time.sleep(120)
'''


def _alive(pid: int) -> bool:
    found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                           text=True, errors="replace").stdout
    return str(pid) in found


@pytest.mark.skipif(sys.platform != "win32", reason="objet de travail : Windows seulement")
def test_a_bound_program_dies_with_aiodos():
    parent = subprocess.Popen([sys.executable, "-c", PARENT], cwd=APP_ROOT,
                              stdout=subprocess.PIPE, text=True)
    try:
        pid, bound = parent.stdout.readline().split()
        assert bound == "True" and _alive(int(pid))
        parent.kill()                                   # AIODOS tué, sans rien ranger
        parent.wait(10)
        deadline = time.monotonic() + 10
        while _alive(int(pid)) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not _alive(int(pid))
    finally:
        if parent.poll() is None:
            parent.kill()


def test_binding_never_raises():
    assert children.bind(0) is False             # processus introuvable : refus sans erreur


def test_the_memory_of_the_programs_and_their_children_is_read():
    """resident : les pages en mémoire des programmes rattachés (sous Windows, tout l'objet
    de travail), pour node/places.py."""
    before = children.resident()
    program = subprocess.Popen([sys.executable, "-c",
                                "import time; x = bytearray(80 * 2 ** 20); time.sleep(30)"])
    try:
        children.bind(program.pid)
        deadline = time.monotonic() + 10
        while children.resident() - before < 60 * 2 ** 20 and time.monotonic() < deadline:
            time.sleep(0.2)
        assert children.resident() - before >= 60 * 2 ** 20
    finally:
        program.kill()
        program.wait()


def test_on_linux_and_android_descendants_are_read_from_proc(tmp_path, monkeypatch):
    """Ailleurs que sous Windows : les programmes rattachés encore en vie et leurs
    descendants, d'après /proc/<pid>/stat (le parent) et /proc/<pid>/statm."""
    page = 4096
    table = {10: (1, 100), 11: (10, 50), 12: (11, 25), 20: (1, 999), 30: (20, 7)}
    for pid, (parent, pages) in table.items():
        folder = tmp_path / str(pid)
        folder.mkdir()
        (folder / "stat").write_bytes(f"{pid} (llama server) S {parent} 0 0".encode())
        (folder / "statm").write_text(f"{pages * 2} {pages} 0 0 0 0 0", encoding="ascii")
    (tmp_path / "self").mkdir()
    monkeypatch.setattr(children.os, "sysconf", lambda name: page, raising=False)
    monkeypatch.setattr(children, "_pids", {10, 99})         # 99 : fini, oublié
    assert children._proc_resident(str(tmp_path)) == (100 + 50 + 25) * page
    assert children._pids == {10}
