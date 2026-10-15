"""File de tâches persistante (SQLite) : création, attribution, suivi.

Ordre de passage : priorité du travail, puis ordre d'arrivée (FIFO). La
réciprocité entre machines s'ajoutera en phase 1, quand il y aura d'autres
propriétaires ; le temps de calcul de chaque unité est déjà mesuré (compute_s).
La file survit aux redémarrages : « lancé le soir, récupéré le matin ».
"""
import json

from coeur import db
from coeur.jobs import registry
from coeur.jobs.models import (CANCELLED, DONE, FAILED, MAX_ATTEMPTS, NORMAL,
                               PAUSED, QUEUED, RUNNING, SKIPPED)


def create_job(kind, params, priority=NORMAL, owner_node="local") -> int:
    spec = registry.JOB_KINDS[kind]
    title, units, info = spec.split(params)
    now = db.now()
    status = QUEUED if units else DONE
    with db.transaction() as c:
        job_id = c.execute(
            "INSERT INTO jobs(kind, title, priority, status, params, owner_node, total, "
            "skipped, result, created, finished) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (kind, title, priority, status, json.dumps(params, ensure_ascii=False),
             owner_node, len(units), int(info.get("skipped", 0)),
             json.dumps(info, ensure_ascii=False), now, None if units else now)).lastrowid
        c.executemany(
            "INSERT INTO units(job_id, task, resource, payload, status, created) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            [(job_id, u.task, u.resource, json.dumps(u.payload, ensure_ascii=False),
              QUEUED, now) for u in units])
    return job_id


def claim_next(resources, node) -> dict | None:
    """Réserve la prochaine unité exécutable sur l'une de ces ressources."""
    if not resources:
        return None
    placeholders = ",".join("?" * len(resources))
    row = db.query_one(
        f"""SELECT u.id, u.job_id, u.task, u.resource, u.payload, u.attempts
            FROM units u JOIN jobs j ON j.id = u.job_id
            WHERE u.status = 'queued' AND j.status IN ('queued', 'running')
              AND u.resource IN ({placeholders})
            ORDER BY j.priority, j.created, j.id, u.id LIMIT 1""", tuple(resources))
    if row is None:
        return None
    now = db.now()
    with db.transaction() as c:
        claimed = c.execute(
            "UPDATE units SET status=?, started=?, node=?, attempts=attempts+1 "
            "WHERE id=? AND status=?", (RUNNING, now, node, row["id"], QUEUED)).rowcount
        if not claimed:
            return None
        c.execute("UPDATE jobs SET status=?, started=COALESCE(started, ?) "
                  "WHERE id=? AND status=?", (RUNNING, now, row["job_id"], QUEUED))
    unit = dict(row)
    unit["payload"] = json.loads(unit["payload"])
    unit["attempts"] += 1
    return unit


def _complete_if_last(c, job_id) -> bool:
    remaining = c.execute("SELECT COUNT(*) FROM units WHERE job_id=? AND status IN (?, ?)",
                          (job_id, QUEUED, RUNNING)).fetchone()[0]
    if remaining:
        return False
    return bool(c.execute(
        "UPDATE jobs SET status=?, finished=? WHERE id=? AND status IN (?, ?, ?)",
        (DONE, db.now(), job_id, QUEUED, RUNNING, PAUSED)).rowcount)


def finish_unit(unit_id, result: dict, compute_s: float):
    """Unité réussie (ou ignorée faute de contenu). → (job_id, travail terminé ?)"""
    status = SKIPPED if result.get("skipped") else DONE
    counter = "skipped" if status == SKIPPED else "done"
    with db.transaction() as c:
        job_id = c.execute("SELECT job_id FROM units WHERE id=?", (unit_id,)).fetchone()[0]
        c.execute("UPDATE units SET status=?, result=?, finished=?, compute_s=? WHERE id=?",
                  (status, json.dumps(result, ensure_ascii=False), db.now(),
                   compute_s, unit_id))
        c.execute(f"UPDATE jobs SET {counter} = {counter} + 1 WHERE id=?", (job_id,))
        return job_id, _complete_if_last(c, job_id)


