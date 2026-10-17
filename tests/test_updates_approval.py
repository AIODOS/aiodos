"""Approbation des versions sur un deuxième appareil (updates/approval.py) : dès qu'un
appareil d'approbation est déclaré, une version signée par l'auteur seul n'est ni
installée ni transmise ; l'appareil voit ce qu'elle contient, l'approuve, et elle part.
Une signature d'auteur ne vaut pas approbation, ni l'inverse ; un ami ne voit rien."""
import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos.network import peers
from aiodos.network.client import PeerError
from aiodos.updates import approval, keys, manifest
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store
from tests.updates_kit import fake_code, new_key, publish, zip_folder

KEY = bytes(32)
ME, PHONE, FRIEND = "a" * 32, "b" * 32, "c" * 32


def declared(private) -> dict:
    raw = manifest.raw_public(private)
    return {manifest.key_id(raw): base64.b64encode(raw).decode("ascii")}


def signed_release(author, version="2.0.1") -> dict:
    data = {"format": 1, "app": "aiodos", "version": version, "published": 0,
            "commit": "0" * 40, "notes": "Essai",
            "files": [{"kind": "source", "name": f"aiodos-{version}.zip", "size": 1,
                       "sha256": "0" * 64}]}
    return manifest.sign(data, author)


def test_an_approval_is_required_once_a_device_is_declared():
    author, trusted = new_key()
    phone = Ed25519PrivateKey.generate()
    approvers = declared(phone)
    signed = signed_release(author)
    assert manifest.verify(signed, trusted, approvers={})["version"] == "2.0.1"   # comme avant
    with pytest.raises(UpdateError, match="pas encore approuvée"):
        manifest.verify(signed, trusted, approvers)
    approved = manifest.approve(signed, phone)
    assert manifest.verify(approved, trusted, approvers)["version"] == "2.0.1"
    assert manifest.approve(approved, phone)["approvals"] == approved["approvals"]  # une fois
    # Une signature de l'appareil sur le texte seul (signature « d'auteur ») ne vaut pas
    # approbation, et une approbation d'un appareil inconnu non plus
    kid = next(iter(approvers))
    plain = base64.b64encode(phone.sign(signed["manifest"].encode())).decode()
    assert not manifest.approved(dict(signed, approvals=[{"key": kid, "signature": plain}]),
                                 approvers)
    stranger = manifest.approve(signed, Ed25519PrivateKey.generate())
    assert not manifest.approved(stranger, approvers)
    # Approbation d'une autre version : refusée
    other = manifest.approve(signed_release(author, "2.0.2"), phone)
    assert not manifest.approved(dict(signed, approvals=other["approvals"]), approvers)


@pytest.fixture
def circle(tmp_path, monkeypatch):
    """Cette machine publie ; le téléphone (une de mes machines) approuve ; tout passe par
    les actions scellées, rejouées ici sur le même dossier de données."""
    author, trusted = new_key()
    monkeypatch.setattr(keys, "TRUSTED_KEYS", trusted)
    peers.save(PHONE, "Téléphone", KEY, "127.0.0.1", 1)
    peers.save(ME, "PC portable", KEY, "127.0.0.1", 1)
    peers.save(FRIEND, "PC de Léa", KEY, "127.0.0.1", 1, trust="friend")
    info = approval.create_key()
    monkeypatch.setattr(keys, "APPROVER_KEYS", {info["id"]: info["public"]})
    store = Store()
    code = fake_code(tmp_path / "code", "2.0.1")
    publish(store, zip_folder(code, tmp_path / "c.zip"), "2.0.1", author, trusted,
            approvers={})                                        # comme tools/publish.py

    def call(my_id, peer, action, payload, key=None, timeout=10):
        return approval.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(approval.client, "call", call)
    return store, info


def test_a_release_waits_for_its_approval_then_leaves(circle):
    store, info = circle
    assert not info["declared"]                     # créée avant d'être déclarée dans keys.py
    assert approval.public_info()["declared"]
    assert info["line"].strip().startswith(f'"{info["id"]}"')
    assert [v for v, _ in store.pending()] == ["2.0.1"]
    assert store.offer() is None and store.available() == {}     # ni installée, ni proposée
    waiting = approval.pending_from_peers(PHONE)
    assert [w["version"] for w in waiting] == ["2.0.1", "2.0.1"]   # vue de ses deux machines
    assert waiting[0]["notes"] == "" and waiting[0]["files"][0]["kind"] == "source"
    assert approval.approve(PHONE, ME, "2.0.1") == {"ok": True, "version": "2.0.1",
                                                    "machine": "PC portable"}
    assert store.pending() == [] and store.offer() == "2.0.1"
    with pytest.raises(UpdateError, match="n'attend plus"):
        approval.approve(PHONE, ME, "2.0.1")


def test_friends_see_nothing_and_forged_approvals_are_refused(circle):
    store, _ = circle
    with pytest.raises(PeerError, match="Seules mes machines"):
        approval.serve_pending(None, FRIEND, {})
    forged = manifest.approve(store.signed("2.0.1"), Ed25519PrivateKey.generate())
    with pytest.raises(PeerError, match="Approbation refusée"):
        approval.serve_approve(None, PHONE, {"version": "2.0.1",
                                             "approval": forged["approvals"][-1]})
    assert store.offer() is None
    with pytest.raises(UpdateError, match="déjà sa clé"):
        approval.create_key()


def test_without_a_declared_device_nothing_changes(tmp_path, monkeypatch):
    author, trusted = new_key()
    monkeypatch.setattr(keys, "TRUSTED_KEYS", trusted)
    store = Store()
    code = fake_code(tmp_path / "code", "2.0.1")
    publish(store, zip_folder(code, tmp_path / "c.zip"), "2.0.1", author, trusted)
    assert store.pending() == [] and store.offer() == "2.0.1"
    assert approval.public_info() is None
