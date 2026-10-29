"""Dossiers suivis des bibliothèques (knowledge/folders.py, 29/10/2026) : la liste des dossiers
ajoutés avec leurs fichiers indexés, en attente, en échec ; « Relire », « Retirer » ; le suivi
(nouveau fichier indexé, fichier modifié relu, fichier supprimé retiré, dossier absent qui ne
retire rien) ; un dossier sans fichier lisible qui le dit ; les dossiers d'avant retrouvés."""
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from aiodos import db
from aiodos.api.server import create_app
from aiodos.jobs import queue, registry
from aiodos.jobs.kinds import index
from aiodos.knowledge import folders, store
from tests.test_embed_model import _drain

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}


@pytest.fixture
def general(fake_embed):
    store.ensure_defaults()
    return store.list_collections()[0]["id"]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def notes(tmp_path):
    folder = tmp_path / "Notes"
    _write(folder / "budget.txt", "Le budget 2024 est de cinquante mille euros.")
    _write(folder / "sous" / "reunion.md", "Réunion de lundi : budget validé.")
    (folder / "outil.exe").write_bytes(b"MZ\x00\x00")                   # non pris en charge
    return folder


def _sources(collection_id):
    return sorted(e["source"] for e in store.list_entries(collection_id))


def _touch_later(path, text):
    """Modifie le fichier avec une date clairement plus récente (horloges grossières)."""
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


def test_an_added_folder_is_listed_with_its_files(general, notes):
    found = folders.add(general, notes)
    assert found["job_id"] and found["planned"] == 2 and found["counts"]["pending"] == 2
    assert found["ignored"] == 1 and found["ignored_types"] == {".exe": 1}
    _drain()
    listed = folders.list_folders(general)
    assert [f["path"] for f in listed] == [str(notes)]
    folder = listed[0]
    assert folder["counts"] == {"indexed": 2, "pending": 0, "failed": 0, "skipped": 0,
                                "cancelled": 0}
    assert folder["online"] and folder["scanned"] and folder["job"] is None
    assert _sources(general) == ["Notes/budget.txt", "Notes/sous/reunion.md"]
    job = queue.job_summary(found["job_id"])
    assert job["result"]["indexed"] == 2 and job["result"]["skipped_types"] == {".exe": 1}


def test_new_changed_and_deleted_files_are_followed(general, notes):
    folders.add(general, notes)
    _drain()
    assert folders.watch_pass()["jobs"] == []                   # rien de nouveau : aucun travail
    _write(notes / "nouveau.txt", "Un fichier ajouté après coup : facture du plombier.")
    _touch_later(notes / "budget.txt", "Le budget 2025 est de soixante mille euros.")
    created = folders.watch_pass()["jobs"]
    assert len(created) == 1
    assert queue.job_summary(created[0])["total"] == 2          # le nouveau et le modifié
    assert queue.job_summary(created[0])["params"]["auto"] is True
    _drain()
    assert "Notes/nouveau.txt" in _sources(general)
    texts = [k["text"] for k in db.query("SELECT text FROM chunks")]
    assert any("2025" in text for text in texts) and not any("2024" in text for text in texts)
    (notes / "sous" / "reunion.md").unlink()
    out = folders.watch_pass()
    assert out["changed"] and out["jobs"] == []
    assert _sources(general) == ["Notes/budget.txt", "Notes/nouveau.txt"]
    assert folders.list_folders(general)[0]["counts"]["indexed"] == 2


def test_an_absent_folder_removes_nothing(general, notes, tmp_path):
    folders.add(general, notes)
    _drain()
    away = tmp_path / "ailleurs"
    shutil.move(str(notes), str(away))                          # disque débranché
    out = folders.watch_pass()
    assert out == {"jobs": [], "changed": False}
    assert _sources(general) == ["Notes/budget.txt", "Notes/sous/reunion.md"]
    listed = folders.list_folders(general)[0]
    assert listed["online"] is False and listed["counts"]["indexed"] == 2
    shutil.move(str(away), str(notes))                          # rebranché
    folders.watch_pass()
    assert folders.list_folders(general)[0]["online"] is True
    assert _sources(general) == ["Notes/budget.txt", "Notes/sous/reunion.md"]


def test_an_unreadable_subfolder_removes_nothing(general, notes, monkeypatch):
    folders.add(general, notes)
    _drain()
    scandir = os.scandir

    def refused(path):
        if os.path.basename(str(path)) == "sous":
            raise PermissionError(str(path))
        return scandir(path)
    monkeypatch.setattr(index.os, "scandir", refused)
    folders.watch_pass()
    assert "Notes/sous/reunion.md" in _sources(general)


