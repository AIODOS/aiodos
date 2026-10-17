"""Une sortie disparue ne fait jamais planter AIODOS (17/10/2026) : un nœud dont la
console ou le tube de sortie n'existe plus (lancé par un essai fini, console fermée)
plantait au redémarrage pour mise à jour sur « [Errno 22] Invalid argument » et
affichait une fenêtre d'erreur. Et un nœud lancé avec AIODOS_NO_DIALOG n'affiche jamais
de fenêtre."""
import logging

from aiodos import instance
from aiodos.updates import launcher, restart


class GoneStream:
    """Le tube d'un processus parti : sous Windows, l'écriture échoue avec EINVAL."""

    def write(self, text):
        raise OSError(22, "Invalid argument")

    def flush(self):
        raise OSError(22, "Invalid argument")

    def fileno(self):
        raise OSError(22, "Invalid argument")


def test_relaunch_survives_a_vanished_output(monkeypatch):
    started = []
    monkeypatch.setattr(launcher, "supervised", lambda: False)
    monkeypatch.setattr(restart.subprocess, "Popen", lambda *a, **k: started.append(a[0]))
    monkeypatch.setattr(restart.os, "execve", lambda *a: started.append(a[1]))
    monkeypatch.setattr(restart.os, "chdir", lambda path: None)
    monkeypatch.setattr(restart.sys, "stdout", GoneStream())
    monkeypatch.setattr(restart.sys, "stderr", GoneStream())
    handler = logging.StreamHandler(GoneStream())
    logging.getLogger().addHandler(handler)
    try:
        assert restart.relaunch(["--headless"]) == 0
    finally:
        logging.getLogger().removeHandler(handler)
    assert started and started[0][1:3] == ["-m", "aiodos"]      # la nouvelle version part


def test_no_dialog_for_a_node_started_by_the_tests(monkeypatch):
    shown = []
    monkeypatch.setenv(instance.NO_DIALOG_ENV, "1")
    monkeypatch.setattr(instance.sys, "stderr", GoneStream())
    if instance.sys.platform == "win32":
        monkeypatch.setattr(instance.ctypes.windll.user32, "MessageBoxW",
                            lambda *a: shown.append(a))
    instance.alert("AIODOS s'est arrêté : essai")                # ni fenêtre, ni erreur
    assert shown == []
