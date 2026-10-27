"""File de tâches persistante (SQLite) : création, attribution, suivi.

Ordre de passage : priorité du travail, puis l'appli qui a le moins d'unités en cours (une
appli qui dépose 500 images ne bloque pas les autres, ni les travaux d'AIODOS), puis ordre
d'arrivée (FIFO). Une unité peut aussi être confiée à une autre machine pour un temps limité
(bail, cf. leases.py) ; le temps de calcul de chaque unité est mesuré (compute_s), c'est la
base de la réciprocité entre machines.

La file se comporte comme un torrent (docs/conception/partage-puissance.md § 2.5) : elle
survit à la fermeture d'AIODOS et au redémarrage du PC ; un travail, ou toute la file, se
met en pause « après l'unité en cours » (rien ne se perd) ou « tout de suite » (le calcul en
cours s'arrête, l'unité est rendue sans compter d'essai) ; l'annulation atteint aussi les
autres machines (leases.renew). Seul un vrai échec du calcul compte un essai : un arrêt
d'AIODOS (interrupts), une machine qui disparaît (lost, leases.expire) ou une unité que
personne ne finit (handouts) ont leurs propres compteurs (models.py). En fin de partie, une
unité en calcul peut avoir un doublon (endgame.py) : la fin sans résultat d'un des deux calculs
ne la remet pas en file tant que l'autre tourne, et seul le premier résultat entre.
"""
import json

from aiodos import db, i18n, settings
from aiodos.jobs import endgame, registry
from aiodos.jobs.models import (CANCELLED, DONE, FAILED, MAX_ATTEMPTS, MAX_HANDOUTS,
                                MAX_INTERRUPTS, NORMAL, PAUSE_AFTER, PAUSE_MODES, PAUSE_NOW,
                                PAUSED, QUEUED, RECOVER_GRACE, RUNNING, SKIPPED)


def create_job(kind, params, priority=NORMAL, owner_node="local", *, owner_app=None,
               label=None, reach=None, public_id=None, client_ref=None) -> int:
    """owner_app : l'appli qui dépose le travail (gateway/), None pour AIODOS ; reach : les
    cercles où ses unités peuvent se calculer (« here,mine »…), None : les règles d'avant ;
    public_id : le numéro que voit l'appli (job_…) ; client_ref : sa référence, unique par
    appli (le même client_ref rend le même travail)."""
    spec = registry.JOB_KINDS[kind]
    title, units, info = spec.split(params)
    if isinstance(title, list):        # i18n.lazy : traduit dans la langue de qui le lit
        info = dict(info, _title=title)
        title = i18n.render(title)
    now = db.now()
    status = QUEUED if units else DONE
    with db.transaction() as c:
        job_id = c.execute(
            "INSERT INTO jobs(kind, title, priority, status, params, owner_node, total, "
            "skipped, result, created, finished, owner_app, label, reach, public_id, "
            "client_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, title, priority, status, json.dumps(params, ensure_ascii=False),
             owner_node, len(units), int(info.get("skipped", 0)),
             json.dumps(info, ensure_ascii=False), now, None if units else now, owner_app,
             label, reach, public_id, client_ref)).lastrowid
        c.executemany(
            "INSERT INTO units(job_id, task, resource, payload, status, created, local_only, "
            "reach, queued_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(job_id, u.task, u.resource, json.dumps(u.payload, ensure_ascii=False),
              QUEUED, now, 0 if _portable(u.task) else 1, reach, now) for u in units])
    return job_id


def _portable(task) -> bool:
    spec = registry.TASKS.get(task)
    return bool(spec and spec.portable)


def not_here(resources) -> tuple:
    """(condition SQL, paramètres) qui écarte les unités que cette machine ne sait pas
    calculer (TaskSpec.here : modèle d'images absent…) : elles restent en file pour une de
    mes machines, sans retenir les autres. Jugé une fois par tâche et par modèle."""
    tasks = registry.conditional_tasks()
    resources = list(resources or [])
    if not tasks or not resources:
        return "", ()
    rows = db.query(
        f"""SELECT DISTINCT u.task, json_extract(u.payload, '$.model') AS model
            FROM units u JOIN jobs j ON j.id = u.job_id
            WHERE u.status = 'queued' AND j.status IN ('queued', 'running')
              AND u.resource IN ({",".join("?" * len(resources))})
              AND u.task IN ({",".join("?" * len(tasks))})""", (*resources, *tasks))
    blocked = [f"{r['task']}|{r['model'] or ''}" for r in rows
               if not registry.runnable_here(r["task"], r["model"])]
    if not blocked:
        return "", ()
    return (" AND u.task || '|' || COALESCE(json_extract(u.payload, '$.model'), '') "
            f"NOT IN ({','.join('?' * len(blocked))})", tuple(blocked))


