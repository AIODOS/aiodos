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
