"""Baux : unités confiées pour un temps limité à une autre machine (phase 1).

Modèle « pull », comme BOINC : c'est la machine qui a de la puissance libre qui
demande du travail. On lui confie une unité pour LEASE_S secondes ; elle
renouvelle le bail tant qu'elle calcule. Le propriétaire garde la main : lui seul
intègre les résultats dans ses données.

La file comme un torrent (docs/conception/partage-puissance.md § 2.5) :
- le renouvellement dit à la machine où en est l'unité (renew) : « running » (elle
  continue), « resumed » (son bail avait échu sans que l'unité soit redonnée : elle se
  raccroche et continue), « paused » (pause « tout de suite » : elle arrête et rend l'unité
  sans essai), « cancelled » (travail annulé : son résultat sera refusé), « reassigned »
  (l'unité est partie ailleurs) ;
- sans nouvelles à l'échéance (expire), l'unité revient en file en gardant sa machine.
  Chez une de mes machines, sans compter d'essai mais une disparition (lost) : à
  LOST_MINE_ONLY, elle ne part plus que chez mes machines ; à MAX_LOST, elle échoue ; tant
  qu'elle n'est pas redonnée, son renouvellement la reprend. Chez un ami, l'essai compte et
  elle ne lui revient pas (règles de l'audit du 20/10). Tant qu'elle n'est pas redonnée, le
  résultat de sa machine est accepté ;
- un travail annulé refuse les résultats en route (take_back).
"""
import json

from aiodos import db, i18n
from aiodos.jobs import queue
from aiodos.jobs.models import (CANCELLED, FAILED, LOST_MINE_ONLY, MAX_ATTEMPTS, MAX_LOST,
                                PAUSE_NOW, PAUSED, QUEUED, RUNNING)

LEASE_S = 90          # durée d'un bail ; la machine qui calcule le renouvelle toutes les ~30 s
SCAN = 200            # unités examinées à la fois (les premières de la file)
SCAN_MAX = 2000       # au plus, par demande : au-delà, la machine redemandera
STATES_GOING = ("running", "resumed")       # la machine continue son calcul
FOREIGN = ("friend", "public")             # unités calculées hors de mes machines


def _unit(row) -> dict:
    return dict(row, payload=json.loads(row["payload"]))


def claim_remote(resources, node, acceptable, lease_s=LEASE_S, circle=None) -> dict | None:
    """Confie à la machine `node` la première unité de la file qu'elle sait
    traiter : acceptable(unité) -> bool (modèles, bibliothèque, taille…). circle : son
    cercle (« mine », « friends ») ; une unité ne part que dans les cercles qui lui sont
    permis (units.reach). Rien quand toute la file est en pause."""
    resources = [r for r in resources or [] if isinstance(r, str)]
    if not resources or queue.queue_pause():
        return None
    placeholders = ",".join("?" * len(resources))
    where, circled = queue.in_circle(circle) if circle else ("", ())
    fair, fairness = queue.fair_order()
    # Page par page : des unités que cette machine ne sait pas calculer (images sans le
    # modèle…) ne cachent pas celles qui suivent
    for offset in range(0, SCAN_MAX, SCAN):
        rows = db.query(
            f"""SELECT u.id, u.job_id, u.task, u.resource, u.payload, u.attempts
                FROM units u JOIN jobs j ON j.id = u.job_id
                WHERE u.status = 'queued' AND u.local_only = 0
                  AND j.status IN ('queued', 'running') AND u.resource IN ({placeholders}){where}
                ORDER BY j.priority, {fair}j.created, j.id, u.id LIMIT ? OFFSET ?""",
            (*resources, *circled, *fairness, SCAN, offset))
        unit = _first(rows, node, acceptable, lease_s, circle)
        if unit is not None or len(rows) < SCAN:
            return unit
    return None


