"""Fin de partie : les dernières unités d'un travail doublées par une machine qui se libère.

Le « mode fin de partie » de BitTorrent (docs/conception/essaim.md § 4, partage-puissance.md
§ 2.7). Quand un travail n'a plus d'unité en file (toutes sont en calcul, ici ou ailleurs) et
qu'une ressource se libère (une machine qui demande du travail, network/work.lease, ou
l'exécuteur d'ici, executor._fill), elle reçoit un doublon de l'unité qui tourne depuis le
plus longtemps, si elle traîne (deux fois la durée moyenne des unités faites du même travail,
ENDGAME_MIN_S au moins, ENDGAME_COLD_S sans référence), si elle n'a pas déjà MAX_RUNS
calculs, et si cette machine n'est pas celle qui la calcule déjà. Les règles de remise sont
celles d'une unité en file, par les mêmes fonctions : cercles (queue.in_circle), tâche
portable, ressources proposées, ce que ce PC sait calculer (registry.runnable_here), et pour
une autre machine le filtre de work.lease (modèles, bibliothèque, contrôle, result_max…).
Un doublon partage l'essai en cours : le dernier essai ne part donc jamais chez un ami.

Le calcul principal reste dans units (node, place, started, lease_until, run_uuid) ; le
doublon a sa ligne dans unit_copies, avec son bail (NULL : calculé ici) et son propre
run_uuid (deux calculs de moteur ne partagent pas un dossier). Le premier résultat valable
gagne :
- le premier arrivé marque l'unité (units.taken) : le principal par leases.take_back, ou par
  win s'il est calculé ici ; un doublon par take_back ou win, en échangeant d'abord sa place
  avec le principal (_swap). Plus aucun autre résultat n'est pris, mais l'autre calcul
  continue, au cas où celui-ci serait refusé ;
- accepté, l'unité est faite (queue.finish_unit) et ses doublons oubliés : la machine qui
  calcule encore l'apprend à son renouvellement (« done ») et rend l'unité sans essai ni
  reproche ; son résultat, s'il arrive, est refusé poliment ; calculé ici, son arrêt est levé
  (beaten, vu par l'exécuteur à chaque tour) ;
- refusé, ou quand le principal échoue, est rendu ou perd son bail : le doublon encore vivant
  prend sa place (promote), l'unité reste en calcul et rien n'est compté. Un doublon qui
  échoue, est rendu ou perd son bail : sa ligne s'en va (drop, expire), l'unité continue.
  Les deux tombés, les règles d'avant s'appliquent (queue.fail_unit, release_unit…).
Un doublon ne compte ni essai (attempts) ni remise (handouts). Seul le calcul gagnant compte
pour la réciprocité (peers.account, au résultat accepté, des deux côtés). Les vues
(queue.job_summary, gateway/jobs_api) ne lisent que units : un doublon n'y est pas une unité
de plus.
"""
import json
import time
import uuid

from aiodos import db
from aiodos.jobs import registry
from aiodos.jobs.models import CANCELLED, DONE, RECOVER_GRACE, RUNNING, SKIPPED

# Quand une unité traîne : deux fois la durée moyenne des unités déjà faites du même travail,
# ENDGAME_MIN_S au moins ; sans unité faite pour en juger, ENDGAME_COLD_S. Un seuil fixe de
# 30 s ferait doubler par un téléphone lent une vidéo de dix minutes confiée à un PC rapide :
# un calcul presque sûr de perdre, et sa batterie pour rien (relecture du 27/10/2026)
ENDGAME_MIN_S = 30
ENDGAME_COLD_S = 120
SLOW_FACTOR = 2
MAX_RUNS = 2            # calculs d'une même unité à la fois : le principal et un doublon
SCAN = 200              # unités en calcul examinées à chaque demande

# Une unité en calcul qu'on peut doubler pour la machine `?` : son travail n'a plus rien en
# file, aucun résultat n'entre encore, elle traîne (voir plus haut), elle n'a pas déjà
# MAX_RUNS calculs, et cette machine n'en calcule ni le principal ni un doublon
_OPEN = """u.status = 'running' AND u.taken IS NULL AND u.local_only = 0
    AND j.status = 'running' AND u.node != ?
    AND u.started <= ? - MAX(?, COALESCE(? * (SELECT AVG(d.compute_s) FROM units d
        WHERE d.job_id = u.job_id AND d.status = 'done' AND d.compute_s > 0), ?))
    AND NOT EXISTS (SELECT 1 FROM units q WHERE q.job_id = u.job_id AND q.status = 'queued')
    AND (SELECT COUNT(*) FROM unit_copies uc WHERE uc.unit_id = u.id) < ?
    AND NOT EXISTS (SELECT 1 FROM unit_copies uc WHERE uc.unit_id = u.id AND uc.node = ?)"""


