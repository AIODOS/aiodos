"""Point d'entrée.

    python -m aiodos              → nœud + fenêtre (usage normal)
    python -m aiodos --headless   → nœud seul, sans écran (futures machines de calcul)

D'abord, le lanceur (updates/launcher.py) regarde si une version plus récente a été
installée par mise à jour : elle tourne alors dans son propre processus, que
celui-ci se contente d'accompagner. D'où les imports lourds (FastAPI…) faits ensuite.
"""
import argparse
import io
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from aiodos import __version__, instance, logfiles, paths
from aiodos.updates import launcher, restart

log = logging.getLogger("aiodos")

# Journaux bornés : une requête malformée envoyée en boucle par un appareil du réseau écrit
# une trace d'erreur à chaque fois, et ne doit pas remplir le disque (ni le téléphone)
LOG_BYTES = 10 * 1024 * 1024           # aiodos.log, puis aiodos.log.1 à .3 : 40 Mio au plus
LOG_BACKUPS = 3
ERRORS_BYTES = 2 * 1024 * 1024         # erreurs.log, puis erreurs.log.1 : 4 Mio au plus


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _keep_tail(source: Path, backup: Path, limit: int, before=None):
    """Rotation d'un journal sans le renommer : sa fin (limit octets au plus, à partir d'une
    ligne entière) est copiée dans backup, puis il est vidé. Il reste le même fichier pour
    un autre processus qui l'a ouvert : sous Windows, le lanceur d'une version d'origine
    (updates/launcher.py) garde aiodos.log ouvert et empêcherait de le renommer. Un journal
    énorme laissé par une version d'avant n'est pas recopié en entier. before est appelé
    une fois le journal ouvert (décaler les sauvegardes) : s'il ne peut pas l'être (lecture
    seule, ouvert ailleurs sans partage), OSError, et rien n'a bougé."""
    try:
        journal = open(source, "r+b")
    except FileNotFoundError:             # effacé entre-temps : il repart de zéro
        return
    with journal:
        if before is not None:
            before()
        size = journal.seek(0, os.SEEK_END)
        try:
            journal.seek(max(0, size - limit))
            if size > limit:
                journal.readline()               # la ligne coupée
            with open(backup, "wb") as out:
                shutil.copyfileobj(journal, out)
        except OSError:      # disque plein, sauvegarde ouverte ailleurs : la place d'abord
            pass
        journal.truncate(0)


class _BoundedLog(logging.handlers.RotatingFileHandler):
    """aiodos.log : limit octets au plus, puis aiodos.log.1 à .<backups>. Rotation par
    copie (_keep_tail), qui réussit même quand un autre processus a le fichier ouvert. Si
    elle échoue quand même (fichier en lecture seule, ouvert ailleurs sans partage, projeté
    en mémoire), le journal continue, la retente au plus une fois par minute (RETRY) et se
    tait entre-temps au double de la limite : ni ligne perdue avec une trace « Logging
    error » à chaque enregistrement, ni disque rempli."""
    RETRY = 60.0
    clock = staticmethod(time.monotonic)

    def __init__(self, path: Path, limit: int, backups: int):
        super().__init__(path, maxBytes=limit, backupCount=max(1, backups),
                         encoding="utf-8", errors="replace")
        self.stuck = None                        # moment du dernier échec de la rotation

    def emit(self, record):
        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self._waiting() and self._full():
                return                           # rotation impossible : le disque d'abord
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)

    def shouldRollover(self, record):
        return not self._waiting() and super().shouldRollover(record)

    def _waiting(self) -> bool:
        return self.stuck is not None and self.clock() - self.stuck < self.RETRY

    def _full(self) -> bool:
        if self.stream is None:                  # pas pu être rouvert : rien à écrire
            return True
        try:
            return self.stream.tell() >= 2 * self.maxBytes
        except (OSError, ValueError):
            return True

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        base = self.baseFilename
        try:
            _keep_tail(Path(base), Path(f"{base}.1"), self.maxBytes, before=self._shift)
            self.stream = self._open()
            self.stuck = None
        except OSError as exc:
            first = self.stuck is None
            self.stuck = self.clock()
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    return                       # emit se tait jusqu'au prochain essai
            if first:
                self._note(f"Rotation du journal impossible ({exc}) : nouvel essai chaque "
                           f"minute, et jusque-là les lignes au-delà du double de sa taille "
                           f"normale sont perdues.")

    def _shift(self):
        """aiodos.log.1 → .2 → … ; la plus ancienne est écrasée."""
        base = self.baseFilename
        for i in range(self.backupCount - 1, 0, -1):
            if os.path.exists(f"{base}.{i}"):
                try:
                    os.replace(f"{base}.{i}", f"{base}.{i + 1}")
                except OSError:      # ouverte ailleurs (visionneuse) : écrasée plus tard
                    pass

    def _note(self, text: str):
        """Une ligne du journal écrite par le journal lui-même (sans repasser par emit)."""
        record = logging.makeLogRecord({"name": "aiodos", "levelno": logging.WARNING,
                                        "levelname": "WARNING", "msg": text})
        try:
            self.stream.write(self.format(record) + self.terminator)
            self.stream.flush()
        except (OSError, ValueError):
            pass


