"""Sécurité des mises à jour (audit du 20/10/2026) : aucune machine appairée ne gèle le
service ni ne lui fait garder une enveloppe gonflée ; l'appareil d'approbation ne signe que
ce qu'il a montré ; un nœud dont la version d'origine ignore les amis le dit ; les outils
de publication refusent un dépôt ouvert à d'autres comptes et lancent git sans ce que
.git/config ferait exécuter ; l'installeur fige ses paquets par empreinte et ne s'installe
que dans le profil."""
import base64
import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from importlib import metadata

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos.network import envelope, peers
from aiodos.updates import approval, keys, launcher, p2p, private, service, state
from aiodos.updates import manifest as manifests
from aiodos.updates import store as store_module
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store
from tests.nodes import APP_ROOT
from tests.test_updates_approval import ME, PHONE, circle  # noqa: F401 (installation partagée)
from tests.test_updates_service import PEER, world  # noqa: F401 (installation partagée)
from tests.updates_kit import fake_code, installed_fake, new_key, publish, zip_folder

sys.path.insert(0, str(APP_ROOT / "tools"))
import build_installer  # noqa: E402
import companion_build  # noqa: E402
import publish as publish_tool  # noqa: E402

HOSTILE = "e" * 32


@pytest.fixture(autouse=True)
def no_stuck_calls():
    """Aucun appel resté en attente d'un essai précédent (p2p._stuck), rien de montré à
    l'appareil d'approbation (approval.forget)."""
    getattr(p2p, "_stuck", {}).clear()
    approval.forget()
    yield
    getattr(p2p, "_stuck", {}).clear()
    approval.forget()


# ======================================================== une machine qui gèle le service
def test_a_call_that_never_ends_is_abandoned_and_the_machine_not_asked_again(monkeypatch):
    release, calls = threading.Event(), []

    def trickle(*args, **kwargs):              # un octet de temps en temps : jamais fini
        calls.append(True)
        release.wait(30)
        return {"data": ""}
    monkeypatch.setattr(p2p.client, "call", trickle)
    peer = {"node_id": "lente"}
    started = time.monotonic()
    with pytest.raises(UpdateError, match="trop lentement"):
        p2p._call("moi", peer, "update/chunk", {}, limit=0.2)
    assert time.monotonic() - started < 5
    with pytest.raises(UpdateError, match="toujours pas répondu"):
        p2p._call("moi", peer, "update/chunk", {}, limit=0.2)
    assert len(calls) == 1                     # un seul fil, au plus, l'attend
    release.set()
    p2p._stuck["lente"].join(5)
    assert p2p._call("moi", peer, "update/chunk", {}) == {"data": ""}   # fini : de nouveau


@pytest.fixture
def lender(tmp_path, monkeypatch):
    """Une machine qui transmet la 2.0.1 (archive de quelques Ko)."""
    private_key, trusted = new_key()
    monkeypatch.setattr(keys, "TRUSTED_KEYS", trusted)
    store = Store(tmp_path / "lender")
    code = fake_code(tmp_path / "code", "2.0.1", server=f"X = '{os.urandom(4000).hex()}'")
    publish(store, zip_folder(code, tmp_path / "a.zip"), "2.0.1", private_key, trusted)
    monkeypatch.setattr(p2p, "_store", lambda: store)
    monkeypatch.setattr(p2p, "CHUNK", 1000)
    return store


def test_a_fetch_from_a_machine_that_stalls_ends_on_time(lender, tmp_path, monkeypatch):
    release = threading.Event()

    def stall(my_id, peer, action, payload, key=None, timeout=10):
        if action == "update/chunk":
            release.wait(30)
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", stall)
    monkeypatch.setattr(p2p, "CALL_LIMIT", 0.3)
    started = time.monotonic()
    try:
        with pytest.raises(UpdateError, match="trop lentement"):
            p2p.fetch("moi", {"node_id": "lente"}, "2.0.1", Store(tmp_path / "mine"))
        assert time.monotonic() - started < 10
    finally:
        release.set()


def test_each_chunk_must_have_the_size_asked_for(lender, tmp_path, monkeypatch):
    def drip(my_id, peer, action, payload, key=None, timeout=10):
        reply = p2p.HANDLERS[action](None, my_id, payload)
        if action == "update/chunk":           # un octet utile par réponse
            reply = {"data": envelope.b64e(envelope.b64d(reply["data"])[:1])}
        return reply
    monkeypatch.setattr(p2p.client, "call", drip)
    mine = Store(tmp_path / "mine")
    with pytest.raises(UpdateError, match="taille inattendue"):
        p2p.fetch("moi", {"node_id": "lente"}, "2.0.1", mine)
    assert not mine.complete("2.0.1")


def test_a_file_too_slow_overall_is_abandoned_then_resumed(lender, tmp_path, monkeypatch):
    def slow(my_id, peer, action, payload, key=None, timeout=10):
        time.sleep(0.15)
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", slow)
    monkeypatch.setattr(p2p, "MIN_RATE", 10 ** 9)          # échéance : la marge seule
    monkeypatch.setattr(p2p, "FILE_MARGIN", 0.4)
    mine = Store(tmp_path / "mine")
    with pytest.raises(UpdateError, match="trop lentement"):
        p2p.fetch("moi", {"node_id": "lente"}, "2.0.1", mine)
    part = mine.partial("2.0.1", "aiodos-2.0.1.zip", "lente")
    kept = part.stat().st_size
    assert 0 < kept < lender.file_path("2.0.1", "aiodos-2.0.1.zip").stat().st_size
    if "lente" in p2p._stuck:                                 # le dernier appel, abandonné,
        p2p._stuck["lente"].join(5)                           # doit d'abord finir
    monkeypatch.setattr(p2p, "FILE_MARGIN", 60)
    p2p.fetch("moi", {"node_id": "lente"}, "2.0.1", mine)    # reprend là où il en était
    assert mine.complete("2.0.1")


def test_a_failing_machine_is_set_aside_and_the_next_one_tried_at_once(world,  # noqa: F811
                                                                      monkeypatch):
    peers.save(HOSTILE, "Machine piégée", b"h" * 32, "127.0.0.1", 47702)
    peers.set_profile(HOSTILE, {"updates": {"offer": "9999.0.0"}})       # numéro fictif
    asked = []

    def call(my_id, peer, action, payload, key=None, timeout=10):
        asked.append(peer["node_id"])
        if peer["node_id"] == HOSTILE:
            raise p2p.PeerError("Version indisponible sur cette machine.")
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", call)
    world.updates.tick()
    assert world.updates.store.installed() == ["2.0.1"]     # dans le même tour
    peers.set_profile(HOSTILE, {"updates": {"offer": "9999.0.1"}})       # autre numéro
    before = len(asked)
    world.updates.tick()
    assert HOSTILE not in asked[before:]                    # la machine reste de côté


def test_a_slow_machine_holds_neither_installation_nor_restart(world, tmp_path,  # noqa: F811
                                                                monkeypatch):
    store = world.updates.store                             # la 2.0.1 est déjà là
    publish(store, zip_folder(fake_code(tmp_path / "c201", "2.0.1"), tmp_path / "c201.zip"),
            "2.0.1", world.private, world.trusted)
    peers.set_profile(PEER, {"updates": {"offer": "2.0.2"}})            # et ne vient jamais
    release = threading.Event()

    def stuck(*args, **kwargs):
        release.wait(30)
        raise p2p.PeerError("Coupé.")
    monkeypatch.setattr(p2p.client, "call", stuck)
    monkeypatch.setattr(service, "RECEIVE_WAIT", 0.2)
    started = time.monotonic()
    try:
        world.updates.tick()
        assert time.monotonic() - started < 10
        assert store.installed() == ["2.0.1"] and world.restarts == [True]
    finally:
        release.set()
        world.updates._receiving.join(10)