def _opened(node, now) -> tuple:
    return node, now, ENDGAME_MIN_S, SLOW_FACTOR, ENDGAME_COLD_S, MAX_RUNS - 1, node


# ------------------------------------------------------------------ doubler
def claim_remote(resources, node, acceptable, lease_s, circle=None) -> dict | None:
    """Un doublon pour la machine `node`, qui demande du travail et n'a rien trouvé en file
    (leases.claim_remote) ; acceptable(unité) : le filtre de work.lease. Le doublon partage
    l'essai en cours : il est jugé comme une remise, avec les essais d'avant (attempts − 1),
    si bien que le dernier essai ne part jamais chez un ami. circle : son cercle."""
    from aiodos.jobs import queue           # à la demande : queue importe ce module-ci
    resources = [r for r in resources or [] if isinstance(r, str)]
    if not resources or queue.queue_pause():
        return None
    where, circled = queue.in_circle(circle) if circle else ("", ())
    place = {"friends": "friend"}.get(circle, circle or "mine")      # comme leases._first
    return _claim(resources, node, where, circled, place, lease_s,
                  lambda unit: acceptable(dict(unit, attempts=max(0, unit["attempts"] - 1))))


def claim_local(resources, node) -> dict | None:
    """Un doublon pour l'exécuteur d'ici (node : ce nœud), quand plus rien en file ne lui
    revient (queue.claim_next) : ce que ce PC sait calculer et que son cercle permet."""
    from aiodos.jobs import queue           # à la demande : queue importe ce module-ci
    resources = list(resources or [])
    if not resources or queue.queue_pause():
        return None
    where, circled = queue.in_circle("here")
    return _claim(resources, node, where, circled, "here", None,
                  lambda unit: registry.runnable_here(unit["task"],
                                                      unit["payload"].get("model")))


def _claim(resources, node, where, params, place, lease_s, acceptable) -> dict | None:
    now = time.time()
    opened = _opened(node, now)
    rows = db.query(
        f"""SELECT u.id, u.job_id, u.task, u.resource, u.payload, u.attempts
            FROM units u JOIN jobs j ON j.id = u.job_id
            WHERE {_OPEN} AND u.resource IN ({",".join("?" * len(resources))}){where}
            ORDER BY u.started, u.id LIMIT ?""", (*opened, *resources, *params, SCAN))
    for row in rows:
        unit = dict(row, payload=json.loads(row["payload"]))
        if not acceptable(unit):
            continue
        spec = registry.TASKS.get(unit["task"])
        # Le sien : l'unité le reprend s'il gagne (_swap), le principal garde le sien
        run_uuid = str(uuid.uuid4()) if spec is not None and spec.expand else None
        with db.transaction() as c:
            if c.execute(f"SELECT 1 FROM units u JOIN jobs j ON j.id = u.job_id "
                         f"WHERE u.id = ? AND {_OPEN}", (unit["id"], *opened)).fetchone() is None:
                continue                    # finie, rendue ou doublée entre-temps
            c.execute("INSERT INTO unit_copies(unit_id, node, place, started, lease_until, "
                      "run_uuid) VALUES(?, ?, ?, ?, ?, ?)",
                      (unit["id"], node, place, now, None if lease_s is None else now + lease_s,
                       run_uuid))
        return dict(unit, copy=True, run_uuid=run_uuid, place=place, started=now)
    return None


def payload_for(unit, here: bool = True) -> dict:
    """Le payload d'une unité au moment de la calculer (registry.payload_for). Un doublon ne
    tire pas de run_uuid pour l'unité (celui du principal reste) : il calcule sous le sien.
    Calculé ici, il ne dit pas son étape : celle de l'unité reste celle du principal."""
    if not unit.get("copy"):
        return registry.payload_for(unit, here=here)
    payload = registry.payload_for(unit, here=here, fresh=False)
    if unit.get("run_uuid") and "run_uuid" in payload:
        payload = dict(payload, run_uuid=unit["run_uuid"])
    if isinstance(payload.get("local"), dict):
        payload = dict(payload, local=dict(payload["local"], unit_id=None))
    return payload


# ------------------------------------------------------- le premier gagne
def _row(c, unit_id):
    return c.execute("SELECT u.*, j.status AS job_status FROM units u "
                     "JOIN jobs j ON j.id = u.job_id WHERE u.id=?", (unit_id,)).fetchone()


def _copy(c, unit_id, node):
    return c.execute("SELECT * FROM unit_copies WHERE unit_id=? AND node=?",
                     (unit_id, node)).fetchone()


