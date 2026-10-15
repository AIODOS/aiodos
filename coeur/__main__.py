"""Point d'entrée.

    python -m coeur              → nœud + fenêtre (usage normal)
    python -m coeur --headless   → nœud seul, sans écran (futures machines de calcul)
"""
import argparse
import logging
import os
import secrets
import socket
import sys
import threading
import time

import uvicorn

from coeur import __version__, db, instance, migrate_legacy, paths
from coeur.api.server import create_app
from coeur.knowledge import store

log = logging.getLogger("coeur")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _setup_logging():
    # Lancé par pythonw (double-clic, sans console) : sys.stdout/stderr valent None,
    # et des bibliothèques y écrivent quand même (uvicorn, barres de progression…).
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(paths.log_path().with_name("erreurs.log"), "a", encoding="utf-8")
    # Console Windows en cp1252 par défaut : « Cœur » y devient « C�ur »
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handlers = [logging.FileHandler(paths.log_path(), encoding="utf-8")]
    if sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s")


def _windows_app_id():
    """Barre des tâches : notre icône plutôt que celle de Python."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Arkane.CoeurIA")
        except (AttributeError, OSError):
            pass


def main():
    parser = argparse.ArgumentParser(prog="coeur", description="Cœur IA — nœud local")
    parser.add_argument("--headless", action="store_true", help="nœud seul, sans fenêtre")
    parser.add_argument("--port", type=int, default=0, help="port local (défaut : libre)")
    network = parser.add_argument_group("réseau local (tests, machines de calcul)")
    network.add_argument("--network", action="store_true",
                         help="active le réseau quel que soit le réglage")
    network.add_argument("--p2p-host", default=None,
                         help="interface d'écoute entre nœuds (défaut : 0.0.0.0)")
    network.add_argument("--p2p-port", type=int, default=None, help="port entre nœuds")
    network.add_argument("--no-discovery", action="store_true",
                         help="pas d'annonces UDP (ajout par adresse uniquement)")
    parser.add_argument("--no-migrate", action="store_true",
                        help="ne pas importer les données de la v1 (tests)")
    args = parser.parse_args()

    _setup_logging()
    _windows_app_id()
    port = args.port or _free_port()
    if not instance.acquire(port):
        instance.alert("Cœur IA est déjà ouvert sur cette machine.")
        return 1

    try:
        db.connect()
        store.ensure_defaults()
        migrated = None if args.no_migrate else migrate_legacy.run_if_needed()
        if migrated:
            log.info("Import v1 : %s", migrated)

        token = secrets.token_urlsafe(24)
        app = create_app(token, {f"127.0.0.1:{port}", f"localhost:{port}"}, {
            "force": args.network, "host": args.p2p_host, "port": args.p2p_port,
            "discovery": not args.no_discovery})
        # log_config=None : uvicorn utilise notre journalisation (fichier) au lieu
        # de la sienne, qui suppose une console
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="warning", access_log=False,
                                               log_config=None))
        thread = threading.Thread(target=server.run, daemon=True, name="coeur-node")
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            instance.alert("Le nœud Cœur IA n'a pas pu démarrer (voir data/logs).")
            return 1

        url = f"http://127.0.0.1:{port}/?t={token}"
        log.info("Cœur IA %s prêt sur le port %s", __version__, port)
        if args.headless:
            print(f"Nœud Cœur IA prêt : {url}", flush=True)
            try:
                while thread.is_alive():
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            from coeur.ui import window
            window.run(url, on_closed=lambda: setattr(server, "should_exit", True))
        server.should_exit = True
        thread.join(timeout=5)
        return 0
    finally:
        instance.release()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:          # lancé sans console (pythonw) : on montre l'erreur
        log.exception("Arrêt inattendu")
        instance.alert(f"Cœur IA s'est arrêté : {exc}\n\nDétails dans data/logs/coeur.log")
        sys.exit(1)