def in_circle(circle: str) -> tuple:
    """(condition SQL, paramètres) : seulement les unités qui peuvent se calculer dans ce
    cercle (units.reach ; NULL : les règles d'avant, sans restriction de plus)."""
    return (" AND (u.reach IS NULL OR instr(',' || u.reach || ',', ?) > 0)",
            (f",{circle},",))


def fair_order() -> tuple:
    """(terme de l'ORDER BY suivi d'une virgule, paramètres) qui classe, à priorité égale,
    l'appli qui a le moins d'unités en cours d'abord (« aiodos » pour les travaux d'AIODOS
    lui-même) ; vide quand rien n'est en cours (un entier seul y désignerait une colonne)."""
    running = db.query("SELECT COALESCE(j.owner_app, '') AS app, COUNT(*) AS n "
                       "FROM units u JOIN jobs j ON j.id = u.job_id "
                       "WHERE u.status = 'running' GROUP BY COALESCE(j.owner_app, '')")
    if not running:
        return "", ()
    cases = " ".join("WHEN ? THEN ?" for _ in running)
    params = tuple(v for r in running for v in (r["app"], r["n"]))
    return f"CASE COALESCE(j.owner_app, '') {cases} ELSE 0 END, ", params


# ------------------------------------------------------------- pause de la file
def queue_pause() -> str:
    """Toute la file en pause : « », PAUSE_AFTER ou PAUSE_NOW."""
    value = settings.get("queue_pause") or ""
    return value if value in PAUSE_MODES else ""


def pause_all(when: str = PAUSE_AFTER):
    settings.set_value("queue_pause", when if when in PAUSE_MODES else PAUSE_AFTER)


def resume_all():
    settings.set_value("queue_pause", "")


def claim_next(resources, node) -> dict | None:
    """Réserve la prochaine unité exécutable ici sur l'une de ces ressources (cf. not_here,
    in_circle) ; rien quand toute la file est en pause."""
    if not resources or queue_pause():
        return None
    placeholders = ",".join("?" * len(resources))
    skip, skipped = not_here(resources)
    circle, circled = in_circle("here")
    fair, fairness = fair_order()
    row = db.query_one(
        f"""SELECT u.id, u.job_id, u.task, u.resource, u.payload, u.attempts
            FROM units u JOIN jobs j ON j.id = u.job_id
            WHERE u.status = 'queued' AND j.status IN ('queued', 'running')
              AND u.resource IN ({placeholders}){skip}{circle}
            ORDER BY j.priority, {fair}j.created, j.id, u.id LIMIT 1""",
        (*resources, *skipped, *circled, *fairness))
    if row is None:
        return None
    now = db.now()
    with db.transaction() as c:
        claimed = c.execute(
            "UPDATE units SET status=?, started=?, node=?, attempts=attempts+1, "
            "handouts=handouts+1, place='here', progress=NULL WHERE id=? AND status=?",
            (RUNNING, now, node, row["id"], QUEUED)).rowcount
        if not claimed:
            return None
        started = c.execute("UPDATE jobs SET status=?, started=COALESCE(started, ?) "
                            "WHERE id=? AND status=?", (RUNNING, now, row["job_id"], QUEUED)).rowcount
    unit = dict(row)
    unit["payload"] = json.loads(unit["payload"])
    unit["attempts"] += 1
    unit["job_started"] = bool(started)       # le travail passe de « en file » à « en cours »
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
        c.execute("UPDATE units SET status=?, result=?, finished=?, compute_s=?, "
                  "lease_until=NULL, progress=NULL WHERE id=?",
                  (status, json.dumps(result, ensure_ascii=False), db.now(),
                   compute_s, unit_id))
        c.execute(f"UPDATE jobs SET {counter} = {counter} + 1 WHERE id=?", (job_id,))
        endgame.forget(c, unit_id)        # fin de partie : l'autre calcul s'arrête
        return job_id, _complete_if_last(c, job_id)


def _fail_locked(c, unit_id, job_id, error, compute_s) -> bool:
    c.execute("UPDATE units SET status=?, error=?, finished=?, compute_s=?, "
              "lease_until=NULL, progress=NULL, taken=NULL WHERE id=?",
              (FAILED, error, db.now(), compute_s, unit_id))
    c.execute("UPDATE jobs SET failed = failed + 1 WHERE id=?", (job_id,))
    endgame.forget(c, unit_id)
    return _complete_if_last(c, job_id)