def test_the_restart_still_waits_for_the_files_of_its_own_version(world):  # noqa: F811
    world.updates.tick()                                    # 2.0.1 reçue et installée
    release = threading.Event()
    busy = threading.Thread(target=release.wait, args=(30,), daemon=True)
    busy.start()
    try:
        world.updates._receiving = busy                     # son compagnon arrive encore
        world.updates.download = {"version": "2.0.1", "from": "PC de Léa", "received": 0,
                                  "size": 1, "what": "compagnon"}
        assert not world.updates._should_restart()
        world.updates.download = {"version": "2.0.2", "from": "PC de Léa", "received": 0,
                                  "size": 1}                # une autre version : n'attend pas
        assert world.updates._should_restart()
    finally:
        release.set()


# ================================================ profil d'une machine mal formé
def test_malformed_profiles_do_not_stop_the_updates(world):  # noqa: F811
    for number, profile in enumerate(({"updates": "x"}, {"updates": ["x"]}, "profil", [1, 2],
                                      {"updates": {"offer": ["2.0.9"], "files": "x"}},
                                      {"updates": {"offer": 3, "files": {"2.0.1": "source"}}})):
        node = f"{number + 1:032x}"
        peers.save(node, f"Ami {number}", b"a" * 32, "127.0.0.1", 1, trust="friend")
        peers.set_profile(node, profile)
        assert p2p.offered_by({"profile": profile}) is None
        assert p2p.held_by({"profile": profile}, "2.0.1") == []
    world.updates.tick()
    assert world.updates.store.installed() == ["2.0.1"] and world.restarts == [True]


def test_a_failing_step_is_shown_and_does_not_stop_the_others(world, monkeypatch):  # noqa: F811
    def broken():
        raise RuntimeError("relance impossible")
    monkeypatch.setattr(world.updates, "_switch_companion", broken)
    world.updates.tick()
    assert world.updates.store.installed() == ["2.0.1"]     # l'installation a eu lieu

    def boom(*args, **kwargs):
        raise RuntimeError("boum")
    monkeypatch.setattr(service.install, "install", boom)
    publish(world.lender, zip_folder(fake_code(world.updates.store.root.parent / "c2", "2.0.2"),
                                     world.updates.store.root.parent / "c2.zip"),
            "2.0.2", world.private, world.trusted)
    peers.set_profile(PEER, {"updates": {"offer": "2.0.2"}})
    world.updates.tick()                                    # ne lève rien
    error = world.updates.status()["error"]
    assert "installation" in error and "boum" in error


# ========================================================= enveloppe gonflée
def bogus_approvals(count: int, kid: str = None) -> list:
    return [{"key": kid or f"{n:016x}", "signature": base64.b64encode(os.urandom(64)).decode()}
            for n in range(count)]


def test_a_relayed_envelope_is_kept_and_relayed_in_canonical_form(lender, tmp_path):
    signed = lender.signed("2.0.1")
    bloated = dict(signed, junk="x" * 1_000_000, approvals=bogus_approvals(1000))
    mine = Store(tmp_path / "mine")
    mine.add_manifest(bloated)
    kept = json.loads((mine.root / "packages" / "2.0.1" / "manifest.json").read_text("utf-8"))
    assert set(kept) <= {"manifest", "key", "signature", "approvals"}
    assert len(kept["approvals"]) <= manifests.MAX_APPROVALS
    assert kept["manifest"] == signed["manifest"]           # le texte signé, tel quel
    # Gardée gonflée par une version plus ancienne : retransmise réduite
    path = lender.root / "packages" / "2.0.1" / "manifest.json"
    path.write_text(json.dumps(bloated), encoding="utf-8")
    served = p2p.serve_manifest(None, "x", {"version": "2.0.1"})["signed"]
    assert "junk" not in served and len(served["approvals"]) <= manifests.MAX_APPROVALS
    assert manifests.verify(served)["version"] == "2.0.1"


def test_a_giant_manifest_text_is_refused(monkeypatch):
    author, trusted = new_key()
    data = {"format": 1, "app": "aiodos", "version": "2.0.1", "published": 0,
            "commit": "0" * 40, "notes": "x" * (manifests.MAX_TEXT + 1),
            "files": [{"kind": "source", "name": "a.zip", "size": 1, "sha256": "0" * 64}]}
    with pytest.raises(UpdateError, match="démesuré"):
        manifests.verify(manifests.sign(data, author), trusted, approvers={})


def test_approval_checks_are_bounded_and_the_valid_one_is_kept(monkeypatch):
    author, trusted = new_key()
    phone = Ed25519PrivateKey.generate()
    raw = manifests.raw_public(phone)
    kid = manifests.key_id(raw)
    approvers = {kid: base64.b64encode(raw).decode("ascii")}
    data = {"format": 1, "app": "aiodos", "version": "2.0.1", "published": 0,
            "commit": "0" * 40, "notes": "",
            "files": [{"kind": "source", "name": "a.zip", "size": 1, "sha256": "0" * 64}]}
    real = manifests.approve(manifests.sign(data, author), phone)["approvals"][0]
    checked = []
    original = manifests._approval_valid

    def counted(*args):
        checked.append(True)
        return original(*args)
    monkeypatch.setattr(manifests, "_approval_valid", counted)
    flood = dict(manifests.sign(data, author), approvals=bogus_approvals(20_000, kid) + [real])
    assert not manifests.approved(flood, approvers)         # au-delà de MAX_SCAN : ignorée
    assert len(checked) <= manifests.MAX_SCAN
    mixed = dict(flood, approvals=bogus_approvals(10, kid) + [real] + bogus_approvals(5000))
    assert manifests.verify(mixed, trusted, approvers)["version"] == "2.0.1"
    kept = manifests.canonical(mixed, approvers)
    assert kept["approvals"][0] == real and len(kept["approvals"]) <= manifests.MAX_APPROVALS
    # Une machine sans appareil déclaré relaie l'approbation bien formée qu'elle ne sait pas
    # vérifier, pour celles qui l'exigent
    assert real in manifests.canonical(dict(flood, approvals=[real]), {})["approvals"]


