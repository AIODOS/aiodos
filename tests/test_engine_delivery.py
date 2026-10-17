"""Moteur llama.cpp livré avec les versions (gros modèles répartis) : il arrive avec la
version, n'est pas retéléchargé quand il ne change pas, se déplie une fois dans
data/engines/llama.cpp/<empreinte>/, et passe après un dossier choisi dans les réglages."""
import zipfile
from pathlib import Path

import pytest

from aiodos import settings
from aiodos.cluster import delivery, engine
from aiodos.updates import binaries, platforms
from aiodos.updates.store import Store
from tests.test_updates_service import PEER, world  # noqa: F401 (installation partagée)
from tests.test_updates_service_companion import announce
from tests.updates_kit import fake_code, new_key, publish, zip_folder

HERE = platforms.tag() or "linux-x86_64"
ENGINE = f"llama.cpp-{HERE}"


def engine_zip(path: Path, nested: str = "", programs=delivery.PROGRAMS, extra=b"") -> Path:
    """Une fausse archive officielle : les deux programmes (vides), une bibliothèque."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for program in programs:
            z.writestr(f"{nested}{platforms.executable(program, HERE)}", b"MZ" + extra)
        z.writestr(f"{nested}ggml-vulkan.dll", b"bibliotheque")
    return path


@pytest.fixture
def key(monkeypatch):
    private, trusted = new_key()
    monkeypatch.setattr("aiodos.updates.keys.TRUSTED_KEYS", trusted)
    monkeypatch.setattr(platforms, "tag", lambda: HERE)
    monkeypatch.setattr(binaries, "__version__", "2.0.1")        # la version qui tourne
    return private, trusted


def release(tmp_path, store, key, version, engine_path=None):
    code = fake_code(tmp_path / f"code-{version}", version)
    return publish(store, zip_folder(code, tmp_path / f"{version}.zip"), version, *key,
                   engines={HERE: engine_path} if engine_path else None)


# ------------------------------------------------------------------ paquets
def test_an_identical_file_is_copied_not_downloaded(tmp_path, key):
    store = Store()
    archive = engine_zip(tmp_path / "llama-b1-bin.zip")
    release(tmp_path, store, key, "2.0.1", archive)
    found = release(tmp_path, store, key, "2.0.2", archive)
    item = next(f for f in found["files"] if f["kind"] == ENGINE)
    store.package_path("2.0.2", item).unlink()                   # comme s'il manquait
    assert not store.has("2.0.2", item)
    assert store.reuse("2.0.2", item) and store.has("2.0.2", item)
    # Altéré dans l'autre paquet : refusé, rien n'est gardé
    store.package_path("2.0.2", item).unlink()
    store.package_path("2.0.1", item).write_bytes(b"autre chose de la meme taille"[:item["size"]]
                                                  .ljust(item["size"], b"x"))
    assert not store.reuse("2.0.2", item) and not store.has("2.0.2", item)


def test_the_delivered_engine_is_unpacked_once(tmp_path, key):
    store = Store()
    release(tmp_path, store, key, "2.0.1", engine_zip(tmp_path / "llama-b1-bin.zip"))
    folder = delivery.find(store)
    assert folder is not None and folder.parent == delivery.root() and delivery.complete(folder)
    marker = folder / "ajout-local.txt"
    marker.write_text("gardé", encoding="utf-8")
    assert delivery.find(store) == folder and marker.exists()    # pas redéplié
    # Une version plus récente, avec un autre moteur : la 2.0.1 tourne, elle garde le sien
    release(tmp_path, store, key, "2.0.2", engine_zip(tmp_path / "llama-b2-bin.zip", extra=b"2"))
    assert delivery.find(store) == folder


def test_an_engine_in_a_subfolder_or_incomplete(tmp_path, key):
    store = Store()
    release(tmp_path, store, key, "2.0.1",
            engine_zip(tmp_path / "llama-b1-bin.zip", nested="llama-b1/"))
    assert delivery.complete(delivery.find(store))
    other = Store(tmp_path / "autre")
    release(tmp_path, other, key, "2.0.1",
            engine_zip(tmp_path / "llama-b3-bin.zip", programs=("llama-server",)))
    assert delivery.find(other) is None                          # sans serveur RPC


def test_the_folder_of_the_settings_comes_first(tmp_path, key, monkeypatch):
    monkeypatch.setattr(delivery, "find", lambda store=None: None)
    assert engine.folder() is None
    manual = delivery.root()
    for program in delivery.PROGRAMS:
        manual.mkdir(parents=True, exist_ok=True)
        (manual / platforms.executable(program)).write_bytes(b"MZ")
    assert engine.folder() == manual                             # posé à la main
    delivered = tmp_path / "livre"
    monkeypatch.setattr(delivery, "find", lambda store=None: delivered)
    assert engine.folder() == delivered                          # livré : avant la main
    chosen = tmp_path / "choisi"
    settings.set_value("llamacpp_dir", str(chosen))
    assert engine.folder() is None                               # choisi mais incomplet
    for program in delivery.PROGRAMS:
        chosen.mkdir(parents=True, exist_ok=True)
        (chosen / platforms.executable(program)).write_bytes(b"MZ")
    assert engine.folder() == chosen


# ------------------------------------------------------------------ service
def test_the_engine_comes_with_the_new_version(world, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(platforms, "tag", lambda: HERE)
    code = fake_code(tmp_path / "code-moteur", "2.0.1")
    publish(world.lender, zip_folder(code, tmp_path / "c.zip"), "2.0.1", world.private,
            world.trusted, engines={HERE: engine_zip(tmp_path / "llama-b1-bin.zip")})
    announce(world)
    from aiodos.updates import p2p

    def call(my_id, peer, action, payload, key=None, timeout=10):
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", call)
    world.updates.tick()
    store = world.updates.store
    assert sorted(f["kind"] for f in store.held("2.0.1")) == sorted(["source", ENGINE])
    assert not world.updates.status()["error"]