def fail_unit(unit_id, error: str, compute_s: float, node: str | None = None):
    """Unité en échec : nouvel essai si possible. → (job_id, travail terminé ?) Fin de partie
    (endgame.py) : si un doublon tourne encore, il prend la suite sans rien compter. node :
    la machine dont le calcul a échoué (l'exécuteur d'ici) ; si ce n'est pas elle qui a le
    calcul principal, seul son doublon s'arrête."""
    with db.transaction() as c:
        row = c.execute("SELECT job_id, attempts, handouts, node FROM units WHERE id=?",
                        (unit_id,)).fetchone()
        job_id = row["job_id"]
        if node is not None and row["node"] != node:
            endgame.drop(unit_id, node)
            return job_id, False
        if endgame.promote(c, unit_id, db.now(), error):
            return job_id, False
        if row["attempts"] < MAX_ATTEMPTS and row["handouts"] < MAX_HANDOUTS:
            c.execute("UPDATE units SET status=?, error=?, node=NULL, lease_until=NULL, "
                      "queued_at=?, place=NULL, progress=NULL, taken=NULL WHERE id=?",
                      (QUEUED, error, db.now(), unit_id))
            return job_id, False
        return job_id, _fail_locked(c, unit_id, job_id, error, compute_s)


def expire_waiting(now: float | None = None) -> list:
    """Les unités qu'aucune machine n'a prises avant l'échéance de leur travail
    (jobs.wait_until : bounds.wait_s d'un travail d'appli) échouent, no_machine ; celles déjà
    remises une fois suivent leur cours. → [(travail, terminé ?)]."""
    now = db.now() if now is None else now
    rows = db.query(
        "SELECT u.id, u.job_id FROM units u JOIN jobs j ON j.id = u.job_id WHERE u.status=? "
        "AND u.handouts=0 AND j.wait_until IS NOT NULL AND j.wait_until < ? AND j.status IN "
        "(?, ?)", (QUEUED, now, QUEUED, RUNNING))
    out = []
    # Le code d'abord, suivi d'une espace : gateway/jobs_api._unit_error le relit
    error = "no_machine : " + i18n.t("jobs.no_machine")
    for row in rows:
        with db.transaction() as c:
            out.append((row["job_id"], _fail_locked(c, row["id"], row["job_id"], error, 0.0)))
    return out


def release_unit(unit_id, interrupt: bool = False, node: str | None = None):
    """Remet en file une unité en cours sans compter d'essai : ce n'est pas elle qui a
    échoué (moteur injoignable, pause « tout de suite », machine qui la rend…).
    interrupt : AIODOS s'arrête pendant son calcul ici (interrupts + 1 ; au-delà de
    MAX_INTERRUPTS, l'essai compte : une unité qui fait planter AIODOS finit en échec). node :
    seulement si elle est confiée à cette machine ; si cette machine n'en calcule qu'un
    doublon (fin de partie, endgame.py), seul le doublon s'arrête. D'un travail annulé, elle
    est annulée ; si un doublon tourne encore, il prend la suite sans rien compter ; remise
    trop de fois, elle échoue. → (travail, terminé ?) ou None si elle n'était pas en cours."""
    with db.transaction() as c:
        row = c.execute(
            "SELECT u.job_id, u.interrupts, u.handouts, u.node, j.status AS job_status "
            "FROM units u JOIN jobs j ON j.id = u.job_id WHERE u.id=? AND u.status=?",
            (unit_id, RUNNING)).fetchone()
        if row is None or (node is not None and row["node"] != node):
            job_id = endgame.drop(unit_id, node) if node is not None else None
            return None if job_id is None else (job_id, False)
        job_id, now = row["job_id"], db.now()
        if row["job_status"] == CANCELLED:
            c.execute("UPDATE units SET status=?, node=NULL, lease_until=NULL, finished=?, "
                      "progress=NULL, taken=NULL, attempts=MAX(attempts - 1, 0) WHERE id=?",
                      (CANCELLED, now, unit_id))
            endgame.forget(c, unit_id)
            return job_id, False
        if endgame.promote(c, unit_id, now):
            return job_id, False
        if row["handouts"] >= MAX_HANDOUTS:
            return job_id, _fail_locked(c, unit_id, job_id, i18n.t("jobs.no_progress"), 0.0)
        interrupts = row["interrupts"] + (1 if interrupt else 0)
        given_back = 0 if interrupt and interrupts > MAX_INTERRUPTS else 1
        c.execute("UPDATE units SET status=?, node=NULL, lease_until=NULL, queued_at=?, "
                  "place=NULL, progress=NULL, taken=NULL, interrupts=?, "
                  "attempts=MAX(attempts - ?, 0) WHERE id=?",
                  (QUEUED, now, interrupts, given_back, unit_id))
        return job_id, False