# ============================================== l'appareil signe ce qu'il a montré
def test_the_device_signs_only_what_it_showed(circle, tmp_path, monkeypatch):  # noqa: F811
    store, _ = circle
    shown = approval.pending_from_peers(PHONE)
    assert shown[0]["digest"] == approval.digest(store.signed("2.0.1"))
    # La machine qui publie (compromise, ou clé de publication volée) remplace la version
    # sous le même numéro après l'avoir montrée
    other, other_trusted = new_key()
    monkeypatch.setattr(keys, "TRUSTED_KEYS", {**keys.TRUSTED_KEYS, **other_trusted})
    code = fake_code(tmp_path / "piege", "2.0.1", server="PIEGE = True\n")
    publish(store, zip_folder(code, tmp_path / "piege.zip"), "2.0.1", other, other_trusted,
            approvers={}, notes="Version piégée")
    sent = []
    real = approval.client.call

    def watch(my_id, peer, action, payload, key=None, timeout=10):
        sent.append(action)
        return real(my_id, peer, action, payload, key, timeout)
    monkeypatch.setattr(approval.client, "call", watch)
    with pytest.raises(UpdateError, match="n'est plus celle qui t'a été montrée"):
        approval.approve(PHONE, ME, "2.0.1")
    assert "update/approve" not in sent and [v for v, _ in store.pending()] == ["2.0.1"]
    # Montrée à son tour (son empreinte affichée), elle peut être approuvée en connaissance
    changed = approval.pending_from_peers(PHONE)
    assert changed[0]["notes"] == "Version piégée"
    approval._shown.clear()             # souvenir effacé (mémoire bornée) : reste suspecte
    with pytest.raises(UpdateError, match="plusieurs textes différents sous un même numéro"):
        approval.approve(PHONE, ME, "2.0.1")
    approval.forget()
    with pytest.raises(UpdateError, match="Affiche d'abord"):
        approval.approve(PHONE, ME, "2.0.1")
    assert approval.approve(PHONE, ME, "2.0.1", shown=changed[0]["digest"])["ok"]
    assert store.pending() == []


def two_texts_under_one_number(circle, tmp_path, monkeypatch):  # noqa: F811
    """La machine qui publie (clé volée) a deux textes « 2.0.1 » : A, le vrai, et B,
    piégé. Rend (A, B, [approbations envoyées], réponse de update/pending à modifier)."""
    store, _ = circle
    genuine = store.signed("2.0.1")
    thief, thief_trusted = new_key()
    monkeypatch.setattr(keys, "TRUSTED_KEYS", {**keys.TRUSTED_KEYS, **thief_trusted})
    other = Store(tmp_path / "autre")
    code = fake_code(tmp_path / "piege", "2.0.1", server="PIEGE = True\n")
    publish(other, zip_folder(code, tmp_path / "p.zip"), "2.0.1", thief, thief_trusted,
            approvers={})
    trapped = other.signed("2.0.1")
    sent, listed = [], {"pending": [genuine, trapped]}

    def call(my_id, peer, action, payload, key=None, timeout=10):
        if action == "update/pending":
            return {"pending": [{"version": "2.0.1", "signed": s} for s in listed["pending"]]}
        sent.append(payload["approval"])
        return {"ok": True}
    monkeypatch.setattr(approval.client, "call", call)
    return genuine, trapped, sent, listed


def test_two_texts_under_one_number_are_flagged_and_never_guessed(circle, tmp_path,  # noqa: F811
                                                                 monkeypatch):
    """Relecture : l'interface affichait deux lignes « 2.0.1 », l'utilisateur cliquait sur
    la première (A) sans que l'empreinte parte, et approve() signait B, la dernière vue."""
    genuine, trapped, sent, _ = two_texts_under_one_number(circle, tmp_path, monkeypatch)
    shown = [p for p in approval.pending_from_peers(PHONE) if p["node_id"] == ME]
    assert [p["digest"] for p in shown] == [approval.digest(genuine), approval.digest(trapped)]
    assert all(p["conflict"] and "anormal" in p["warning"] for p in shown)
    with pytest.raises(UpdateError, match="plusieurs textes différents"):
        approval.approve(PHONE, ME, "2.0.1")                    # sans empreinte : rien
    assert sent == []
    approval.approve(PHONE, ME, "2.0.1", shown=shown[0]["digest"])      # la ligne A
    assert manifests._approval_valid(genuine["manifest"], sent[-1], keys.APPROVER_KEYS)
    assert not manifests._approval_valid(trapped["manifest"], sent[-1], keys.APPROVER_KEYS)


def test_a_text_swapped_between_two_listings_is_flagged_too(circle, tmp_path,  # noqa: F811
                                                            monkeypatch):
    genuine, trapped, sent, listed = two_texts_under_one_number(circle, tmp_path, monkeypatch)
    listed["pending"] = [genuine]
    first = [p for p in approval.pending_from_peers(PHONE) if p["node_id"] == ME]
    assert not first[0]["conflict"]                             # un seul texte : normal
    listed["pending"] = [trapped]                               # puis remplacé
    second = [p for p in approval.pending_from_peers(PHONE) if p["node_id"] == ME]
    assert second[0]["conflict"]
    with pytest.raises(UpdateError, match="plusieurs textes différents"):
        approval.approve(PHONE, ME, "2.0.1")
    assert sent == []


# ======================================= version d'origine qui ignore les amis
def test_a_node_whose_original_version_ignores_friends_refuses_them(tmp_path, monkeypatch):
    from aiodos.node import vault
    assert launcher.friends_refused() is None                # pas lancé par un lanceur
    monkeypatch.setenv(launcher.CHILD_ENV, "1")
    monkeypatch.setenv(launcher.BASE_ENV, str(fake_code(tmp_path / "origine", "2.0.1")))
    monkeypatch.setattr(vault, "available", lambda: True)    # Windows : clés scellées
    assert launcher.friends_refused() is None
    monkeypatch.setattr(vault, "available", lambda: False)   # Termux, Linux : en clair
    for number, refused in (("2.0.1", True), ("2.0.0", True), ("2.0.2", False),
                            ("2.1.0", False), ("1.0.0", False), ("1.0.1", False)):
        base = fake_code(tmp_path / f"origine-{number}", number)
        if not refused:
            knows_friends(base)
        monkeypatch.setenv(launcher.BASE_ENV, str(base))
        reason = launcher.friends_refused()
        assert bool(reason) == refused, number
        if refused:
            assert number in reason and "Mets AIODOS à jour" in reason
    monkeypatch.setenv(launcher.BASE_ENV, str(tmp_path / "illisible"))
    assert launcher.friends_refused()                        # illisible : par prudence


def knows_friends(base):
    """Le code de `base` connaît les amis (network/sharing.py, comme depuis 804bd7c)."""
    sharing_file = base / "aiodos" / "network" / "sharing.py"
    sharing_file.parent.mkdir(parents=True, exist_ok=True)
    sharing_file.write_text('MINE = "mine"\nFRIEND = "friend"\n', encoding="utf-8")


def test_a_development_copy_older_than_the_friends_is_refused_too(tmp_path, monkeypatch):
    """Relecture : sept commits avant 804bd7c portaient déjà « 2.0.2-dev » sans connaître
    les amis ; une version d'origine construite depuis l'un d'eux passait."""
    from aiodos.node import vault
    monkeypatch.setenv(launcher.CHILD_ENV, "1")
    monkeypatch.setattr(vault, "available", lambda: False)
    base = fake_code(tmp_path / "origine", "2.0.2-dev")
    monkeypatch.setenv(launcher.BASE_ENV, str(base))
    reason = launcher.friends_refused()
    assert reason and "2.0.2-dev" in reason
    knows_friends(base)
    assert launcher.friends_refused() is None


def test_the_updates_view_is_told_when_friends_are_refused(world, monkeypatch):  # noqa: F811
    """Les amis déjà appairés ne sont pas couverts par le refus d'appairer : la vue Mises à
    jour peut le dire (status)."""
    monkeypatch.setattr(launcher, "friends_refused", lambda: "Ce nœud a été installé avec…")
    assert world.updates.status()["friends_refused"].startswith("Ce nœud")


