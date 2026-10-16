"""Installation d'une version reçue : rien ne sort de son dossier, le code annonce le
bon numéro, il se charge sur cette machine, sinon rien ne change."""
import stat
import zipfile

import pytest

from aiodos.updates import install
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store
from tests.updates_kit import fake_code, new_key, publish, zip_folder


@pytest.fixture
def key():
    return new_key()


@pytest.fixture
def store(data_dir):
    return Store(data_dir / "updates")


def package(tmp_path, store, key, version="2.0.1", declared=None, server="", **extra):
    private, trusted = key
    code = fake_code(tmp_path / f"code-{version}", declared or version, server=server)
    archive = zip_folder(code, tmp_path / f"aiodos-{version}.zip")
    return publish(store, archive, version, private, trusted, **extra)


def test_a_signed_version_is_installed_beside_the_running_one(tmp_path, store, key, monkeypatch):
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", key[1])
    found = package(tmp_path, store, key)
    target = install.install(store, found)
    assert target == store.version_dir("2.0.1")
    assert (target / "aiodos" / "__init__.py").is_file()
    assert store.installed() == ["2.0.1"]
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]  # rien d'inachevé
    assert install.install(store, found) == target          # déjà là : rien à refaire


def test_code_announcing_another_number_is_refused(tmp_path, store, key, monkeypatch):
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", key[1])
    found = package(tmp_path, store, key, declared="2.0.9")
    with pytest.raises(UpdateError, match="annonce la version 2.0.9"):
        install.install(store, found)
    assert store.installed() == []


def test_missing_python_package_is_named_and_nothing_changes(tmp_path, store, key, monkeypatch):
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", key[1])
    found = package(tmp_path, store, key, server="import paquet_absent_pour_ce_test\n")
    with pytest.raises(UpdateError, match="« paquet_absent_pour_ce_test »"):
        install.install(store, found)
    assert store.installed() == []
    assert not list((store.root / "versions").iterdir())


def test_too_recent_python_requirement_is_refused(tmp_path, store, key, monkeypatch):
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", key[1])
    found = package(tmp_path, store, key, min_python="99.0")
    with pytest.raises(UpdateError, match="Python 99.0"):
        install.install(store, found)


def test_altered_archive_is_refused_and_dropped(tmp_path, store, key, monkeypatch):
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", key[1])
    found = package(tmp_path, store, key)
    archive = store.file_path("2.0.1", "aiodos-2.0.1.zip")
    archive.write_bytes(archive.read_bytes()[:-1] + b"!")
    with pytest.raises(UpdateError, match="altérée"):
        install.install(store, found)
    assert not archive.exists()                  # elle sera téléchargée de nouveau
    assert not store.complete("2.0.1")


def trapped_zip(path, name, data=b"x", mode=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("aiodos/__init__.py", '__version__ = "2.0.1"\n')
        info = zipfile.ZipInfo(name)
        if mode is not None:
            info.external_attr = mode << 16
        z.writestr(info, data)
    return path


@pytest.mark.parametrize("name, mode", [
    ("../evade.txt", None), ("aiodos/../../evade.txt", None), ("/evade.txt", None),
    ("C:/evade.txt", None), ("aiodos\\..\\..\\evade.txt", None), ("aiodos/con.py", None),
    ("aiodos/lien", stat.S_IFLNK | 0o777),
])
def test_nothing_escapes_the_version_folder(tmp_path, name, mode):
    archive = trapped_zip(tmp_path / "piege.zip", name, mode=mode)
    dest = tmp_path / "versions" / "2.0.1"
    with pytest.raises(UpdateError):
        install.extract(archive, dest)
    assert not (tmp_path / "evade.txt").exists() and not (tmp_path / "versions" / "evade.txt").exists()


def test_archive_bombs_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "MAX_TOTAL", 1000)
    archive = trapped_zip(tmp_path / "bombe.zip", "aiodos/gros.bin", data=b"0" * 5000)
    with pytest.raises(UpdateError, match="trop volumineuse"):
        install.extract(archive, tmp_path / "dest")


def test_archive_without_aiodos_is_refused(tmp_path):
    with zipfile.ZipFile(tmp_path / "vide.zip", "w") as z:
        z.writestr("autre/fichier.txt", "x")
    with pytest.raises(UpdateError, match="sans le code"):
        install.extract(tmp_path / "vide.zip", tmp_path / "dest")