def _first(rows, node, acceptable, lease_s, circle) -> dict | None:
    place = {"friends": "friend"}.get(circle, circle or "mine")
    for row in rows:
        unit = _unit(row)
        if not acceptable(unit):
            continue
        now = db.now()
        with db.transaction() as c:
            claimed = c.execute(
                "UPDATE units SET status=?, started=?, node=?, lease_until=?, "
                "attempts=attempts+1, handouts=handouts+1, place=?, progress=NULL "
                "WHERE id=? AND status=?",
                (RUNNING, now, node, now + lease_s, place, unit["id"], QUEUED)).rowcount
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


def renew(unit_id, node, lease_s=LEASE_S) -> str:
    """Le bail prolongé, et ce que la machine doit faire (cf. le début du module) : running,
    resumed, paused, cancelled ou reassigned."""
    row = db.query_one("SELECT u.status, u.node, u.lease_until, u.place, j.status AS job_status, "
                       "j.pause_mode FROM units u JOIN jobs j ON j.id = u.job_id WHERE u.id=?",
                       (unit_id,))
    if row is None or row["status"] == CANCELLED or row["job_status"] == CANCELLED:
        return "cancelled"
    if row["node"] != node:
        return "reassigned"
    paused_now = (queue.queue_pause() == PAUSE_NOW
                  or row["job_status"] == PAUSED and row["pause_mode"] == PAUSE_NOW)
    now = db.now()
    if row["status"] == RUNNING and row["lease_until"] is not None:
        if paused_now:
            return "paused"
        db.execute("UPDATE units SET lease_until=? WHERE id=? AND status=? AND node=? "
                   "AND lease_until IS NOT NULL", (now + lease_s, unit_id, RUNNING, node))
        return "running"
    if row["status"] == QUEUED:
        # Bail échu sans que l'unité soit redonnée : une de mes machines se raccroche ; un ami
        # non (règles auditées le 20/10 : l'unité ne lui revient pas, son résultat déjà en
        # route reste accepté par take_back)
        if row["place"] in FOREIGN:
            return "reassigned"
        if paused_now:
            return "paused"
        with db.transaction() as c:
            taken = c.execute(
                "UPDATE units SET status=?, lease_until=?, error=NULL, "
                "attempts=attempts+1 WHERE id=? AND status=? AND node=?",
                (RUNNING, now + lease_s, unit_id, QUEUED, node)).rowcount
            if taken:
                c.execute("UPDATE jobs SET status=? WHERE id=(SELECT job_id FROM units "
                          "WHERE id=?) AND status=?", (RUNNING, unit_id, QUEUED))
        return "resumed" if taken else "reassigned"
    return "reassigned"


def take_back(unit_id, node) -> dict | None:
    """Un résultat arrive de `node` : on reprend l'unité pour l'intégrer, si elle
    lui est toujours confiée, ou si elle est revenue en file après un bail échu
    sans avoir été redonnée, et si son travail n'est pas annulé. None : résultat à refuser
    (unité reprise, annulée…)."""
    with db.transaction() as c:
        taken = c.execute(
            "UPDATE units SET status=?, lease_until=NULL WHERE id=? AND node=? "
            "AND ((status=? AND lease_until IS NOT NULL) OR status=?) "
            "AND job_id IN (SELECT id FROM jobs WHERE status != ?)",
            (RUNNING, unit_id, node, RUNNING, QUEUED, CANCELLED)).rowcount
        if not taken:
            return None
        return _unit(c.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone())


def release(unit_id, node) -> int | None:
    """La machine rend l'unité sans l'avoir faite (son utilisateur est revenu, son moteur
    est indisponible, pause « tout de suite »…) : retour en file sans compter d'essai (d'un
    travail annulé : annulée). → id du travail, ou None si l'unité ne lui était plus confiée."""
    if held(unit_id, node) is None:
        return None
    outcome = queue.release_unit(unit_id, node=node)
    return outcome[0] if outcome else None


def fail(unit_id, node, error, compute_s=0.0):
    """La machine signale un échec du calcul. → (travail, terminé ?) ou None."""
    with db.transaction():
        if held(unit_id, node) is None:
            return None
        return queue.fail_unit(unit_id, error, compute_s)


