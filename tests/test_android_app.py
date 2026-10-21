"""Le nœud dans l'appli Android (node/android_app.py) : son environnement (données de l'appli,
programmes rangés en bibliothèques, démarrage avec le téléphone dès la première ouverture),
la batterie et le Wi-Fi lus par Android (un faux module « java » de Chaquopy), les moteurs
pris seulement dans l'appli, et pas de mise à jour du code par le réseau. Aucun téléphone ici."""
import json
import os
import sys
import types
from pathlib import Path

import pytest

from aiodos import settings
from aiodos.cluster import engine as llama
from aiodos.node import android, android_app, autostart, power
from aiodos.updates import engines

TOUCHED = ("AIODOS_ANDROID_APP", "AIODOS_DATA_DIR", "AIODOS_NET_BIN", "AIODOS_ENGINES_DIR",
           "AIODOS_AUTOSTART_FLAG", "TMPDIR", "LD_LIBRARY_PATH")


@pytest.fixture
def app_env(monkeypatch):
    """prepare() écrit dans os.environ : tout est remis en place après le test."""
    for key in TOUCHED:
        monkeypatch.setenv(key, os.environ.get(key, "-"))
        if os.environ[key] == "-":
            monkeypatch.delenv(key)


@pytest.fixture
def links(monkeypatch):
    """Windows refuse les liens symboliques sans privilège : un faux lien (fichier qui
    nomme sa cible) suffit à vérifier ce qui est relié à quoi."""
    def fake_symlink(self, target):
        self.write_text(str(target), encoding="utf-8")
    monkeypatch.setattr(Path, "symlink_to", fake_symlink)


@pytest.fixture
def device(monkeypatch):
    """Le module « java » de Chaquopy, simulé : Device.battery() et Device.wifi()."""
    state = {"battery": "", "wifi": -1, "calls": 0}

    class Device:
        @staticmethod
        def battery():
            state["calls"] += 1
            return state["battery"]

        @staticmethod
        def wifi():
            return state["wifi"]
    java = types.ModuleType("java")
    java.jclass = lambda name: Device if name == "fr.arkane.aiodos.Device" else None
    monkeypatch.setitem(sys.modules, "java", java)
    monkeypatch.setenv(android_app.APP_ENV, "1")
    return state


def _native(folder: Path) -> Path:
    folder.mkdir(parents=True)
    for library in ("libllama-server.so", "libggml-rpc-server.so", android_app.COMPANION):
        (folder / library).write_bytes(b"\x7fELF")
    return folder


def test_the_app_environment(tmp_path, app_env, links):
    files, cache = tmp_path / "files", tmp_path / "cache"
    files.mkdir()
    native = _native(tmp_path / "lib" / "arm64")
    os.environ["LD_LIBRARY_PATH"] = "/system/lib64"
    android_app.prepare(files, native, cache)
    assert android_app.in_app()
    assert os.environ["AIODOS_DATA_DIR"] == str(files / "data")
    assert os.environ["AIODOS_NET_BIN"] == str(native / "libaiodos-net.so")
    assert os.environ["TMPDIR"] == str(cache)
    assert os.environ["LD_LIBRARY_PATH"] == f"{native}:/system/lib64"
    programs = Path(os.environ["AIODOS_ENGINES_DIR"]) / "llama.cpp"
    assert (programs / "llama-server").read_text(encoding="utf-8") == str(native / "libllama-server.so")
    assert (programs / "ggml-rpc-server").read_text(encoding="utf-8") == str(native / "libggml-rpc-server.so")
    # Mise à jour de l'appli : nouveau dossier des bibliothèques, liens refaits
    moved = _native(tmp_path / "lib2" / "arm64")
    android_app.prepare(files, moved, cache)
    assert (programs / "llama-server").read_text(encoding="utf-8") == str(moved / "libllama-server.so")
    assert os.environ["LD_LIBRARY_PATH"] == f"{moved}:{native}:/system/lib64"