def test_the_launcher_says_when_the_original_version_takes_over(data_dir, monkeypatch):
    monkeypatch.setattr(launcher, "developer_checkout", lambda: False)
    store = Store()
    installed_fake(store, "2.0.1", "crash")
    state.update(store.root, lambda d: d.update(current="2.0.1"))
    assert launcher.dispatch([], store, "2.0.0") is None     # la version d'origine tourne
    notice = state.load(store.root)["notice"]
    assert notice["kind"] == "rollback" and notice["version"] == "2.0.1"


# ============================================== outils de publication, poste de l'auteur
def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e"))


def test_git_runs_neither_the_fsmonitor_nor_the_hooks_of_the_repository(tmp_path, monkeypatch):
    repo = tmp_path / "depot"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "x")
    ran = tmp_path / "execute.txt"
    hook = tmp_path / "piege.py"
    hook.write_text(f"open({str(ran)!r}, 'a').write('fsmonitor\\n')\n", encoding="utf-8")
    git(repo, "config", "core.fsmonitor",
        f"'{sys.executable}' '{hook}'".replace("\\", "/"))
    (repo / ".git" / "hooks" / "reference-transaction").write_text(
        f"#!/bin/sh\necho crochet >> '{ran.as_posix()}'\n", encoding="utf-8")
    git(repo, "status", "--porcelain")
    git(repo, "tag", "essai")
    assert ran.exists()                     # le piège marche bien sans GIT_SAFE
    ran.unlink()
    monkeypatch.setattr(publish_tool, "ROOT", repo)
    monkeypatch.setattr(build_installer, "ROOT", repo)
    publish_tool.git("status", "--porcelain")
    build_installer.git("status", "--porcelain")
    publish_tool.git("tag", "-a", "v1", "-m", "v1")
    subprocess.run(companion_build.git_command(repo, "status"), capture_output=True)
    assert not ran.exists()


def open_to_others(*opened):
    """foreign_writers simulé : `opened` modifiables par « Utilisateurs authentifiés »."""
    names = {str(p) for p in opened}
    return lambda path: (["AUTORITE NT\\Utilisateurs authentifiés (S-1-5-11)"]
                         if str(path) in names else [])


def git_repo(folder, files=None):
    """Dépôt git jetable, dans un dossier privé, tout commité."""
    repo = private.private_dir(folder)
    git(repo, "init", "-q")
    for name, text in (files or {"aiodos/x.py": "X = 1\n"}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "x")
    return repo


def test_publication_refuses_a_repository_or_a_key_open_to_other_accounts(tmp_path,
                                                                           monkeypatch):
    repo = git_repo(private.private_dir(tmp_path / "prive") / "depot")
    key = repo.parent / "cle.pem"
    key.write_text("", encoding="utf-8")
    companion_build.require_private_repository(repo)               # privé : rien à dire
    monkeypatch.setattr(private, "foreign_writers", open_to_others(repo / ".git" / "config"))
    real_git = private._git                         # refusé avant que git n'y soit lancé
    monkeypatch.setattr(private, "_git", lambda *a: pytest.fail("git lancé dans ce dépôt"))
    with pytest.raises(companion_build.BuildError, match="modifiable par d'autres comptes") \
            as refused:
        companion_build.require_private_repository(repo)
    assert "config" in str(refused.value) and "S-1-5-11" in str(refused.value)
    monkeypatch.setattr(publish_tool, "ROOT", repo)
    with pytest.raises(SystemExit, match="Rien n'a été publié"):
        publish_tool.require_private_setup(key)
    monkeypatch.setattr(private, "_git", real_git)
    monkeypatch.setattr(private, "foreign_writers", open_to_others())
    monkeypatch.setattr(private, "foreign_readers", lambda path: ["BUILTIN\\Utilisateurs"])
    with pytest.raises(SystemExit, match="lisible par d'autres comptes"):
        publish_tool.require_private_setup(key)


def test_which_access_entries_open_a_folder_to_others():
    me = "S-1-5-21-1-2-3-1001"
    modify, read = 0x1301BF, 0x1200A9
    assert private.foreign([("S-1-5-11", modify)], me, me, private._WRITE) == ["S-1-5-11"]
    assert private.foreign([("S-1-5-11", read)], me, me, private._WRITE) == []
    assert private.foreign([("S-1-5-11", read)], me, me, private._READ) == ["S-1-5-11"]
    for sid in (me, "S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-15-3-1024-1", "S-1-5-33"):
        assert private.foreign([(sid, modify)], me, me, private._WRITE) == [], sid
    assert private.foreign([], "S-1-5-21-9-9-9-1002", me, private._WRITE)    # autre propriétaire


def test_private_folders_are_private_from_birth(tmp_path, monkeypatch):
    with private.private_tempdir("essai-", tmp_path) as work:
        (work / "sous").mkdir()
        (work / "sous" / "f.txt").write_text("x", encoding="utf-8")
        for path in (work, work / "sous", work / "sous" / "f.txt"):
            assert private.foreign_writers(path) == [] and private.foreign_readers(path) == []
    assert not work.exists()
    existing = tmp_path / "existant"
    existing.mkdir()
    monkeypatch.setattr(private, "foreign_writers", open_to_others(existing))
    with pytest.raises(private.NotPrivate, match="modifiable par d'autres comptes"):
        private.private_dir(existing)                    # ses droits ne sont pas changés


def test_the_companion_is_built_in_a_private_folder_outside_the_repository(tmp_path,
                                                                           monkeypatch):
    monkeypatch.delenv("AIODOS_BUILD_DIR", raising=False)
    assert APP_ROOT not in companion_build.build_dir().parents
    monkeypatch.setenv("AIODOS_BUILD_DIR", str(tmp_path / "construction"))
    target = companion_build.target_dir()
    assert target.is_dir() and private.foreign_writers(target) == []
    monkeypatch.setattr(private, "foreign_writers", open_to_others(target))
    with pytest.raises(companion_build.BuildError, match="AIODOS_BUILD_DIR"):
        companion_build.target_dir()


def test_a_cargo_configuration_in_a_parent_folder_is_found(tmp_path, monkeypatch):
    planted = tmp_path / "a" / ".cargo" / "config.toml"
    planted.parent.mkdir(parents=True)
    planted.write_text("[build]\nrustc-wrapper = 'piege'\n", encoding="utf-8")
    work = tmp_path / "a" / "b" / "c"
    work.mkdir(parents=True)
    assert str(planted) in companion_build.cargo_configs(work)
    monkeypatch.setenv("CARGO_HOME", str(planted.parent))     # celui de l'utilisateur : admis
    assert str(planted) not in companion_build.cargo_configs(work)


def test_build_tools_in_a_folder_open_to_others_are_refused(tmp_path, monkeypatch):
    tools = private.private_dir(tmp_path / "outils")
    cargo = tools / ("cargo.cmd" if sys.platform == "win32" else "cargo")
    cargo.write_text("", encoding="utf-8")
    cargo.chmod(0o700)
    monkeypatch.setenv("AIODOS_CARGO", str(cargo))
    companion_build.require_private_tools("windows-x86_64")
    monkeypatch.setattr(private, "foreign_writers", open_to_others(tools.resolve()))
    with pytest.raises(companion_build.BuildError, match="cargo"):
        companion_build.require_private_tools("windows-x86_64")


