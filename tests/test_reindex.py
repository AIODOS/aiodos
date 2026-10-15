"""Réindexation au changement de modèle d'embedding : déclenchement, contenu
recalculé (et d'où il vient), mémoire des conversations, reprise."""
import hashlib
import time

import numpy as np
from fastapi.testclient import TestClient

from coeur import db, settings
from coeur.api.server import create_app
from coeur.chat import conversations
from coeur.jobs import queue, registry
from coeur.jobs.kinds import index as index_kinds
from coeur.jobs.kinds import reindex
from coeur.knowledge import chunking, search, store
from coeur.services import media, ollama, web

AUTH = {"X-Coeur-Token": "jeton-de-test"}


def _new_model(texts, model=None):
    """« Nouveau » modèle factice, de même dimension que l'ancien (comme
    nomic-embed-text et embeddinggemma) : ses vecteurs ne se comparent pas."""
    vectors = []
    for text in texts:
        v = np.zeros(64, dtype=np.float32)
        for word in text.lower().split():
            salted = b"nouveau:" + word.strip(".,:;!?").encode()
            v[int(hashlib.md5(salted).hexdigest(), 16) % 64] += 1
        vectors.append(v.tolist())
    return vectors


def _forbidden(*args, **kwargs):
    raise AssertionError("ni OCR, ni transcription, ni accès réseau pour réindexer")


def _run_queue():
    """Vide la file comme le ferait l'exécuteur, sans threads."""
    while (unit := queue.claim_next({"gpu", "embed", "cpu", "net"}, "local")) is not None:
        result = registry.run_local(unit["task"], unit["payload"])
        job_id, finished = queue.finish_unit(unit["id"], result or {}, 0.0)
        if finished:
            queue.finalize_job(job_id)


def _all_vectors_from(embed) -> bool:
    rows = db.query("SELECT text, embedding FROM chunks")
    return bool(rows) and all(
        np.array_equal(np.frombuffer(r["embedding"], dtype=np.float32),
                       np.asarray(embed([r["text"]])[0], dtype=np.float32)) for r in rows)


def _library(tmp_path, embed):
    """Bibliothèque variée et mémoire d'une conversation, indexées avec l'ancien modèle."""
    store.ensure_defaults()
    assert reindex.schedule(resume=True) is None          # démarrage sur une base vide
    general = store.list_collections()[0]["id"]
    notes = tmp_path / "notes.md"
    notes.write_text("Réunion de lundi : budget validé.", encoding="utf-8")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")

    def add(source, kind, origin, text):
        pieces = chunking.chunk_text(text)
        store.upsert_entry(general, source, kind, origin, list(zip(pieces, embed(pieces))))
    add("notes.md", "file", str(notes), "Réunion de lundi : budget validé.")
    add("photo.png", "file", str(tmp_path / "photo.png"), "Texte lu sur la photo : facture")
    add("https://exemple.fr/doc", "url", "https://exemple.fr/doc", "Documentation du projet")
    add("archive v1.txt", "file", None, "Ancienne note importée de la v1")
    add("disparu.txt", "file", str(tmp_path / "disparu.txt"), "Fichier supprimé depuis")
    conv = conversations.create("Cuisine")
    conversations.add_message(conv, "user", "Comment faire une ratatouille ?")
    conversations.add_message(conv, "assistant", "Aubergines, courgettes et poivrons.")
    index_kinds.run_index_conversation({"conversation_id": conv})
    return general, notes


def test_model_change_reindexes_everything(tmp_path, monkeypatch, fake_embed):
    general, notes = _library(tmp_path, fake_embed)
    assert reindex.schedule() is None                        # modèle inchangé
    notes.write_text("Réunion de lundi : budget validé, 5 000 euros.", encoding="utf-8")
    monkeypatch.setattr(ollama, "embed", _new_model)
    monkeypatch.setattr(media, "vision_extract", _forbidden)
    monkeypatch.setattr(media, "transcribe", _forbidden)
    monkeypatch.setattr(web, "fetch_url_text", _forbidden)

    settings.set_value("embed_model", "nouveau-modele")
    job_id = reindex.schedule()
    assert job_id and reindex.indexed_model() == "nouveau-modele"
    # Anciens vecteurs retirés tout de suite : même dimension, mais d'un autre modèle
    assert search.search(_new_model(["budget"])[0], k=5, conversations=True) == []

    _run_queue()
    job = queue.job_summary(job_id)
    assert job["status"] == "done" and job["done"] == 6 and not job["failed"]
    assert "nouveau-modele" in job["title"] and job["result"]["indexed"] == 6
    assert "dont 1 relu(s)" in job["result"]["message"]
    assert _all_vectors_from(_new_model)
    texts = {e["source"]: store.chunk_texts(e["id"]) for e in store.list_entries(general)}
    assert texts == {
        "notes.md": ["Réunion de lundi : budget validé, 5 000 euros."],   # relu
        "photo.png": ["Texte lu sur la photo : facture"],    # texte déjà extrait (OCR)
        "https://exemple.fr/doc": ["Documentation du projet"],     # pas de réseau
        "archive v1.txt": ["Ancienne note importée de la v1"],     # sans origine
        "disparu.txt": ["Fichier supprimé depuis"],                 # fichier disparu
    }
    assert search.search(_new_model(["budget"])[0], k=1)[0]["source"] == "notes.md"
    memory = search.search(_new_model(["ratatouille"])[0], k=1, conversations=True)
    assert memory[0]["kind"] == "conversation"
    assert reindex.schedule() is None and reindex.schedule(resume=True) is None