def test_starting_with_the_phone_is_on_at_first_opening_only(tmp_path, app_env, links, monkeypatch):
    files = tmp_path / "files"
    files.mkdir()
    native = _native(tmp_path / "lib")
    android_app.prepare(files, native, tmp_path)
    flag = files / "autostart"                      # lu par BootReceiver.java
    assert flag.is_file()
    monkeypatch.setattr(autostart, "_startup_folder", lambda: None)
    assert autostart.state() == {"supported": True, "enabled": True, "device": "android"}
    assert not autostart.set_enabled(False)["enabled"] and not flag.exists()
    android_app.prepare(files, native, tmp_path)    # ouvertures suivantes : le choix tient
    assert not flag.exists()
    assert autostart.set_enabled(True)["enabled"] and flag.is_file()


def test_the_battery_is_read_from_android(device, tmp_path, monkeypatch):
    monkeypatch.setattr(power, "_run_termux", lambda: pytest.fail("Termux dans l'appli"))
    closed = tmp_path / "power_supply"                  # fermé par SELinux : rien de lisible

    def read():
        return power._linux(closed, mobile=True)
    device["battery"] = json.dumps({"percentage": 84, "plugged": "PLUGGED_USB",
                                    "status": "CHARGING", "temperature": 31.5})
    assert read() == {"plugged": True, "level": 84, "temperature": 31.5,
                      "mobile": True, "source": "android"}
    device["battery"] = json.dumps({"percentage": 40, "plugged": "UNPLUGGED",
                                    "status": "DISCHARGING"})
    assert read()["plugged"] is False
    device["battery"] = ""                                               # illisible
    assert read() == {"plugged": None, "level": None, "temperature": None,
                      "mobile": True, "source": None}


def test_wifi_is_read_from_android(device, monkeypatch):
    monkeypatch.setitem(android._wifi, "at", None)
    monkeypatch.setattr(android, "_wifi_termux", lambda: pytest.fail("Termux dans l'appli"))
    monkeypatch.setattr(android, "_wifi_interface", lambda: None)
    device["wifi"] = 1
    assert android.on_wifi() is True
    for value, expected in ((0, False), (-1, None)):
        monkeypatch.setitem(android._wifi, "at", None)
        device["wifi"] = value
        assert android.on_wifi() is expected


def test_outside_the_app_nothing_asks_java(monkeypatch):
    monkeypatch.delenv(android_app.APP_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "java", None)            # import java : ImportError
    assert android_app.battery() is None and android_app.on_wifi() is None


def test_in_the_app_only_its_own_programs_run(tmp_path, monkeypatch):
    packaged = tmp_path / "engines"
    (packaged / "llama.cpp").mkdir(parents=True)
    for program in ("llama-server", "ggml-rpc-server"):
        (packaged / "llama.cpp" / program).write_bytes(b"\x7fELF")
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    for program in ("llama-server", "ggml-rpc-server"):
        (elsewhere / program).write_bytes(b"\x7fELF")
    settings.set_value("llamacpp_dir", str(elsewhere))
    monkeypatch.setattr(engines.platforms, "executable", lambda name, *a: name)
    monkeypatch.setenv(engines.PACKAGED_ENV, str(packaged))
    assert engines.packaged_only()
    assert engines.find(engines.LLAMA) is None                   # livré par les versions : non
    assert llama.folder() == packaged / "llama.cpp"              # réglage ignoré dans l'appli
    monkeypatch.delenv(engines.PACKAGED_ENV)
    assert llama.folder() == elsewhere


def test_the_app_does_not_update_its_code_by_the_network(monkeypatch, tmp_path):
    from aiodos import __version__
    from aiodos.updates import launcher
    from aiodos.updates import service as updates
    from aiodos.updates.store import Store

    class Service(updates.UpdateService):
        def __init__(self):
            self.store = Store()

        def _best_offer(self):
            pytest.fail("version demandée aux autres machines dans l'appli")
    newer = "99.0.0"
    monkeypatch.setattr(launcher, "developer_checkout", lambda: False)   # comme l'appli
    monkeypatch.setattr(Store, "installed", lambda self: [newer, __version__])
    pending = {"pending": {"version": newer}}
    monkeypatch.delenv(android_app.APP_ENV, raising=False)
    assert Service().ready(pending) == newer            # hors de l'appli : elle démarrerait
    monkeypatch.setenv(android_app.APP_ENV, "1")
    settings.set_value("auto_update", True)
    Service().tick()
    assert Service().ready(pending) is None             # dans l'appli : jamais


