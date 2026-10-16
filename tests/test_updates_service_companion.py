"""Service de mise à jour, compagnon Internet : celui de cette plateforme arrive avec une
nouvelle version ; une version installée par une machine 2.0.x (qui ne prend que
l'archive du code) ou depuis une archive va le chercher elle-même, chez une machine qui
l'annonce ; arrivé, il remplace le programme en service dès que rien ne passe par lui."""
from types import SimpleNamespace

import pytest

from aiodos.network import peers
from aiodos.node import identity
from aiodos.updates import binaries, launcher, manifest, p2p, platforms, service
from aiodos.updates.store import Store
from tests.test_updates_service import PEER, world  # noqa: F401 (installation partagée)
from tests.updates_kit import fake_code, fake_program, publish, zip_folder

HERE = platforms.tag() or "linux-x86_64"
COMPANION = f"aiodos-net-{HERE}"


@pytest.fixture
def lender(world, tmp_path, monkeypatch):  # noqa: F811
    """La machine appairée publie la 2.0.1 avec le compagnon de cette plateforme (et
    l'annonce dans son profil) ; les appels qu'on lui fait sont notés."""
    monkeypatch.setattr(platforms, "tag", lambda: HERE)
    code = fake_code(tmp_path / "code-companion", "2.0.1")
    publish(world.lender, zip_folder(code, tmp_path / "c.zip"), "2.0.1", world.private,
            world.trusted, companions={HERE: fake_program(tmp_path / "prog", b"livre")})
    announce(world)
    asked = []

    def call(my_id, peer, action, payload, key=None, timeout=10):
        asked.append((action, payload.get("name")))
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", call)
    world.asked = asked
    return world.lender


def announce(world, files=True):
    profile = {"offer": world.lender.offer()}
    if files:
        profile["files"] = world.lender.available()
    peers.set_profile(PEER, {"updates": profile})


def kinds(store, version="2.0.1"):
    return sorted(f["kind"] for f in store.held(version))


def companion_name(store, version="2.0.1"):
    return manifest.binary(store.manifest(version))["name"]


def test_the_companion_of_this_platform_comes_with_the_new_version(world, lender):  # noqa: F811
    world.updates.tick()
    store = world.updates.store
    assert kinds(store) == sorted(["source", COMPANION])
    assert store.installed() == ["2.0.1"] and world.restarts == [True]   # reçu avant
    assert not world.updates.status()["error"]


def as_installed_by_an_old_machine(world, monkeypatch):  # noqa: F811
    """Cette machine tourne déjà en 2.0.1 : une version 2.0.x l'a reçue et installée,
    sans son compagnon (elle ne prend que l'archive du code)."""
    monkeypatch.setattr(service, "__version__", "2.0.1")
    mine = world.updates.store
    found = mine.add_manifest(world.lender.signed("2.0.1"))
    source = manifest.source(found)
    mine.save_file("2.0.1", source, world.lender.package_path("2.0.1", source))
    return mine


def test_a_version_installed_by_an_old_machine_fetches_its_companion(world, lender,  # noqa: F811
                                                                     monkeypatch):
    mine = as_installed_by_an_old_machine(world, monkeypatch)
    world.updates.tick()
    assert kinds(mine) == sorted(["source", COMPANION])
    assert world.asked[-1] == ("update/chunk", companion_name(mine))
    assert world.restarts == []                          # rien d'autre à faire


def test_installed_from_an_archive_the_companion_and_its_manifest_are_fetched(  # noqa: F811
        world, lender, monkeypatch):
    monkeypatch.setattr(service, "__version__", "2.0.1")
    world.updates.tick()
    mine = world.updates.store
    assert kinds(mine) == [COMPANION] and mine.offer() is None
    assert [a for a, _ in world.asked] == ["update/manifest", "update/chunk"]


def test_machines_that_do_not_announce_it_are_not_asked(world, lender, monkeypatch):  # noqa: F811
    announce(world, files=False)                         # machine 2.0.x : rien d'annoncé
    world.updates.tick()
    assert kinds(world.updates.store) == ["source"]
    assert ("update/chunk", companion_name(world.lender)) not in world.asked


def test_a_machine_that_fails_to_give_it_is_left_alone_for_a_while(world, lender,  # noqa: F811
                                                                   caplog):
    world.lender.package_path("2.0.1", manifest.binary(world.lender.manifest("2.0.1"))).unlink()
    world.updates.tick()                                 # annoncé, mais plus là
    assert "Compagnon de la version 2.0.1 non reçu de « PC de Léa »" in caplog.text
    asked = len(world.asked)
    world.updates.tick()
    assert len(world.asked) == asked                     # pas redemandé aussitôt
    assert world.updates.store.installed() == ["2.0.1"]  # la version, elle, est là


def test_the_companion_is_relaunched_onto_it_when_nothing_goes_through_it(world, monkeypatch):  # noqa: F811
    relaunched = []
    running = SimpleNamespace(outdated=lambda: True)
    world.updates.network = SimpleNamespace(node_id="moi", internet=SimpleNamespace(
        companion=running, relaunch_from_thread=lambda: relaunched.append(True)))
    world.executor.busy["embed"] = 1                    # une unité en cours
    world.updates.tick()
    assert relaunched == []
    world.executor.busy["embed"] = 0
    world.updates.windowed = True                       # quelqu'un au clavier : peu importe,
    monkeypatch.setattr(identity, "idle_seconds", lambda: 5)    # la relance ne se voit pas
    world.updates.tick()
    assert relaunched == [True]
    running.outdated = lambda: False
    world.updates.tick()
    assert relaunched == [True]


def test_the_package_of_the_companion_in_service_and_of_the_original_version_are_kept(
        world, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(platforms, "tag", lambda: HERE)
    mine = world.updates.store
    for number in ("2.0.0", "2.0.1", "2.0.2", "2.0.3", "2.0.4"):
        code = fake_code(tmp_path / f"code-{number}", number)
        publish(mine, zip_folder(code, tmp_path / f"{number}.zip"), number, world.private,
                world.trusted, companions={HERE: fake_program(tmp_path / f"p-{number}")})
    monkeypatch.setattr(binaries, "__version__", "2.0.1")
    assert binaries.find(mine)[1] == "2.0.1"            # compagnon en service : 2.0.1
    base = fake_code(tmp_path / "origine", "2.0.0")     # version d'origine : 2.0.0
    monkeypatch.setattr(launcher, "supervised", lambda: True)
    monkeypatch.setattr(launcher, "base_dir", lambda: base)
    monkeypatch.setattr(service, "__version__", "2.0.2")
    world.updates._tidy()
    assert mine.package_versions() == ["2.0.0", "2.0.1", "2.0.2", "2.0.3", "2.0.4"]
    monkeypatch.setattr(launcher, "supervised", lambda: False)
    world.updates._tidy()                               # sans eux : les deux plus récents
    assert mine.package_versions() == ["2.0.1", "2.0.2", "2.0.3", "2.0.4"]
    assert Store(mine.root).manifest("2.0.1")           # et ceux de `keep`