def _swap(c, unit, copy, now):
    """Le doublon prend la place du principal, l'unité marquée (taken : son résultat entre) ;
    le principal devient le doublon et continue, au cas où ce résultat serait refusé."""
    c.execute("UPDATE units SET node=?, place=?, started=?, lease_until=NULL, "
              "run_uuid=COALESCE(?, run_uuid), taken=? WHERE id=?",
              (copy["node"], copy["place"], copy["started"], copy["run_uuid"], now, unit["id"]))
    c.execute("DELETE FROM unit_copies WHERE unit_id=? AND node=?", (unit["id"], copy["node"]))
    c.execute("INSERT OR REPLACE INTO unit_copies(unit_id, node, place, started, lease_until, "
              "run_uuid) VALUES(?, ?, ?, ?, ?, ?)",
              (unit["id"], unit["node"], unit["place"] or "here", unit["started"] or now,
               unit["lease_until"], unit["run_uuid"]))


def win(unit_id, node) -> bool:
    """Le calcul de `node`, fait ici (l'exécuteur, entre le calcul et l'intégration :
    registry.run_local), a son résultat : est-il encore attendu ? Vrai : l'unité est marquée
    (taken) et ce résultat entre, un doublon prenant d'abord la place du principal. Faux : un
    autre résultat est entré ou entre en ce moment ; celui-ci n'entre pas."""
    now = time.time()
    with db.transaction() as c:
        unit = _row(c, unit_id)
        if unit is None or unit["status"] != RUNNING or unit["taken"] is not None:
            return False
        if unit["node"] == node:
            c.execute("UPDATE units SET taken=? WHERE id=?", (now, unit_id))
            return True
        copy = _copy(c, unit_id, node)
        if copy is None or unit["job_status"] == CANCELLED:
            return False
        _swap(c, unit, copy, now)
        return True


def take_back(unit_id, node) -> dict | None:
    """Un résultat arrive de `node`, qui calcule un doublon (leases.take_back n'y a pas trouvé
    le principal) : premier arrivé, il prend la place du principal et l'unité est rendue pour
    l'intégrer, comme leases.take_back. None : résultat à refuser (l'unité est faite, un autre
    résultat entre, ou son travail est annulé) ; ce doublon a fini, sa ligne s'en va."""
    now = time.time()
    with db.transaction() as c:
        copy = _copy(c, unit_id, node)
        if copy is None:
            return None
        unit = _row(c, unit_id)
        if unit["status"] != RUNNING or unit["taken"] is not None \
                or unit["job_status"] == CANCELLED:
            c.execute("DELETE FROM unit_copies WHERE unit_id=? AND node=?", (unit_id, node))
            return None
        _swap(c, unit, copy, now)
        row = c.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        return dict(row, payload=json.loads(row["payload"]))


def finished(unit_id) -> bool:
    """L'unité est-elle faite, ou un résultat y entre-t-il en ce moment ? (Le refus poli d'un
    résultat arrivé après, network/work.result.)"""
    row = db.query_one("SELECT status, taken FROM units WHERE id=?", (unit_id,))
    return row is not None and (row["status"] in (DONE, SKIPPED) or row["taken"] is not None)


# ---------------------------------------------------------- un calcul tombe
def promote(c, unit_id, now, error=None) -> bool:
    """Le principal s'arrête sans résultat (échec, résultat refusé, unité rendue, bail échu :
    queue.fail_unit, queue.release_unit, leases.expire) : le plus ancien doublon encore vivant
    prend sa place, l'unité reste en calcul et rien n'est compté. Faux : aucun (ou travail
    annulé), les règles d'avant s'appliquent. Dans la transaction c."""
    c.execute("DELETE FROM unit_copies WHERE unit_id=? AND lease_until IS NOT NULL "
              "AND lease_until < ?", (unit_id, now))
    copy = c.execute("SELECT * FROM unit_copies WHERE unit_id=? ORDER BY started LIMIT 1",
                     (unit_id,)).fetchone()
    if copy is None:
        return False
    job = c.execute("SELECT j.status FROM units u JOIN jobs j ON j.id = u.job_id "
                    "WHERE u.id=?", (unit_id,)).fetchone()
    if job is None or job["status"] == CANCELLED:
        forget(c, unit_id)
        return False
    c.execute("UPDATE units SET status=?, node=?, place=?, started=?, lease_until=?, "
              "run_uuid=COALESCE(?, run_uuid), taken=NULL, progress=NULL, "
              "error=COALESCE(?, error) WHERE id=?",
              (RUNNING, copy["node"], copy["place"], copy["started"], copy["lease_until"],
               copy["run_uuid"], error, unit_id))
    c.execute("DELETE FROM unit_copies WHERE unit_id=? AND node=?", (unit_id, copy["node"]))
    return True


def drop(unit_id, node) -> int | None:
    """Le doublon de `node` s'arrête sans résultat (échec, rendu, battu) : sa ligne s'en va,
    l'unité continue avec l'autre calcul. → id du travail, ou None s'il n'y en avait pas."""
    with db.transaction() as c:
        row = c.execute("SELECT u.job_id FROM unit_copies uc JOIN units u ON u.id = uc.unit_id "
                        "WHERE uc.unit_id=? AND uc.node=?", (unit_id, node)).fetchone()
        if row is None:
            return None
        c.execute("DELETE FROM unit_copies WHERE unit_id=? AND node=?", (unit_id, node))
        return row["job_id"]


