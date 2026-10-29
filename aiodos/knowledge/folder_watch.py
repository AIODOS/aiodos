"""Le suivi des dossiers des bibliothèques : une lecture peu après l'ouverture d'AIODOS, puis
toutes les WATCH_EVERY secondes (knowledge/folders.py, watch_pass). Une scrutation légère
(la date et la taille de chaque fichier), sans dépendance de plus : les fichiers nouveaux ou
modifiés sont indexés en priorité basse, ceux qui ont disparu quittent la bibliothèque ; un
dossier hors ligne ne perd rien. Lancé par le serveur (api/server.py), dans sa boucle asyncio ;
chaque lecture se fait dans un fil à part."""
import asyncio
import logging
import os

from aiodos.knowledge import folders

log = logging.getLogger("aiodos.knowledge")

FIRST_DELAY = 45              # s après l'ouverture : le démarrage d'abord
WATCH_EVERY = 600             # s entre deux lectures de tous les dossiers suivis


async def loop(executor, bus, first_delay=FIRST_DELAY, every=WATCH_EVERY):
    """Lit les dossiers suivis, à l'ouverture puis à intervalles ; chaque travail créé
    réveille la file, et la vue des bibliothèques est prévenue de ce qui a changé. Pas dans
    les essais automatiques (AIODOS_QUIET=1) : ils lisent les dossiers quand ils le veulent
    (folders.watch_pass), sans travail surgi au milieu d'un autre essai."""
    if os.environ.get("AIODOS_QUIET") == "1":
        return
    await asyncio.sleep(first_delay)
    first = True
    while True:
        try:
            found = await asyncio.to_thread(folders.watch_pass, first)
            for job_id in found["jobs"]:
                executor.publish_job(job_id, force=True)
            if found["jobs"]:
                executor.wake()
            if found["changed"]:
                bus.publish({"type": "knowledge"})
            if found["jobs"]:
                log.info("Dossiers suivis : %d travail(s) d'indexation lancé(s)",
                         len(found["jobs"]))
        except Exception:  # noqa: BLE001 — le suivi reprend au tour suivant
            log.exception("Suivi des dossiers : lecture impossible")
        first = False
        await asyncio.sleep(every)