def test_removed_content_is_not_resurrected(tmp_path, monkeypatch, fake_embed):
    general, _ = _library(tmp_path, fake_embed)
    monkeypatch.setattr(ollama, "embed", _new_model)
    settings.set_value("embed_model", "nouveau-modele")
    job_id = reindex.schedule()
    notes = next(e for e in store.list_entries(general) if e["source"] == "notes.md")
    store.remove_entry(notes["id"])                          # retiré pendant l'attente
    memory = store.conversation_collection_id()
    for entry in store.list_entries(memory, include_system=True):
        store.remove_entry(entry["id"])                      # mémoire vidée entre-temps
    _run_queue()
    job = queue.job_summary(job_id)
    assert job["status"] == "done" and job["skipped"] == 2 and job["done"] == 4
    assert "notes.md" not in {e["source"] for e in store.list_entries(general)}
    assert store.list_entries(memory, include_system=True) == []


def test_new_change_replaces_pending_reindex(tmp_path, fake_embed):
    _library(tmp_path, fake_embed)
    settings.set_value("embed_model", "bge-m3")
    first = reindex.schedule()
    settings.set_value("embed_model", "snowflake-arctic-embed2")
    second = reindex.schedule()
    assert queue.job_summary(first)["status"] == "cancelled"
    assert queue.job_summary(second)["status"] == "queued"
    assert "snowflake-arctic-embed2" in queue.job_summary(second)["title"]


def test_interrupted_reindex_resumes_at_startup(tmp_path, monkeypatch, fake_embed):
    _library(tmp_path, fake_embed)
    monkeypatch.setattr(ollama, "embed", _new_model)
    settings.set_value("embed_model", "nouveau-modele")
    queue.cancel_job(reindex.schedule())
    assert reindex.schedule() is None                        # le modèle n'a pas changé
    resumed = reindex.schedule(resume=True)                  # au démarrage : on reprend
    assert resumed and queue.job_summary(resumed)["total"] == 6
    assert reindex.schedule(resume=True) is None             # déjà en file
    _run_queue()
    assert _all_vectors_from(_new_model)


def test_base_from_before_the_new_default(fake_embed):
    """Vecteurs calculés quand nomic-embed-text était le défaut, sans réglage enregistré."""
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    store.upsert_entry(general, "a.txt", "file", None, [("alpha", fake_embed(["alpha"])[0])])
    assert reindex.indexed_model() == "nomic-embed-text"
    assert reindex.schedule(resume=True)                     # défaut : embeddinggemma


def test_base_imported_from_v1_keeps_its_model(fake_embed):
    settings.set_value("embed_model", "nomic-embed-text")    # réglage repris de la v1
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    store.upsert_entry(general, "a.txt", "file", None, [("alpha", fake_embed(["alpha"])[0])])
    assert reindex.schedule(resume=True) is None
    assert settings.get("rag_min_score") == 0.70             # seuil de ce modèle


def test_empty_base_needs_no_reindex():
    store.ensure_defaults()
    assert reindex.schedule(resume=True) is None
    settings.set_value("embed_model", "bge-m3")
    assert reindex.schedule() is None and reindex.indexed_model() == "bge-m3"
    assert queue.list_jobs() == []


def test_settings_change_runs_reindex_through_the_queue(fake_embed, monkeypatch):
    store.ensure_defaults()
    with TestClient(create_app(AUTH["X-Coeur-Token"], {"testserver"})) as client:
        general = store.list_collections()[0]["id"]
        store.upsert_entry(general, "a.txt", "file", None,
                           [("alpha bêta", fake_embed(["alpha bêta"])[0])])
        client.put("/api/settings", json={"web_results": 4}, headers=AUTH)
        assert client.get("/api/jobs", headers=AUTH).json()["jobs"] == []
        monkeypatch.setattr(ollama, "embed", _new_model)
        view = client.put("/api/settings", json={"embed_model": "bge-m3"}, headers=AUTH).json()
        assert view["rag_min_score"] == 0.45                 # le seuil suit le modèle
        deadline = time.time() + 15
        while time.time() < deadline:
            jobs = client.get("/api/jobs", headers=AUTH).json()["jobs"]
            if jobs and jobs[0]["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert [j["kind"] for j in jobs] == ["reindex"]
        assert jobs[0]["status"] == "done" and jobs[0]["result"]["indexed"] == 1
    assert _all_vectors_from(_new_model)
