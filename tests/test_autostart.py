"""Démarrer AIODOS avec l'appareil, en arrière-plan (node/autostart.py) : le raccourci du
dossier Démarrage sous Windows, le fichier lu par l'appli Android au démarrage du
téléphone. Dossiers temporaires : rien n'est jamais ajouté au vrai démarrage du PC."""
import subprocess
import sys

import pytest

from aiodos.node import autostart

windows = pytest.mark.skipif(sys.platform != "win32", reason="raccourci Windows")


@windows
def test_windows_shortcut_starts_aiodos_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert autostart.state() == {"supported": True, "enabled": False, "device": "windows"}
    assert autostart.set_enabled(True)["enabled"]
    link = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AIODOS.lnk"
    assert link.is_file()
    read = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut('" + str(link) + "'); "
            "$s.TargetPath; $s.Arguments; $s.WorkingDirectory")
    target, arguments, folder = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", read],
        capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
    exe, wanted_arguments, wanted_folder = autostart.command()
    assert target.lower() == exe.lower() and arguments == "-m aiodos --background"
    assert folder.lower() == wanted_folder.lower()
    assert not autostart.set_enabled(False)["enabled"] and not link.exists()
    autostart.set_enabled(False)                   # déjà retiré : rien à faire


def test_the_phone_starts_its_node_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "_startup_folder", lambda: None)
    monkeypatch.setattr(autostart.android, "is_android", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert autostart.state() == {"supported": True, "enabled": False, "device": "android"}
    autostart.set_enabled(True)
    assert (tmp_path / ".aiodos-autostart").is_file()       # lu par BootReceiver.java
    assert not autostart.set_enabled(False)["enabled"]


def test_elsewhere_it_is_said_plainly(monkeypatch):
    monkeypatch.setattr(autostart, "_startup_folder", lambda: None)
    monkeypatch.setattr(autostart.android, "is_android", lambda: False)
    assert autostart.state()["supported"] is False
    with pytest.raises(OSError, match="Windows et sur Android"):
        autostart.set_enabled(True)


def test_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aiodos.api.server import create_app
    monkeypatch.setattr(autostart, "_startup_folder", lambda: None)
    monkeypatch.setattr(autostart.android, "is_android", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as client:
        headers = {"X-Aiodos-Token": "jeton"}
        assert client.get("/api/node/autostart", headers=headers).json()["enabled"] is False
        reply = client.post("/api/node/autostart", json={"enabled": True}, headers=headers)
        assert reply.status_code == 200 and reply.json()["enabled"] is True