def interrupt_units(unit_ids, node: str | None = None) -> list:
    """AIODOS s'arrête : les unités en calcul ici sont rendues sans compter d'essai
    (interrupts + 1). node : ce nœud (l'exécuteur), dont un calcul n'est peut-être qu'un
    doublon (release_unit). → [(travail, terminé ?)]"""
    settled = []
    for unit_id in unit_ids:
        outcome = release_unit(unit_id, interrupt=True, node=node)
        if outcome:
            settled.append(outcome)
    return settled


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
        status, error = FAILED, i18n.t("jobs.all_units_failed")
    db.execute("UPDATE jobs SET result=?, status=?, error=? WHERE id=?",
               (json.dumps(result, ensure_ascii=False), status, error, job_id))


# ------------------------------------------------------------ commandes usager
def cancel_job(job_id, by: str = "aiodos"):
    """Annule : les unités en file ; celles confiées ailleurs (leur machine l'apprend à son
    prochain renouvellement et arrête, leur résultat est refusé) ; celles en calcul ici, leur
    arrêt est levé par l'exécuteur (Executor._halt_withdrawn). Les résultats déjà rendus
    restent. by : qui a annulé (aiodos, app, revoked)."""
    now = db.now()
    with db.transaction() as c:
        c.execute("UPDATE units SET status=?, finished=? WHERE job_id=? AND status=?",
                  (CANCELLED, now, job_id, QUEUED))
        c.execute("UPDATE units SET status=?, lease_until=NULL, finished=?, progress=NULL "
                  "WHERE job_id=? AND status=? AND lease_until IS NOT NULL",
                  (CANCELLED, now, job_id, RUNNING))
        c.execute("UPDATE jobs SET status=?, cancelled_by=COALESCE(cancelled_by, ?), "
                  "pause_mode=NULL, finished=COALESCE(finished, ?) "
                  "WHERE id=? AND status IN (?, ?, ?)",
                  (CANCELLED, by, now, job_id, QUEUED, RUNNING, PAUSED))


def pause_job(job_id, when: str = PAUSE_AFTER):
    """Pause « après l'unité en cours » (rien ne se perd) ou « tout de suite » (le calcul
    en cours s'arrête, ici et ailleurs, l'unité est rendue sans compter d'essai). Une pause
    « après » peut devenir « tout de suite »."""
    when = when if when in PAUSE_MODES else PAUSE_AFTER
    db.execute("UPDATE jobs SET status=?, pause_mode=? WHERE id=? AND status IN (?, ?, ?)",
               (PAUSED, when, job_id, QUEUED, RUNNING, PAUSED))


def resume_job(job_id):
    """Reprise : le travail reprend son rang (sa date de création ne change pas)."""
    db.execute("UPDATE jobs SET status = CASE WHEN started IS NULL THEN ? ELSE ? END, "
               "pause_mode=NULL WHERE id=? AND status=?", (QUEUED, RUNNING, job_id, PAUSED))


def halted_now(job_ids) -> set:
    """Parmi ces travaux, ceux dont le calcul en cours doit s'arrêter : annulés, supprimés,
    en pause « tout de suite » (eux, ou toute la file)."""
    ids = sorted(job_ids)
    if not ids:
        return set()
    if queue_pause() == PAUSE_NOW:
        return set(ids)
    rows = db.query(f"SELECT id, status, pause_mode FROM jobs WHERE id IN "
                    f"({','.join('?' * len(ids))})", tuple(ids))
    alive = {r["id"] for r in rows if r["status"] != CANCELLED
             and not (r["status"] == PAUSED and r["pause_mode"] == PAUSE_NOW)}
    return set(ids) - alive


def set_priority(job_id, priority: int):
    db.execute("UPDATE jobs SET priority=? WHERE id=?", (int(priority), job_id))


def retry_failed(job_id):
    with db.transaction() as c:
        n = c.execute("UPDATE units SET status=?, attempts=0, handouts=0, lost=0, "
                      "interrupts=0, error=NULL, node=NULL, queued_at=? "
                      "WHERE job_id=? AND status=?", (QUEUED, db.now(), job_id, FAILED)).rowcount
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
    """Au démarrage, après un arrêt brutal (l'arrêt propre a déjà rendu ses unités,
    interrupt_units) :
    - les unités qui calculaient ici repartent en file, sans compter d'essai (interrupts + 1) ;
    - celles confiées à une autre machine gardent leur machine, avec un bail de grâce
      (RECOVER_GRACE) : elle se raccroche à son prochain renouvellement, ou livre son
      résultat ; sinon l'unité revient en file à l'échéance (leases.expire) ;
    - les doublons (fin de partie) : ceux d'ici sont oubliés, ceux d'ailleurs gardent de même
      leur machine, et prennent la place d'un calcul principal d'ici (endgame.recover)."""
    now = db.now()
    endgame.recover(now)
    local = db.query("SELECT id FROM units WHERE status=? AND lease_until IS NULL", (RUNNING,))
    interrupt_units([r["id"] for r in local])
    db.execute("UPDATE units SET lease_until=? WHERE status=? AND lease_until IS NOT NULL",
               (now + RECOVER_GRACE, RUNNING))


