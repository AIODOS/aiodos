"""Modèle d'embeddings absent de cette machine (knowledge/embed_model.py, 29/10/2026) : un seul
message, aucun échec par fichier ; au premier indexage, un autre modèle déjà là pris d'office et
enregistré ; des vecteurs déjà en base, jamais de changement en silence."""
import asyncio
import json
import logging

import pytest

from aiodos import db, settings
from aiodos.jobs import queue
from aiodos.jobs.executor import Executor
from aiodos.jobs.kinds import reindex
from aiodos.knowledge import chunking, embed_model, store
from aiodos.llm import integrated
from aiodos.services import ollama
from tests.conftest import _bag_of_words

ALL = {"gpu", "embed", "cpu", "net"}


@pytest.fixture(autouse=True)
def clean_state():
    embed_model.forget()
    yield
    embed_model.forget()


def _engine(monkeypatch, here, broken=()):
    """Un moteur factice : les modèles d'embeddings `here` sont là (ceux de `broken` ne se
    chargent pas) ; tout autre manque, comme dans le moteur intégré. → les appels reçus."""
    calls = []

    def embed(texts, model=None, timeout=None):
        name = embed_model.full(model or settings.get("embed_model"))
        calls.append(name)
        if name in broken:
            raise integrated.Incompatible("ne se charge pas", name)
        if name not in [embed_model.full(n) for n in here]:
            raise integrated.ModelMissing(f"Le modèle « {name} » n'est pas sur cette machine",
                                          name)
        return _bag_of_words(texts)
    monkeypatch.setattr(ollama, "embed", embed)
    monkeypatch.setattr(embed_model, "models_here",
                        lambda: [embed_model.full(n) for n in list(here) + list(broken)])
    return calls


def _drain(executor=None, rounds=50):
    """Vide la file comme l'exécuteur (son traitement des erreurs compris), sans sa boucle."""
    executor = executor or Executor("ici", lambda event: None)

    async def run():
        executor._loop = asyncio.get_running_loop()
        executor._wake = asyncio.Event()
        for _ in range(rounds):
            unit = queue.claim_next(ALL, executor.node_id)
            if unit is None:
                return
            executor.busy[unit["resource"]] += 1
            await executor._execute(unit)
    asyncio.run(run())
    return executor


def _folder(tmp_path, n=4):
    folder = tmp_path / "Notes"
    folder.mkdir()
    for i in range(n):
        (folder / f"note{i}.txt").write_text(f"Note numéro {i} : budget et réunion.",
                                             encoding="utf-8")
    return folder


def _index(folder):
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    job_id = queue.create_job("index_paths", {"paths": [str(folder)], "collection_id": general})
    return general, job_id


def _vectors_exist():
    store.ensure_defaults()
    memory = store.conversation_collection_id()
    store.upsert_entry(memory, "Conv · #1", "conversation", "1",
                       [("souvenir", _bag_of_words(["souvenir"])[0])])


def test_a_missing_model_blocks_once_without_failing_any_file(tmp_path, monkeypatch, caplog):
    """Le cas du PC de l'auteur : embeddinggemma absent, des vecteurs déjà là (rien à prendre
    d'office) : une unité essaie, elle est rendue sans compter d'essai, les autres attendent ;
    le travail le dit, une seule fois au journal."""
    _vectors_exist()
    calls = _engine(monkeypatch, here=["nomic-embed-text"])
    _general, job_id = _index(_folder(tmp_path))
    with caplog.at_level(logging.INFO):
        _drain()
    assert calls == ["embeddinggemma:latest"]                 # une seule unité a essayé
    job = queue.job_summary(job_id)
    assert job["failed"] == 0 and job["done"] == 0 and job["status"] in ("queued", "running")
    assert job["blocked"]["model"] == "embeddinggemma"
    assert job["blocked"]["message"][0] == "embed_model.job_blocked"
    units = db.query("SELECT status, attempts, handouts FROM units WHERE job_id=?", (job_id,))
    assert all(u["status"] == "queued" and u["attempts"] == 0 and u["handouts"] == 0
               for u in units)
    absent = [r for r in caplog.records if "absent de cette machine" in r.getMessage()]
    assert len(absent) == 1
    assert not [r for r in caplog.records if "en échec" in r.getMessage()]
    assert settings.get("embed_model") == "embeddinggemma"    # rien changé en silence
    # Ni rien de plus tant que rien ne change : la file ne reprend pas ces unités
    _drain()
    assert calls == ["embeddinggemma:latest"]


