"""Compagnon réseau aiodos-net : relie les machines appairées par Internet (phase 2).

Programme Rust à part (dossier compagnon/, sur Iroh), livré avec les versions publiées
(updates/binaries.py) : ce module le trouve (locate), le lance, le surveille et le
relance s'il s'arrête. Le compagnon n'écoute que sur 127.0.0.1 ; un
jeton tiré au hasard à chaque lancement protège son API (passé par une variable
d'environnement, jamais dans les journaux ni sur la ligne de commande). AIODOS lui donne
la liste des machines appairées (liste blanche) et lui confie les requêtes qui ne
passent pas par le réseau local (cf. route.py). Si AIODOS s'arrête, même brutalement,
le compagnon s'arrête avec lui (il surveille son entrée standard).
"""
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import requests

from aiodos import paths
from aiodos.network.client import error_detail
from aiodos.network.route import NO_PROXY
from aiodos.updates import binaries, launcher
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

log = logging.getLogger("aiodos.net")

EXE = "aiodos-net.exe" if sys.platform == "win32" else "aiodos-net"
READY_TIMEOUT = 30         # premier lancement : création de la clé, ouverture des sockets
API_TIMEOUT = 5
INFO_TTL = 60              # infos Internet (relais, adresses) relues au plus toutes les 60 s
RESTART_DELAYS = (2, 5, 15, 30, 60)      # relances successives après un arrêt inattendu
STABLE_AFTER = 60          # un compagnon qui a tenu une minute repart de la première attente
TOKEN_HEADER = "X-Aiodos-Net-Token"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0     # pas de console qui clignote
_LEVELS = {"ERROR": logging.ERROR, "WARN": logging.WARNING, "INFO": logging.INFO,
           "DEBUG": logging.DEBUG, "TRACE": logging.DEBUG}


class CompanionError(Exception):
    """Compagnon introuvable, arrêté, ou qui refuse une demande."""


BUILT_HERE = "compilé dans compagnon/target"
BUILT_IN_BASE = "compilé dans la version d'origine"


def _built(root) -> Path | None:
    """Le compagnon compilé dans le dossier de code `root` (compagnon/target/release ou debug)."""
    if root is None:
        return None
    for build in ("release", "debug"):
        path = Path(root) / "compagnon" / "target" / build / EXE
        if path.is_file():
            return path
    return None


def _file(path) -> Path | None:
    return Path(path) if path and Path(path).is_file() else None


def _inside(path, folder) -> bool:
    path, folder = (os.path.normcase(os.path.abspath(p)) for p in (path, folder))
    return path.startswith(folder.rstrip(os.sep) + os.sep)


def _delivered(activate: bool, failed: set | None = None):
    """Les programmes livrés (updates/binaries.py), avec leur origine ; chacun mis en
    place au moment où on le prend, avec `activate`. Un programme qui ne peut pas l'être
    (illisible, disque plein…) est passé, et son origine notée dans `failed`."""
    store = Store()
    for version, item in binaries.candidates(store):
        origin = f"livré avec la version {version}"
        if not activate:
            yield origin, lambda version=version, item=item: store.package_path(version, item)
            continue

        def prepare(version=version, item=item, origin=origin):
            try:
                return binaries.activate(store, version, item)
            except (UpdateError, OSError) as exc:
                log.warning("%s", exc)
                if failed is not None:
                    failed.add(origin)
                return None
        yield origin, prepare