# ------------------------------------------------------------------- lecture
def _summary(row, remote, local, names=None) -> dict:
    job = dict(row)
    job["params"] = json.loads(job["params"])
    job["result"] = json.loads(job["result"] or "{}")
    job["remote"] = remote.get(job["id"], {})       # {machine: unités qu'elle calcule}
    job["local"] = local.get(job["id"], 0)          # unités qui calculent ici
    # Le titre, écrit à la création dans la langue d'alors (title), et sa clé (title_t) :
    # l'interface le traduit dans la sienne (jobs.js, jobTitle)
    lazy_title = job["result"].pop("_title", None)
    if lazy_title:
        job["title_t"] = lazy_title
        job["title"] = i18n.render(lazy_title) or job["title"]
    if job.get("owner_app"):                        # l'appli qui l'a déposé (vue Travaux)
        names = _app_names([job["owner_app"]]) if names is None else names
        job["app_name"] = names.get(job["owner_app"])
    return job


def _app_names(ids) -> dict:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    return {r["id"]: r["name"] for r in db.query(
        f"SELECT id, name FROM gateway_apps WHERE id IN ({','.join('?' * len(ids))})",
        tuple(ids))}


def remote_counts(job_id=None) -> dict:
    """Unités confiées en ce moment à d'autres machines : {travail: {machine: n}}."""
    out = {}
    for r in db.query("SELECT job_id, node, COUNT(*) AS n FROM units WHERE status=? "
                      "AND lease_until IS NOT NULL AND (? IS NULL OR job_id=?) "
                      "GROUP BY job_id, node", (RUNNING, job_id, job_id)):
        out.setdefault(r["job_id"], {})[r["node"]] = r["n"]
    return out


def local_counts(job_id=None) -> dict:
    """Unités en calcul sur ce PC en ce moment, sans bail (pas confiées à une autre
    machine) : {travail: n}. La vue Tâches dit ainsi ce que calcule chaque machine."""
    return {r["job_id"]: r["n"] for r in db.query(
        "SELECT job_id, COUNT(*) AS n FROM units WHERE status=? AND lease_until IS NULL "
        "AND (? IS NULL OR job_id=?) GROUP BY job_id", (RUNNING, job_id, job_id))}


def job_summary(job_id) -> dict | None:
    row = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    return _summary(row, remote_counts(job_id), local_counts(job_id)) if row else None


def list_jobs(limit=200) -> list:
    remote, local = remote_counts(), local_counts()
    rows = db.query(
        "SELECT * FROM jobs ORDER BY CASE status WHEN 'running' THEN 0 "
        "WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, "
        "CASE WHEN status IN ('running','queued','paused') THEN priority ELSE 0 END, "
        "COALESCE(finished, created) DESC LIMIT ?", (limit,))
    names = _app_names(r["owner_app"] for r in rows)
    return [_summary(r, remote, local, names) for r in rows]


def failed_units(job_id, limit=50) -> list:
    return [{"id": r["id"], "source": json.loads(r["payload"]).get("source")
             or json.loads(r["payload"]).get("url"), "error": r["error"]}
            for r in db.query("SELECT id, payload, error FROM units WHERE job_id=? AND "
                              "status=? LIMIT ?", (job_id, FAILED, limit))]


def counts() -> dict:
    return {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


def set_progress(unit_id, progress: dict):
    """L'étape d'une unité en calcul (units.progress : {stage, at…}), pour l'avancement
    que voit l'appli (sending_inputs, waiting_engine, running, receiving, checking)."""
    db.execute("UPDATE units SET progress=? WHERE id=? AND status=?",
               (json.dumps(progress, ensure_ascii=False), unit_id, RUNNING))


def running_here() -> int:
    """Unités en calcul sur ce PC (la question à la fermeture d'AIODOS)."""
    return db.query_one("SELECT COUNT(*) AS n FROM units WHERE status=? AND "
                        "lease_until IS NULL", (RUNNING,))["n"]
