"""Les dossiers d'une bibliothèque, suivis (29/10/2026 : l'auteur ne voyait nulle part le
dossier ajouté, et ce qu'il y a mis ensuite n'a jamais été lu).

- Un dossier ajouté (add) est gardé (table library_folders) : son chemin, le préfixe de ses
  entrées dans la bibliothèque (root : son nom, rendu unique dans la bibliothèque), sa
  dernière lecture, en ligne ou non, ses fichiers non pris en charge. Chacun de ses fichiers
  lus a sa ligne (library_files) : taille et date à sa lecture, état (en attente, indexé,
  sauté, en échec, annulé), raison, travail qui l'a en charge.
- Une lecture (scan) : à l'ajout, à l'ouverture d'AIODOS puis toutes les quelques minutes
  (knowledge/folder_watch.py), et sur « Relire ». Elle indexe les fichiers nouveaux ou
  modifiés (un travail index_paths qui porte « folder_id », fait des mêmes unités index_file
  qu'avant, calculables sur mes machines) et retire de la bibliothèque les fichiers
  supprimés. Un dossier hors ligne (disque débranché, partage absent) n'en retire rien, ni un
  sous-dossier qui ne se lit pas. Un fichier en échec n'est relu qu'à « Relire » et à la
  première lecture après l'ouverture d'AIODOS (RETRY_AT_START : sinon il échouerait toutes
  les dix minutes) ; un fichier annulé, à « Relire » (RETRY_ALL) ; un fichier sauté (aucun
  texte, image illisible), seulement s'il change. Rien ne se lit tant qu'un travail du
  dossier est en cours.
- L'état des fichiers vient des unités de ses travaux (sync), gardé une fois le travail fini :
  un travail effacé de la vue Tâches ne fait rien perdre.
- Les dossiers indexés avant cette version sont retrouvés une fois (adopt_legacy) : d'après
  leurs entrées (origine et source), et d'après les travaux d'un dossier encore en cours ou
  qui n'a rien donné (celui de l'auteur, dont chaque fichier avait échoué faute de modèle
  d'embeddings).
"""
import json
import logging
import os
import threading
from collections import Counter
from pathlib import Path

from aiodos import db, i18n, paths
from aiodos.i18n import t
from aiodos.jobs import queue
from aiodos.jobs.models import BACKGROUND, DONE, FAILED, NORMAL
from aiodos.knowledge import embed_model, search, store

log = logging.getLogger("aiodos.knowledge")

KIND = "index_paths"
ACTIVE = ("queued", "running", "paused")
STATES = ("indexed", "pending", "failed", "skipped", "cancelled")
# L'état d'un fichier d'après le statut de son unité
FROM_UNIT = {"queued": "pending", "running": "pending", "done": "indexed",
             "skipped": "skipped", "failed": "failed", "cancelled": "cancelled"}
RETRY_ALL = ("failed", "cancelled")       # « Relire »
RETRY_AT_START = ("failed",)              # première lecture après l'ouverture d'AIODOS
PROBLEMS_MAX = 200                        # fichiers non lus montrés, au plus

_local = threading.local()                # la lecture que split reprend (sans reparcourir)
_adopt_lock = threading.Lock()
_adopted = {"db": None}


# ------------------------------------------------------------------- chemins
def _clean(path) -> Path:
    return Path(os.path.abspath(str(path)))


def _same(a, b) -> bool:
    return os.path.normcase(str(_clean(a))) == os.path.normcase(str(_clean(b)))


def _relative(path, root: Path) -> str | None:
    try:
        return Path(path).relative_to(root).as_posix()
    except (TypeError, ValueError):
        return None


def _online(root: Path) -> bool:
    """Le dossier se lit-il en ce moment ? (disque débranché, partage absent : non)"""
    try:
        with os.scandir(root):
            return True
    except OSError:
        return False