def test_the_installer_offers_no_folder_outside_the_profile():
    script = (APP_ROOT / "tools" / "installer" / "aiodos.iss").read_text(encoding="utf-8")
    setup = script.split("[Setup]", 1)[1].split("\n[", 1)[0]
    assert "DisableDirPage=yes" in setup and "UsePreviousAppDir=no" in setup
    assert "DefaultDirName={autopf}\\AIODOS" in setup and "PrivilegesRequired=lowest" in setup
    code = script.split("[Code]", 1)[1]
    assert "function PrepareToInstall" in code and "InsideProfile(ExpandConstant('{app}'))" in code
    assert all(not line.lstrip().startswith("[") for line in code.splitlines())
    # Relecture : le raccourci du démarrage avec Windows lançait encore l'ancien dossier,
    # ouvert aux autres comptes ; il est refait sur la nouvelle installation, comme
    # node/autostart.py le fait
    from aiodos.node import autostart
    assert "MoveStartupShortcut;" in code.split("procedure CurStepChanged", 1)[1]
    assert f"'{autostart.command()[1]}'" in code and "{userstartup}\\" + autostart.SHORTCUT in code


# ===================================================== installeur : paquets figés
def test_the_installer_packages_are_pinned_by_hash():
    lock = build_installer.read_lock()
    for name in build_installer.PACKAGES:
        assert build_installer.canonical_name(name) in lock, name
    assert all(len(hashes) >= 1 for _, hashes in lock.values())
    options = build_installer.install_options()
    for wanted in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-build-isolation"):
        assert wanted in options


def test_the_runtime_is_installed_from_the_pinned_file_only(tmp_path, monkeypatch):
    lock = build_installer.read_lock()
    monkeypatch.setattr(build_installer, "closure",
                        lambda packages=None: {n: v for n, (v, _) in lock.items()})
    archive = tmp_path / "python.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("python314._pth", "")
        z.writestr("python.exe", "")
    ran = []
    monkeypatch.setattr(build_installer.subprocess, "run",
                        lambda command, **kwargs: ran.append(command) or
                        subprocess.CompletedProcess(command, 0))
    build_installer.make_runtime(tmp_path / "stage", archive)
    command = ran[0]
    assert command[command.index("-r") + 1] == str(build_installer.LOCK)
    assert "--require-hashes" in command and "--prefer-binary" not in command
    assert "-c" not in command and not [c for c in command if "==" in c]
    runtime = json.loads((tmp_path / "stage" / "runtime" / "aiodos-runtime.json").read_text())
    assert runtime["lock_sha256"] == build_installer.sha256_of(build_installer.LOCK)


def test_the_pinned_packages_must_be_those_of_this_machine():
    lock = {"fastapi": ("1.0", ["0" * 64]), "ancien": ("1.0", ["0" * 64])}
    with pytest.raises(build_installer.BuildError) as refused:
        build_installer.check_lock(lock, {"fastapi": "2.0", "nouveau": "1.0"})
    message = str(refused.value)
    assert "nouveau" in message and "ancien" in message and "fastapi 1.0 → 2.0" in message
    assert "--lock" in message
    build_installer.check_lock({"fastapi": ("2.0", ["0" * 64])}, {"fastapi": "2.0"})


def wheel_like_installed(name: str, folder, alter: bool = False):
    """Une roue faite des fichiers du paquet `name` installé ici (altérée : un fichier
    changé, sa ligne du RECORD recalculée, comme le ferait un fichier ajouté sur PyPI)."""
    import hashlib
    dist = metadata.distribution(name)
    rows = list(csv.reader((dist.read_text("RECORD") or "").splitlines()))
    target = folder / f"{name}-{dist.version}-py3-none-any.whl"
    out_rows, altered = [], False
    with zipfile.ZipFile(target, "w") as z:
        for row in rows:
            if len(row) < 3 or not row[1] or row[0].startswith("../") or "__pycache__" in row[0] \
                    or row[0].rsplit("/", 1)[-1] in ("INSTALLER", "REQUESTED", "direct_url.json"):
                continue
            data = dist.locate_file(row[0]).read_bytes()
            if alter and not altered and row[0].endswith(".py"):
                data += b"\nPIEGE = True\n"
                altered = True
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                row = [row[0], f"sha256={digest.decode()}", str(len(data))]
            z.writestr(row[0], data)
            out_rows.append(row)
        record = io.StringIO()
        csv.writer(record, lineterminator="\n").writerows(out_rows)
        info = next(r[0] for r in rows if r[0].endswith(".dist-info/RECORD"))
        z.writestr(info, record.getvalue())
    return target


def test_a_wheel_that_differs_from_the_installed_one_is_not_pinned(tmp_path):
    try:
        metadata.distribution("striprtf")
    except metadata.PackageNotFoundError:
        pytest.skip("striprtf n'est pas installé ici")
    build_installer.same_as_installed(wheel_like_installed("striprtf", tmp_path), "striprtf")
    trapped = tmp_path / "piege"
    trapped.mkdir()
    with pytest.raises(build_installer.BuildError, match="installés sur cette machine"):
        build_installer.same_as_installed(wheel_like_installed("striprtf", trapped, alter=True),
                                          "striprtf")
    assert build_installer.distribution_of("proxy_tools-0.1.0.tar.gz") == ("proxy-tools", "0.1.0")
    assert build_installer.distribution_of("pydantic_core-2.46.3-cp314-cp314-win_amd64.whl") == \
        ("pydantic-core", "2.46.3")


def test_relocking_takes_only_wheels_like_the_installed_ones(tmp_path, monkeypatch):
    """--lock, avec un faux « pip download » : la roue servie doit être celle installée
    ici ; l'archive source de proxy-tools n'est jamais téléchargée (pip exécuterait son
    setup.py), son empreinte figée est reprise tant que sa version ne change pas."""
    try:
        number = metadata.version("striprtf")
    except metadata.PackageNotFoundError:
        pytest.skip("striprtf n'est pas installé ici")
    wanted = {"striprtf": number, "proxy-tools": "0.1.0"}
    monkeypatch.setattr(build_installer, "closure", lambda packages=None: dict(wanted))
    lock = tmp_path / "paquets.txt"
    lock.write_text(f"proxy-tools==0.1.0 --hash=sha256:{'a' * 64}\n", encoding="utf-8")
    served = {"alter": False}
    asked = []

    def fake_pip(command, **kwargs):
        asked.append(command)
        dest = command[command.index("--dest") + 1]
        wheel_like_installed("striprtf", build_installer.Path(dest), alter=served["alter"])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(build_installer.subprocess, "run", fake_pip)
    build_installer.relock(lock)
    pinned = build_installer.read_lock(lock)
    assert pinned["proxy-tools"] == ("0.1.0", ["a" * 64])           # repris tel quel
    assert pinned["striprtf"][0] == number and len(pinned["striprtf"][1]) == 1
    assert all("proxy-tools" not in " ".join(c) for c in asked)       # jamais téléchargé
    assert all("--only-binary=:all:" in c for c in asked)
    served["alter"] = True                                            # roue ajoutée sur PyPI
    with pytest.raises(build_installer.BuildError, match="installés sur cette machine"):
        build_installer.relock(lock)
    assert build_installer.read_lock(lock) == pinned                  # rien n'a changé
    wanted["proxy-tools"] = "0.2.0"
    with pytest.raises(build_installer.BuildError, match="à la main"):
        build_installer.relock(lock)


