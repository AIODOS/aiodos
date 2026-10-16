"""Versions transmises entre machines appairées : la machine qui transmet ne sert que
les fichiers d'un paquet complet ; celle qui reçoit vérifie tout et reprend un
transfert interrompu. Le transport scellé est remplacé ici par un appel direct."""
import os

import pytest

from aiodos.network import envelope
from aiodos.network.client import PeerError
from aiodos.updates import p2p
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store
from tests.updates_kit import fake_code, new_key, publish, zip_folder


@pytest.fixture
def key(monkeypatch):
    private, trusted = new_key()
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", trusted)
    return private, trusted


@pytest.fixture
def lender(tmp_path, key, monkeypatch):
    """La machine qui a la version 2.0.1 et la transmet."""
    store = Store(tmp_path / "lender")
    code = fake_code(tmp_path / "code", "2.0.1", server=f"X = '{os.urandom(4000).hex()}'")
    publish(store, zip_folder(code, tmp_path / "a.zip"), "2.0.1", *key)
    monkeypatch.setattr(p2p, "_store", lambda: store)
    return store


@pytest.fixture
def calls(monkeypatch):
    """Les appels de la machine qui reçoit, servis directement par les fonctions de l'autre."""
    seen = []

    def call(my_id, peer, action, payload, key=None, timeout=10):
        seen.append((action, payload.get("offset")))
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", call)
    return seen


def test_only_complete_packages_are_served(lender, tmp_path):
    assert p2p.serve_manifest(None, "x", {"version": "2.0.1"})["signed"]
    for wanted in ("2.0.2", "../2.0.1", None):
        with pytest.raises(PeerError):
            p2p.serve_manifest(None, "x", {"version": wanted})
    (lender.root / "packages" / "2.0.1" / "aiodos-2.0.1.zip").unlink()
    with pytest.raises(PeerError):
        p2p.serve_manifest(None, "x", {"version": "2.0.1"})


def test_only_files_named_in_the_manifest_are_served(lender):
    data = p2p.serve_chunk(None, "x", {"version": "2.0.1", "name": "aiodos-2.0.1.zip",
                                       "offset": 0, "size": 10 ** 9})
    assert 0 < len(envelope.b64d(data["data"])) <= p2p.CHUNK
    for name in ("manifest.json", "../state.json", "autre.zip"):
        with pytest.raises(PeerError):
            p2p.serve_chunk(None, "x", {"version": "2.0.1", "name": name})
    with pytest.raises(PeerError):
        p2p.serve_chunk(None, "x", {"version": "2.0.1", "name": "aiodos-2.0.1.zip",
                                    "offset": "beaucoup"})


def test_version_is_fetched_verified_and_kept_for_others(lender, calls, tmp_path, monkeypatch):
    monkeypatch.setattr(p2p, "CHUNK", 1000)                 # plusieurs morceaux
    mine = Store(tmp_path / "mine")
    progress = []
    found = p2p.fetch("moi", {"node_id": "lui"}, "2.0.1", mine, lambda r, s: progress.append(r))
    assert found["version"] == "2.0.1"
    assert mine.complete("2.0.1") and mine.offer() == "2.0.1"
    assert progress[-1] == found["files"][0]["size"] and len(progress) > 1
    assert not (mine.root / "downloads" / "2.0.1").exists()


def test_interrupted_transfer_resumes_where_it_stopped(lender, calls, tmp_path, monkeypatch):
    monkeypatch.setattr(p2p, "CHUNK", 1000)
    mine = Store(tmp_path / "mine")
    source = lender.file_path("2.0.1", "aiodos-2.0.1.zip").read_bytes()
    mine.partial("2.0.1", "aiodos-2.0.1.zip").write_bytes(source[:2500])
    p2p.fetch("moi", {"node_id": "lui"}, "2.0.1", mine)
    assert [offset for action, offset in calls if action == "update/chunk"][0] == 2500
    assert mine.file_path("2.0.1", "aiodos-2.0.1.zip").read_bytes() == source


def test_altered_transfer_is_dropped(lender, calls, tmp_path, monkeypatch):
    real = p2p.serve_chunk

    def altered(manager, sender, payload):
        reply = real(manager, sender, payload)
        data = bytearray(envelope.b64d(reply["data"]))
        data[0] ^= 1
        return {"data": envelope.b64e(bytes(data))}
    monkeypatch.setitem(p2p.HANDLERS, "update/chunk", altered)
    mine = Store(tmp_path / "mine")
    with pytest.raises(UpdateError, match="altéré"):
        p2p.fetch("moi", {"node_id": "lui"}, "2.0.1", mine)
    assert not mine.complete("2.0.1")
    assert not mine.partial("2.0.1", "aiodos-2.0.1.zip").exists()


def test_another_version_than_announced_is_refused(lender, tmp_path, key, monkeypatch):
    code = fake_code(tmp_path / "old", "2.0.0")
    publish(lender, zip_folder(code, tmp_path / "old.zip"), "2.0.0", *key)
    monkeypatch.setattr(p2p.client, "call", lambda *a, **k: p2p.serve_manifest(
        None, "x", {"version": "2.0.0"}))
    with pytest.raises(UpdateError, match="autre version"):
        p2p.fetch("moi", {"node_id": "lui"}, "2.0.1", Store(tmp_path / "mine"))


def test_version_signed_by_an_unknown_key_is_refused(lender, tmp_path, monkeypatch):
    reply = p2p.serve_manifest(None, "x", {"version": "2.0.1"})
    monkeypatch.setattr(p2p.client, "call", lambda *a, **k: reply)
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", {})      # ici, autre clé de confiance
    with pytest.raises(UpdateError, match="clé inconnue"):
        p2p.fetch("moi", {"node_id": "lui"}, "2.0.1", Store(tmp_path / "mine"))
