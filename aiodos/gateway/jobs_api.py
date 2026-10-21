"""Les travaux de moteur des applications (docs/conception/partage-puissance.md § 2.5, 6.7) :
POST /aiodos/v1/jobs dépose un travail ComfyUI ; l'appli le suit (GET, événements SSE), le met
en pause, le reprend, l'annule, relance ses unités en échec, l'oublie. Une appli n'agit que
sur ses propres travaux (I10) : ceux d'une autre lui sont introuvables (404 job_not_found).

Cette version calcule sur ce PC (le ComfyUI accordé de ce PC) ; mes machines suivront.
Le même client_ref (ou Idempotency-Key) rend le même travail (200 au lieu de 202).
"""
import asyncio
import json
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aiodos import db
from aiodos.engines import comfy_client, comfyui, machines
from aiodos.gateway import errors, file_store, input_store, scopes
from aiodos.gateway.errors import ApiError
from aiodos.i18n import t
from aiodos.jobs import queue
from aiodos.jobs.kinds import engine
from aiodos.jobs.models import BACKGROUND, NORMAL, PAUSE_AFTER, PAUSE_MODES, URGENT

router = APIRouter(prefix="/aiodos/v1")
CAPABILITY = "engines:comfyui"
MAX_ACTIVE = 50
MAX_UNITS_LISTED = 64
EVENTS_EVERY = 0.25
PING_EVERY = 15.0
STATUS = {"queued": "queued", "running": "in_progress", "paused": "paused",
          "done": "completed", "failed": "failed", "cancelled": "cancelled"}
DB_STATUS = {v: k for k, v in STATUS.items()}
ACTIVE = ("queued", "running", "paused")
PRIORITY_NAMES = {URGENT: "high", NORMAL: "normal", BACKGROUND: "low"}


def _fail(request: Request, exc: ApiError):
    return errors.response(request.url.path, exc, errors.lang_of(request.headers))


def _invalid(exc: engine.Invalid) -> ApiError:
    return ApiError(exc.code, str(exc), param=exc.param, detail=exc.detail, status=exc.status)


def _row(app_id: str, public_id: str):
    """Le travail de cette appli, ou None (celui d'une autre n'existe pas pour elle)."""
    return db.query_one("SELECT * FROM jobs WHERE public_id=? AND owner_app=?",
                        (str(public_id), app_id))