# -------------------------------------------------------------------- dossiers
def _row(folder_id) -> dict:
    row = db.query_one("SELECT * FROM library_folders WHERE id=?", (folder_id,))
    if row is None:
        raise ValueError(t("library_folders.not_found"))
    return dict(row)


def find(collection_id, path) -> dict | None:
    """Le dossier suivi de ce chemin dans cette bibliothèque (casse comprise sous Windows)."""
    for row in db.query("SELECT * FROM library_folders WHERE collection_id=?", (collection_id,)):
        if _same(row["path"], path):
            return dict(row)
    return None


def _unique_root(collection_id, name) -> str:
    """Le préfixe des entrées d'un nouveau dossier : son nom, suivi de « (2) »… si un autre
    dossier de la bibliothèque le porte déjà (deux « Photos » ne se mêlent plus)."""
    taken = {r["root"] for r in db.query("SELECT root FROM library_folders WHERE "
                                         "collection_id=?", (collection_id,))}
    root, n = name, 2
    while root in taken:
        root, n = f"{name} ({n})", n + 1
    return root


def _insert(collection_id, path: Path, root: str, added=None) -> int:
    db.execute("INSERT OR IGNORE INTO library_folders(collection_id, path, root, added) "
               "VALUES(?, ?, ?, ?)", (collection_id, str(path), root, added or db.now()))
    return find(collection_id, path)["id"]


def add(collection_id, path, priority=NORMAL) -> dict:
    """Ajoute ce dossier à la bibliothèque (ou retrouve celui déjà suivi) et le lit tout de
    suite. ValueError : bibliothèque inconnue, chemin qui n'est pas un dossier, dossier
    d'AIODOS. → sa description (describe), avec job_id."""
    from aiodos.jobs.kinds import index
    store.collection_name(collection_id)                   # ValueError si elle n'existe pas
    folder = _clean(path)
    if index._inside_aiodos(folder):
        raise ValueError(t("library_folders.aiodos_folder"))
    if not folder.is_dir():
        raise ValueError(t("library_folders.not_a_folder", path=str(folder)))
    ensure_adopted()
    found = find(collection_id, folder)
    if found is None:
        folder_id = _insert(collection_id, folder,
                            _unique_root(collection_id, folder.name or str(folder)))
    else:
        folder_id = found["id"]
    return scan(folder_id, RETRY_ALL, priority, first=found is None)


def remove(folder_id) -> int:
    """Retire le dossier de la bibliothèque, et ses passages (ses fichiers ne sont pas
    touchés) ; ses travaux en cours sont annulés. → nombre d'entrées retirées."""
    folder = _row(folder_id)
    for job in _jobs(folder_id, active=True):
        queue.cancel_job(job["id"])
    root, path = folder["root"], folder["path"]
    under = path if path.endswith(("\\", "/")) else path + os.sep
    with db.transaction() as c:
        removed = c.execute(
            "DELETE FROM entries WHERE collection_id=? AND kind='folder' "
            "AND (source=? OR substr(source, 1, ?)=?) "
            "AND (origin IS NULL OR origin=? OR substr(origin, 1, ?)=?)",
            (folder["collection_id"], root, len(root) + 1, root + "/", path, len(under),
             under)).rowcount
        c.execute("DELETE FROM library_folders WHERE id=?", (folder_id,))
    search.invalidate()
    log.info("Dossier suivi retiré de la bibliothèque %s : %s (%d entrée(s))",
             folder["collection_id"], path, removed)
    return removed


def watched(folder_id) -> bool:
    """Ce dossier est-il encore suivi ? (une unité calculée pendant son retrait n'entre pas
    dans la bibliothèque, jobs/kinds/index.assimilate_index_file)"""
    return db.query_one("SELECT 1 FROM library_folders WHERE id=?", (folder_id,)) is not None


