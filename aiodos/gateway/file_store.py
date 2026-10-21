"""Les fichiers produits pour les applications (docs/conception/partage-puissance.md § 5.2,
5.3, 6.8) : rangés par appli et par travail, gardés jusqu'à ce que l'appli les récupère,
puis OUT_DAYS jours. Un fichier jamais récupéré n'est jamais effacé seul (I18).

- Rangement : data/gateway/out/<app_id>/<travail>/<n>-<nom>, une ligne de gateway_files
  (id « file_<22 caractères> »), hors du dossier Images (qui peut être synchronisé).
- Récupéré : servi jusqu'au dernier octet, et les octets servis atteignent sa taille (en une
  fois, ou par plages) : fetched_at est posé, les OUT_DAYS jours commencent.
- Quota : QUOTA de fichiers non récupérés par appli ; au-delà, un nouveau travail est refusé
  (429 quota_exceeded).
"""
import logging
import os
import secrets
import shutil
from pathlib import Path

from aiodos import db, paths
from aiodos.gateway import apps

log = logging.getLogger("aiodos.gateway")

OUT_DAYS = 7
QUOTA = 20 * 1024 ** 3


def _folder(app_id: str, job: str) -> Path:
    if not apps.valid_id(app_id):
        raise ValueError("identifiant d'appli invalide")
    return paths.gateway_dir("out") / app_id / job


def _size_of_image(path: Path, kind: str):
    if not kind.startswith("image/"):
        return None, None
    try:
        from PIL import Image
        with Image.open(path) as picture:
            return picture.size
    except Exception:
        return None, None


def add(app_id: str, job_id: int, job: str, unit_index: int, source: Path, *, node: str,
        kind: str, size: int, sha256: str, machine: str | None, place: str,
        engine: str | None) -> dict:
    """Range un fichier produit (déplacé depuis le dossier de calcul) → sa fiche publique."""
    folder = _folder(app_id, job)
    folder.mkdir(parents=True, exist_ok=True)
    count = db.query_one("SELECT COUNT(*) AS n FROM gateway_files WHERE job_id=?",
                         (job_id,))["n"]
    name = Path(source).name.split("-", 1)[-1] or "sortie"
    target = folder / f"{count + 1:04d}-{name}"
    shutil.move(str(source), str(target))
    width, height = _size_of_image(target, kind)
    file_id = "file_" + secrets.token_urlsafe(16)
    db.execute(
        "INSERT INTO gateway_files(id, app_id, job_id, unit_index, node, type, bytes, sha256, "
        "width, height, machine, place, engine, path, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (file_id, app_id, job_id, unit_index, node, kind, size, sha256, width, height,
         machine, place, engine, str(target.relative_to(paths.gateway_dir("out"))), db.now()))
    return public(get_row(file_id))


def get_row(file_id: str):
    return db.query_one("SELECT * FROM gateway_files WHERE id=?", (file_id,))


def get(app_id: str, file_id: str) -> dict | None:
    """La ligne d'un fichier de cette appli (jamais celui d'une autre, I10), ou None."""
    row = db.query_one("SELECT * FROM gateway_files WHERE id=? AND app_id=?",
                       (str(file_id), app_id))
    return dict(row) if row else None


def path_of(row) -> Path:
    return paths.gateway_dir("out") / row["path"]


def public(row) -> dict:
    fetched = row["fetched_at"]
    return {"id": row["id"], "unit": row["unit_index"], "node": row["node"],
            "type": row["type"], "bytes": row["bytes"], "sha256": row["sha256"],
            "width": row["width"], "height": row["height"], "machine": row["machine"],
            "place": row["place"], "engine": row["engine"], "verified": "none",
            "fetched": fetched is not None,
            "expires_at": int(fetched + OUT_DAYS * 86400) if fetched else None,
            "created_at": int(row["created"])}


def for_job(job_id: int, unfetched: bool = False) -> list:
    rows = db.query("SELECT * FROM gateway_files WHERE job_id=? "
                    + ("AND fetched_at IS NULL " if unfetched else "")
                    + "ORDER BY created, id", (job_id,))
    return [public(r) for r in rows]


def ids_by_unit(job_id: int) -> dict:
    out = {}
    for r in db.query("SELECT id, unit_index FROM gateway_files WHERE job_id=? "
                      "ORDER BY created, id", (job_id,)):
        out.setdefault(r["unit_index"], []).append(r["id"])
    return out


def served(file_id: str, start: int, end: int):
    """Des octets [start, end] ont été servis : à son dernier octet, et une fois servis en
    tout autant d'octets que sa taille, le fichier est récupéré (fetched_at)."""
    row = get_row(file_id)
    if row is None:
        return
    total = int(row["served"]) + max(0, end - start + 1)
    fetched = row["fetched_at"]
    if fetched is None and end + 1 >= row["bytes"] and total >= row["bytes"]:
        fetched = db.now()
    db.execute("UPDATE gateway_files SET served=?, fetched_at=? WHERE id=?",
               (total, fetched, file_id))


def unfetched_bytes(app_id: str) -> int:
    row = db.query_one("SELECT COALESCE(SUM(bytes), 0) AS n FROM gateway_files WHERE "
                       "app_id=? AND fetched_at IS NULL", (app_id,))
    return int(row["n"])


def delete(app_id: str, file_id: str) -> bool:
    row = get(app_id, file_id)
    if row is None:
        return False
    _remove(row)
    return True


def _remove(row):
    try:
        path_of(row).unlink(missing_ok=True)
    except OSError:
        log.warning("Fichier %s non effacé", row["id"])
        return
    db.execute("DELETE FROM gateway_files WHERE id=?", (row["id"],))
    folder = path_of(row).parent
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def delete_job_files(job_id: int):
    for row in db.query("SELECT * FROM gateway_files WHERE job_id=?", (job_id,)):
        _remove(row)


def purge(now: float | None = None) -> int:
    """Efface les fichiers récupérés depuis plus de OUT_DAYS jours → nombre."""
    now = db.now() if now is None else now
    rows = db.query("SELECT * FROM gateway_files WHERE fetched_at IS NOT NULL AND "
                    "fetched_at < ?", (now - OUT_DAYS * 86400,))
    for row in rows:
        _remove(row)
    return len(rows)


def release_app(app_id: str, now: float | None = None):
    """L'appli est retirée : ses fichiers non récupérés commencent leurs OUT_DAYS jours."""
    db.execute("UPDATE gateway_files SET fetched_at=? WHERE app_id=? AND fetched_at IS NULL",
               (db.now() if now is None else now, app_id))


def clean_reception():
    """Au démarrage : les dossiers de calcul des unités, d'un lancement précédent."""
    folder = paths.engine_runs_dir()
    for entry in folder.iterdir():
        try:
            shutil.rmtree(entry) if entry.is_dir() else os.unlink(entry)
        except OSError:
            pass
