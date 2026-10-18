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
import signal
import socket
import sys
import threading
import time
from pathlib import Path

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
    parser.add_argument("--ui-file", default=None, metavar="FICHIER",
                        help="écrit l'adresse de l'interface, jeton compris, dans ce fichier "
                             "lisible par soi seul, effacé à l'arrêt (application Android)")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["AIODOS_DATA_DIR"] = os.path.abspath(args.data_dir)

    _setup_logging()
    _windows_app_id()
    code = launcher.dispatch(sys.argv[1:])
    if code is not None:
        return code                       # une version plus récente a tourné à notre place
    return _serve(args)


def _stop_on_signals():
    """Nœud sans fenêtre : SIGINT et SIGTERM l'arrêtent proprement (KeyboardInterrupt).
    Lancé en arrière-plan par un script (application Android, service), il hérite d'un
    SIGINT ignoré, et SIGTERM le tuerait sans rendre son verrou ni effacer --ui-file."""
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, interrupt)


def _write_ui_file(path: str, url: str) -> Path:
    """L'adresse de l'interface pour l'application Android, qui la lit par Termux : un
    fichier lisible par soi seul (0600), écrit d'un coup."""
    target = Path(path).expanduser().resolve()
    part = target.with_name(target.name + ".part")
    handle = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(url + "\n")
    os.replace(part, target)
    return target


class NodeError(Exception):
    pass


class StartCancelled(NodeError):
    """La fenêtre a été fermée pendant le démarrage : ce n'est pas un échec (une nouvelle
    version à l'essai n'est pas écartée pour autant)."""


class _Node:
    """Le nœud : la base, l'application et le serveur local de l'interface et de l'API.
    Avec une fenêtre, il démarre hors du fil graphique : elle s'ouvre aussitôt sur un écran
    d'attente (ui/window.py), et l'interface s'y affiche quand il est prêt."""

    def __init__(self, args, port: int, token: str, show_key: str):
        self.args, self.port, self.token, self.show_key = args, port, token, show_key
        self.server = None
        self.thread = None
        self.ui_file = None
        self.failed = False
        self.done = threading.Event()              # démarrage fini, réussi ou non
        self.cancelled = threading.Event()         # fenêtre fermée pendant le démarrage

    def start(self) -> str:
        """Démarre tout et rend l'adresse de l'interface (jeton compris) ; NodeError si le
        serveur ne répond pas."""
        try:
            import uvicorn

            from aiodos import db, migrate_legacy
            from aiodos.api.server import create_app
            from aiodos.knowledge import store

            args, port = self.args, self.port
            self._go_on()
            db.connect()
            store.ensure_defaults()
            migrated = None if args.no_migrate else migrate_legacy.run_if_needed()
            if migrated:
                log.info("Import v1 : %s", migrated)
            self._go_on()
            app = create_app(self.token, {f"127.0.0.1:{port}", f"localhost:{port}"}, {
                "force": args.network, "host": args.p2p_host, "port": args.p2p_port,
                "discovery": not args.no_discovery,
                "internet": args.internet or args.internet_local or bool(args.internet_relay_only),
                "internet_local": args.internet_local,
                "internet_relay_only": args.internet_relay_only}, _parse_slots(args.slots),
                windowed=not args.headless, show_key=self.show_key)
            # log_config=None : uvicorn utilise notre journalisation (fichier) au lieu
            # de la sienne, qui suppose une console
            self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                        log_level="warning", access_log=False,
                                                        log_config=None))
            self.thread = threading.Thread(target=self.server.run, daemon=True,
                                           name="aiodos-node")
            self.thread.start()
            deadline = time.monotonic() + 20
            while not self.server.started and self.thread.is_alive() \
                    and time.monotonic() < deadline and not self.cancelled.is_set():
                time.sleep(0.05)
            self._go_on()
            if not self.server.started:
                self.server.should_exit = True     # il démarrerait sinon derrière l'erreur
                raise NodeError("Le nœud AIODOS n'a pas pu démarrer (voir data/logs).")
            launcher.confirm_when_healthy(self.alive)
            _protect_secrets_later(self.alive)
            url = f"http://127.0.0.1:{port}/?t={self.token}"
            log.info("AIODOS %s prêt sur le port %s", __version__, port)
            if args.ui_file:
                self.ui_file = _write_ui_file(args.ui_file, url)
            return url
        except StartCancelled:
            raise
        except Exception:
            self.failed = True
            raise
        finally:
            self.done.set()

    def _go_on(self):
        """Fenêtre fermée pendant le démarrage : on s'arrête là."""
        if self.cancelled.is_set():
            if self.server is not None:
                self.server.should_exit = True
            raise StartCancelled("Démarrage abandonné : la fenêtre a été fermée.")

    def alive(self) -> bool:
        return (self.thread is not None and self.thread.is_alive()
                and not self.server.should_exit)

    def stop_soon(self):
        self.cancelled.set()                       # pendant le démarrage : il s'interrompt
        if self.server is not None:
            self.server.should_exit = True

    def stop(self):
        """Arrêt complet avant de rendre la main : port réseau rendu, compagnon Internet
        arrêté (jusqu'à 8 s), sinon une nouvelle version les trouverait pris."""
        self.done.wait(timeout=120)                # fenêtre fermée pendant le démarrage
        self.stop_soon()
        if self.thread is not None:
            self.thread.join(timeout=30 if restart.requested() else 5)
        if self.ui_file is not None:
            self.ui_file.unlink(missing_ok=True)


def _protect_secrets_later(alive, delay=60):
    """Les clés enregistrées avant leur protection (node/vault.py) sont protégées une fois
    cette version en bonne santé : une version précédente, relancée par les mises à jour
    si celle-ci s'arrêtait au démarrage, sait encore les lire jusque-là."""
    def run():
        time.sleep(delay)
        if not alive():
            return
        try:
            from aiodos import settings
            from aiodos.network import peers
            done = peers.protect_keys() + settings.protect_secrets()
            if done:
                log.info("%s clé(s) protégée(s) sur le disque (DPAPI)", done)
        except Exception as exc:
            log.warning("Protection des clés impossible : %s", exc)

    threading.Thread(target=run, daemon=True, name="aiodos-coffre").start()


def _serve(args) -> int:
    port = args.port or _free_port()
    token = secrets.token_urlsafe(24)
    show_key = secrets.token_urlsafe(24)        # pour faire revenir la fenêtre, sans le jeton
    if not instance.acquire(port, show_key):
        # Déjà ouvert, fenêtre peut-être cachée dans la zone de notification : elle revient
        if args.headless or not instance.wake_running():
            _alert("AIODOS est déjà ouvert sur cette machine.")
        return launcher.ALREADY_RUNNING

    node = _Node(args, port, token, show_key)
    try:
        if args.headless:
            try:
                url = node.start()
            except NodeError as exc:
                _alert(str(exc))
                return 1
            print(f"Nœud AIODOS prêt : {url}", flush=True)
            _stop_on_signals()
            try:
                while node.alive() and not restart.requested():
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            # La fenêtre d'abord, sur un écran d'attente ; le nœud démarre pendant ce temps
            from aiodos.ui import window
            restart.on_request(window.close)
            window.run(node.start, on_closed=node.stop_soon,
                       quit_on_error=os.environ.get(launcher.TRIAL_ENV) == "1")
    finally:
        node.stop()                               # même après une erreur : le port est rendu
        instance.release()
    if node.failed:
        return 1
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