# --------------------------------------------------------------------- lecture
def _jobs(folder_id, active=False, unsynced=False) -> list:
    """Les travaux de ce dossier, du plus ancien au plus récent."""
    sql = ("SELECT id, status FROM jobs WHERE kind=? AND json_extract(params, '$.folder_id')=?")
    if active:
        sql += " AND status IN ('queued', 'running', 'paused')"
    if unsynced:
        sql += " AND json_extract(params, '$.folder_synced') IS NULL"
    return [dict(r) for r in db.query(sql + " ORDER BY created, id", (KIND, folder_id))]


def _active_job(folder_id) -> dict | None:
    found = _jobs(folder_id, active=True)
    return found[-1] if found else None


def _changed(row, stamp) -> bool:
    if stamp is None:
        return False                    # illisible en ce moment : on n'y touche pas
    if row["size"] is None:             # retrouvé d'une version d'avant : date de son entrée
        return row["mtime_ns"] is None or stamp[1] > row["mtime_ns"]
    return [row["size"], row["mtime_ns"]] != list(stamp)


def _plan(folder, retry=()) -> dict:
    """Parcourt le dossier : les unités des fichiers nouveaux, modifiés ou à refaire (retry :
    états à refaire), les fichiers non pris en charge (par extension), ceux qui ont disparu
    (hors des sous-dossiers qui ne se lisent pas)."""
    from aiodos.jobs.kinds import index
    root = Path(folder["path"])
    known = {r["rel"]: dict(r) for r in db.query(
        "SELECT rel, size, mtime_ns, state FROM library_files WHERE folder_id=?",
        (folder["id"],))}
    errors, seen, units, ignored = [], set(), [], Counter()
    for found in index._walk(root, errors):
        rel = _relative(found, root)
        if rel is None:
            continue
        seen.add(rel)
        row = known.get(rel)
        if row is not None and not _changed(row, index._stamp(found)) \
                and row["state"] not in retry:
            continue
        unit = index._plan_file(found, folder["collection_id"], f"{folder['root']}/{rel}",
                                "folder", ignored)
        if unit is not None:
            unit.payload["folder_id"] = folder["id"]     # retiré entre-temps : n'entre pas
            units.append(unit)
    blind = [r for r in (_relative(e, root) for e in errors) if r is not None]
    gone = [rel for rel in known if rel not in seen
            and not any(b == "." or rel == b or rel.startswith(b + "/") for b in blind)]
    return {"units": units, "ignored": ignored, "gone": gone}


def _forget(folder, gone) -> int:
    """Les fichiers disparus du dossier quittent la bibliothèque. → entrées retirées."""
    if not gone:
        return 0
    root, removed = Path(folder["path"]), 0
    with db.transaction() as c:
        for rel in gone:
            origin = str(root.joinpath(*rel.split("/")))
            removed += c.execute(
                "DELETE FROM entries WHERE collection_id=? AND source=? AND kind='folder' "
                "AND (origin IS NULL OR origin=?)",
                (folder["collection_id"], f"{folder['root']}/{rel}", origin)).rowcount
            c.execute("DELETE FROM library_files WHERE folder_id=? AND rel=?",
                      (folder["id"], rel))
    search.invalidate()
    log.info("Dossier suivi %s : %d fichier(s) disparu(s), %d entrée(s) retirée(s)",
             folder["path"], len(gone), removed)
    return removed