def expire(now=None) -> list:
    """Baux échus : l'unité revient en file en gardant sa machine (cf. le début du module).
    Chez une de mes machines, sans compter d'essai mais une disparition ; chez un ami (ou un
    inconnu), comme avant la file en torrent, l'essai compte (le dernier essai ne part jamais
    chez un ami, et l'ami ne la fait pas échouer : règles de l'audit du 20/10). → [(travail,
    terminé ?)] pour prévenir l'interface."""
    now = now or db.now()
    touched = []
    # Les erreurs gardées pour l'unité, dans la langue du nœud (i18n/*/jobs.json)
    expired, vanished = i18n.t("jobs.lease_expired"), i18n.t("jobs.lease_lost")
    for r in db.query("SELECT id, job_id, lost, reach, place, attempts FROM units WHERE status=? "
                      "AND lease_until IS NOT NULL AND lease_until < ?", (RUNNING, now)):
        lost = r["lost"] + 1
        foreign = r["place"] in FOREIGN
        with db.transaction() as c:
            if foreign and r["attempts"] >= MAX_ATTEMPTS:
                n = c.execute("UPDATE units SET status=?, lease_until=NULL, error=?, finished=?, "
                              "lost=? WHERE id=? AND status=? AND lease_until < ?",
                              (FAILED, expired, now, lost, r["id"], RUNNING, now)).rowcount
                if n:
                    c.execute("UPDATE jobs SET failed = failed + 1 WHERE id=?", (r["job_id"],))
                finished = bool(n) and queue._complete_if_last(c, r["job_id"])
            elif foreign:
                n = c.execute("UPDATE units SET status=?, lease_until=NULL, error=?, lost=?, "
                              "queued_at=? WHERE id=? AND status=? AND lease_until < ?",
                              (QUEUED, expired, lost, now, r["id"], RUNNING, now)).rowcount
                finished = False
            elif lost >= MAX_LOST:
                n = c.execute("UPDATE units SET status=?, lease_until=NULL, error=?, finished=?, "
                              "lost=? WHERE id=? AND status=? AND lease_until < ?",
                              (FAILED, vanished, now, lost, r["id"], RUNNING, now)).rowcount
                if n:
                    c.execute("UPDATE jobs SET failed = failed + 1 WHERE id=?", (r["job_id"],))
                finished = bool(n) and queue._complete_if_last(c, r["job_id"])
            else:
                reach = r["reach"]
                if lost >= LOST_MINE_ONLY:          # plus jamais chez un ami ni un inconnu
                    kept = [x for x in (reach or "here,mine").split(",") if x in ("here", "mine")]
                    reach = ",".join(kept) or "here,mine"
                n = c.execute("UPDATE units SET status=?, lease_until=NULL, error=?, lost=?, "
                              "reach=?, queued_at=?, attempts=MAX(attempts - 1, 0) "
                              "WHERE id=? AND status=? AND lease_until < ?",
                              (QUEUED, expired, lost, reach, now, r["id"], RUNNING, now)).rowcount
                finished = False
        if n:
            touched.append((r["job_id"], finished))
    return touched


def local_demand(resources) -> set:
    """Ressources pour lesquelles ce nœud a lui-même du travail en attente, qu'il sait
    calculer : on ne les prête pas, son propre travail passe d'abord. Des images qu'il
    attend d'une autre machine ne l'empêchent pas de prêter sa carte."""
    resources = list(resources)
    if not resources or queue.queue_pause():      # ma file en pause : je peux prêter
        return set()
    placeholders = ",".join("?" * len(resources))
    skip, skipped = queue.not_here(resources)
    here, circled = queue.in_circle("here")
    return {r["resource"] for r in db.query(
        f"""SELECT DISTINCT u.resource FROM units u JOIN jobs j ON j.id = u.job_id
            WHERE u.status = 'queued' AND j.status IN ('queued', 'running')
              AND u.resource IN ({placeholders}){skip}{here}""",
        (*resources, *skipped, *circled))}
