"""Compagnon livré avec les versions : chaque machine prend celui de sa plateforme, le met
en place à un emplacement fixe (empreinte revérifiée) et le préfère à un programme plus
ancien ; jamais celui d'une version plus récente. Un programme qui ne démarre pas laisse
place au suivant."""
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiodos import paths
from aiodos.network import companion as companion_mod
from aiodos.updates import binaries, launcher, manifest, platforms
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store
from tests.updates_kit import fake_code, fake_program, new_key, publish, zip_folder

HERE = platforms.tag()
OTHER = "android-aarch64" if HERE != "android-aarch64" else "windows-x86_64"
REAL = companion_mod._built(paths.APP_ROOT)          # compagnon compilé dans ce dépôt
pytestmark = pytest.mark.skipif(HERE is None, reason="pas de compagnon pour cette plateforme")


@pytest.mark.parametrize("system, machine, on_android, expected", [
    ("win32", "AMD64", False, "windows-x86_64"),
    ("win32", "ARM64", False, "windows-aarch64"),
    ("linux", "aarch64", True, "android-aarch64"),           # Termux, Python ≤ 3.12
    ("android", "aarch64", True, "android-aarch64"),
    ("linux", "x86_64", False, "linux-x86_64"),
    ("darwin", "arm64", False, "macos-aarch64"),
    ("linux", "armv7l", True, None),                        # téléphone 32 bits : aucun
    ("sunos5", "x86_64", False, None),
])
def test_platform_names(system, machine, on_android, expected):
    assert platforms.name(system, machine, on_android) == expected


def test_program_names():
    assert platforms.kind("aiodos-net", "android-aarch64") == "aiodos-net-android-aarch64"
    assert platforms.executable("aiodos-net", "windows-x86_64") == "aiodos-net.exe"
    assert platforms.executable("aiodos-net", "android-aarch64") == "aiodos-net"


@pytest.fixture
def key(monkeypatch):
    private, trusted = new_key()
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", trusted)
    return private, trusted


@pytest.fixture
def store(key, monkeypatch):
    monkeypatch.setattr(binaries, "__version__", "2.0.1")      # la version qui tourne
    return Store()


def package(tmp_path, store, key, version, companions=(HERE,), program=None) -> dict:
    """Paquet signé de `version`, avec un faux compagnon par plateforme de `companions`."""
    code = fake_code(tmp_path / f"code-{version}", version)
    programs = {where: program or fake_program(tmp_path / f"prog-{version}-{where}",
                                               f"{version} {where}".encode())
                for where in companions}
    return publish(store, zip_folder(code, tmp_path / f"{version}.zip"), version, *key,
                   companions=programs)


def in_place(store) -> Path:
    return store.root / "bin" / platforms.executable("aiodos-net")


def test_companion_of_the_running_version_is_put_in_place(tmp_path, store, key):
    found = package(tmp_path, store, key, "2.0.1", companions=(HERE, OTHER))
    path, version = binaries.find(store)
    assert (path, version) == (in_place(store), "2.0.1")
    assert path.read_bytes().endswith(f"2.0.1 {HERE}".encode())
    item = manifest.binary(found)
    assert binaries.active(store) == {"version": "2.0.1", "kind": f"aiodos-net-{HERE}",
                                      "sha256": item["sha256"]}
    if sys.platform != "win32":
        assert os.access(path, os.X_OK)
    before = path.stat().st_mtime_ns
    assert binaries.find(store) == (path, "2.0.1")          # déjà en place : pas recopié
    assert path.stat().st_mtime_ns == before
    path.write_bytes(b"abime")                              # abîmé sur place : recopié
    assert binaries.find(store) == (path, "2.0.1")
    assert path.read_bytes().endswith(f"2.0.1 {HERE}".encode())


def test_older_companion_is_the_fallback_never_a_newer_one(tmp_path, store, key):
    package(tmp_path, store, key, "2.0.0")
    package(tmp_path, store, key, "2.0.1", companions=(OTHER,))     # rien pour cette machine
    package(tmp_path, store, key, "2.0.2")                          # plus récente : écrite
    assert [v for _, v in binaries.delivered(store)] == ["2.0.0"]   # pour un autre code
    path, version = binaries.find(store)
    assert version == "2.0.0" and path.read_bytes().endswith(f"2.0.0 {HERE}".encode())


def test_altered_companion_is_refused_and_dropped(tmp_path, store, key):
    item = manifest.binary(package(tmp_path, store, key, "2.0.1"))
    source = store.package_path("2.0.1", item)
    data = bytearray(source.read_bytes())
    data[0] ^= 1
    source.write_bytes(bytes(data))
    with pytest.raises(UpdateError, match="altéré"):
        binaries.activate(store, "2.0.1", item)
    assert not source.exists()                          # il sera téléchargé de nouveau
    assert binaries.find(store) is None and not in_place(store).exists()


def test_companion_still_running_stays_and_the_new_one_runs_from_its_package(
        tmp_path, store, key, monkeypatch):
    package(tmp_path, store, key, "2.0.0")
    item = manifest.binary(package(tmp_path, store, key, "2.0.1"))
    monkeypatch.setattr(binaries, "__version__", "2.0.0")
    target, _ = binaries.find(store)
    monkeypatch.setattr(binaries, "__version__", "2.0.1")
    replace = os.replace

    def locked(source, dest):                           # Windows : programme en service
        if Path(dest) == target:
            raise PermissionError("programme en cours d'exécution")
        return replace(source, dest)
    monkeypatch.setattr(binaries.os, "replace", locked)
    assert binaries.find(store) == (store.package_path("2.0.1", item), "2.0.1")
    assert binaries.active(store)["version"] == "2.0.0"
    assert not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]


