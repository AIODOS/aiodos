# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Le coffre de la clé : DPAPI (Windows), fichier en 0600, mémoire ; jamais la clé dans repr."""

import os
import stat
import sys

import pytest

from aiodos_client import Aiodos, Keystore
from aiodos_client.fake import new_key
from support import APP

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI : Windows seulement")


@windows_only
def test_dpapi_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("COFFRE_ESSAI", str(tmp_path))
    store = Keystore.dpapi(r"%COFFRE_ESSAI%\PANDORA\aiodos.key", app_id="arkane.pandora")
    assert store.path == str(tmp_path / "PANDORA" / "aiodos.key")
    assert store.get() is None
    key = new_key()
    store.set(key)
    raw = (tmp_path / "PANDORA" / "aiodos.key").read_bytes()
    assert key.encode() not in raw and key[20:].encode() not in raw
    assert raw.startswith(b"aiodos-client:dpapi1:")
    assert store.get() == key
    again = Keystore.dpapi(store.path, app_id="arkane.pandora")
    assert again.get() == key
    other = Keystore.dpapi(store.path, app_id="autre.appli")     # autre entropie : illisible
    assert other.get() is None
    store.delete()
    assert store.get() is None and not os.path.exists(store.path)
    store.delete()                                               # déjà effacée : rien


@windows_only
def test_dpapi_takes_the_app_id_from_the_client(tmp_path):
    store = Keystore.dpapi(tmp_path / "aiodos.key")
    with pytest.raises(ValueError):
        store.get()                                              # pas encore d'app_id
    Aiodos(**APP, keystore=store)
    key = new_key()
    store.set(key)
    assert Keystore.dpapi(tmp_path / "aiodos.key", app_id=APP["app_id"]).get() == key
    with pytest.raises(ValueError):
        Aiodos(app="Autre", app_id="autre.appli", version="1", keystore=store)


@windows_only
def test_damaged_dpapi_file_reads_as_absent(tmp_path):
    path = tmp_path / "aiodos.key"
    store = Keystore.dpapi(path, app_id="arkane.pandora")
    path.write_bytes(b"aiodos-client:dpapi1:abime")
    assert store.get() is None
    path.write_bytes(b"n'importe quoi")
    assert store.get() is None


@pytest.mark.skipif(sys.platform == "win32", reason="hors de Windows seulement")
def test_dpapi_is_refused_elsewhere(tmp_path):
    with pytest.raises(OSError):
        Keystore.dpapi(tmp_path / "aiodos.key")


def test_file_round_trip(tmp_path):
    path = tmp_path / "secret" / "aiodos.key"
    store = Keystore.file(path)
    assert store.get() is None
    key = new_key()
    store.set(key)
    assert store.get() == key
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    store.set(new_key())                                         # remplacée d'un coup
    assert store.get() != key
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".aiodos-key-")]
    store.delete()
    assert store.get() is None


def test_memory_and_repr():
    key = new_key()
    store = Keystore.memory(key)
    assert store.get() == key
    for text in (repr(store), str(store)):
        assert key not in text and key[20:] not in text
    store.delete()
    assert store.get() is None and repr(store) == "Keystore.memory(vide)"
    with pytest.raises(ValueError):
        store.set("  ")


def test_custom_keystore_and_bad_ones():
    class Mine:
        def __init__(self):
            self.value = None

        def get(self):
            return self.value

        def set(self, key):
            self.value = key

        def delete(self):
            self.value = None

    Aiodos(**APP, keystore=Mine())
    with pytest.raises(TypeError):
        Aiodos(**APP, keystore=object())
    for wrong in ({"app": ""}, {"app": "x" * 41}, {"app_id": "Arkane"}, {"app_id": "a"},
                  {"version": "1" * 41}, {"timeout": 0}):
        values = dict(APP)
        values.update(wrong)
        with pytest.raises(ValueError):
            Aiodos(**values)
    assert Aiodos(**dict(APP, app="PAN‮DORA")).app == "PANDORA"   # sens d'écriture retiré