def scan(folder_id, retry=(), priority=BACKGROUND, auto=False, first=False) -> dict:
    """Lit le dossier : indexe ce qui est nouveau ou modifié (et les états de retry), retire
    ce qui a disparu. Rien quand un de ses travaux est en cours (busy) ou qu'il est hors ligne
    (offline : rien n'est retiré). auto : lecture du suivi (folder_watch), sans annonce
    quand tout va bien. → describe(), avec job_id, planned, removed."""
    folder = _row(folder_id)
    busy = _active_job(folder_id)
    if busy is not None:
        return describe(folder_id, job_id=busy["id"], busy=True)
    sync(folder_id)
    if not _online(Path(folder["path"])):
        db.execute("UPDATE library_folders SET online=0 WHERE id=?", (folder_id,))
        return describe(folder_id, offline=True)
    plan = _plan(folder, retry)
    removed = _forget(folder, plan["gone"])
    db.execute("UPDATE library_folders SET scanned=?, online=1, ignored=?, ignored_types=? "
               "WHERE id=?", (db.now(), sum(plan["ignored"].values()),
                              json.dumps(dict(plan["ignored"].most_common())), folder_id))
    job_id = None
    if plan["units"]:
        _local.plan = (folder_id, plan)
        try:
            job_id = queue.create_job(KIND, {"paths": [folder["path"]],
                                             "collection_id": folder["collection_id"],
                                             "folder_id": folder_id, "first": first,
                                             "auto": auto, "retry": list(retry)}, priority)
        finally:
            _local.plan = None
        sync(folder_id)
    return describe(folder_id, job_id=job_id, planned=len(plan["units"]), removed=removed)


def split(params):
    """Le découpage d'un travail de dossier (jobs/kinds/index.split_index_paths) : la lecture
    que scan vient de faire, sinon une nouvelle (sans rien retirer)."""
    folder = _row(params["folder_id"])
    planned = getattr(_local, "plan", None)
    plan = planned[1] if planned and planned[0] == folder["id"] \
        else _plan(folder, tuple(params.get("retry") or ()))
    target = store.collection_name(folder["collection_id"])
    name = Path(folder["path"]).name or folder["path"]
    units = plan["units"]
    if params.get("first"):
        title = i18n.lazy("index.job_index_folder", name=name, target=target)
        # Les fichiers non pris en charge : dits par leur extension (skipped_types), pas
        # comptés parmi les unités sautées (la progression passait 100 %, « 133 % »)
        info = {"skipped_types": dict(plan["ignored"].most_common())}
    else:
        title = i18n.lazy("library_folders.job_rescan", name=name, target=target,
                          n=len(units))
        info = {}
    if not units:
        info["message"] = t("library_folders.nothing_to_read")
    return title, units, info


def sync(folder_id):
    """Reporte sur les fichiers du dossier l'état des unités de ses travaux (un travail fini
    n'est plus relu ensuite : « folder_synced ») ; un fichier en attente d'un travail disparu
    (annulé puis effacé) passe à « annulé »."""
    folder = db.query_one("SELECT id, path FROM library_folders WHERE id=?", (folder_id,))
    if folder is None:
        return
    root = Path(folder["path"])
    with db.transaction() as c:
        for job in _jobs(folder_id, unsynced=True):
            rows = []
            for u in c.execute("SELECT payload, status, error, result FROM units "
                               "WHERE job_id=? AND task='index_file'", (job["id"],)):
                payload = json.loads(u["payload"])
                rel = _relative(payload.get("path"), root)
                if rel is None:
                    continue
                state = FROM_UNIT.get(u["status"], "pending")
                reason = None
                if state == "failed":
                    reason = u["error"]
                elif state == "skipped":
                    reason = json.loads(u["result"] or "{}").get("reason")
                stamp = payload.get("stamp")
                size, mtime = stamp if isinstance(stamp, list) and len(stamp) == 2 \
                    else (None, None)
                rows.append((folder_id, rel, size, mtime, state, reason,
                             job["id"] if state == "pending" else None))
            c.executemany(
                "INSERT INTO library_files(folder_id, rel, size, mtime_ns, state, reason, "
                "job_id) VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(folder_id, rel) DO UPDATE "
                "SET size=excluded.size, mtime_ns=excluded.mtime_ns, state=excluded.state, "
                "reason=excluded.reason, job_id=excluded.job_id", rows)
            if job["status"] not in ACTIVE:
                c.execute("UPDATE jobs SET params=json_set(params, '$.folder_synced', 1) "
                          "WHERE id=?", (job["id"],))
        c.execute("UPDATE library_files SET state='cancelled', job_id=NULL "
                  "WHERE folder_id=? AND state='pending' AND NOT EXISTS ("
                  "SELECT 1 FROM jobs j WHERE j.id = library_files.job_id "
                  "AND j.status IN ('queued', 'running', 'paused') "
                  "AND json_extract(j.params, '$.folder_id') = library_files.folder_id)",
                  (folder_id,))