_BoundedFile = logfiles.BoundedFile      # erreurs.log (cf. logfiles.py)


def _error_stream(path: Path, limit: int) -> io.TextIOWrapper:
    """La sortie d'erreur sans console (pythonw), ou héritée d'une version relancée :
    erreurs.log borné (_BoundedFile). Une bibliothèque qui y écrit en boucle ne remplit pas
    le disque."""
    return io.TextIOWrapper(io.BufferedWriter(_BoundedFile(path, limit)), encoding="utf-8",
                            errors="replace", line_buffering=True)


def _descriptor(stream) -> int | None:
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return None


def _follow_on_descriptor_2(stream):
    """Une version relancée sous Windows reçoit erreurs.log comme descripteur 2
    (updates/restart.py), sans le mode ajout et à son ancienne position : ce qui y écrit
    directement (code C, faulthandler, erreur fatale) laisserait, après chaque réduction du
    fichier, un trou d'octets nuls jusqu'à cette position. Le descripteur 2 devient une
    copie du nôtre, en mode ajout : il écrit à la fin, et la réduction suivante le compte."""
    try:
        os.dup2(stream.fileno(), 2)
    except (OSError, ValueError, io.UnsupportedOperation):
        pass


def _is_file(stream, path: Path) -> bool:
    """La sortie est-elle ce fichier-là ? (sous Windows, une version relancée par
    updates/restart.py reçoit en sortie d'erreur le erreurs.log de la précédente)"""
    try:
        return path.exists() and os.path.samestat(os.fstat(stream.fileno()), os.stat(path))
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return False


class _Repeats(logging.Filter):
    """Une même alerte ou erreur répétée en boucle (requête malformée envoyée sans arrêt,
    trace d'erreur de plusieurs Kio à chaque fois) n'est écrite que KEPT fois par minute ;
    la suivante le dit, sans sa trace, et le nombre des autres est donné à la reprise. Le
    journal garde ainsi la place des autres événements. Les informations passent toutes."""
    KEPT = 10
    WINDOW = 60.0

    def __init__(self, clock=time.monotonic):
        super().__init__()
        self.clock = clock
        self.lock = threading.Lock()
        self.seen = {}                           # signature → [début, écrites, tues]

    def filter(self, record):
        if record.levelno < logging.WARNING:
            return True
        kept = getattr(record, "aiodos_kept", None)      # déjà décidé (autre destination)
        if kept is None:
            kept = record.aiodos_kept = self._decide(record)
        return kept

    def _decide(self, record) -> bool:
        key = _signature(record)
        now = self.clock()
        with self.lock:
            entry = self.seen.get(key)
            if entry is None or now - entry[0] >= self.WINDOW:
                if len(self.seen) > 1000:
                    self.seen.clear()
                self.seen[key] = [now, 1, 0]
                if entry is not None and entry[2] > 1:
                    record.msg = (f"{str(record.msg).rstrip()} "
                                  f"(et {entry[2] - 1} fois de plus, non écrites)")
                return True
            if entry[1] < self.KEPT:
                entry[1] += 1
                return True
            entry[2] += 1
            if entry[2] > 1:
                return False
        record.msg = f"{str(record.msg).rstrip()} (répétée : plus écrite pendant une minute)"
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


