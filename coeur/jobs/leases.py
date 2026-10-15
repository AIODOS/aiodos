"""Baux : unités confiées pour un temps limité à une autre machine (phase 1).

Modèle « pull », comme BOINC : c'est la machine qui a de la puissance libre qui
demande du travail. On lui confie une unité pour LEASE_S secondes ; elle
renouvelle le bail tant qu'elle calcule. Sans nouvelles à l'échéance, l'unité
compte un essai (une unité qui ferait planter chaque machine finit en échec)
et revient dans la file. Elle garde le nom de la dernière machine : si le
résultat arrive en retard mais avant que l'unité soit redonnée, il est accepté.

Le propriétaire garde la main : lui seul intègre les résultats dans ses données.
"""
import json

from coeur import db
from coeur.jobs import queue
from coeur.jobs.models import FAILED, MAX_ATTEMPTS, QUEUED, RUNNING

LEASE_S = 90          # durée d'un bail ; la machine qui calcule le renouvelle toutes les ~30 s
SCAN = 30             # unités examinées par demande (les premières de la file)
EXPIRED = "La machine qui calculait cette unité ne donne plus de nouvelles."


def _unit(row) -> dict:
    return dict(row, payload=json.loads(row["payload"]))


def claim_remote(resources, node, acceptable, lease_s=LEASE_S) -> dict | None:
    """Confie à la machine `node` la première unité de la file qu'elle sait
    traiter : acceptable(unité) -> bool (modèles, bibliothèque, taille…)."""
    resources = [r for r in resources or [] if isinstance(r, str)]
    if not resources:
        return None
    placeholders = ",".join("?" * len(resources))
    rows = db.query(
        f"""SELECT u.id, u.job_id, u.task, u.resource, u.payload, u.attempts
            FROM units u JOIN jobs j ON j.id = u.job_id
            WHERE u.status = 'queued' AND u.local_only = 0
              AND j.status IN ('queued', 'running') AND u.resource IN ({placeholders})
            ORDER BY j.priority, j.created, j.id, u.id LIMIT ?""", (*resources, SCAN))
    for row in rows:
        unit = _unit(row)
        if not acceptable(unit):
            continue
        now = db.now()
        with db.transaction() as c:
            claimed = c.execute(
                "UPDATE units SET status=?, started=?, node=?, lease_until=?, "
                "attempts=attempts+1 WHERE id=? AND status=?",
                (RUNNING, now, node, now + lease_s, unit["id"], QUEUED)).rowcount
            if not claimed:
                continue                  # prise entre-temps par l'exécuteur local
            c.execute("UPDATE jobs SET status=?, started=COALESCE(started, ?) "
                      "WHERE id=? AND status=?", (RUNNING, now, unit["job_id"], QUEUED))
        unit["attempts"] += 1
        return unit
    return None


def held(unit_id, node) -> dict | None:
    """L'unité, si elle est bien confiée en ce moment à cette machine."""
    row = db.query_one("SELECT * FROM units WHERE id=? AND status=? AND node=? "
                       "AND lease_until IS NOT NULL", (unit_id, RUNNING, node))
    return _unit(row) if row else None


def renew(unit_id, node, lease_s=LEASE_S) -> bool:
    """Prolonge le bail. False : l'unité a été reprise ou annulée, inutile de continuer."""
    return bool(db.execute(
        "UPDATE units SET lease_until=? WHERE id=? AND status=? AND node=? "
        "AND lease_until IS NOT NULL", (db.now() + lease_s, unit_id, RUNNING, node)).rowcount)


def take_back(unit_id, node) -> dict | None:
    """Un résultat arrive de `node` : on reprend l'unité pour l'intégrer, si elle
    lui est toujours confiée, ou si elle est revenue en file après un bail échu
    sans avoir été redonnée. None : résultat à refuser (unité reprise, annulée…)."""
    with db.transaction() as c:
        taken = c.execute(
            "UPDATE units SET status=?, lease_until=NULL WHERE id=? AND node=? "
            "AND ((status=? AND lease_until IS NOT NULL) OR status=?)",
            (RUNNING, unit_id, node, RUNNING, QUEUED)).rowcount
        if not taken:
            return None
        return _unit(c.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone())


def release(unit_id, node) -> int | None:
    """La machine rend l'unité sans l'avoir faite (son utilisateur est revenu,
    son moteur est indisponible…) : retour en file sans compter d'essai.
    → id du travail, ou None si l'unité ne lui était plus confiée."""
    with db.transaction() as c:
        row = c.execute("SELECT job_id FROM units WHERE id=? AND status=? AND node=? "
                        "AND lease_until IS NOT NULL", (unit_id, RUNNING, node)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE units SET status=?, node=NULL, lease_until=NULL, "
                  "attempts=MAX(attempts - 1, 0) WHERE id=?", (QUEUED, unit_id))
        return row["job_id"]


def fail(unit_id, node, error, compute_s=0.0):
    """La machine signale un échec du calcul. → (travail, terminé ?) ou None."""
    with db.transaction():
        if held(unit_id, node) is None:
            return None
        return queue.fail_unit(unit_id, error, compute_s)


def expire(now=None) -> list:
    """Baux échus. → [(travail, terminé ?)] pour prévenir l'interface."""
    now = now or db.now()
    touched = []
    for r in db.query("SELECT id, job_id, attempts FROM units WHERE status=? "
                      "AND lease_until IS NOT NULL AND lease_until < ?", (RUNNING, now)):
        with db.transaction() as c:
            if r["attempts"] < MAX_ATTEMPTS:
                n = c.execute("UPDATE units SET status=?, lease_until=NULL, error=? "
                              "WHERE id=? AND status=? AND lease_until < ?",
                              (QUEUED, EXPIRED, r["id"], RUNNING, now)).rowcount
                finished = False
            else:
                n = c.execute("UPDATE units SET status=?, lease_until=NULL, error=?, finished=? "
                              "WHERE id=? AND status=? AND lease_until < ?",
                              (FAILED, EXPIRED, now, r["id"], RUNNING, now)).rowcount
                if n:
                    c.execute("UPDATE jobs SET failed = failed + 1 WHERE id=?", (r["job_id"],))
                finished = bool(n) and queue._complete_if_last(c, r["job_id"])
        if n:
            touched.append((r["job_id"], finished))
    return touched


def local_demand(resources) -> set:
    """Ressources pour lesquelles ce nœud a lui-même du travail en attente :
    on ne les prête pas, son propre travail passe d'abord."""
    resources = list(resources)
    if not resources:
        return set()
    placeholders = ",".join("?" * len(resources))
    return {r["resource"] for r in db.query(
        f"""SELECT DISTINCT u.resource FROM units u JOIN jobs j ON j.id = u.job_id
            WHERE u.status = 'queued' AND j.status IN ('queued', 'running')
              AND u.resource IN ({placeholders})""", tuple(resources))}
