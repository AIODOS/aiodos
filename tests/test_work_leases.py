"""Travail confié à une autre machine : baux, contrôle des modèles, fichiers,
résultats (côté propriétaire du travail, sans réseau ni Ollama)."""
import hashlib
import threading

import numpy as np
import pytest

from aiodos import db, settings
from aiodos.jobs import kinds, leases, queue, registry  # noqa: F401  (enregistre les tâches)
from aiodos.jobs.models import MAX_ATTEMPTS
from aiodos.knowledge import store
from aiodos.network import peers, wire, work
from aiodos.network.client import PeerError
from aiodos.network.worker import safe_name
from aiodos.services import ollama

B = "machine-b"
C = "machine-c"
EMBED_DIGEST = "sha256:nomic"


class FakeExecutor:
    def __init__(self):
        self.settled, self.published = [], []

    def settle(self, job_id, finished):
        if finished:
            queue.finalize_job(job_id)
        self.settled.append((job_id, finished))

    def publish_job(self, job_id, force=False):
        self.published.append(job_id)

    def wake(self):
        pass


class FakeManager:
    def __init__(self):
        self.executor = FakeExecutor()
        self.changes = 0

    def changed(self):
        self.changes += 1

    def name(self):
        return "PC portable"


@pytest.fixture
def owner(monkeypatch, tmp_path):
    """Ce nœud a nomic-embed-text ; B est appairée. Trois fichiers à indexer."""
    store.ensure_defaults()
    settings.set_value("embed_model", "nomic-embed-text")    # n'est plus le défaut
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": "nomic-embed-text:latest", "digest": EMBED_DIGEST, "size": 1}])
    peers.save(B, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(C, "Vieux portable", b"c" * 32, "127.0.0.1", 2)
    folder = tmp_path / "docs"
    folder.mkdir()
    for name, text in [("a.txt", "Les chats dorment."), ("b.md", "Le café est chaud."),
                       ("c.txt", "La pluie tombe.")]:
        (folder / name).write_text(text, encoding="utf-8")
    collection = store.list_collections()[0]["id"]
    job_id = queue.create_job("index_paths", {"paths": [str(folder)], "collection_id": collection})
    return {"manager": FakeManager(), "job": job_id, "collection": collection, "folder": folder}


def _caps(digest=EMBED_DIGEST, api=work.WORK_API):
    return {"work_api": api, "models": {"nomic-embed-text:latest": digest}, "whisper": False}


def _lease(owner, node=B, caps=None, resources=("embed",)):
    return work.lease(owner["manager"], node, {"resources": list(resources),
                                               "caps": caps or _caps()})


def _computed(order):
    """Ce que la machine B calcule : la vraie tâche, avec un embedding factice."""
    pieces = [order["payload"]["source"]]
    return {"pieces": pieces, "vectors": np.ones((1, 8), dtype=np.float32)}


def _unit(unit_id):
    return dict(db.query_one("SELECT * FROM units WHERE id=?", (unit_id,)))


def test_index_units_are_portable_but_conversation_memory_is_not():
    assert registry.TASKS["index_file"].portable and registry.TASKS["index_url"].portable
    assert not registry.TASKS["index_conversation"].portable


def test_lease_gives_the_first_unit_with_file_fingerprint(owner):
    order = _lease(owner)["unit"]
    assert order["task"] == "index_file" and "path" not in order["payload"]
    content = (owner["folder"] / "a.txt").read_bytes()
    assert order["blob"] == {"name": "a.txt", "size": len(content),
                             "sha256": hashlib.sha256(content).hexdigest()}
    assert order["options"]["embed_model"] == "nomic-embed-text"
    unit = _unit(order["id"])
    assert unit["status"] == "running" and unit["node"] == B and unit["lease_until"]
    assert queue.job_summary(owner["job"])["remote"] == {B: 1}


def test_nothing_is_lent_without_the_exact_same_embedding_model(owner):
    assert _lease(owner, caps=_caps(digest="sha256:autre"))["unit"] is None
    assert _lease(owner, caps=_caps(api=work.WORK_API + 1))["unit"] is None
    assert _lease(owner, resources=("gpu", "cpu"))["unit"] is None


def test_owner_without_ollama_says_why_nothing_is_lent(owner, monkeypatch):
    def down(max_age=10):
        raise ConnectionError("Ollama arrêté")
    monkeypatch.setattr(ollama, "list_models", down)
    reply = _lease(owner)
    assert reply["unit"] is None and "Ollama ne répond pas sur « PC portable »" in reply["reason"]
    assert db.query_one("SELECT COUNT(*) AS n FROM units WHERE status='running'")["n"] == 0


def test_only_a_document_needs_a_machine_that_reads_its_format():
    """Texte et extensions inconnues se lisent partout ; un document demande le
    paquet qui lit son format (python-pptx…), qui peut manquer là-bas."""
    options = {"embed_model": "nomic-embed-text", "vision": None, "whisper": None}

    def need(task, payload):
        unit = {"task": task, "resource": "embed", "payload": payload}
        return registry.TASKS[task].requires(unit, options)
    assert need("index_file", {"path": "C:/cours/Séance 1.PPTX"}) == {
        "embed": "nomic-embed-text", "format": ".pptx"}
    assert need("index_file", {"path": "C:/cours/notes.md"}) == {"embed": "nomic-embed-text"}
    assert need("index_file", {"path": "C:/cours/LISEZMOI"}) == {"embed": "nomic-embed-text"}
    assert need("index_url", {"url": "https://example.org/plan.pdf"}) == {
        "embed": "nomic-embed-text"}


def test_nothing_is_lent_when_offloading_is_off_or_library_is_local(owner):
    settings.set_value("offload_enabled", False)
    assert _lease(owner)["unit"] is None
    settings.set_value("offload_enabled", True)
    store.set_local_only(owner["collection"], True)
    assert _lease(owner)["unit"] is None
    store.set_local_only(owner["collection"], False)
    assert _lease(owner)["unit"] is not None


def test_blob_is_served_only_to_the_machine_holding_the_unit(owner):
    order = _lease(owner)["unit"]
    data = work.blob(owner["manager"], B, {"unit": order["id"], "offset": 0, "size": 1024})
    assert wire_bytes(data) == (owner["folder"] / "a.txt").read_bytes()
    with pytest.raises(PeerError):
        work.blob(owner["manager"], C, {"unit": order["id"], "offset": 0})


def wire_bytes(reply):
    from aiodos.network.envelope import b64d
    return b64d(reply["data"])


def test_result_is_checked_then_stored_here_and_accounted(owner):
    orders = [_lease(owner)["unit"] for _ in range(3)]
    for order in orders:
        reply = work.result(owner["manager"], B, {"unit": order["id"], "compute_s": 2.5,
                                                  "output": wire.pack(_computed(order))})
        assert reply == {"accepted": True}
    job = queue.job_summary(owner["job"])
    assert job["status"] == "done" and job["done"] == 3 and job["result"]["indexed"] == 3
    sources = sorted(e["source"] for e in store.list_entries(owner["collection"]))
    assert sources == ["docs/a.txt", "docs/b.md", "docs/c.txt"]
    ledger = {p["node_id"]: p for p in peers.list_peers()}[B]
    assert ledger["received_units"] == 3 and ledger["received_s"] == pytest.approx(7.5)


def test_invalid_result_is_refused_and_the_unit_retried(owner):
    order = _lease(owner)["unit"]
    bad = {"pieces": ["a", "b"], "vectors": np.ones((1, 8), dtype=np.float32)}
    reply = work.result(owner["manager"], B, {"unit": order["id"], "output": wire.pack(bad)})
    assert reply["accepted"] is False
    unit = _unit(order["id"])
    assert unit["status"] == "queued" and "refusé" in unit["error"]
    assert store.list_entries(owner["collection"]) == []


def test_result_from_another_machine_is_refused(owner):
    order = _lease(owner)["unit"]
    reply = work.result(owner["manager"], C, {"unit": order["id"],
                                              "output": wire.pack(_computed(order))})
    assert reply["accepted"] is False and _unit(order["id"])["node"] == B


def test_expired_lease_counts_an_attempt_and_late_result_is_still_welcome(owner):
    order = _lease(owner)["unit"]
    db.execute("UPDATE units SET lease_until=1 WHERE id=?", (order["id"],))
    assert leases.expire() == [(owner["job"], False)]
    unit = _unit(order["id"])
    assert unit["status"] == "queued" and unit["node"] == B and unit["attempts"] == 1
    # renouvellement refusé : la machine sait qu'elle peut arrêter
    assert work.renew(owner["manager"], B, {"unit": order["id"]}) == {"ok": False}
    # mais son résultat, arrivé avant que l'unité soit redonnée, est accepté
    reply = work.result(owner["manager"], B, {"unit": order["id"],
                                              "output": wire.pack(_computed(order))})
    assert reply == {"accepted": True} and _unit(order["id"])["status"] == "done"


def test_unit_failing_on_every_machine_ends_failed(owner):
    order = _lease(owner)["unit"]
    for attempt in range(MAX_ATTEMPTS):
        db.execute("UPDATE units SET status='running', node=?, lease_until=1, attempts=? "
                   "WHERE id=?", (B, attempt + 1, order["id"]))
        leases.expire()
    assert _unit(order["id"])["status"] == "failed"
    assert queue.job_summary(owner["job"])["failed"] == 1


def test_release_returns_the_unit_without_counting_an_attempt(owner):
    order = _lease(owner)["unit"]
    assert work.release(owner["manager"], B, {"unit": order["id"]}) == {"ok": True}
    unit = _unit(order["id"])
    assert unit["status"] == "queued" and unit["attempts"] == 0 and unit["node"] is None
    assert work.release(owner["manager"], B, {"unit": order["id"]}) == {"ok": False}


def test_failure_reported_by_the_machine_names_it(owner):
    order = _lease(owner)["unit"]
    assert work.fail(owner["manager"], B, {"unit": order["id"], "error": "PDF illisible"}) == {"ok": True}
    assert _unit(order["id"])["error"] == "Sur « PC fixe » : PDF illisible"


def test_local_executor_and_remote_machine_never_get_the_same_unit(owner):
    seen = []
    lock = threading.Lock()

    def remote():
        while (reply := _lease(owner)["unit"]) is not None:
            with lock:
                seen.append(reply["id"])

    def local():
        while (unit := queue.claim_next({"embed"}, "local")) is not None:
            with lock:
                seen.append(unit["id"])

    threads = [threading.Thread(target=remote), threading.Thread(target=local)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == sorted(set(seen)) and len(seen) == 3


def test_vectors_travel_as_float32_and_bad_arrays_are_rejected():
    vectors = np.arange(6, dtype=np.float32).reshape(2, 3)
    back = wire.unpack(wire.pack({"vectors": vectors, "pieces": ["a", "b"]}))
    assert back["pieces"] == ["a", "b"] and np.array_equal(back["vectors"], vectors)
    with pytest.raises(ValueError):
        wire.unpack({"__array__": "AAAA", "shape": [5, 5]})


def test_received_file_names_are_made_safe():
    assert safe_name("rapport final.pdf") == "rapport final.pdf"
    assert safe_name("..\\..\\Windows\\system32\\evil.dll") == "evil.dll"
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("CON.txt") == "_CON.txt"
    assert safe_name("") == "fichier"


def test_big_files_stay_with_machines_of_the_local_network(owner):
    """Étape 2.6 : une machine joignable seulement par Internet ne reçoit pas de fichier
    plus gros que le plafond internet_max_blob_mb (64 Mo par défaut)."""
    settings.set_value("internet_max_blob_mb", 0.00001)          # une dizaine d'octets
    peers.save(B, "PC fixe", b"k" * 32, None, None)               # B : plus de réseau local
    assert _lease(owner)["unit"] is None
    assert _lease(owner, node=C)["unit"] is not None              # C : sur le réseau local
    settings.set_value("internet_max_blob_mb", 64)
    assert _lease(owner)["unit"] is not None
