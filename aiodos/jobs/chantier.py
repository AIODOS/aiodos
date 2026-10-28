"""Le chantier en direct d'un travail (docs/conception/essaim.md § 13, étape 7, accepté par
l'auteur le 27/10/2026) : ses fragments (les unités), la machine qui calcule chacun, depuis
quand, son étape et son avancement quand on les connaît, ses doublons de fin de partie
(endgame.py : « doublée par », puis « faite par » et « battue »), et les places de chaque
machine qui y travaille (network/places_seen.py, executor.status pour celle-ci).

Lu à la demande par la vue Tâches, travail ouvert (GET /api/jobs/{id}/chantier, vue
js/views/chantier.js), jamais poussé par les événements fréquents : l'événement d'un travail
ne porte que ses compteurs et son nombre de doublons (queue.job_summary, « copies »), qui
disent quand relire. Limité à ce qui se voit : toutes les unités d'un petit travail
(ALL_SHOWN au plus) ; d'un grand, celles en calcul (RUNNING_SHOWN au plus), les dernières
faites (DONE_SHOWN), les dernières en échec (FAILED_SHOWN) et les prochaines en file
(QUEUED_SHOWN), toujours dans l'ordre des fragments ; le reste en nombres (« hidden »).

Chaque machine est dite par ce qu'en sait cette machine-ci (machine) : « here » (cette
machine), « mine » ou « friend » (une machine appairée, par son nom), « public » (une
machine du réseau public, jamais nommée), « other » (une machine qui n'est plus appairée).

L'avancement, honnêtement (partage-puissance.md § 2.8) : l'étape quand la machine la dit
(units.progress, unités de moteur), et la durée habituelle (« typical ») : la médiane des
unités déjà faites du même travail par cette machine, sinon par toutes ; l'interface en
tire min(0,95 ; écoulé / habituelle), dite comme une estimation. Sans unité faite : rien.
"""
import json
import re
import statistics
import time

from aiodos import db
from aiodos.jobs import endgame
from aiodos.jobs.models import DONE, FAILED, QUEUED, RUNNING, SKIPPED
from aiodos.network import peers, places_seen

ALL_SHOWN = 40          # un travail d'au plus autant d'unités : toutes montrées
RUNNING_SHOWN = 60      # d'un grand travail : les unités en calcul, au plus
DONE_SHOWN = 12         # … les dernières faites (ou ignorées)
FAILED_SHOWN = 6        # … les dernières en échec
QUEUED_SHOWN = 6        # … les prochaines en file
TYPICAL_SAMPLE = 200    # unités faites lues pour la durée habituelle
LABEL_MAX = 160
ERROR_MAX = 300
_STAGE = re.compile(r"[a-z_]{1,32}")

_COLUMNS = """u.id, u.status, u.node, u.place, u.resource, u.started, u.finished, u.compute_s,
    u.lease_until, u.progress, u.attempts, u.error, u.created, u.queued_at,
    json_extract(u.payload, '$.source') AS source, json_extract(u.payload, '$.url') AS url"""


def machine(node, place, me, known) -> dict:
    """Qui calcule (cf. le début du module) : {"kind"}, et pour une machine appairée son
    nom et son identifiant. known : {machine: ligne de peers.list_peers}."""
    if place == "public":
        return {"kind": "public"}                  # un inconnu : jamais nommé
    if place == "here" or (node is not None and node == me):
        return {"kind": "here"}
    peer = known.get(node)
    if peer is None:
        return {"kind": "other"}
    return {"kind": "friend" if peer.get("trust") == "friend" else "mine",
            "name": peer.get("name") or "", "id": node}


def _key(found: dict) -> str:
    """Une machine dans la liste des machines du chantier (les inconnus ensemble)."""
    return found.get("id") or found["kind"]


def _label(row) -> str:
    """Ce que calcule le fragment, quand son payload le dit (un fichier, une page)."""
    value = row["source"] or row["url"]
    return str(value)[:LABEL_MAX] if value is not None else ""


def _stage(progress) -> str | None:
    try:
        stage = (json.loads(progress) or {}).get("stage") if progress else None
    except (ValueError, AttributeError):
        return None
    return stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else None


def _typical(job_id) -> tuple:
    """(médiane de toutes les unités faites du travail, {machine: sa médiane}), en s."""
    rows = db.query("SELECT node, compute_s FROM units WHERE job_id=? AND status=? "
                    "AND compute_s > 0 ORDER BY finished DESC LIMIT ?",
                    (job_id, DONE, TYPICAL_SAMPLE))
    by_node = {}
    for r in rows:
        by_node.setdefault(r["node"], []).append(r["compute_s"])
    overall = statistics.median([r["compute_s"] for r in rows]) if rows else None
    return overall, {node: statistics.median(found) for node, found in by_node.items()}