def candidates(activate: bool = True, skip=(), failed: set | None = None):
    """(programme, origine) des compagnons possibles, du préféré au dernier recours,
    hors des origines de `skip` (programmes qui n'ont pas démarré ici). `failed` reçoit
    les origines des programmes livrés qui n'ont pas pu être mis en place :

    1. AIODOS_NET_BIN, choix explicite (et alors rien d'autre) ; sauf s'il désigne le
       compagnon compilé dans la version d'origine : un lanceur 2.0.x l'y met pour la
       version qu'il lance, et ce programme, plus ancien, passe après celui livré avec elle ;
    2. dans une copie de développement, celui qu'on y a compilé (compagnon/target) ;
    3. celui livré avec la version qui tourne, puis avec les versions plus anciennes
       (updates/binaries.py) ; avec `activate`, il est d'abord mis en place ;
    4. compilé dans le dossier de cette version, puis dans celui de la version d'origine ;
    5. le PATH (Termux : copié dans ~/bin ou $PREFIX/bin).
    """
    base = launcher.base_dir() if launcher.supervised() else None
    explicit = os.environ.get("AIODOS_NET_BIN")
    if explicit and not (base and _inside(explicit, Path(base) / "compagnon" / "target")):
        if _file(explicit) and "AIODOS_NET_BIN" not in skip:
            yield Path(explicit), "AIODOS_NET_BIN"
        return

    def ordered():                        # (origine, de quoi trouver le programme)
        if launcher.developer_checkout():
            yield BUILT_HERE, lambda: _built(paths.APP_ROOT)
        yield from _delivered(activate, failed)
        yield BUILT_HERE, lambda: _built(paths.APP_ROOT)
        yield BUILT_IN_BASE, lambda: _built(base)
        yield BUILT_IN_BASE, lambda: _file(explicit)            # mis par un lanceur 2.0.x
        yield "PATH", lambda: _file(shutil.which("aiodos-net"))

    for origin, get in ordered():
        if origin not in skip:
            path = get()
            if path:
                yield path, origin


def locate(activate: bool = True, skip=(), failed: set | None = None):
    """(programme, origine) du compagnon à lancer : le premier des candidats hors de
    `skip`, ou None."""
    return next(candidates(activate, skip, failed), None)


def find_binary() -> Path | None:
    """Le compagnon qui serait lancé (cf. locate), sans le mettre en place."""
    found = locate(activate=False)
    return found[0] if found else None


def _terminate(process):
    """Arrête un compagnon dont on ne veut plus : entrée standard fermée (il s'arrête
    proprement), puis tué s'il traîne."""
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(5)