# ------------------------------------------------------------------ l'objet
def job_object(row) -> dict:
    """Le travail tel que le voit l'appli (§ 6.7)."""
    params = engine.params_of(row["id"]) if row["status"] in ACTIVE else json.loads(row["params"])
    units = db.query("SELECT id, status, node, place, result, error, compute_s, started, "
                     "progress, payload FROM units WHERE job_id=? ORDER BY id", (row["id"],))
    files = file_store.for_job(row["id"])
    by_unit = file_store.ids_by_unit(row["id"])
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    machines, listed, stages = {}, [], []
    for u in units:
        counts[u["status"]] = counts.get(u["status"], 0) + 1
        payload = json.loads(u["payload"])
        index = payload.get("item")
        item = params["items"][index] if index is not None and index < len(params["items"]) \
            else {}
        result = json.loads(u["result"] or "{}")
        progress = json.loads(u["progress"] or "null") if u["progress"] else None
        machine = result.get("machine") if u["status"] == "done" else None
        if u["status"] == "running":
            machine = machine or _machine_name(u["node"], u["place"])
            if progress and progress.get("stage"):
                stages.append(progress["stage"])
        if machine:
            entry = machines.setdefault(machine, {"name": machine, "place": u["place"] or "here",
                                                  "running": 0, "done": 0})
            entry["running" if u["status"] == "running" else "done"] += \
                1 if u["status"] in ("running", "done") else 0
        if len(listed) < MAX_UNITS_LISTED:
            unit = {"index": index, "label": item.get("label"),
                    "status": {"running": "in_progress", "done": "completed"}.get(u["status"],
                                                                                 u["status"]),
                    "place": u["place"] if u["status"] in ("running", "done") else None,
                    "machine": machine, "files": by_unit.get(index, []),
                    "seconds": round(u["compute_s"], 1) if u["compute_s"] else None}
            # L'étape, toujours présente : celle du calcul en cours, l'attente d'une machine,
            # ou null (fini, ou pas encore commencé)
            unit["stage"] = (progress or {}).get("stage") if u["status"] == "running" else (
                "waiting_machine" if u["status"] == "queued" else None)
            if u["status"] == "failed" and u["error"]:
                unit["error"] = _unit_error(u["error"])
            listed.append(unit)
    total = len(units)
    done, failed, running = counts["done"], counts["failed"], counts["running"]
    status = STATUS.get(row["status"], row["status"])
    if row["status"] == "paused" and running:
        status = "pausing"
    if row["status"] == "cancelled" and running:
        status = "cancelling"
    now = db.now()
    elapsed = int(now - row["started"]) if row["started"] else 0
    percent = int(100 * (done + failed) / total) if total else 100
    message = _message(done, total, running, len(machines), status)
    return {
        "id": row["public_id"], "object": "aiodos.job", "engine": params.get("engine", "comfyui"),
        "label": row["label"], "client_ref": row["client_ref"], "status": status,
        "pause": ({"when": row["pause_mode"] or PAUSE_AFTER, "pending": running}
                  if row["status"] == "paused" else None),
        "priority": PRIORITY_NAMES.get(row["priority"], "normal"),
        "where": params.get("where"), "where_effective": (row["reach"] or "").split(",")
        if row["reach"] else [], "where_reason": None,
        "progress": {"total": total, "done": done, "running": running,
                     "queued": counts["queued"], "failed": failed, "percent": percent,
                     "estimated": True, "elapsed_s": elapsed, "eta_s": None,
                     "stage": _stage(stages, status), "machines": list(machines.values()),
                     "message": message},
        "waiting": None, "units": listed, "files": files,
        "error": {"code": "all_failed", "message": row["error"]} if row["error"] else None,
        "cancelled_by": row["cancelled_by"],
        "created_at": int(row["created"]), "started_at": int(row["started"]) if row["started"]
        else None, "completed_at": int(row["finished"]) if row["finished"] else None}


_STAGE_ORDER = ("waiting_machine", "waiting_engine", "sending_inputs", "running", "receiving",
                "checking")


def _stage(stages, status):
    if status in ("completed", "failed", "cancelled"):
        return "done"
    if not stages:
        return "queued"
    return max(stages, key=lambda s: _STAGE_ORDER.index(s) if s in _STAGE_ORDER else -1)


def _unit_error(text: str) -> dict:
    code = text.split(" ", 1)[0] if text.split(" ", 1)[0] in (
        "engine_error", "engine_timeout", "output_invalid", "output_too_large",
        "no_machine") else "engine_error"
    return {"code": code, "message": text[:300]}


def _machine_name(node, place=None) -> str:
    """Le nom de la machine qui calcule : ce PC (l'exécuteur range son propre identifiant),
    ou une machine appairée."""
    from aiodos import settings
    from aiodos.network import peers
    if not node or node == "local" or (place or "here") == "here":
        return str(settings.get("node_name") or settings.machine_name())[:80]
    try:
        peer = peers.get(node)
        return (peer or {}).get("name") or "?"
    except Exception:
        return "?"


def _message(done, total, running, machines, status) -> str:
    """L'avancement en une phrase, dans la langue de la requête de l'appli."""
    if status == "completed":
        return t("gateway_jobs.progress_done", done=done, total=total)
    if status in ("paused", "pausing"):
        return t("gateway_jobs.progress_paused", done=done, total=total)
    if status in ("cancelled", "cancelling"):
        return t("gateway_jobs.progress_cancelled", done=done, total=total)
    if running:
        return t("gateway_jobs.progress_running", done=done, total=total, running=running,
                 n=machines)
    return t("gateway_jobs.progress", done=done, total=total)