# ================================================ relecture des corrections (21/10/2026)
# --------------------------------------------- un fichier commencé chez une autre machine
def test_a_partial_file_started_by_another_machine_starts_over(lender, tmp_path, monkeypatch):
    """Relecture : le .part laissé par une machine (un morceau faux de la bonne taille,
    puis plus rien) était repris chez la suivante ; l'empreinte échouait, et c'est
    l'honnête qui était laissée de côté."""
    mine = Store(tmp_path / "mine")
    name = "aiodos-2.0.1.zip"
    mine.partial_from("2.0.1", name, "hostile").write_bytes(os.urandom(1000))
    asked = []

    def call(my_id, peer, action, payload, key=None, timeout=10):
        asked.append(payload.get("offset"))
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", call)
    p2p.fetch("moi", {"node_id": "honnete"}, "2.0.1", mine)
    assert [o for o in asked if o is not None][0] == 0      # repart de zéro
    assert mine.complete("2.0.1")
    assert not list((mine.root / "downloads").rglob("*.source"))


def test_each_machine_resumes_its_own_start_of_the_file(lender, tmp_path, monkeypatch):
    """Plusieurs machines instables (téléphone en 4G, relais) : chacune reprend son propre
    début de fichier quand elle revient, au lieu de tout recommencer à chaque changement de
    machine ; une fois le fichier complet, plus aucun début ne reste (relecture finale du
    21/10/2026)."""
    monkeypatch.setattr(p2p, "CHUNK", 1000)
    mine = Store(tmp_path / "mine")
    name = "aiodos-2.0.1.zip"
    whole = lender.file_path("2.0.1", name).read_bytes()
    mine.partial_from("2.0.1", name, "a" * 32).write_bytes(whole[:2000])
    mine.partial_from("2.0.1", name, "b" * 32).write_bytes(whole[:3000])
    asked = []

    def call(my_id, peer, action, payload, key=None, timeout=10, limit=None):
        if action == "update/chunk":
            asked.append((peer["node_id"], payload["offset"]))
        return p2p.HANDLERS[action](None, my_id, payload)
    monkeypatch.setattr(p2p.client, "call", call)
    p2p.fetch("moi", {"node_id": "a" * 32}, "2.0.1", mine)
    assert asked[0] == ("a" * 32, 2000)                       # son début à elle
    assert mine.file_path("2.0.1", name).read_bytes() == whole
    assert not (mine.root / "downloads" / "2.0.1").exists()   # ni le sien, ni celui de b
    for n in range(5):                                         # au plus KEEP_PARTIALS
        mine.partial_from("2.0.2", name, f"{n:032x}").write_bytes(b"x")
    started = list((mine.root / "downloads" / "2.0.2").glob(f"{name}.part*"))
    assert len(started) == store_module.KEEP_PARTIALS


def poison_then_stall(hostile: str):
    """client.call d'essai : `hostile` envoie un morceau faux de la bonne taille, puis
    plus rien ; les autres machines répondent juste. Rend (appel, machines appelées)."""
    sent, order = {"n": 0}, []

    def call(my_id, peer, action, payload, key=None, timeout=10):
        order.append(peer["node_id"])
        reply = p2p.HANDLERS[action](None, my_id, payload)
        if peer["node_id"] == hostile and action == "update/chunk":
            sent["n"] += 1
            if sent["n"] > 1:
                raise p2p.PeerError("Coupé.")
            reply = {"data": envelope.b64e(os.urandom(len(envelope.b64d(reply["data"]))))}
        return reply
    return call, order


def test_a_poisoned_partial_blames_the_machine_that_sent_it(world, monkeypatch):  # noqa: F811
    peers.set_trust(PEER, "friend")                        # deux amis : « AAA » d'abord
    peers.save(HOSTILE, "AAA", b"h" * 32, "127.0.0.1", 47702, trust="friend")
    peers.set_profile(HOSTILE, {"updates": {"offer": "2.0.1"}})
    monkeypatch.setattr(p2p, "CHUNK", 64)
    call, order = poison_then_stall(HOSTILE)
    monkeypatch.setattr(p2p.client, "call", call)
    world.updates.tick()
    assert order[0] == HOSTILE
    assert world.updates.store.installed() == ["2.0.1"]     # reçue de l'honnête, en entier
    assert HOSTILE in world.updates._failures and PEER not in world.updates._failures


def test_programs_are_asked_to_my_machines_before_friends(world, tmp_path,  # noqa: F811
                                                          monkeypatch):
    """Relecture : le compagnon et les moteurs se demandaient dans l'ordre des noms ; un ami
    « AAA » passait devant mes machines, et son .part empoisonné faisait écarter la mienne."""
    from aiodos.updates import platforms
    from tests.test_updates_service_companion import HERE, as_installed_by_an_old_machine
    from tests.updates_kit import fake_program
    monkeypatch.setattr(platforms, "tag", lambda: HERE)
    code = fake_code(tmp_path / "code-companion", "2.0.1")
    publish(world.lender, zip_folder(code, tmp_path / "c.zip"), "2.0.1", world.private,
            world.trusted, companions={HERE: fake_program(tmp_path / "prog", b"livre" * 200)})
    files = world.lender.available()
    peers.set_profile(PEER, {"updates": {"offer": "2.0.1", "files": files}})      # la mienne
    peers.save(HOSTILE, "AAA", b"h" * 32, "127.0.0.1", 47702, trust="friend")
    peers.set_profile(HOSTILE, {"updates": {"offer": "2.0.1", "files": files}})
    mine = as_installed_by_an_old_machine(world, monkeypatch)
    monkeypatch.setattr(p2p, "CHUNK", 64)
    call, order = poison_then_stall(HOSTILE)
    monkeypatch.setattr(p2p.client, "call", call)
    world.updates.tick()
    assert HOSTILE not in order                             # ma machine l'a donné
    kind = platforms.kind(manifests.COMPANION, HERE)
    assert kind in [f["kind"] for f in mine.held("2.0.1")]
    assert PEER not in world.updates._failures


def test_set_aside_and_unknown_machines_come_after_my_machines(world):  # noqa: F811
    peers.save(HOSTILE, "AAA", b"h" * 32, "127.0.0.1", 47702)          # « mine »…
    peers.set_aside(HOSTILE, "contrôle échoué")                          # … mise de côté
    peers.set_profile(HOSTILE, {"updates": {"offer": "2.0.1"}})
    assert [o[0] for o in world.updates._offers()] == [PEER, HOSTILE]
    assert service.UpdateService._rank({"trust": None}) == 1             # inconnu : ami
    assert service.UpdateService._rank({"trust": "mine"}) == 0


def test_the_companion_is_relaunched_once_per_round(world):  # noqa: F811
    """Relecture : relancé en fin de réception, puis de nouveau par tick, alors que sa
    relance est asynchrone (il reste « dépassé » d'ici là)."""
    from types import SimpleNamespace
    relaunched = []
    world.updates.network = SimpleNamespace(node_id="moi", internet=SimpleNamespace(
        companion=SimpleNamespace(outdated=lambda: True),
        relaunch_from_thread=lambda: relaunched.append(True)))
    world.updates.tick()
    assert relaunched == [True]


# ------------------------- approbations relayées par une machine qui n'en déclare pas
def signed_with_listed_approvers(approvers: dict):
    author, trusted = new_key()
    data = {"format": 1, "app": "aiodos", "version": "2.0.1", "published": 0,
            "commit": "0" * 40, "notes": "", "approvers": approvers,
            "files": [{"kind": "source", "name": "a.zip", "size": 1, "sha256": "0" * 64}]}
    return manifests.sign(data, author), trusted