def test_failed_files_wait_for_read_again(general, notes, monkeypatch):
    real = registry.TASKS["index_file"].compute

    def broken(payload, file, options, stop=None):
        if file.endswith("budget.txt"):
            raise RuntimeError("lecteur en panne")
        return real(payload, file, options, stop)
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", broken)
    folders.add(general, notes)
    _drain()
    folder = folders.list_folders(general)[0]
    assert folder["counts"]["failed"] == 1 and folder["counts"]["indexed"] == 1
    problems = folders.problems(folder["id"])
    assert problems == [{"file": "budget.txt", "state": "failed", "reason": "lecteur en panne"}]
    assert folders.watch_pass()["jobs"] == []                   # pas toutes les dix minutes
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", real)
    out = folders.scan(folder["id"], folders.RETRY_ALL)         # « Relire »
    assert out["planned"] == 1
    _drain()
    assert folders.list_folders(general)[0]["counts"]["failed"] == 0
    assert "Notes/budget.txt" in _sources(general)


def test_failed_files_are_retried_once_at_start(general, notes, monkeypatch):
    real = registry.TASKS["index_file"].compute
    monkeypatch.setattr(registry.TASKS["index_file"], "compute",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("panne")))
    folders.add(general, notes)
    _drain()
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", real)
    assert len(folders.watch_pass(first=True)["jobs"]) == 1     # à l'ouverture d'AIODOS
    _drain()
    assert folders.list_folders(general)[0]["counts"]["indexed"] == 2


def test_remove_takes_the_folder_and_its_passages_not_the_files(general, notes):
    folders.add(general, notes)
    _drain()
    store.upsert_entry(general, "autre.txt", "file", None, [])  # une entrée qui n'en est pas
    folder = folders.list_folders(general)[0]
    assert folders.remove(folder["id"]) == 2
    assert _sources(general) == ["autre.txt"]
    assert folders.list_folders(general) == []
    assert (notes / "budget.txt").is_file()


def test_remove_cancels_the_folder_work_in_progress(general, notes):
    found = folders.add(general, notes)
    folders.remove(found["id"])
    assert queue.job_summary(found["job_id"])["status"] == "cancelled"


def test_two_folders_of_the_same_name_do_not_mix(general, notes, tmp_path):
    other = tmp_path / "autre" / "Notes"
    _write(other / "budget.txt", "Un autre budget, d'un autre dossier.")
    folders.add(general, notes)
    second = folders.add(general, other)
    _drain()
    assert second["root"] == "Notes (2)"
    assert "Notes (2)/budget.txt" in _sources(general)
    folders.remove(second["id"])
    assert _sources(general) == ["Notes/budget.txt", "Notes/sous/reunion.md"]


def test_a_folder_without_readable_file_says_so(general, tmp_path):
    folder = tmp_path / "Programmes"
    folder.mkdir()
    (folder / "a.exe").write_bytes(b"MZ")
    (folder / "b.dll").write_bytes(b"MZ")
    found = folders.add(general, folder)
    assert found["job_id"] is None and found["files"] == 0
    assert found["ignored"] == 2 and found["ignored_types"] == {".exe": 1, ".dll": 1}


def test_adding_twice_reads_again_and_refuses_what_is_not_a_folder(general, notes, tmp_path):
    folders.add(general, notes)
    _drain()
    again = folders.add(general, str(notes).upper() if os.name == "nt" else notes)
    assert len(folders.list_folders(general)) == 1 and again["planned"] == 0
    with pytest.raises(ValueError):
        folders.add(general, tmp_path / "introuvable")
    with pytest.raises(ValueError):
        folders.add(9999, notes)


def test_the_folders_of_older_versions_are_found(general, notes, monkeypatch):
    """Indexé avant le suivi (travail sans folder_id) : retrouvé d'après ses entrées."""
    job_id = queue.create_job("index_paths", {"paths": [str(notes)], "collection_id": general})
    _drain()
    assert queue.job_summary(job_id)["done"] == 2
    db.execute("DELETE FROM jobs")                               # la vue Tâches vidée
    folders._adopted["db"] = None
    listed = folders.list_folders(general)
    assert [f["path"] for f in listed] == [str(notes)]
    assert listed[0]["root"] == "Notes" and listed[0]["counts"]["indexed"] == 2
    assert folders.watch_pass()["jobs"] == []                    # rien de changé : rien relu
    _touch_later(notes / "budget.txt", "Budget revu à la hausse.")
    assert len(folders.watch_pass()["jobs"]) == 1