def forget(c, unit_id):
    """Les doublons d'une unité faite (queue.finish_unit), échouée ou annulée : leurs
    machines l'apprennent à leur renouvellement, celui d'ici par l'exécuteur (beaten)."""
    c.execute("DELETE FROM unit_copies WHERE unit_id=?", (unit_id,))


def expire(now) -> int:
    """Doublons dont le bail a échu (leur machine a disparu), ou dont l'unité n'est plus en
    calcul : leur ligne s'en va, l'unité continue (leases.expire)."""
    return db.execute("DELETE FROM unit_copies WHERE (lease_until IS NOT NULL AND lease_until < ?) "
                      "OR unit_id NOT IN (SELECT id FROM units WHERE status=?)",
                      (now, RUNNING)).rowcount


def recover(now):
    """Au démarrage (queue.recover), avant de rendre les unités calculées ici : les doublons
    d'ici sont morts avec AIODOS ; ceux confiés ailleurs gardent leur machine, avec le bail de
    grâce du principal (ils peuvent ainsi prendre sa place)."""
    with db.transaction() as c:
        c.execute("DELETE FROM unit_copies WHERE lease_until IS NULL")
        c.execute("UPDATE unit_copies SET lease_until=? WHERE lease_until IS NOT NULL",
                  (now + RECOVER_GRACE,))


# ------------------------------------------------- la machine qui le calcule
def renew(unit_id, node, status, paused_now, lease_s) -> str:
    """Le renouvellement d'une machine qui n'a pas le principal (leases.renew) : son doublon
    continue (« running », bail prolongé), s'arrête pour une pause « tout de suite »
    (« paused »), ou n'existe plus (« reassigned »)."""
    copy = db.query_one("SELECT lease_until FROM unit_copies WHERE unit_id=? AND node=?",
                        (unit_id, node))
    if copy is None or copy["lease_until"] is None or status != RUNNING:
        return "reassigned"
    if paused_now:
        return "paused"
    db.execute("UPDATE unit_copies SET lease_until=? WHERE unit_id=? AND node=? "
               "AND lease_until IS NOT NULL", (time.time() + lease_s, unit_id, node))
    return "running"


def held(unit_id, node) -> dict | None:
    """L'unité, si `node` en calcule un doublon en ce moment (son fichier, ses entrées, le
    temps accordé à un ami : network/work.py), avec le départ, le lieu et le bail du doublon."""
    copy = db.query_one("SELECT * FROM unit_copies WHERE unit_id=? AND node=? "
                        "AND lease_until IS NOT NULL", (unit_id, node))
    row = copy and db.query_one("SELECT * FROM units WHERE id=? AND status=?",
                                (unit_id, RUNNING))
    if not row:
        return None
    return dict(row, payload=json.loads(row["payload"]), node=node, place=copy["place"],
                started=copy["started"], lease_until=copy["lease_until"], copy=True)


def held_by(node) -> int:
    """Doublons que calcule cette machine en ce moment (FRIEND_MAX_HELD, network/work.py)."""
    row = db.query_one("SELECT COUNT(*) AS n FROM unit_copies uc JOIN units u "
                       "ON u.id = uc.unit_id WHERE uc.node=? AND uc.lease_until IS NOT NULL "
                       "AND u.status=?", (node, RUNNING))
    return row["n"] if row else 0


def held_tasks(node) -> list:
    """[{task, model}] des doublons de cette machine : la taille de résultat qu'elle peut
    m'envoyer (network/work.result_limit)."""
    return db.query("SELECT u.task, json_extract(u.payload, '$.model') AS model "
                    "FROM unit_copies uc JOIN units u ON u.id = uc.unit_id "
                    "WHERE uc.node=? AND u.status=?", (node, RUNNING))


def beaten(unit_ids, node) -> set:
    """Parmi les unités que calcule l'exécuteur d'ici (node : ce nœud), celles dont il n'a
    plus aucun calcul : faites ailleurs, ou parties chez une autre machine. Leur arrêt est
    levé (executor._halt_withdrawn)."""
    ids = sorted(unit_ids)
    if not ids:
        return set()
    rows = db.query(
        f"""SELECT u.id FROM units u WHERE u.id IN ({",".join("?" * len(ids))})
            AND NOT (u.status = 'running' AND (u.node = ? OR EXISTS (
                SELECT 1 FROM unit_copies uc WHERE uc.unit_id = u.id AND uc.node = ?)))""",
        (*ids, node, node))
    return {r["id"] for r in rows}