# ------------------------------------------------------------------ déposer
def _existing(app_id, client_ref):
    if not client_ref:
        return None
    return db.query_one("SELECT * FROM jobs WHERE owner_app=? AND client_ref=?",
                        (app_id, client_ref))


def _prepare(app_id: str, body: dict, where: list) -> dict:
    """Vérifie et met en file (dans un fil : ComfyUI se lit, la base s'écrit)."""
    active = db.query_one("SELECT COUNT(*) AS n FROM jobs WHERE owner_app=? AND status IN "
                          "('queued', 'running', 'paused')", (app_id,))["n"]
    if active >= MAX_ACTIVE:
        raise ApiError("quota_exceeded", t("gateway_jobs.active_quota", n=MAX_ACTIVE),
                       detail={"quota": "active_jobs", "max": MAX_ACTIVE})
    if file_store.unfetched_bytes(app_id) >= file_store.QUOTA:
        raise ApiError("quota_exceeded", t("gateway_jobs.unfetched_quota"),
                       detail={"quota": "unfetched_files"})
    # Le ComfyUI de ce PC donne ses définitions (vérification complète, modèles par dossier) ;
    # sans lui, celui de mes machines vérifiera à son tour (I14)
    found = engine.instance_here()
    inventory = None
    if found is not None:
        try:
            inventory = comfyui.inventory(found.url)
        except comfy_client.Unreachable:
            found = None
    try:
        params = engine.normalize(body, app_id=app_id, where=where, inventory=inventory)
    except engine.Invalid as exc:
        raise _invalid(exc) from exc
    capable = machines.known(where)
    if not capable:
        raise ApiError("engine_unavailable", t("gateway_jobs.no_comfyui"), status=409,
                       detail={"engine": "comfyui", "where": where})
    needed = list(params["models"].values())
    gaps = [(machines.missing_on(m, needed), m) for m in capable]
    if all(missing for missing, _ in gaps):
        missing, best = min(gaps, key=lambda g: len(g[0]))
        listed = ", ".join(m["name"] for m in missing[:5])
        raise ApiError("model_unavailable",
                       t("gateway_jobs.models_missing", machine=best["name"], models=listed),
                       detail={"models": missing, "machine": best["name"]})
    reach = ",".join(c for c in where if c in scopes.SERVED[CAPABILITY])
    public_id = "job_" + secrets.token_urlsafe(12)
    job_id = queue.create_job(engine.KIND, params, engine.priority_of(params),
                              owner_app=app_id, label=params["label"], reach=reach,
                              public_id=public_id, client_ref=params["client_ref"])
    input_store.link(job_id, app_id, [v["sha256"] for v in params["inputs"].values()])
    # Une unité qu'aucune machine n'a prise avant bounds.wait_s échoue (no_machine)
    db.execute("UPDATE jobs SET wait_until=? WHERE id=?",
               (db.now() + float(params["bounds"]["wait_s"]), job_id))
    return db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))


@router.post("/jobs")
async def submit(request: Request):
    context = request.state.aiodos_app
    try:
        body = await request.json()
    except ValueError:
        body = None
    try:
        if not isinstance(body, dict):
            raise ApiError("invalid_request", t("gateway_api.json_not_object"))
        if context is None or context.kind == "general":
            raise ApiError("scope_denied", t("gateway_jobs.app_key_required"),
                           detail={"scope": CAPABILITY})
        ref = body.get("client_ref") or request.headers.get("idempotency-key")
        if ref and not body.get("client_ref"):
            body = {**body, "client_ref": str(ref)[:200]}
        old = await asyncio.to_thread(_existing, context.id, body.get("client_ref"))
        if old is not None:
            return JSONResponse(await asyncio.to_thread(job_object, old), status_code=200)
        where = scopes.for_job(request, CAPABILITY, body.get("where"))
        body = {k: v for k, v in body.items() if k != "where"}
        row = await asyncio.to_thread(_prepare, context.id, body, where)
    except ApiError as exc:
        return _fail(request, exc)
    executor = getattr(request.app.state.node, "executor", None)
    if executor is not None:
        executor.wake()
        executor.publish_job(row["id"], force=True)
    obj = await asyncio.to_thread(job_object, row)
    return JSONResponse(obj, status_code=202,
                        headers={"Location": f"/aiodos/v1/jobs/{row['public_id']}"})