@pytest.fixture
def installed(tmp_path, store, monkeypatch):
    """Cette machine fait tourner la version 2.0.1, installée par mise à jour (pas de
    dépôt git), lancée par le lanceur de la version d'origine ; rien dans le PATH."""
    root, base = tmp_path / "versions" / "2.0.1", tmp_path / "origine"
    monkeypatch.setattr(paths, "APP_ROOT", root)
    monkeypatch.setattr(launcher, "developer_checkout", lambda: False)
    monkeypatch.setenv(launcher.CHILD_ENV, "1")
    monkeypatch.setenv(launcher.BASE_ENV, str(base))
    monkeypatch.delenv("AIODOS_NET_BIN", raising=False)
    monkeypatch.setattr(companion_mod.shutil, "which", lambda name: None)
    return SimpleNamespace(root=root, base=base)


def built(root: Path) -> Path:
    return fake_program(root / "compagnon" / "target" / "release" / companion_mod.EXE)


def test_choice_order_down_to_the_path(tmp_path, store, key, installed, monkeypatch):
    assert companion_mod.locate() is None
    on_path = fake_program(tmp_path / "PATH" / companion_mod.EXE)
    monkeypatch.setattr(companion_mod.shutil, "which", lambda name: str(on_path))
    assert companion_mod.locate() == (on_path, "PATH")
    in_base = built(installed.base)
    assert companion_mod.locate() == (in_base, companion_mod.BUILT_IN_BASE)
    here = built(installed.root)
    assert companion_mod.locate() == (here, companion_mod.BUILT_HERE)
    package(tmp_path, store, key, "2.0.0")
    assert companion_mod.locate()[1] == "livré avec la version 2.0.0"
    package(tmp_path, store, key, "2.0.1")
    assert companion_mod.locate() == (in_place(store), "livré avec la version 2.0.1")
    assert companion_mod.find_binary() == store.package_path(
        "2.0.1", manifest.binary(store.manifest("2.0.1")))       # sans le mettre en place
    # Un programme qui n'a pas démarré laisse place au suivant ; s'ils sont tous écartés,
    # le préféré est réessayé
    assert companion_mod.locate(skip={"livré avec la version 2.0.1"}) == \
        (in_place(store), "livré avec la version 2.0.0")
    assert binaries.active(store)["version"] == "2.0.0"
    every = {origin for _, origin in companion_mod.candidates(activate=False)}
    assert companion_mod.locate(skip=every)[1] == "livré avec la version 2.0.1"
    assert binaries.active(store)["version"] == "2.0.1"


def test_companion_imposed_by_an_old_launcher_comes_after_the_delivered_one(
        tmp_path, store, key, installed, monkeypatch):
    old = built(installed.base)
    monkeypatch.setenv("AIODOS_NET_BIN", str(old))      # ce que fait un lanceur 2.0.x
    assert companion_mod.locate() == (old, companion_mod.BUILT_IN_BASE)
    package(tmp_path, store, key, "2.0.1")
    assert companion_mod.locate() == (in_place(store), "livré avec la version 2.0.1")


def test_explicit_choice_wins_and_stands_alone(tmp_path, store, key, installed, monkeypatch):
    package(tmp_path, store, key, "2.0.1")
    mine = fake_program(tmp_path / "ailleurs" / companion_mod.EXE)
    monkeypatch.setenv("AIODOS_NET_BIN", str(mine))
    assert companion_mod.locate() == (mine, "AIODOS_NET_BIN")
    monkeypatch.setenv("AIODOS_NET_BIN", str(tmp_path / "absent.exe"))
    assert companion_mod.locate() is None


def test_development_copy_runs_what_is_compiled_there(tmp_path, store, key, installed,
                                                      monkeypatch):
    package(tmp_path, store, key, "2.0.1")
    here = built(installed.root)
    monkeypatch.setattr(launcher, "developer_checkout", lambda: True)
    assert companion_mod.locate() == (here, companion_mod.BUILT_HERE)


def test_running_companion_knows_when_a_better_program_arrived(tmp_path, store, key,
                                                               installed, monkeypatch):
    on_path = fake_program(tmp_path / "PATH" / companion_mod.EXE)
    monkeypatch.setattr(companion_mod.shutil, "which", lambda name: str(on_path))
    running = companion_mod.Companion("127.0.0.1:1")
    running.process = SimpleNamespace(poll=lambda: None)
    running.listen, running.binary, running.origin = "127.0.0.1:2", on_path, "PATH"
    assert not running.outdated()
    package(tmp_path, store, key, "2.0.1")
    assert running.outdated()
    running._broken.add("livré avec la version 2.0.1")     # n'a pas démarré ici
    assert not running.outdated()


@pytest.mark.skipif(REAL is None, reason="compagnon aiodos-net non compilé")
def test_delivered_program_that_does_not_start_gives_way(tmp_path, store, key, installed):
    """Un compagnon livré inutilisable ici (construction ratée) : la machine garde le
    lien par Internet avec le programme suivant."""
    package(tmp_path, store, key, "2.0.1")             # faux programme : ne démarre pas
    shutil.copyfile(REAL, built(installed.base))
    companion = companion_mod.Companion("127.0.0.1:1", {
        "relays": ["none"], "directory": "none", "bind": "127.0.0.1:0", "portmap": False,
        "data_dir": tmp_path / "net"})
    try:
        assert not companion._launch()
        assert "livré avec la version 2.0.1" in companion.error
        assert companion._launch(), companion.error
        assert companion.origin == companion_mod.BUILT_IN_BASE
        assert not companion.outdated()
    finally:
        companion.stop()