def phone_key():
    phone = Ed25519PrivateKey.generate()
    raw = manifests.raw_public(phone)
    return phone, {manifests.key_id(raw): base64.b64encode(raw).decode("ascii")}


def test_a_relay_without_declared_devices_keeps_the_real_approval():
    """Relecture : une machine sans appareil déclaré (toutes aujourd'hui) gardait la
    première approbation par identifiant, et 8 au plus : une fausse placée devant la vraie
    la faisait retirer, et les machines qui l'exigent mettaient la mienne de côté."""
    phone, approvers = phone_key()
    kid = next(iter(approvers))
    signed, trusted = signed_with_listed_approvers(approvers)
    real = manifests.approval_entry(signed["manifest"], phone)
    fake = {"key": kid, "signature": base64.b64encode(os.urandom(64)).decode()}
    for junk in ([fake], bogus_approvals(manifests.MAX_APPROVALS),
                 [fake] + bogus_approvals(20)):
        kept = manifests.canonical(dict(signed, approvals=junk + [real]), {})
        assert kept["approvals"] == [real]                   # vérifiée avec le texte signé
        assert manifests.verify(kept, trusted, approvers)["version"] == "2.0.1"
    # Manifeste plus ancien, qui ne nomme pas ses appareils : rien n'est retiré parce
    # qu'une autre approbation porte le même identifiant
    author, _ = new_key()
    old = manifests.sign({"format": 1, "app": "aiodos", "version": "2.0.1", "published": 0,
                          "commit": "0" * 40, "notes": "", "files": [
                              {"kind": "source", "name": "a.zip", "size": 1,
                               "sha256": "0" * 64}]}, author)
    real = manifests.approval_entry(old["manifest"], phone)
    assert real in manifests.canonical(dict(old, approvals=[fake, real]), {})["approvals"]


def test_approvers_named_in_the_text_must_match_their_key():
    phone, approvers = phone_key()
    other, other_approvers = phone_key()
    kid = next(iter(approvers))
    mixed = {kid: next(iter(other_approvers.values()))}           # identifiant d'une autre clé
    signed, _ = signed_with_listed_approvers(mixed)
    assert manifests.listed_approvers(signed["manifest"]) == {}
    forged = manifests.approval_entry(signed["manifest"], other)
    assert "approvals" not in manifests.canonical(
        dict(signed, approvals=[dict(forged, key=kid)]), {})


def test_an_approval_received_once_is_not_lost_when_another_machine_omits_it(tmp_path,
                                                                            monkeypatch):
    phone, approvers = phone_key()
    signed, trusted = signed_with_listed_approvers(approvers)
    monkeypatch.setattr(keys, "TRUSTED_KEYS", trusted)
    store = Store(tmp_path / "relais")
    real = manifests.approval_entry(signed["manifest"], phone)
    store.add_manifest(dict(signed, approvals=[real]), approvers={})
    store.add_manifest(dict(signed), approvers={})                  # transmise sans elle
    assert store.signed("2.0.1")["approvals"] == [real]


def test_the_release_names_the_approval_devices_of_the_published_commit():
    _, approvers = phone_key()
    line = "".join(f'    "{k}": "{v}",\n' for k, v in approvers.items())
    text = f"TRUSTED_KEYS = {{\n}}\n\nAPPROVER_KEYS = {{\n{line}}}\n"
    assert publish_tool.committed_approvers(text) == approvers
    assert publish_tool.committed_approvers("TRUSTED_KEYS = {}\n") == {}
    kid = next(iter(approvers))
    with pytest.raises(SystemExit, match="ne correspond pas"):
        publish_tool.committed_approvers(text.replace(kid, "0" * 16))
    with pytest.raises(SystemExit, match="illisible"):
        publish_tool.committed_approvers("APPROVER_KEYS = {open('x')}\n")


# ------------------------------------------------------ le dépôt d'où l'on publie
def test_untracked_modules_where_the_tools_look_are_refused(tmp_path):
    """Relecture : un paquet planté et non suivi (tools/engine_release/__init__.py) passait
    devant engine_release.py et s'exécutait avant le contrôle."""
    repo = git_repo(tmp_path / "depot", {"aiodos/x.py": "", "tools/engine_release.py": "",
                                         ".gitignore": "data/\n__pycache__/\n"})
    (repo / "data").mkdir()
    (repo / "data" / "base.db").write_text("", encoding="utf-8")         # ignoré, pas importé
    (repo / "aiodos" / "__pycache__").mkdir()
    (repo / "aiodos" / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"")   # jamais lu
    assert private.repository_problems(repo) == []
    planted = repo / "tools" / "engine_release" / "__init__.py"
    planted.parent.mkdir()
    planted.write_text("PIEGE = True\n", encoding="utf-8")
    with pytest.raises(private.NotPrivate, match="non suivis") as refused:
        private.require_private_repository(repo)
    assert "tools/engine_release/" in str(refused.value)
    planted.unlink()
    planted.parent.rmdir()
    (repo / "cryptography").mkdir()                                       # à la racine
    (repo / "cryptography" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "aiodos" / "x.cp314-win_amd64.pyd").write_bytes(b"MZ")
    (repo / "git.exe").write_bytes(b"MZ")                     # devant celui du PATH
    message = "\n\n".join(private.repository_problems(repo))
    assert "cryptography/" in message and "x.cp314-win_amd64.pyd" in message
    assert "git.exe" in message