def test_choosing_a_present_model_lifts_the_block(tmp_path, monkeypatch):
    _vectors_exist()
    calls = _engine(monkeypatch, here=["nomic-embed-text"])
    _general, job_id = _index(_folder(tmp_path))
    _drain()
    assert embed_model.blocked() == "embeddinggemma"
    settings.set_value("embed_model", "nomic-embed-text")       # le bouton « Prendre … »
    reindex.schedule()
    assert embed_model.blocked() is None
    _drain()
    job = queue.job_summary(job_id)
    assert job["status"] == "done" and job["done"] == 4 and job["failed"] == 0
    assert job["blocked"] is None
    assert calls.count("nomic-embed-text:latest") >= 4


def test_the_block_lifts_when_the_model_appears(tmp_path, monkeypatch):
    """Le modèle téléchargé, ou Ollama relancé : relu toutes les RECHECK_S secondes."""
    _vectors_exist()
    here = ["nomic-embed-text"]
    _engine(monkeypatch, here=here)
    _general, job_id = _index(_folder(tmp_path, n=2))
    _drain()
    assert embed_model.blocked() == "embeddinggemma"
    here.append("embeddinggemma")                               # installé entre-temps
    monkeypatch.setattr(embed_model, "RECHECK_S", 0)
    told = []
    forget = embed_model.on_change(lambda: told.append(True))
    try:
        assert embed_model.blocked() is None and told
    finally:
        forget()
    _drain()
    assert queue.job_summary(job_id)["done"] == 2


def test_first_indexing_takes_a_present_model_and_records_it(tmp_path, monkeypatch, caplog):
    """Aucun vecteur nulle part : le meilleur modèle présent qui se charge est pris d'office
    (bge-m3 avant nomic-embed-text, docs/embeddings.md ; snowflake ne se charge pas), enregistré,
    son seuil de pertinence suit, et c'est dit (notice et journal)."""
    calls = _engine(monkeypatch, here=["nomic-embed-text", "bge-m3"],
                    broken=["snowflake-arctic-embed2"])
    general, job_id = _index(_folder(tmp_path))
    with caplog.at_level(logging.INFO):
        _drain()
    assert settings.get("embed_model") == "bge-m3:latest"
    assert settings.get("rag_min_score") == settings.RAG_THRESHOLDS["bge-m3"]
    assert reindex.indexed_model() == "bge-m3:latest"
    notice = embed_model.notice()
    assert notice["missing"] == "embeddinggemma" and notice["model"] == "bge-m3:latest"
    assert any("pris d'office" in r.getMessage() for r in caplog.records)
    job = queue.job_summary(job_id)
    assert job["status"] == "done" and job["done"] == 4 and job["failed"] == 0
    assert embed_model.blocked() is None
    assert "nomic-embed-text:latest" not in calls             # bge-m3, le premier, a suffi
    assert len(store.list_entries(general)) == 4
    embed_model.dismiss()
    assert embed_model.notice() is None
    embed_model._remember("embeddinggemma", "bge-m3:latest")
    settings.set_value("embed_model", "nomic-embed-text")        # changé depuis : plus rien à dire
    assert embed_model.notice() is None


def test_first_indexing_skips_models_that_do_not_load(tmp_path, monkeypatch):
    _engine(monkeypatch, here=["nomic-embed-text"], broken=["bge-m3"])
    _index(_folder(tmp_path, n=1))
    _drain()
    assert settings.get("embed_model") == "nomic-embed-text:latest"
    assert settings.get("rag_min_score") == settings.RAG_THRESHOLDS["nomic-embed-text"]


def test_existing_vectors_are_never_switched_silently(tmp_path, monkeypatch):
    _vectors_exist()
    _engine(monkeypatch, here=["nomic-embed-text", "bge-m3"])
    _index(_folder(tmp_path, n=2))
    _drain()
    assert settings.get("embed_model") == "embeddinggemma"
    assert embed_model.notice() is None
    found = embed_model.status()
    assert found["blocked"] == "embeddinggemma" and found["present"] is False
    assert found["indexed"] and found["candidates"] == ["bge-m3:latest",
                                                        "nomic-embed-text:latest"]
    assert found["download"]["name"] == "embeddinggemma"       # proposé, jamais lancé seul


def test_nothing_else_present_blocks_and_downloads_nothing(tmp_path, monkeypatch):
    _engine(monkeypatch, here=[])
    pulls = []
    monkeypatch.setattr(ollama, "pull", lambda *a, **k: pulls.append(a))
    _index(_folder(tmp_path, n=2))
    _drain()
    assert embed_model.blocked() == "embeddinggemma" and pulls == []
    assert settings.get("embed_model") == "embeddinggemma"


