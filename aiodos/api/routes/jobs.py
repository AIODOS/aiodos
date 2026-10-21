"""File de tâches : consultation et commandes (pause, reprise, priorité…)."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos.i18n import t
from aiodos.jobs import queue
from aiodos.jobs.models import PRIORITIES

router = APIRouter(prefix="/api")

_ACTIONS = {
    "pause": queue.pause_job,
    "resume": queue.resume_job,
    "cancel": queue.cancel_job,
    "retry": queue.retry_failed,
    "delete": queue.delete_job,
}


class PriorityIn(BaseModel):
    priority: int


@router.get("/jobs")
def jobs(request: Request):
    return {"jobs": queue.list_jobs(), "counts": queue.counts(),
            "executor": request.app.state.executor.status(),
            "priorities": {p: t(f"jobs.priority_{p}") for p in PRIORITIES}}


@router.get("/jobs/{job_id}/failures")
def failures(job_id: int):
    return queue.failed_units(job_id)


@router.post("/jobs/clear_finished")
def clear_finished(request: Request):
    removed = queue.clear_finished()
    request.app.state.bus.publish({"type": "jobs_reset"})
    return {"removed": removed}


@router.post("/jobs/{job_id}/{action}")
def act(job_id: int, action: str, request: Request):
    if action not in _ACTIONS:
        raise HTTPException(404, t("jobs.unknown_action"))
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
    if body.priority not in PRIORITIES:
        raise HTTPException(400, t("jobs.unknown_priority"))
    queue.set_priority(job_id, body.priority)
    request.app.state.executor.publish_job(job_id, force=True)
    return {"ok": True}