def test_a_phone_lends_no_rpc_server(monkeypatch):
    """ggml-rpc-server n'a ni mot de passe ni chiffrement : dans l'appli, toute appli du
    téléphone joindrait son port. Le téléphone ne prête donc pas aux gros modèles répartis."""
    from aiodos.cluster import engine as llama_engine
    from aiodos.cluster import helper
    from aiodos.network import sharing
    from aiodos.network.client import PeerError

    class Manager:
        def name(self):
            return "Samsung SM-S911B"
    settings.set_value("lend_gpu", True)
    monkeypatch.setattr(sharing, "level", lambda sender: sharing.MINE)
    monkeypatch.setattr(llama_engine, "rpc_server",
                        lambda *a, **k: pytest.fail("serveur RPC lancé dans l'appli"))
    monkeypatch.setenv(android_app.APP_ENV, "1")
    with pytest.raises(PeerError, match="téléphone"):
        helper.lend(Manager(), "pc", {"version": "11707-1268edac5"})


def test_a_phone_never_announces_that_it_lends(monkeypatch):
    """Le téléphone refuse tout prêt aux gros modèles répartis (ci-dessus) : son profil ne
    l'annonce donc jamais (« cluster.lends » faux), même avec un compagnon qui tiendrait le
    serveur, et sans interroger llama.cpp. Sinon mes machines le choisissaient, et l'échec
    n'arrivait qu'au premier message."""
    from aiodos.cluster import engine as llama_engine
    from aiodos.network import route
    from aiodos.node import identity

    class Companion:
        running = True

        def supports(self, feature):
            return True
    settings.set_value("lend_gpu", True)
    monkeypatch.setattr(route, "companion", lambda: Companion())
    monkeypatch.setattr(llama_engine, "info",
                        lambda *a, **k: pytest.fail("llama.cpp interrogé dans l'appli"))
    monkeypatch.setenv(android_app.APP_ENV, "1")
    assert identity._cluster() == {"lends": False}


def test_the_service_can_ask_whether_the_node_still_runs(monkeypatch):
    class Node:
        running = True

        def alive(self):
            return self.running
    node = Node()
    monkeypatch.setitem(android_app._running, "node", None)
    assert android_app.alive() is False
    monkeypatch.setitem(android_app._running, "node", node)
    assert android_app.alive() is True
    node.running = False
    assert android_app.alive() is False


def test_the_engine_is_called_llama_cpp_in_the_app_even_when_missing(monkeypatch):
    """Dans l'appli, pas d'Ollama : l'interface ne doit jamais conseiller de le lancer."""
    from aiodos.node import identity
    from aiodos.services import ollama
    monkeypatch.setattr(ollama, "use_integrated", lambda: False)
    monkeypatch.delenv(android_app.APP_ENV, raising=False)
    assert identity._engine() == "ollama"
    monkeypatch.setenv(android_app.APP_ENV, "1")
    assert identity._engine() == "llama.cpp"


def test_the_app_installs_over_the_2_0_x_already_there():
    """La numérotation a repris à 1.0.0 : l'APK doit s'installer par-dessus l'appli en
    2.0.2-dev (versionCode 20002) sans qu'il faille la désinstaller, et donc garder les
    données du nœud."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import build_android
    name, code = build_android.version()
    assert name == __import__("aiodos").__version__
    assert code == build_android.CODE_OFFSET + 10000 * int(name.split(".")[0]) \
        + 100 * int(name.split(".")[1]) + int(name.split(".")[2].split("-")[0])
    assert code > 20002