def fail_unit(unit_id, error: str, compute_s: float):
    """Unité en échec : nouvel essai si possible. → (job_id, travail terminé ?)"""
    with db.transaction() as c:
        row = c.execute("SELECT job_id, attempts FROM units WHERE id=?", (unit_id,)).fetchone()
        job_id = row["job_id"]
        if row["attempts"] < MAX_ATTEMPTS:
            c.execute("UPDATE units SET status=?, error=?, node=NULL WHERE id=?",
                      (QUEUED, error, unit_id))
            return job_id, False
        c.execute("UPDATE units SET status=?, error=?, finished=?, compute_s=? WHERE id=?",
                  (FAILED, error, db.now(), compute_s, unit_id))
        c.execute("UPDATE jobs SET failed = failed + 1 WHERE id=?", (job_id,))
        return job_id, _complete_if_last(c, job_id)


def finalize_job(job_id):
    """Agrégation finale : le type de travail résume les résultats de ses unités."""
    job = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if job is None:
        return
    result = json.loads(job["result"] or "{}")
    spec = registry.JOB_KINDS.get(job["kind"])
    if spec and spec.finalize:
        units = [dict(u, payload=json.loads(u["payload"]),
                      result=json.loads(u["result"] or "{}"))
                 for u in db.query("SELECT * FROM units WHERE job_id=?", (job_id,))]
        result.update(spec.finalize(job_id, json.loads(job["params"]), units) or {})
    status, error = job["status"], None
    if job["failed"] and not job["done"] and status == DONE:
        status, error = FAILED, "Toutes les unités ont échoué."
    db.execute("UPDATE jobs SET result=?, status=?, error=? WHERE id=?",
               (json.dumps(result, ensure_ascii=False), status, error, job_id))


# ------------------------------------------------------------ commandes usager
def cancel_job(job_id):
    with db.transaction() as c:
        c.execute("UPDATE units SET status=? WHERE job_id=? AND status=?",
                  (CANCELLED, job_id, QUEUED))
        c.execute("UPDATE jobs SET status=?, finished=COALESCE(finished, ?) "
                  "WHERE id=? AND status IN (?, ?, ?)",
                  (CANCELLED, db.now(), job_id, QUEUED, RUNNING, PAUSED))


def pause_job(job_id):
    db.execute("UPDATE jobs SET status=? WHERE id=? AND status IN (?, ?)",
               (PAUSED, job_id, QUEUED, RUNNING))


def resume_job(job_id):
    db.execute("UPDATE jobs SET status = CASE WHEN started IS NULL THEN ? ELSE ? END "
               "WHERE id=? AND status=?", (QUEUED, RUNNING, job_id, PAUSED))


def set_priority(job_id, priority: int):
    db.execute("UPDATE jobs SET priority=? WHERE id=?", (int(priority), job_id))


def retry_failed(job_id):
    with db.transaction() as c:
        n = c.execute("UPDATE units SET status=?, attempts=0, error=NULL, node=NULL "
                      "WHERE job_id=? AND status=?", (QUEUED, job_id, FAILED)).rowcount
        if n:
            c.execute("UPDATE jobs SET status=?, failed=failed-?, error=NULL, finished=NULL "
                      "WHERE id=?", (RUNNING, n, job_id))
    return n


def delete_job(job_id):
    db.execute("DELETE FROM jobs WHERE id=? AND status NOT IN (?, ?)",
               (job_id, QUEUED, RUNNING))


def clear_finished() -> int:
    return db.execute("DELETE FROM jobs WHERE status IN (?, ?, ?)",
                      (DONE, FAILED, CANCELLED)).rowcount


def recover():
    """Au démarrage : les unités interrompues par un arrêt repartent en file."""
    db.execute("UPDATE units SET status=?, node=NULL WHERE status=?", (QUEUED, RUNNING))


# ------------------------------------------------------------------- lecture
def _summary(row) -> dict:
    job = dict(row)
    job["params"] = json.loads(job["params"])
    job["result"] = json.loads(job["result"] or "{}")
    return job


def job_summary(job_id) -> dict | None:
    row = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    return _summary(row) if row else None


def list_jobs(limit=200) -> list:
    return [_summary(r) for r in db.query(
        "SELECT * FROM jobs ORDER BY CASE status WHEN 'running' THEN 0 "
        "WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, "
        "CASE WHEN status IN ('running','queued','paused') THEN priority ELSE 0 END, "
        "COALESCE(finished, created) DESC LIMIT ?", (limit,))]


def failed_units(job_id, limit=50) -> list:
    return [{"id": r["id"], "source": json.loads(r["payload"]).get("source")
             or json.loads(r["payload"]).get("url"), "error": r["error"]}
            for r in db.query("SELECT id, payload, error FROM units WHERE job_id=? AND "
                              "status=? LIMIT ?", (job_id, FAILED, limit))]


def counts() -> dict:
    return {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