# ------------------------------------------------------------------- suivre
@router.get("/jobs")
async def list_jobs(request: Request):
    context = request.state.aiodos_app
    query = request.query_params
    clauses, args = ["owner_app=?"], [context.id]
    active = query.get("active")
    if active in ("1", "true"):
        clauses.append("status IN ('queued', 'running', 'paused')")
    elif active in ("0", "false"):
        clauses.append("status NOT IN ('queued', 'running', 'paused')")
    if query.get("client_ref"):
        clauses.append("client_ref=?")
        args.append(query["client_ref"])
    if query.get("status"):                 # les états vus par l'appli, séparés par des virgules
        wanted = {DB_STATUS.get(s.strip()) for s in query["status"].split(",")} - {None}
        if not wanted:
            return JSONResponse({"object": "list", "jobs": []})
        clauses.append(f"status IN ({','.join('?' * len(wanted))})")
        args.extend(sorted(wanted))
    try:
        limit = max(1, min(200, int(query.get("limit") or 50)))
    except ValueError:
        limit = 50

    def read():
        rows = db.query(f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} "
                        "ORDER BY created DESC, id DESC LIMIT ?", (*args, limit))
        return [job_object(r) for r in rows]
    return JSONResponse({"object": "list", "jobs": await asyncio.to_thread(read)})


@router.get("/jobs/{public_id}")
async def get_job(public_id: str, request: Request):
    context = request.state.aiodos_app
    row = await asyncio.to_thread(_row, context.id, public_id)
    if row is None:
        return _fail(request, ApiError("job_not_found"))
    return JSONResponse(await asyncio.to_thread(job_object, row))


@router.get("/jobs/{public_id}/files")
async def job_files(public_id: str, request: Request):
    context = request.state.aiodos_app
    row = await asyncio.to_thread(_row, context.id, public_id)
    if row is None:
        return _fail(request, ApiError("job_not_found"))
    unfetched = request.query_params.get("unfetched") in ("1", "true")
    files = await asyncio.to_thread(file_store.for_job, row["id"], unfetched)
    return JSONResponse({"object": "list", "files": files})


@router.get("/jobs/{public_id}/events")
async def events(public_id: str, request: Request):
    """L'état complet d'abord, puis à chaque changement (4 fois par seconde au plus) ; un
    événement « file » par nouveau fichier ; « end » à la fin ; « : ping » toutes les 15 s.
    Rien n'est rejoué : à la reconnexion, l'état complet."""
    context = request.state.aiodos_app
    row = await asyncio.to_thread(_row, context.id, public_id)
    if row is None:
        return _fail(request, ApiError("job_not_found"))

    async def stream():
        last, seen, pinged = None, set(), time.monotonic()
        while True:
            current = await asyncio.to_thread(_row, context.id, public_id)
            if current is None:
                yield _event("end", {"id": public_id, "status": "deleted"})
                return
            obj = await asyncio.to_thread(job_object, current)
            for f in obj["files"]:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    if last is not None:
                        yield _event("file", f)
            text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
            if text != last:
                last = text
                yield _event("job", obj)
                pinged = time.monotonic()
            if obj["status"] in ("completed", "failed", "cancelled"):
                yield _event("end", {"id": public_id, "status": obj["status"]})
                return
            if time.monotonic() - pinged > PING_EVERY:
                yield b": ping\n\n"
                pinged = time.monotonic()
            if await request.is_disconnected():
                return
            await asyncio.sleep(EVENTS_EVERY)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


def _event(kind: str, data) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


