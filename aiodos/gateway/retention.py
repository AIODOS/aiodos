"""La garde des fichiers des applications (docs/conception/partage-puissance.md § 5.1, 5.3) :
au démarrage, les dossiers de calcul d'un lancement précédent sont vidés ; puis toutes les
heures, les entrées qu'aucun travail en cours ne cite depuis 24 h et les fichiers récupérés
depuis 7 jours sont effacés. Un fichier jamais récupéré ne l'est jamais."""
import asyncio
import logging

from aiodos.gateway import file_store, input_store

log = logging.getLogger("aiodos.gateway")

EVERY = 3600


def run_once() -> dict:
    return {"inputs": input_store.purge(), "files": file_store.purge()}


async def loop():
    await asyncio.to_thread(file_store.clean_reception)
    while True:
        try:
            done = await asyncio.to_thread(run_once)
            if any(done.values()):
                log.info("Fichiers des applications effacés : %s", done)
        except Exception:
            log.exception("Garde des fichiers des applications : échec")
        await asyncio.sleep(EVERY)