class Companion:
    """Un compagnon lancé par ce nœud.

    options : relays (liste : « n0 », « none », https://…), directory (« n0-dns,dht »,
    « none »…), bind (sockets UDP, ex. « 127.0.0.1:0 » pour les tests), portmap (bool),
    data_dir (dossier de la clé ; défaut : celui d'AIODOS).
    """

    def __init__(self, forward: str, options: dict | None = None):
        options = options or {}
        self.forward = forward
        self.data_dir = options.get("data_dir")
        self.relays = [str(r) for r in options.get("relays") or []]
        self.directory = options.get("directory")
        self.bind = options.get("bind")
        self.portmap = bool(options.get("portmap", True))
        self.process = None
        self.token = None
        self.id = None
        self.listen = None
        self.error = None
        self.binary = None             # programme lancé, et d'où il vient (cf. locate)
        self.origin = None
        self._broken = set()           # origines des programmes qui n'ont pas démarré
        self.generation = 0            # +1 à chaque lancement : liste blanche à renvoyer
        self._stderr = deque(maxlen=20)
        self._lock = threading.Lock()
        self._info = None
        self._stopping = False
        self._started_at = 0.0
        self._watcher = None

    # ------------------------------------------------------------ cycle de vie
    @property
    def running(self) -> bool:
        process = self.process
        return process is not None and process.poll() is None and self.listen is not None

    @property
    def headers(self) -> dict:
        return {TOKEN_HEADER: self.token or ""}

    def proxy_base(self, net_id: str) -> str:
        return f"http://{self.listen}/peer/{net_id}"

    def command(self, binary) -> list:
        data_dir = self.data_dir or paths.data_dir()
        args = [str(binary), "--data-dir", str(data_dir), "--listen", "127.0.0.1:0",
                "--forward", self.forward, "--supervised"]
        for relay in self.relays:
            args += ["--relay", relay]
        if self.directory is not None:
            args += ["--directory", self.directory or "none"]
        if self.bind:
            args += ["--bind", self.bind]
        if not self.portmap:
            args.append("--no-portmap")
        return args

    def start(self) -> bool:
        """Lance le compagnon et attend qu'il soit prêt (bloquant) ; une surveillance le
        relance ensuite s'il s'arrête. Faux si impossible : self.error dit pourquoi."""
        self._stopping = False
        try:
            return self._launch()
        finally:                                # la surveillance, quoi qu'il arrive
            if self._watcher is None or not self._watcher.is_alive():
                self._watcher = threading.Thread(target=self._watch, daemon=True,
                                                 name="aiodos-net-surveillance")
                self._watcher.start()

    def _fail(self, message: str) -> bool:
        """Échec de lancement : au journal une fois, pas à chaque nouvel essai."""
        log.log(logging.DEBUG if message == self.error else logging.WARNING, "%s", message)
        self.error = message
        return False

    def _launch(self) -> bool:
        if self._broken and next(candidates(False, self._broken), None) is None:
            self._broken.clear()                # tous essayés en vain : on recommence
        # Un programme livré qui ne peut pas être mis en place est écarté comme celui qui
        # ne démarre pas : sans quoi outdated() le réclamerait à chaque passage.
        try:
            located = locate(skip=self._broken, failed=self._broken)
        except Exception as exc:                # jamais au point d'arrêter la surveillance
            log.exception("Choix du compagnon aiodos-net")
            return self._fail(f"Compagnon aiodos-net : choix du programme impossible ({exc}).")
        if located is None:
            return self._fail("Compagnon aiodos-net introuvable : il arrive avec les mises à "
                              "jour, d'une machine appairée qui l'a (ou le compiler : voir "
                              "compagnon/README.md).")
        binary, origin = located
        token = secrets.token_urlsafe(24)
        env = dict(os.environ, AIODOS_NET_TOKEN=token)
        self._stderr.clear()
        try:
            process = subprocess.Popen(
                self.command(binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8",
                errors="replace", creationflags=_NO_WINDOW)
        except OSError as exc:
            self._broken.add(origin)          # le suivant sera essayé (cf. locate)
            return self._fail(f"Le compagnon aiodos-net ({origin}) ne se lance pas : {exc}")
        ready = threading.Event()
        found = {}
        threading.Thread(target=self._read_stdout, args=(process, ready, found), daemon=True,
                         name="aiodos-net-sortie").start()
        threading.Thread(target=self._read_stderr, args=(process,), daemon=True,
                         name="aiodos-net-journal").start()
        deadline = time.monotonic() + READY_TIMEOUT
        while not ready.wait(0.1):
            if process.poll() is not None or time.monotonic() > deadline:
                break
        if not ready.is_set():
            if process.poll() is None:
                process.kill()
            process.wait(5)
            time.sleep(0.2)                  # dernières lignes du journal
            last = " / ".join(self._stderr) or f"code {process.returncode}"
            self._broken.add(origin)
            return self._fail(f"Le compagnon aiodos-net ({origin}) n'a pas démarré : {last}")
        with self._lock:
            # Arrêt demandé pendant le lancement : ce compagnon-là ne doit pas rester
            # orphelin (stop() n'a rien trouvé à arrêter, ou l'ancien seulement).
            stopping = self._stopping
            if not stopping:
                self.process, self.token = process, token
                self.id, self.listen = found.get("id"), found.get("listen")
                self.binary, self.origin = binary, origin
                self._broken.discard(origin)
                self.generation += 1
                self._info = None
                self.error = None
                self._started_at = time.monotonic()
        if stopping:
            _terminate(process)
            return False
        build = ""
        if found.get("version"):
            build += f", version {found['version']}"
        if found.get("commit"):
            build += f", commit {str(found['commit'])[:10]}"
        log.info("Compagnon aiodos-net prêt (API locale %s ; %s%s)", self.listen, origin, build)
        return True

    def outdated(self) -> bool:
        """Un programme préféré à celui qui tourne est arrivé depuis son lancement (le
        compagnon livré avec la version, reçu après coup) : il vaut une relance."""
        if not self.running or self.origin is None:
            return False
        found = locate(activate=False, skip=self._broken)
        return found is not None and found[1] != self.origin

    def _read_stdout(self, process, ready, found):
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                log.info("%s", line)
                continue
            if isinstance(event, dict) and event.get("event") == "ready" and not ready.is_set():
                found.update(event)
                ready.set()

    def _read_stderr(self, process):
        """Journal du compagnon (« LEVEL message ») versé dans celui d'AIODOS."""
        for line in process.stderr:
            line = line.rstrip()
            if not line:
                continue
            word, _, message = line.strip().partition(" ")
            level = _LEVELS.get(word)
            if level is None:
                level, message = logging.INFO, line.strip()
            self._stderr.append(message.strip())
            log.log(level, "%s", message.strip())

    def _watch(self):
        """Relance le compagnon s'il s'arrête sans qu'on le lui ait demandé."""
        failures = 0
        while not self._stopping:
            time.sleep(1)
            if self._stopping:
                break
            process = self.process
            if process is not None and process.poll() is None:
                if time.monotonic() - self._started_at > STABLE_AFTER:
                    failures = 0
                continue
            delay = RESTART_DELAYS[min(failures, len(RESTART_DELAYS) - 1)]
            failures += 1
            if process is not None:             # dit une fois ; les essais suivants, par _fail
                log.warning("Le compagnon aiodos-net s'est arrêté (code %s) : relance dans %s s",
                            process.returncode, delay)
                with self._lock:
                    if self.process is process:
                        self.process, self.listen = None, None
            deadline = time.monotonic() + delay
            while not self._stopping and time.monotonic() < deadline:
                time.sleep(0.2)
            if not self._stopping:
                try:
                    self._launch()
                except Exception:               # la surveillance ne s'arrête jamais
                    log.exception("Relance du compagnon aiodos-net")

    def stop(self):
        """Arrêt propre : le compagnon prévient les autres machines, puis s'arrête. Un
        lancement en cours s'arrête de lui-même dès qu'il aboutit (cf. _launch)."""
        with self._lock:
            self._stopping = True
            process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            self.api("POST", "/net/v1/shutdown", timeout=2)
        except CompanionError:
            pass
        _terminate(process)
        with self._lock:
            self.listen = None
        log.info("Compagnon aiodos-net arrêté")

    # ------------------------------------------------------------- API locale
    def api(self, method: str, path: str, body=None, timeout=API_TIMEOUT) -> dict:
        listen = self.listen
        if not listen:
            raise CompanionError("Compagnon arrêté.")
        try:
            response = requests.request(method, f"http://{listen}{path}", json=body,
                                        headers=self.headers, timeout=timeout,
                                        proxies=NO_PROXY)
        except requests.RequestException as exc:
            raise CompanionError(f"Compagnon injoignable ({exc.__class__.__name__}).") from exc
        if response.status_code >= 400:
            raise CompanionError(error_detail(response))
        try:
            return response.json()
        except ValueError as exc:
            raise CompanionError("Réponse illisible du compagnon.") from exc

    def status(self) -> dict:
        return self.api("GET", "/net/v1/status")

    def set_peers(self, entries: list) -> dict:
        """Remplace la liste blanche : [{"id", "relays", "addrs"}…]."""
        return self.api("PUT", "/net/v1/peers", {"peers": entries})

    def info(self) -> dict | None:
        """Clé et indices de cette machine, à donner aux machines appairées."""
        with self._lock:
            cached = self._info
            if cached and cached[0] == self.generation and time.monotonic() - cached[1] < INFO_TTL:
                return cached[2]
        try:
            ticket = self.api("GET", "/net/v1/ticket")
        except CompanionError:
            return None
        info = {"id": ticket.get("id"), "relays": list(ticket.get("relays") or []),
                "addrs": list(ticket.get("addrs") or [])}
        with self._lock:
            self._info = (self.generation, time.monotonic(), info)
        return info