def test_a_folder_that_gave_nothing_is_found_from_its_job(general, notes, monkeypatch):
    """Le dossier de l'auteur : chaque fichier en échec (modèle d'embeddings absent, avant ce
    correctif), rien dans la bibliothèque. Il devient suivi, et sa première lecture refait
    les fichiers en échec."""
    real = registry.TASKS["index_file"].compute
    monkeypatch.setattr(registry.TASKS["index_file"], "compute",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("modèle absent")))
    job_id = queue.create_job("index_paths", {"paths": [str(notes)], "collection_id": general})
    _drain()
    assert queue.job_summary(job_id)["status"] == "failed"
    folders._adopted["db"] = None
    listed = folders.list_folders(general)
    assert [f["path"] for f in listed] == [str(notes)] and listed[0]["counts"]["failed"] == 2
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", real)
    assert len(folders.watch_pass(first=True)["jobs"]) == 1
    _drain()
    assert _sources(general) == ["Notes/budget.txt", "Notes/sous/reunion.md"]


def test_the_watch_loop_reads_the_folders_and_wakes_the_queue(general, notes, monkeypatch):
    """Le suivi (folder_watch.loop) : une lecture à l'ouverture, qui refait les fichiers en
    échec, crée le travail des fichiers nouveaux, réveille la file et prévient la vue."""
    import asyncio
    from aiodos.knowledge import folder_watch
    folders.add(general, notes)
    _drain()
    _write(notes / "nouveau.txt", "Arrivé pendant qu'AIODOS était fermé.")
    monkeypatch.delenv("AIODOS_QUIET", raising=False)
    told = {"jobs": [], "events": [], "woken": 0}

    class Executor:
        def publish_job(self, job_id, force=False):
            told["jobs"].append(job_id)

        def wake(self):
            told["woken"] += 1

    class Bus:
        def publish(self, event):
            told["events"].append(event)

    async def one_pass():
        task = asyncio.create_task(folder_watch.loop(Executor(), Bus(), first_delay=0,
                                                     every=3600))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if told["jobs"]:
                break
        task.cancel()
    asyncio.run(one_pass())
    assert len(told["jobs"]) == 1 and told["woken"] == 1
    assert {"type": "knowledge"} in told["events"]
    assert queue.job_summary(told["jobs"][0])["total"] == 1
    monkeypatch.setenv("AIODOS_QUIET", "1")             # les essais : pas de suivi de lui-même
    assert asyncio.run(folder_watch.loop(Executor(), Bus(), first_delay=0)) is None


def test_the_api_lists_reads_again_and_removes(general, notes):
    with TestClient(create_app(TOKEN, {"testserver"})) as client:
        reply = client.post("/api/index", json={"paths": [str(notes)], "collection_id": general},
                            headers=AUTH).json()
        assert reply["job_id"] == reply["jobs"][0] and reply["folders"][0]["planned"] == 2
        listing = f"/api/library_folders?collection_id={general}"
        deadline = time.time() + 15
        while time.time() < deadline:
            listed = client.get(listing, headers=AUTH).json()
            if listed["folders"][0]["counts"]["indexed"] == 2:
                break
            time.sleep(0.1)
        folder = listed["folders"][0]
        assert folder["counts"]["indexed"] == 2 and folder["path"] == str(notes)
        _write(notes / "ajout.txt", "Ajouté ensuite : il doit être lu.")
        one = f"/api/library_folders/{folder['id']}"
        assert client.post(f"{one}/rescan", headers=AUTH).json()["planned"] == 1
        assert client.get(f"{one}/problems", headers=AUTH).json() == {"files": []}
        assert client.delete(one, headers=AUTH).json()["removed"] >= 2
        assert client.get(listing, headers=AUTH).json()["folders"] == []
        assert client.delete("/api/library_folders/12345", headers=AUTH).status_code == 404
        # Les dossiers des conversations, eux, ont leur propre route (/api/folders) : intacts
        assert client.post("/api/folders", json={"name": "Projets"}, headers=AUTH).status_code \
            == 200


def test_the_api_says_when_a_folder_has_nothing_readable(general, tmp_path):
    folder = tmp_path / "Vide"
    folder.mkdir()
    (folder / "x.exe").write_bytes(b"MZ")
    with TestClient(create_app(TOKEN, {"testserver"})) as client:
        reply = client.post("/api/index", json={"paths": [str(folder)], "collection_id": general},
                            headers=AUTH).json()
    assert reply["job_id"] is None and reply["jobs"] == []
    assert reply["folders"][0]["files"] == 0 and reply["folders"][0]["ignored"] == 1
