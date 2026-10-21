"""File de tâches : consultation et commandes (pause, reprise, priorité…).

Pause « après l'unité en cours » (par défaut : rien ne se perd) ou « tout de suite » (le
calcul en cours s'arrête, ici et chez les autres machines, l'unité est rendue sans compter
d'essai), pour un travail ou pour toute la file (jobs/queue.py)."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos.jobs import queue
from aiodos.jobs.models import PAUSE_AFTER, PAUSE_MODES, PRIORITY_LABELS

router = APIRouter(prefix="/api")

_ACTIONS = {
    "resume": queue.resume_job,
    "cancel": queue.cancel_job,
    "retry": queue.retry_failed,
    "delete": queue.delete_job,
}


class PriorityIn(BaseModel):
    priority: int


class PauseIn(BaseModel):
    when: str = PAUSE_AFTER


def _when(body: PauseIn | None) -> str:
    when = body.when if body else PAUSE_AFTER
    if when not in PAUSE_MODES:
        raise HTTPException(400, "Pause : « after_current » ou « now ».")
    return when


@router.get("/jobs")
def jobs(request: Request):
    return {"jobs": queue.list_jobs(), "counts": queue.counts(),
            "executor": request.app.state.executor.status(),
            "priorities": PRIORITY_LABELS, "queue_pause": queue.queue_pause()}


@router.get("/jobs/{job_id}/failures")
def failures(job_id: int):
    return queue.failed_units(job_id)


@router.post("/jobs/clear_finished")
def clear_finished(request: Request):
    removed = queue.clear_finished()
    request.app.state.bus.publish({"type": "jobs_reset"})
    return {"removed": removed}


@router.post("/jobs/pause_all")
def pause_all(request: Request, body: PauseIn | None = None):
    queue.pause_all(_when(body))
    request.app.state.executor.wake()
    request.app.state.bus.publish({"type": "jobs_reset"})
    return {"queue_pause": queue.queue_pause()}


@router.post("/jobs/resume_all")
def resume_all(request: Request):
    queue.resume_all()
    request.app.state.executor.wake()
    request.app.state.bus.publish({"type": "jobs_reset"})
    return {"queue_pause": queue.queue_pause()}


@router.post("/jobs/{job_id}/pause")
def pause(job_id: int, request: Request, body: PauseIn | None = None):
    queue.pause_job(job_id, _when(body))
    executor = request.app.state.executor
    executor.wake()
    executor.publish_job(job_id, force=True)
    return {"ok": True}


@router.post("/jobs/{job_id}/{action}")
def act(job_id: int, action: str, request: Request):
    if action not in _ACTIONS:
        raise HTTPException(404, "Action inconnue.")
    _ACTIONS[action](job_id)
    executor = request.app.state.executor
    executor.wake()
    if action == "delete":
        request.app.state.bus.publish({"type": "jobs_reset"})
    else:
        executor.publish_job(job_id, force=True)
    return {"ok": True}


@router.patch("/jobs/{job_id}")
def set_priority(job_id: int, body: PriorityIn, request: Request):
    if body.priority not in PRIORITY_LABELS:
        raise HTTPException(400, "Priorité inconnue.")
    queue.set_priority(job_id, body.priority)
    request.app.state.executor.publish_job(job_id, force=True)
    return {"ok": True}
