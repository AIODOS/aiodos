"""File de tâches : ordre de passage, réessais, pause, annulation, reprise."""
from aiodos import db
from aiodos.jobs import queue, registry
from aiodos.jobs.models import BACKGROUND, NORMAL, URGENT
from aiodos.jobs.registry import UnitSpec

finalized = []


def _split(params):
    units = [UnitSpec("test_task", params.get("resource", "cpu"), {"n": i})
             for i in range(params["count"])]
    return f"test {params['name']}", units, {}


def _finalize(job_id, params, units):
    finalized.append(job_id)
    return {"units_seen": len(units)}


registry.register_job_kind("test_kind", _split, _finalize)


def _job(name, count=1, priority=NORMAL, resource="cpu"):
    return queue.create_job("test_kind", {"name": name, "count": count,
                                          "resource": resource}, priority)


def _drain(resources=("cpu",)):
    order = []
    while (unit := queue.claim_next(set(resources), "local")) is not None:
        order.append(unit["job_id"])
        queue.finish_unit(unit["id"], {}, 0.1)
    return order


def test_priority_then_arrival_order():
    background = _job("fond", priority=BACKGROUND)
    normal_1 = _job("normal 1")
    urgent = _job("urgent", priority=URGENT)
    normal_2 = _job("normal 2")
    assert _drain() == [urgent, normal_1, normal_2, background]


def test_the_first_unit_says_the_job_has_started():
    """L'exécuteur annonce alors « en cours » tout de suite (jobs/executor.py)."""
    job_id = _job("deux", count=2)
    first, second = (queue.claim_next({"cpu"}, "local") for _ in range(2))
    assert first["job_started"] and not second["job_started"]
    assert queue.job_summary(job_id)["status"] == "running"


def test_only_requested_resources_are_claimed():
    gpu_job = _job("gpu", resource="gpu")
    cpu_job = _job("cpu", resource="cpu")
    assert queue.claim_next({"cpu"}, "local")["job_id"] == cpu_job
    assert queue.claim_next({"cpu"}, "local") is None
    assert queue.claim_next({"gpu"}, "local")["job_id"] == gpu_job


def test_job_completes_and_is_finalized():
    job_id = _job("complet", count=3)
    for _ in range(3):
        unit = queue.claim_next({"cpu"}, "local")
        _, finished = queue.finish_unit(unit["id"], {}, 0.2)
    assert finished
    queue.finalize_job(job_id)
    job = queue.job_summary(job_id)
    assert job["status"] == "done" and job["done"] == 3
    assert job["result"]["units_seen"] == 3 and job_id in finalized


def test_failure_is_retried_then_marked_failed():
    job_id = _job("fragile")
    unit = queue.claim_next({"cpu"}, "local")
    assert queue.fail_unit(unit["id"], "boum", 0.1) == (job_id, False)   # réessai
    unit = queue.claim_next({"cpu"}, "local")
    assert unit["attempts"] == 2
    assert queue.fail_unit(unit["id"], "boum encore", 0.1) == (job_id, True)
    queue.finalize_job(job_id)
    job = queue.job_summary(job_id)
    assert job["status"] == "failed" and job["failed"] == 1
    assert queue.failed_units(job_id)[0]["error"] == "boum encore"


def test_skipped_units_count_apart():
    job_id = _job("vide")
    unit = queue.claim_next({"cpu"}, "local")
    queue.finish_unit(unit["id"], {"skipped": True, "ext": ".zip"}, 0.0)
    job = queue.job_summary(job_id)
    assert job["skipped"] == 1 and job["done"] == 0 and job["status"] == "done"


def test_pause_resume_cancel():
    paused = _job("pause", count=2)
    queue.pause_job(paused)
    assert queue.claim_next({"cpu"}, "local") is None
    queue.resume_job(paused)
    assert queue.claim_next({"cpu"}, "local")["job_id"] == paused
    queue.cancel_job(paused)
    assert queue.claim_next({"cpu"}, "local") is None
    assert queue.job_summary(paused)["status"] == "cancelled"


def test_interrupted_units_are_recovered():
    job_id = _job("nuit")
    queue.claim_next({"cpu"}, "local")          # puis « coupure de courant »
    queue.recover()
    assert queue.claim_next({"cpu"}, "local")["job_id"] == job_id


def test_engine_outage_does_not_burn_attempts():
    job_id = _job("pendant la mise à jour d'Ollama")
    unit = queue.claim_next({"cpu"}, "local")
    queue.release_unit(unit["id"])                 # Ollama injoignable : ce n'est pas l'unité
    unit = queue.claim_next({"cpu"}, "local")
    assert unit["job_id"] == job_id and unit["attempts"] == 1
    assert queue.job_summary(job_id)["failed"] == 0


def test_engine_down_classification():
    import requests
    from aiodos.jobs.executor import engine_down
    from aiodos.services import ollama

    def error(url):
        return requests.ConnectionError("refusé", request=requests.Request("POST", url).prepare())
    assert engine_down(error(ollama.base() + "/api/embed"))
    assert not engine_down(error("https://site-introuvable.example/page"))   # vraie erreur
    assert not engine_down(ValueError("autre chose"))


def test_empty_job_is_done_immediately():
    job_id = _job("rien", count=0)
    assert queue.job_summary(job_id)["status"] == "done"
    assert db.query_one("SELECT COUNT(*) AS n FROM units")["n"] == 0
