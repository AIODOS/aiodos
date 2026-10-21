"""Ce qu'une appli possède dans AIODOS (ses travaux, leurs fichiers, ses entrées) quand son
accès change (docs/conception/partage-puissance.md § 5.3) :

- transfer : l'appli remplace son accès (clé perdue, appli réinstallée : même identifiant,
  même programme, accord de l'utilisateur) : tout passe à la nouvelle clé, elle retrouve ses
  travaux et ses fichiers ;
- release : l'accès est retiré : ses travaux en cours sont annulés, ses entrées oubliées, et
  ses fichiers pas encore récupérés commencent leurs 7 jours de garde (« Le retrait de
  l'appli fait commencer les 7 jours »).
"""
import logging
import shutil

from aiodos import db, paths
from aiodos.gateway import apps, file_store, input_store

log = logging.getLogger("aiodos.gateway")


def transfer(old: str, new: str) -> None:
    if not (apps.valid_id(old) and apps.valid_id(new)) or old == new:
        return
    with db.transaction() as c:
        c.execute("UPDATE jobs SET owner_app=? WHERE owner_app=?", (new, old))
        c.execute("UPDATE gateway_files SET app_id=? WHERE app_id=?", (new, old))
        c.execute("UPDATE OR IGNORE job_inputs SET app_id=? WHERE app_id=?", (new, old))
        c.execute("UPDATE OR IGNORE gateway_inputs SET app_id=? WHERE app_id=?", (new, old))
        c.execute("DELETE FROM gateway_inputs WHERE app_id=?", (old,))
    for part in ("inputs", "out"):
        source = paths.gateway_dir(part) / old
        target = paths.gateway_dir(part) / new
        if source.is_dir():
            _merge(source, target)
    # Les fichiers rangés gardent leur chemin relatif : il commence par l'id de l'appli
    for row in db.query("SELECT id, path FROM gateway_files WHERE app_id=?", (new,)):
        if row["path"].startswith(old + "/") or row["path"].startswith(old + "\\"):
            db.execute("UPDATE gateway_files SET path=? WHERE id=?",
                       (new + row["path"][len(old):], row["id"]))
    log.info("Travaux et fichiers de l'accès %s passés à %s", old, new)


def _merge(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        dest = target / entry.name
        if entry.is_dir():
            _merge(entry, dest)
        elif not dest.exists():
            shutil.move(str(entry), str(dest))
    shutil.rmtree(source, ignore_errors=True)


def release(app_id: str) -> None:
    from aiodos.jobs import queue
    for row in db.query("SELECT id FROM jobs WHERE owner_app=? AND status IN ('queued', "
                        "'running', 'paused')", (app_id,)):
        queue.cancel_job(row["id"], by="aiodos")
    file_store.release_app(app_id)
    db.execute("DELETE FROM job_inputs WHERE app_id=?", (app_id,))
    input_store.forget_app(app_id)