# ----------------------------------------------------------------- commandes
async def _command(request: Request, public_id: str, action):
    context = request.state.aiodos_app
    row = await asyncio.to_thread(_row, context.id, public_id)
    if row is None:
        return _fail(request, ApiError("job_not_found"))
    try:
        await asyncio.to_thread(action, row)
    except ApiError as exc:
        return _fail(request, exc)
    executor = getattr(request.app.state.node, "executor", None)
    if executor is not None:
        executor.wake()
        executor.publish_job(row["id"], force=True)
    fresh = await asyncio.to_thread(_row, context.id, public_id)
    if fresh is None:
        return JSONResponse({"id": public_id, "deleted": True})
    return JSONResponse(await asyncio.to_thread(job_object, fresh), status_code=202)


def _active(row, code):
    if row["status"] not in ACTIVE:
        raise ApiError(code)


@router.post("/jobs/{public_id}/pause")
async def pause(public_id: str, request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    when = (body or {}).get("when", PAUSE_AFTER) if isinstance(body, dict) else PAUSE_AFTER
    if when not in PAUSE_MODES:
        return _fail(request, ApiError("invalid_request", t("gateway_jobs.when_invalid"),
                                       param="when"))

    def act(row):
        _active(row, "job_not_pausable")
        queue.pause_job(row["id"], when)
    return await _command(request, public_id, act)


@router.post("/jobs/{public_id}/resume")
async def resume(public_id: str, request: Request):
    def act(row):
        _active(row, "job_not_pausable")
        queue.resume_job(row["id"])
    return await _command(request, public_id, act)


@router.post("/jobs/{public_id}/cancel")
async def cancel(public_id: str, request: Request):
    def act(row):
        _active(row, "job_not_cancellable")
        queue.cancel_job(row["id"], by="app")
    return await _command(request, public_id, act)


@router.post("/jobs/{public_id}/retry")
async def retry(public_id: str, request: Request):
    def act(row):
        queue.retry_failed(row["id"])
    return await _command(request, public_id, act)


@router.delete("/jobs/{public_id}")
async def forget(public_id: str, request: Request):
    delete_files = request.query_params.get("files") == "delete"

    def act(row):
        if row["status"] in ACTIVE:
            raise ApiError("job_not_cancellable", t("gateway_jobs.cancel_first"))
        if delete_files:
            file_store.delete_job_files(row["id"])
        queue.delete_job(row["id"])
        engine.forget_params(row["id"])
    return await _command(request, public_id, act)


@router.post("/jobs/pause")
async def pause_mine(request: Request):
    """Tous les travaux actifs de cette appli (« Tout mettre en pause » de l'appli)."""
    context = request.state.aiodos_app
    try:
        body = await request.json()
    except ValueError:
        body = {}
    when = (body or {}).get("when", PAUSE_AFTER) if isinstance(body, dict) else PAUSE_AFTER
    if when not in PAUSE_MODES:
        return _fail(request, ApiError("invalid_request", t("gateway_jobs.when_invalid"),
                                       param="when"))

    def act():
        rows = db.query("SELECT id FROM jobs WHERE owner_app=? AND status IN ('queued', "
                        "'running')", (context.id,))
        for r in rows:
            queue.pause_job(r["id"], when)
        return len(rows)
    count = await asyncio.to_thread(act)
    executor = getattr(request.app.state.node, "executor", None)
    if executor is not None:
        executor.wake()
    return JSONResponse({"paused": count})


@router.post("/jobs/resume")
async def resume_mine(request: Request):
    context = request.state.aiodos_app

    def act():
        rows = db.query("SELECT id FROM jobs WHERE owner_app=? AND status='paused'",
                        (context.id,))
        for r in rows:
            queue.resume_job(r["id"])
        return len(rows)
    count = await asyncio.to_thread(act)
    executor = getattr(request.app.state.node, "executor", None)
    if executor is not None:
        executor.wake()
    return JSONResponse({"resumed": count})