_PLACEHOLDER = re.compile(r"%[-#0 +\d.]*[a-zA-Z]")


def _signature(record) -> tuple:
    """Ce qui fait qu'un message est « le même » : d'où il vient, son modèle (pas ses
    valeurs), et pour une erreur, son type et la ligne qui l'a levée. Un modèle sans texte
    (« %s » : le journal du compagnon, network/companion.py) compte par son contenu."""
    text = str(record.msg)
    if record.args and not _PLACEHOLDER.sub("", text).strip():
        try:
            text = record.getMessage()
        except (TypeError, ValueError):
            pass
    where = None
    if record.exc_info and record.exc_info[0] is not None:
        trace = record.exc_info[2]
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        where = (record.exc_info[0].__name__,
                 trace.tb_frame.f_code.co_filename if trace else "",
                 trace.tb_lineno if trace else 0)
    return (record.name, record.levelno, record.pathname, record.lineno, text[:200], where)


def _setup_logging():
    # Premier geste de tout démarrage (main, et l'appli Android, node/android_app.py) :
    # hors Windows, les fichiers écrits ensuite ne sont lisibles que par l'utilisateur
    left_open = paths.keep_private()
    errors = paths.log_path().with_name("erreurs.log")
    # Lancé par pythonw (double-clic, sans console) : sys.stdout/stderr valent None,
    # et des bibliothèques y écrivent quand même (uvicorn, barres de progression…).
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None or _is_file(sys.stderr, errors):
        inherited = None if sys.stderr is None else _descriptor(sys.stderr)
        sys.stderr = _error_stream(errors, ERRORS_BYTES)
        if inherited == 2:
            _follow_on_descriptor_2(sys.stderr)
    # Console Windows en cp1252 par défaut : « nœud » y devient « n�ud »
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    repeats = _Repeats()
    handlers = [_BoundedLog(paths.log_path(), LOG_BYTES, LOG_BACKUPS)]
    if sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.addFilter(repeats)
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    rights = paths.rights()              # hors Windows : ce qui a vraiment pris ici
    if rights:
        log.info("Droits des données (attendus : dossier 0700, base 0600) : %s", rights)
    if left_open:                        # dit maintenant, pour qu'il soit dans aiodos.log
        shown = ", ".join(left_open[:10]) + (" …" if len(left_open) > 10 else "")
        log.warning("%d élément(s) des données (%s) restent lisibles par d'autres comptes "
                    "de la machine : %s. Leurs droits n'ont pas pu être fermés (disque sans "
                    "droits Unix, fichier d'un autre compte), sauf mention contraire.",
                    len(left_open), paths.data_dir(), shown)


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


def parser() -> argparse.ArgumentParser:
    """Les options de la ligne de commande (l'appli Android part de leurs valeurs par
    défaut, node/android_app.py)."""
    parser = argparse.ArgumentParser(prog="aiodos", description="AIODOS — nœud local")
    parser.add_argument("--headless", action="store_true", help="nœud seul, sans fenêtre")
    parser.add_argument("--background", action="store_true",
                        help="sans ouvrir la fenêtre : l'icône près de l'horloge la rouvre "
                             "(démarrage avec Windows, node/autostart.py)")
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
    return parser


def main():
    args = parser().parse_args()
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
        if args.background:               # démarrage avec Windows : déjà là, rien à montrer
            return launcher.ALREADY_RUNNING
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
                       quit_on_error=os.environ.get(launcher.TRIAL_ENV) == "1",
                       hidden=args.background)
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