# --------------------------------------------------------------------- lecture
def _file_states(folder) -> dict:
    """{fichier : (état, raison)} : ceux gardés, et ceux du travail en cours d'après ses
    unités (exact pendant le travail, sans rien écrire)."""
    states = {r["rel"]: (r["state"], r["reason"]) for r in db.query(
        "SELECT rel, state, reason FROM library_files WHERE folder_id=?", (folder["id"],))}
    job = _active_job(folder["id"])
    if job is None:
        return {rel: ("cancelled" if state == "pending" else state, reason)
                for rel, (state, reason) in states.items()}
    root = Path(folder["path"])
    for u in db.query("SELECT json_extract(payload, '$.path') AS path, status, error, "
                      "json_extract(result, '$.reason') AS reason FROM units "
                      "WHERE job_id=? AND task='index_file'", (job["id"],)):
        rel = _relative(u["path"], root)
        if rel is not None:
            state = FROM_UNIT.get(u["status"], "pending")
            states[rel] = (state, u["error"] if state == "failed" else u["reason"])
    return states


def describe(folder_id, **extra) -> dict:
    """Un dossier suivi pour la vue : chemin, dernière lecture, en ligne, nombre de fichiers
    par état, non pris en charge (par extension), travail en cours et ce qui le retient."""
    folder = _row(folder_id)
    counts = dict.fromkeys(STATES, 0)
    for state, _reason in _file_states(folder).values():
        counts[state if state in counts else "pending"] += 1
    job = _active_job(folder_id)
    try:
        types = json.loads(folder["ignored_types"] or "{}")
    except ValueError:
        types = {}
    found = {"id": folder["id"], "collection_id": folder["collection_id"],
             "path": folder["path"], "root": folder["root"], "added": folder["added"],
             "scanned": folder["scanned"], "online": bool(folder["online"]),
             "ignored": folder["ignored"], "ignored_types": types, "counts": counts,
             "files": sum(counts.values()), "job": job,
             "blocked": embed_model.job_note(KIND, job["status"]) if job else None}
    found.update(extra)
    return found


def list_folders(collection_id=None) -> list:
    ensure_adopted()
    rows = db.query("SELECT id FROM library_folders WHERE (? IS NULL OR collection_id=?) "
                    "ORDER BY path COLLATE NOCASE", (collection_id, collection_id))
    return [describe(r["id"]) for r in rows]


def problems(folder_id, limit=PROBLEMS_MAX) -> list:
    """Les fichiers du dossier en échec ou sautés, et pourquoi (les premiers, par nom)."""
    states = _file_states(_row(folder_id))
    bad = sorted((rel, state, reason) for rel, (state, reason) in states.items()
                 if state in ("failed", "skipped"))
    return [{"file": rel, "state": state, "reason": reason or ""}
            for rel, state, reason in bad[:limit]]


# ------------------------------------------------------------------------ suivi
def watch_pass(first=False) -> dict:
    """Une lecture de tous les dossiers suivis (knowledge/folder_watch.py). first : la
    première depuis l'ouverture d'AIODOS (les fichiers en échec y sont refaits). → {"jobs":
    travaux créés, "changed": la bibliothèque a changé}."""
    ensure_adopted()
    created, changed = [], False
    for row in db.query("SELECT id FROM library_folders ORDER BY id"):
        try:
            out = scan(row["id"], RETRY_AT_START if first else (), BACKGROUND, auto=True)
        except Exception:  # noqa: BLE001 — un dossier en erreur n'arrête pas les autres
            log.exception("Lecture du dossier suivi %s impossible", row["id"])
            continue
        if out.get("job_id") and not out.get("busy"):
            created.append(out["job_id"])
        changed = changed or bool(out.get("removed"))
    return {"jobs": created, "changed": changed or bool(created)}