def test_memory_and_reindexing_wait_too(monkeypatch):
    """La mémoire des conversations et la réindexation calculent aussi des embeddings : elles
    attendent de même, sans échec."""
    from aiodos.chat import conversations
    from aiodos.jobs.kinds import index
    _vectors_exist()
    _engine(monkeypatch, here=[])
    conv = conversations.create("Cuisine")
    conversations.add_message(conv, "user", "Comment faire une ratatouille ?")
    job_id = index.queue_conversation(conv)
    _drain()
    job = queue.job_summary(job_id)
    assert job["failed"] == 0 and job["blocked"]["model"] == "embeddinggemma"


def test_ollama_files_that_rarely_load_come_last(monkeypatch):
    """Avec le moteur intégré, snowflake et embeddinggemma d'Ollama (convertis à sa façon) sont
    proposés après les autres ; la version d'AIODOS d'embeddinggemma, elle, reste en tête."""
    from types import SimpleNamespace
    from aiodos.llm import models as llm_models
    sources = {"snowflake-arctic-embed2:latest": "ollama", "embeddinggemma:latest": "aiodos",
               "nomic-embed-text:latest": "ollama", "bge-m3:latest": "ollama"}
    monkeypatch.setattr(ollama, "use_integrated", lambda: True)
    monkeypatch.setattr(embed_model, "models_here", lambda: list(sources))
    monkeypatch.setattr(llm_models, "find",
                        lambda name: SimpleNamespace(source=sources[embed_model.full(name)]))
    assert embed_model.candidates("modele-absent") == [
        "embeddinggemma:latest", "bge-m3:latest", "nomic-embed-text:latest",
        "snowflake-arctic-embed2:latest"]


def test_job_summary_says_nothing_when_not_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama, "embed", _bag_of_words)
    _general, job_id = _index(_folder(tmp_path, n=1))
    assert queue.job_summary(job_id)["blocked"] is None
    _drain()
    assert queue.job_summary(job_id)["status"] == "done"


def test_the_api_tells_the_state_and_dismisses_the_notice(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aiodos.api.server import create_app
    _engine(monkeypatch, here=["nomic-embed-text"])
    store.ensure_defaults()
    auth = {"X-Aiodos-Token": "jeton"}
    with TestClient(create_app("jeton", {"testserver"})) as client:
        found = client.get("/api/knowledge/embed", headers=auth).json()
        assert found["model"] == "embeddinggemma" and found["present"] is False
        assert found["candidates"] == ["nomic-embed-text:latest"] and found["notice"] is None
        assert found["download"] == {"name": "embeddinggemma", "size": 333_590_944,
                                     "license": "Gemma"}
        # Pris d'office : dit tant que c'est le modèle et qu'on ne l'a pas fermé
        settings.set_value("embed_model", "nomic-embed-text:latest")
        db.execute("INSERT INTO settings(key, value) VALUES(?, ?)",
                   (embed_model.NOTICE_KEY, json.dumps({"missing": "embeddinggemma",
                                                        "model": "nomic-embed-text:latest"})))
        found = client.get("/api/knowledge/embed", headers=auth).json()
        assert found["present"] is True and found["download"] is None
        assert found["notice"]["model"] == "nomic-embed-text:latest"
        assert client.delete("/api/knowledge/embed/notice", headers=auth).json()["ok"]
        assert client.get("/api/knowledge/embed", headers=auth).json()["notice"] is None


def test_a_model_just_chosen_is_not_undone(tmp_path, monkeypatch):
    """Un modèle choisi dans les Réglages : les vecteurs sont retirés et la réindexation attend.
    S'il manque, il n'est pas remplacé d'office (les passages en attente comptent comme
    indexés) : la réindexation attend, et le dit."""
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    pieces = chunking.chunk_text("Texte gardé, vecteur retiré.")
    store.upsert_entry(general, "a.txt", "file", None, list(zip(pieces, _bag_of_words(pieces))))
    reindex.schedule()
    _engine(monkeypatch, here=["nomic-embed-text"])
    settings.set_value("embed_model", "modele-choisi")
    job_id = reindex.schedule()
    assert job_id and embed_model.indexed_anything()
    _drain()
    assert settings.get("embed_model") == "modele-choisi" and embed_model.notice() is None
    job = queue.job_summary(job_id)
    assert job["failed"] == 0 and job["blocked"]["model"] == "modele-choisi"