def test_replacement_objects_are_neither_applied_nor_accepted(tmp_path):
    """Relecture : git archive appliquait refs/replace ; un remplacement rangé dans
    packed-refs faisait publier un autre keys.py, git status ne montrant rien."""
    repo = git_repo(tmp_path / "depot", {"aiodos/keys.py": "TRUSTED_KEYS = {}\n"})
    blob = git(repo, "rev-parse", "HEAD:aiodos/keys.py").stdout.strip()
    trapped = tmp_path / "piege.py"
    trapped.write_text("TRUSTED_KEYS = {'cle': 'du voleur'}\n", encoding="utf-8")
    other = git(repo, "hash-object", "-w", str(trapped)).stdout.strip()
    git(repo, "replace", blob, other)
    assert "voleur" in git(repo, "show", "HEAD:aiodos/keys.py").stdout   # sans GIT_SAFE
    safe = subprocess.run(companion_build.git_command(repo, "show", "HEAD:aiodos/keys.py"),
                          capture_output=True, text=True)
    assert "voleur" not in safe.stdout and "TRUSTED_KEYS" in safe.stdout
    with pytest.raises(private.NotPrivate, match="remplacement"):
        private.require_private_repository(repo)
    git(repo, "replace", "-d", blob)
    private.require_private_repository(repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("keys.py export-ignore\n",
                                                       encoding="utf-8")
    with pytest.raises(private.NotPrivate, match="attributes"):
        private.require_private_repository(repo)


def test_git_settings_and_hooks_of_a_trapped_repository_are_refused(tmp_path):
    repo = git_repo(tmp_path / "depot")
    git(repo, "config", "core.fsmonitor", "true")                    # celui de git : admis
    private.require_private_repository(repo)
    for key, value in (("core.fsmonitor", "C:/Users/Public/x.exe"),
                       ("core.hooksPath", "C:/Users/Public/crochets"),
                       ("core.sshCommand", "x.exe"), ("diff.external", "x.exe"),
                       ("core.worktree", str(tmp_path))):
        git(repo, "config", key, value)
        with pytest.raises(private.NotPrivate) as refused:
            private.require_private_repository(repo)
        assert key.lower() in str(refused.value).lower(), key
        git(repo, "config", "--unset", key)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(private.NotPrivate, match="crochets actifs"):
        private.require_private_repository(repo)


def runnable_copy(tmp_path):
    """Copie jetable du dépôt (code et outils), tout commité, dans un dossier privé."""
    import shutil
    repo = private.private_dir(tmp_path / "depot")
    for name in ("aiodos", "tools"):
        shutil.copytree(APP_ROOT / name, repo / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "x")
    return repo


def run_tool(repo, *args):
    return subprocess.run([sys.executable, str(repo / "tools" / "publish.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8",
                                   AIODOS_RELEASE_KEY=str(repo.parent / "absente.pem")))


def test_the_tool_checks_the_repository_before_importing_anything(tmp_path):
    """Relecture : le contrôle tournait après l'import du code du dépôt ; un module planté
    s'exécutait d'abord, sous le compte de l'auteur, la clé de publication à portée."""
    repo = runnable_copy(tmp_path)
    ran = tmp_path / "execute.txt"
    planted = repo / "tools" / "engine_release" / "__init__.py"
    planted.parent.mkdir()
    planted.write_text(f"open({str(ran)!r}, 'w').write('piege')\n", encoding="utf-8")
    done = run_tool(repo, "show")
    assert done.returncode != 0 and "non suivis" in done.stderr, done.stderr
    assert not ran.exists()


def test_the_tool_reads_no_planted_bytecode(tmp_path):
    """Un .pyc planté dans __pycache__, à la date et à la taille du vrai module, serait
    exécuté à sa place : l'amorce n'en lit aucun."""
    import importlib.util
    from importlib._bootstrap_external import _code_to_timestamp_pyc
    repo = runnable_copy(tmp_path)
    ran = tmp_path / "execute.txt"
    source = repo / "tools" / "companion_build.py"
    info = source.stat()
    code = compile(f"open({str(ran)!r}, 'w').write('piege')\n", str(source), "exec")
    pyc = importlib.util.cache_from_source(str(source))
    os.makedirs(os.path.dirname(pyc), exist_ok=True)
    with open(pyc, "wb") as f:
        f.write(_code_to_timestamp_pyc(code, int(info.st_mtime), info.st_size))
    subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import companion_build", str(repo / "tools")], capture_output=True)
    assert ran.exists()                                  # le piège marche sans l'amorce
    ran.unlink()
    done = run_tool(repo, "show")
    assert "Pas de clé de publication" in done.stderr, done.stderr     # le contrôle est passé
    assert not ran.exists()


# --------------------------------------------------------- outils de construction
def test_the_installer_is_staged_in_a_fresh_private_folder(tmp_path, monkeypatch):
    """Relecture : fresh_work n'existait pas, et build_installer.py levait NameError."""
    root = private.private_dir(tmp_path / "depot")
    old = root / "build" / "installateur" / "vieux.txt"
    old.parent.mkdir(parents=True)
    old.write_text("ancien", encoding="utf-8")
    monkeypatch.setattr(build_installer, "ROOT", root)
    monkeypatch.setattr(companion_build, "require_private_repository", lambda *a, **k: None)
    monkeypatch.setattr(build_installer, "version", lambda: "9.9.9")
    monkeypatch.setattr(build_installer, "git",
                        lambda *a: "0" * 40 if a[0] == "rev-parse" else "")
    monkeypatch.setattr(build_installer, "fetch_python", lambda: tmp_path / "python.zip")
    staged = []
    monkeypatch.setattr(build_installer, "make_runtime",
                        lambda stage, archive: staged.append(stage))
    monkeypatch.setattr(build_installer, "make_app", lambda stage, commit, number: None)
    monkeypatch.setattr(build_installer, "precompile", lambda stage: None)
    assert build_installer.main(["--stage-only", "--no-companion", "--no-engines"]) == 0
    work = root / "build" / "installateur"
    assert staged == [work / "stage"] and not old.exists()
    assert private.foreign_writers(work) == []
    monkeypatch.setattr(private, "foreign_writers", open_to_others(work))
    monkeypatch.setattr(build_installer.shutil, "rmtree", lambda *a, **k: None)   # resté là
    with pytest.raises(build_installer.BuildError, match="modifiable par d'autres comptes"):
        build_installer.fresh_work(work)


def test_the_lock_is_not_relocked_from_an_exposed_repository(monkeypatch):
    def exposed(*args, **kwargs):
        raise companion_build.BuildError("Le dépôt est modifiable par d'autres comptes")
    monkeypatch.setattr(companion_build, "require_private_repository", exposed)
    monkeypatch.setattr(build_installer, "relock", lambda: pytest.fail("relevé quand même"))
    with pytest.raises(SystemExit, match="modifiable"):
        build_installer.main(["--lock"])


def test_inno_setup_in_a_folder_open_to_others_is_refused(tmp_path, monkeypatch):
    folder = private.private_dir(tmp_path / "inno")
    (folder / "ISCC.exe").write_bytes(b"MZ")
    monkeypatch.setenv("AIODOS_ISCC", str(folder / "ISCC.exe"))
    assert build_installer.iscc() == (folder / "ISCC.exe").resolve()
    monkeypatch.setattr(private, "foreign_writers", open_to_others(folder.resolve()))
    with pytest.raises(build_installer.BuildError, match="Inno Setup"):
        build_installer.iscc()


FAKE_CARGO_CONFIG = '''import os, sys
args = sys.argv[1:]
triple, profile = args[args.index("--target") + 1], args[args.index("--profile") + 1]
planted = os.path.join(os.environ["AIODOS_BUILD_DIR"], ".cargo")
os.makedirs(planted, exist_ok=True)                  # posée pendant la construction
open(os.path.join(planted, "config.toml"), "w").write("[build]\\nrustc-wrapper = 'x'\\n")
out = os.path.join(os.environ["CARGO_TARGET_DIR"], triple, profile)
os.makedirs(out, exist_ok=True)
name = "aiodos-net.exe" if "windows" in triple else "aiodos-net"
open(os.path.join(out, name), "wb").write(b"MZ" + os.environ["AIODOS_NET_COMMIT"].encode())
'''


def test_a_cargo_configuration_appearing_during_the_build_rejects_it(tmp_path, monkeypatch):
    from aiodos.updates import platforms
    target = platforms.tag()
    if target is None:
        pytest.skip("plateforme sans compagnon")
    repo = git_repo(tmp_path / "depot", {"compagnon/Cargo.toml": 'version = "1.0.0"\n'})
    commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    tools = private.private_dir(tmp_path / "outils")
    script = tools / "faux_cargo.py"
    script.write_text(FAKE_CARGO_CONFIG, encoding="utf-8")
    if sys.platform == "win32":
        cargo = tools / "cargo.cmd"
        cargo.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        cargo = tools / "cargo"
        cargo.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                         encoding="utf-8")
        cargo.chmod(0o700)
    monkeypatch.setenv("AIODOS_CARGO", str(cargo))
    monkeypatch.setenv("AIODOS_BUILD_DIR", str(private.private_dir(tmp_path / "construction")))
    with pytest.raises(companion_build.BuildError, match="apparue pendant la construction"):
        companion_build.build(target, commit, "9.9.9", tmp_path / "sortie", repo=repo)
    assert not list((tmp_path / "sortie").glob("*"))
