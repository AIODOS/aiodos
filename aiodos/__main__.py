"""Point d'entrée.

    python -m aiodos              → nœud + fenêtre (usage normal)
    python -m aiodos --headless   → nœud seul, sans écran (futures machines de calcul)

D'abord, le lanceur (updates/launcher.py) regarde si une version plus récente a été
installée par mise à jour : elle tourne alors dans son propre processus, que
celui-ci se contente d'accompagner. D'où les imports lourds (FastAPI…) faits ensuite.
"""
import argparse
import logging
import os
import secrets
import socket
import sys
import threading
import time

from aiodos import __version__, instance, paths
from aiodos.updates import launcher, restart

log = logging.getLogger("aiodos")


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
    # Console Windows en cp1252 par défaut : « nœud » y devient « n�ud »
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handlers = [logging.FileHandler(paths.log_path(), encoding="utf-8")]
    if sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s")


def _parse_slots(text: str) -> dict:
    slots = {}
    for item in filter(None, (s.strip() for s in text.split(","))):
        name, _, n = item.partition("=")
        slots[name.strip()] = int(n)
    return slots


def _alert(message: str):
    """Message à l'utilisateur, sauf au premier démarrage d'une nouvelle version : si
    elle échoue, le lanceur relance la précédente sans rien demander."""
    if os.environ.get(launcher.TRIAL_ENV) != "1":
        instance.alert(message)


def _windows_app_id():
    """Barre des tâches : notre icône plutôt que celle de Python."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Arkane.Aiodos")
        except (AttributeError, OSError):
            pass


def main():
    parser = argparse.ArgumentParser(prog="aiodos", description="AIODOS — nœud local")
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
    network.add_argument("--internet", action="store_true",
                         help="lance le compagnon aiodos-net (Internet) quel que soit le réglage")
    network.add_argument("--internet-local", action="store_true",
                         help="compagnon en essai local : ni relais, ni annuaire, ni box (tests)")
    network.add_argument("--internet-relay-only", metavar="URL", default=None,
                         help="compagnon en essai par ce relais seulement, jamais en direct, "
                              "sans annuaire ni box (tests)")
    parser.add_argument("--slots", default="", metavar="RESSOURCE=N,…",
                        help="unités simultanées sur ce nœud, ex. « embed=1,gpu=0 »")
    parser.add_argument("--no-migrate", action="store_true",
                        help="ne pas importer les données de la v1 (tests)")
    parser.add_argument("--data-dir", default=None,
                        help="dossier de données (plusieurs nœuds sur une même machine)")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["AIODOS_DATA_DIR"] = os.path.abspath(args.data_dir)

    _setup_logging()
    _windows_app_id()
    code = launcher.dispatch(sys.argv[1:])
    if code is not None:
        return code                       # une version plus récente a tourné à notre place
    return _serve(args)


def _serve(args) -> int:
    import uvicorn

    from aiodos import db, migrate_legacy
    from aiodos.api.server import create_app
    from aiodos.knowledge import store

    port = args.port or _free_port()
    token = secrets.token_urlsafe(24)
    if not instance.acquire(port, token):
        # Déjà ouvert, fenêtre peut-être cachée dans la zone de notification : elle revient
        if args.headless or not instance.wake_running():
            _alert("AIODOS est déjà ouvert sur cette machine.")
        return launcher.ALREADY_RUNNING

    try:
        db.connect()
        store.ensure_defaults()
        migrated = None if args.no_migrate else migrate_legacy.run_if_needed()
        if migrated:
            log.info("Import v1 : %s", migrated)

        app = create_app(token, {f"127.0.0.1:{port}", f"localhost:{port}"}, {
            "force": args.network, "host": args.p2p_host, "port": args.p2p_port,
            "discovery": not args.no_discovery,
            "internet": args.internet or args.internet_local or bool(args.internet_relay_only),
            "internet_local": args.internet_local,
            "internet_relay_only": args.internet_relay_only}, _parse_slots(args.slots),
            windowed=not args.headless)
        # log_config=None : uvicorn utilise notre journalisation (fichier) au lieu
        # de la sienne, qui suppose une console
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="warning", access_log=False,
                                               log_config=None))
        thread = threading.Thread(target=server.run, daemon=True, name="aiodos-node")
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            _alert("Le nœud AIODOS n'a pas pu démarrer (voir data/logs).")
            return 1
        launcher.confirm_when_healthy(lambda: thread.is_alive() and not server.should_exit)

        url = f"http://127.0.0.1:{port}/?t={token}"
        log.info("AIODOS %s prêt sur le port %s", __version__, port)
        if args.headless:
            print(f"Nœud AIODOS prêt : {url}", flush=True)
            try:
                while thread.is_alive() and not restart.requested():
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            from aiodos.ui import window
            restart.on_request(window.close)
            window.run(url, on_closed=lambda: setattr(server, "should_exit", True))
        server.should_exit = True
        # Avant de passer la main, l'arrêt doit être complet : port réseau rendu,
        # compagnon Internet arrêté (jusqu'à 8 s), sinon la nouvelle version les trouve pris
        thread.join(timeout=30 if restart.requested() else 5)
    finally:
        instance.release()
    if restart.requested():               # nouvelle version installée : on passe la main
        return restart.relaunch(sys.argv[1:])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:          # lancé sans console (pythonw) : on montre l'erreur
        log.exception("Arrêt inattendu")
        _alert(f"AIODOS s'est arrêté : {exc}\n\nDétails dans data/logs/aiodos.log")
        sys.exit(1)