# ------------------------------------------------------------- versions d'avant
def ensure_adopted():
    """adopt_legacy, une fois par base et par processus."""
    current = str(paths.db_path())
    with _adopt_lock:
        if _adopted["db"] == current:
            return
        try:
            adopt_legacy()
        except Exception:  # noqa: BLE001 — une base d'avant illisible n'empêche pas le reste
            log.exception("Dossiers indexés avant le suivi : reprise impossible")
        _adopted["db"] = current


def adopt_legacy():
    """Les dossiers indexés avant le suivi deviennent des dossiers suivis :
    - d'après leurs entrées : la source « nom/chemin/relatif » et l'origine (le chemin du
      fichier) donnent le dossier ; ses fichiers comptent comme indexés à la date de leur
      entrée (un fichier modifié depuis sera relu) ;
    - d'après les travaux d'un seul dossier, encore en cours, ou finis sans rien avoir indexé
      (tous en échec) : le dossier est suivi, et le travail lui est rattaché (sync en reprend
      l'état de chaque fichier)."""
    groups = {}
    for r in db.query("SELECT e.collection_id, e.source, e.origin, e.created FROM entries e "
                      "JOIN collections c ON c.id = e.collection_id WHERE e.kind='folder' "
                      "AND c.system=0 AND e.origin IS NOT NULL AND e.origin != ''"):
        parts = r["source"].split("/")
        origin = Path(r["origin"]).parts
        rel = parts[1:]
        if not rel or len(origin) <= len(rel) or list(origin[-len(rel):]) != rel:
            continue
        base = Path(*origin[:-len(rel)])
        group = groups.setdefault((r["collection_id"], str(base)), {"root": parts[0],
                                                                   "files": []})
        group["files"].append(("/".join(rel), r["created"]))
    for (collection_id, base), group in groups.items():
        found = find(collection_id, base)
        folder_id = found["id"] if found else _insert(
            collection_id, Path(base), group["root"], min(c for _, c in group["files"]))
        with db.transaction() as c:
            c.executemany("INSERT OR IGNORE INTO library_files(folder_id, rel, size, mtime_ns, "
                          "state) VALUES(?, ?, NULL, ?, 'indexed')",
                          [(folder_id, rel, int(created * 1e9))
                           for rel, created in group["files"]])
        if not found:
            log.info("Dossier indexé avant le suivi, désormais suivi : %s", base)
    collections = set(store.collection_names())
    for job in db.query("SELECT id, status, params, done, failed FROM jobs WHERE kind=? "
                        "AND json_extract(params, '$.folder_id') IS NULL ORDER BY created, id",
                        (KIND,)):
        params = json.loads(job["params"])
        collection_id = params.get("collection_id")
        dirs = [p for p in params.get("paths") or [] if isinstance(p, str) and Path(p).is_dir()]
        if collection_id not in collections or len(dirs) != 1:
            continue
        found = find(collection_id, dirs[0])
        if found is None:
            nothing = job["status"] in (DONE, FAILED) and not job["done"] and job["failed"]
            if job["status"] not in ACTIVE and not nothing:
                continue
            folder = _clean(dirs[0])
            found = {"id": _insert(collection_id, folder, folder.name or str(folder))}
            log.info("Dossier d'un travail d'avant le suivi, désormais suivi : %s", folder)
        db.execute("UPDATE jobs SET params=json_set(params, '$.folder_id', ?) WHERE id=?",
                   (found["id"], job["id"]))
        sync(found["id"])
