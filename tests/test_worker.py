"""La machine qui prête sa puissance : conditions de partage, unité rendue quand
l'utilisateur revient ou qu'Ollama ne répond pas, échec signalé, fichier reçu
vérifié puis effacé. Sans réseau : les appels vont directement au propriétaire."""
import numpy as np
import pytest
import requests

from coeur import db, paths, settings
from coeur.jobs import kinds, queue, registry  # noqa: F401  (enregistre les tâches)
from coeur.knowledge import store
from coeur.network import peers, work, worker
from coeur.network.worker import Assignment, Worker, sharing_state
from coeur.node import identity
from coeur.services import ollama

OWNER, LENDER = "machine-a", "machine-b"


class OwnerExecutor:
    def settle(self, job_id, finished):
        if finished:
            queue.finalize_job(job_id)

    def publish_job(self, job_id, force=False):
        pass

    def wake(self):
        pass


class OwnerManager:
    executor = OwnerExecutor()

    def changed(self):
        pass


class LenderExecutor:
    interactive = 0

    def __init__(self):
        self.cooldown = {}


class LenderManager:
    node_id = LENDER

    def __init__(self):
        self.executor = LenderExecutor()

    def changed(self):
        pass


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """Propriétaire et machine qui prête partagent ici la même base : leurs
    écritures ne se croisent pas (comptes tenus sous des noms différents)."""
    store.ensure_defaults()
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": "nomic-embed-text:latest", "digest": "sha256:x", "size": 1}])
    peers.save(LENDER, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2)
    note = tmp_path / "note.txt"
    note.write_text("Le crachin breton est une pluie fine.", encoding="utf-8")
    collection = store.list_collections()[0]["id"]
    job_id = queue.create_job("index_paths", {"paths": [str(note)], "collection_id": collection})
    owner = OwnerManager()
    lender = Worker(LenderManager())
    monkeypatch.setattr(lender, "_call", lambda a, action, payload, timeout=30:
                        work.HANDLERS[action](owner, LENDER, payload))
    order = work.lease(owner, LENDER, {"resources": ["embed"],
                                       "caps": {"work_api": work.WORK_API,
                                                "models": {"nomic-embed-text:latest": "sha256:x"}}})
    return {"lender": lender, "job": job_id, "collection": collection,
            "assignment": Assignment(OWNER, "PC portable", order["unit"])}


def _unit(order):
    return dict(db.query_one("SELECT * FROM units WHERE id=?", (order["id"],)))


def _compute_with(monkeypatch, fn):
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", fn)


def test_unit_is_computed_with_the_received_file_then_the_file_is_erased(setup, monkeypatch):
    received = {}

    def compute(payload, file, options, stop):
        received["text"] = open(file, encoding="utf-8").read()
        received["name"] = file.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        return {"pieces": [received["text"]], "vectors": np.ones((1, 4), dtype=np.float32)}
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert setup["lender"]._process(a).startswith("faite")
    assert received == {"text": "Le crachin breton est une pluie fine.", "name": "note.txt"}
    assert _unit(a.order)["status"] == "done"
    assert not any(paths.work_dir().iterdir())                    # fichier effacé
    ledger = {p["node_id"]: p for p in peers.list_peers()}
    assert ledger[OWNER]["given_units"] == 1 and ledger[LENDER]["received_units"] == 1


def test_user_coming_back_stops_the_unit_and_gives_it_back(setup, monkeypatch):
    def compute(payload, file, options, stop):
        stop.set()                         # l'utilisateur revient pendant le calcul…
        return kinds.index._embed(["a", "b"], options, stop)
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert setup["lender"]._process(a) == "interrompue et rendue"
    unit = _unit(a.order)
    assert unit["status"] == "queued" and unit["attempts"] == 0     # aucun essai perdu


def test_engine_down_on_the_lender_gives_the_unit_back_and_pauses_the_resource(setup, monkeypatch):
    def compute(payload, file, options, stop):
        error = requests.ConnectionError("refusé")
        error.request = requests.Request("POST", ollama.base() + "/api/embed").prepare()
        raise error
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert "Ollama" in setup["lender"]._process(a)
    assert _unit(a.order)["status"] == "queued"
    assert "embed" in setup["lender"].executor.cooldown


def test_real_failure_is_reported_to_the_owner_with_the_machine_name(setup, monkeypatch):
    def compute(payload, file, options, stop):
        raise ValueError("document chiffré")
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert setup["lender"]._process(a) == "échec : document chiffré"
    unit = _unit(a.order)
    assert unit["status"] == "queued" and unit["error"] == "Sur « PC fixe » : document chiffré"


def test_file_changed_during_transfer_is_detected(setup, monkeypatch):
    _compute_with(monkeypatch, lambda *args: pytest.fail("ne doit pas calculer"))
    a = setup["assignment"]
    a.order["blob"]["sha256"] = "0" * 64
    assert "transfert" in setup["lender"]._process(a)


def test_sharing_conditions(monkeypatch):
    monkeypatch.setattr(identity, "on_ac_power", lambda: True)
    monkeypatch.setattr(identity, "idle_seconds", lambda: 30.0)
    assert sharing_state()["state"] == "off"
    settings.set_many({"share_enabled": True, "idle_minutes": 10})
    state = sharing_state()
    assert state["state"] == "busy" and "10 min" in state["detail"]
    monkeypatch.setattr(identity, "idle_seconds", lambda: 11 * 60.0)
    assert sharing_state()["active"]
    monkeypatch.setattr(identity, "on_ac_power", lambda: False)
    assert sharing_state()["state"] == "battery"
    settings.set_many({"share_on_ac_only": False, "share_when_idle": False})
    monkeypatch.setattr(identity, "idle_seconds", lambda: 0.0)
    assert sharing_state()["active"]


def test_gpu_share_below_100_percent_imposes_a_rest(setup):
    settings.set_value("share_max_gpu", 25)
    lender = setup["lender"]
    lender._rest_gpu(setup["assignment"], compute_s=2.0)          # unité « embed »
    remaining = lender._gpu_rest_until - worker.time.monotonic()
    assert 5.5 < remaining <= 6.0                                  # 2 s × 75 / 25