def _rows(job_id, total) -> list:
    """Les unités montrées (cf. le début du module), dans l'ordre des fragments."""
    if total <= ALL_SHOWN:
        return db.query(f"SELECT {_COLUMNS} FROM units u WHERE u.job_id=? ORDER BY u.id",
                        (job_id,))
    picked = {}
    for status, order, limit in (
            ((RUNNING,), "u.started, u.id", RUNNING_SHOWN),
            ((DONE, SKIPPED), "u.finished DESC, u.id DESC", DONE_SHOWN),
            ((FAILED,), "u.finished DESC, u.id DESC", FAILED_SHOWN),
            ((QUEUED,), "u.id", QUEUED_SHOWN)):
        marks = ",".join("?" * len(status))
        for r in db.query(f"SELECT {_COLUMNS} FROM units u WHERE u.job_id=? AND u.status IN "
                          f"({marks}) ORDER BY {order} LIMIT ?", (job_id, *status, limit)):
            picked[r["id"]] = r
    return [picked[i] for i in sorted(picked)]


def view(job_id, me: str, here: dict | None = None) -> dict | None:
    """Le chantier de ce travail (cf. le début du module) ; None s'il n'existe pas. me : ce
    nœud ; here : l'état de son exécuteur (executor.status : places et places prises)."""
    job = db.query_one("SELECT id, status, total FROM jobs WHERE id=?", (job_id,))
    if job is None:
        return None
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM units WHERE job_id=? GROUP BY status", (job_id,))}
    total = sum(counts.values())
    first = db.query_one("SELECT MIN(id) AS id FROM units WHERE job_id=?", (job_id,))["id"]
    rows = _rows(job_id, total)
    copies = {}
    for c in db.query("SELECT uc.unit_id, uc.node, uc.place, uc.started, uc.lease_until "
                      "FROM unit_copies uc JOIN units u ON u.id = uc.unit_id "
                      "WHERE u.job_id=? AND u.status=? ORDER BY uc.started", (job_id, RUNNING)):
        copies.setdefault(c["unit_id"], []).append(c)
    beaten = endgame.losers([r["id"] for r in rows if r["status"] in (DONE, SKIPPED)])
    known = {p["node_id"]: p for p in peers.list_peers()}
    overall, by_node = _typical(job_id)

    def typical(node):
        found = by_node.get(node, overall)
        return round(found, 1) if found else None

    machines = {}

    def working(found, field):
        entry = machines.setdefault(_key(found), {"machine": found, "units": 0, "copies": 0})
        entry[field] += 1

    units, shown = [], {}
    for r in rows:
        shown[r["status"]] = shown.get(r["status"], 0) + 1
        unit = {"id": r["id"], "n": r["id"] - first + 1, "status": r["status"],
                "label": _label(r), "resource": r["resource"]}
        if r["status"] == RUNNING:
            unit["machine"] = machine(r["node"], r["place"], me, known)
            unit["started"] = r["started"]
            unit["typical"] = typical(r["node"])
            stage = _stage(r["progress"])
            if stage:
                unit["stage"] = stage
            working(unit["machine"], "units")
            unit["copies"] = []
            for c in copies.get(r["id"], []):
                found = machine(c["node"], c["place"], me, known)
                unit["copies"].append({"machine": found, "started": c["started"],
                                       "typical": typical(c["node"])})
                working(found, "copies")
        elif r["status"] in (DONE, SKIPPED, FAILED):
            unit["machine"] = machine(r["node"], r["place"], me, known) if r["node"] else None
            unit["finished"] = r["finished"]
            unit["compute_s"] = r["compute_s"]
            unit["beaten"] = [machine(node, place, me, known)
                              for node, place in beaten.get(r["id"], [])]
            if r["status"] == FAILED:
                unit["error"] = str(r["error"] or "")[:ERROR_MAX]
        elif r["status"] == QUEUED:
            unit["since"] = r["queued_at"] or r["created"]
            unit["retry"] = bool(r["attempts"] or r["error"])
        units.append(unit)

    listed = []
    for entry in machines.values():
        found = entry["machine"]
        if found["kind"] == "here":
            seen = {"places": dict((here or {}).get("slots") or {}),
                    "busy": dict((here or {}).get("busy") or {})} if here else None
        elif found.get("id"):
            seen = places_seen.get(found["id"])
        else:
            seen = None                               # un inconnu : ses places ne se disent pas
        listed.append(dict(entry, places=seen))
    order = {"here": 0, "mine": 1, "friend": 2, "other": 3, "public": 4}
    listed.sort(key=lambda e: (order.get(e["machine"]["kind"], 5), -e["units"] - e["copies"],
                               e["machine"].get("name") or ""))
    resources = sorted({u["resource"] for u in units if u["status"] == RUNNING}
                       or {u["resource"] for u in units})
    hidden = {status: n - shown.get(status, 0) for status, n in counts.items()
              if n - shown.get(status, 0) > 0}
    return {"job": job["id"], "status": job["status"], "total": total, "counts": counts,
            "units": units, "hidden": hidden, "machines": listed, "resources": resources,
            "typical": round(overall, 1) if overall else None, "now": time.time()}
