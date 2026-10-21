"""Les programmes de llama.cpp : llama-server (la machine qui pose la question) et
ggml-rpc-server (les machines qui prêtent leur carte graphique).

Le dossier du moteur : réglage « llamacpp_dir », sinon celui livré avec les mises à jour
signées (delivery.py). Toutes les machines d'un groupe doivent avoir la même version
(« build ») : le protocole RPC change d'une version à l'autre.

Les deux programmes n'écoutent que sur 127.0.0.1 : le serveur RPC n'a ni mot de passe
ni chiffrement, il n'est joint que par un tunnel du compagnon ; llama-server exige une
clé tirée au hasard à chaque lancement. Chez la machine qui emprunte, le tunnel ne relaie
que ce llama-server-là (son numéro de programme est donné au compagnon dès son lancement,
cf. Process.start et cluster/group.py). Chez la machine qui prête, le compagnon tient le
serveur RPC : il y garde ses propres connexions, et aucun autre programme de la machine
n'y est servi (cf. cluster/helper.py).

La clé de llama-server passe par son environnement (LLAMA_API_KEY), jamais par sa ligne de
commande, que tout compte de la machine lit (/proc/<pid>/cmdline, ps ; faille 20 de l'audit
du 20/10/2026) ; une fois le programme prêt, Process.start vérifie qu'il l'exige bien (401
sans elle), sinon il est arrêté : un llama.cpp qui ne la lirait pas tournerait ouvert. Leur
sortie va dans un journal borné (logfiles.capture : ENGINE_LOG_BYTES, puis <nom>.log.1) ;
seuls les KEEP_LOGS plus récents journaux des programmes d'avant sont gardés (faille 11).
"""
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests

from aiodos import logfiles, paths, settings
from aiodos.cluster import delivery
from aiodos.updates import engines
from aiodos.node import children

log = logging.getLogger("aiodos.cluster")

EXE = ".exe" if os.name == "nt" else ""
SERVER = "llama-server"
RPC = "ggml-rpc-server"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
INFO_TTL = 300                    # versions et appareils relus au plus toutes les 5 min
WAIT_NOTE = 5                     # s entre deux nouvelles d'un chargement en cours
KEY_VARIABLE = "LLAMA_API_KEY"    # la clé exigée par llama-server, lue dans son environnement
ENGINE_LOG_BYTES = 4 * 1024 ** 2  # journal d'un programme, puis <nom>.log.1 : 8 Mio au plus
# Journaux d'anciens programmes gardés, par rôle : un nom par port (llama-rpc-50123…), car
# plusieurs tournent parfois ensemble, et un prêt ou un groupe nouveau en lance un autre
KEEP_LOGS = 4
_ROLE = re.compile(r"^(.+)-\d+$")       # « llama-rpc-50123 » : le rôle « llama-rpc »
NO_PROXY = {"http": None, "https": None}
_live = set()                           # journaux des programmes en marche : jamais effacés
_live_lock = threading.Lock()
_VERSION = re.compile(r"version:\s*(\S+)\s*\(build (\d+), commit (\w+)\)")
_DEVICE = re.compile(r"^\s*([A-Za-z]+\d+):\s*(.+?)\s*\((\d+) MiB, (\d+) MiB free\)\s*$")

_cache = {"at": 0.0, "key": None, "info": None}
_cache_lock = threading.Lock()


class EngineError(Exception):
    pass


def folder() -> Path | None:
    """Dossier de llama.cpp s'il contient les deux programmes, sinon None : celui du
    réglage, sinon celui livré avec les versions (delivery.py) ou joint par l'installeur,
    sinon un llama.cpp posé à la main dans data/engines/llama.cpp."""
    if engines.packaged_only():          # appli Android : seulement ses programmes
        return engines.bundled(engines.LLAMA)
    configured = str(settings.get("llamacpp_dir") or "").strip()
    if configured:
        return Path(configured) if delivery.complete(Path(configured)) else None
    delivered = delivery.find() or engines.bundled(engines.LLAMA)
    if delivered is not None:
        return delivered
    manual = delivery.root()
    return manual if delivery.complete(manual) else None


def parse_version(text: str) -> str | None:
    """« version: 0.7.0-dev (build 11707, commit 1268edac5) » → « 11707-1268edac5 »."""
    found = _VERSION.search(text or "")
    return f"{found.group(2)}-{found.group(3)}" if found else None


def parse_devices(text: str) -> list:
    """Appareils listés par --list-devices : nom, description, mémoire totale et libre."""
    devices = []
    for line in (text or "").splitlines():
        found = _DEVICE.match(line)
        if found:
            devices.append({"name": found.group(1), "label": found.group(2),
                            "total_mib": int(found.group(3)), "free_mib": int(found.group(4))})
    return devices


def _run(program: Path, *args, timeout=30) -> str:
    try:
        done = subprocess.run([str(program), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineError(f"llama.cpp ne répond pas ({exc.__class__.__name__}).") from exc
    return (done.stdout or "") + (done.stderr or "")


def info(max_age=INFO_TTL) -> dict | None:
    """{"folder", "version", "devices"} du moteur de cette machine, ou None s'il manque."""
    where = folder()
    if where is None:
        return None
    with _cache_lock:
        if _cache["key"] == str(where) and time.monotonic() - _cache["at"] < max_age:
            return _cache["info"]
    server = where / f"{SERVER}{EXE}"
    try:
        version = parse_version(_run(server, "--version"))
        devices = parse_devices(_run(server, "--list-devices"))
    except EngineError as exc:
        log.warning("llama.cpp illisible dans %s : %s", where, exc)
        return None
    found = {"folder": str(where), "version": version, "devices": devices} if version else None
    with _cache_lock:
        _cache.update(at=time.monotonic(), key=str(where), info=found)
    return found


def best_device(devices: list) -> dict | None:
    """La carte graphique qui a le plus de mémoire libre (pas celle intégrée au processeur
    quand il y en a une autre : elle partage la mémoire vive)."""
    cards = sorted(devices, key=lambda d: d["free_mib"], reverse=True)
    dedicated = [d for d in cards if not re.search(r"(?i)\b(UHD|Iris|Radeon\(TM\) Graphics|"
                                                    r"Graphics Controller)\b", d["label"])]
    return (dedicated or cards or [None])[0]


def lent_device(devices: list) -> dict | None:
    """La carte que cette machine prête : celle du réglage « lend_gpu_device » si elle
    existe (« CPU » compris), sinon la meilleure."""
    wanted = str(settings.get("lend_gpu_device") or "").strip()
    if not wanted:
        return best_device(devices)
    if wanted.upper() == "CPU":
        return {"name": "CPU", "label": "processeur", "total_mib": 0, "free_mib": _free_ram_mib()}
    return next((d for d in devices if d["name"] == wanted), None)


def _free_ram_mib() -> int:
    """Mémoire vive libre (le processeur prêté garde les poids en mémoire vive)."""
    from aiodos.node import identity       # ici : identity importe ce module
    return int((identity.memory().get("available") or 0) / 2**20)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def requires_key(port: int) -> bool:
    """llama-server exige-t-il sa clé (401 sans elle, sur /props) ? Vérifié avec le
    llama.cpp livré (routeur et serveur réparti) : 401 dès qu'il est prêt, 200 avec la clé,
    et 200 sans clé s'il n'en a reçu aucune."""
    try:
        return requests.get(f"http://127.0.0.1:{port}/props", timeout=5,
                            proxies=NO_PROXY).status_code == 401
    except requests.RequestException:
        return False


def _tidy_logs(name: str):
    """Journaux des programmes d'avant de ce rôle (un nom par port) : les KEEP_LOGS plus
    récents restent, ceux des programmes en marche aussi, les autres sont effacés."""
    role = _ROLE.match(name)
    if role is None:
        return                            # un nom fixe (llama-routeur, sd-server) : réécrit
    folder = paths.log_path().parent
    with _live_lock:
        live = set(_live)
    stems = {}
    try:
        for path in folder.glob(f"{role.group(1)}-*.log*"):
            stem = path.name.split(".log", 1)[0]
            if _ROLE.match(stem) and _ROLE.match(stem).group(1) == role.group(1):
                stems.setdefault(stem, []).append(path)
    except OSError:
        return

    def newest(stem):
        try:
            return max(p.stat().st_mtime for p in stems[stem])
        except OSError:
            return 0.0
    old = [stem for stem in sorted(stems, key=newest, reverse=True)
           if folder / f"{stem}.log" not in live]
    for stem in old[KEEP_LOGS:]:
        for path in stems[stem]:
            try:
                path.unlink()
            except OSError:
                pass                      # ouvert ailleurs : effacé une autre fois


class Process:
    """Un programme de llama.cpp lancé par AIODOS, journal borné dans data/logs/. Une clé
    dans `env` (KEY_VARIABLE) est exigée de lui : vérifiée une fois prêt (requires_key)."""

    def __init__(self, name: str, program: Path, args: list, port: int, env: dict | None = None):
        self.name, self.program, self.args, self.port = name, program, args, port
        self.env = env
        self.log_path = paths.log_path().parent / f"{name}.log"
        self.process = None
        self._journal = None              # fil qui recopie sa sortie (logfiles.capture)

    def start(self, timeout: float, ready=None, stop=None, waiting=None,
              spawned=None) -> "Process":
        """Lance le programme et attend qu'il écoute sur son port, puis que `ready()` soit
        vrai s'il est donné (llama-server écoute avant d'avoir chargé le modèle). `stop`
        (threading.Event, bouton Stop d'une conversation) interrompt l'attente : le
        programme est arrêté. `waiting(secondes)` est appelé toutes les WAIT_NOTE secondes
        d'attente (avancement montré dans la conversation). `spawned(pid)` est appelé dès
        le lancement, avant toute attente (tunnels réservés à ce programme) : s'il échoue,
        le programme est arrêté et l'erreur remonte. Une clé donnée (KEY_VARIABLE dans
        `env`) doit être exigée une fois le programme prêt : sinon il est arrêté."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        _tidy_logs(self.name)
        try:
            self.process = subprocess.Popen(
                [str(self.program), *self.args], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env={**os.environ, **(self.env or {})}, creationflags=NO_WINDOW)
        except OSError as exc:
            raise EngineError(f"{self.program.name} ne démarre pas : {exc}") from exc
        with _live_lock:
            _live.add(self.log_path)
        self._journal = logfiles.capture(self.process.stdout, self.log_path, ENGINE_LOG_BYTES)
        children.bind(self.process.pid)   # arrêté avec AIODOS, même si AIODOS est tué
        if spawned is not None:
            try:
                spawned(self.process.pid)
            except BaseException:
                self.stop()
                raise
        began = time.monotonic()
        end, noted = began + timeout, began
        while time.monotonic() < end:
            if waiting is not None and time.monotonic() - noted >= WAIT_NOTE:
                noted = time.monotonic()
                waiting(int(noted - began))
            process = self.process            # None : arrêté entre-temps (groupe défait)
            if process is None:
                raise EngineError("Chargement interrompu.")
            if process.poll() is not None:
                raise EngineError(f"{self.program.name} s'est arrêté : {self.tail()}")
            if stop is not None and stop.is_set():
                self.stop()
                raise EngineError("Chargement interrompu.")
            if _listening(self.port) and (ready is None or ready()):
                if (self.env or {}).get(KEY_VARIABLE) and not requires_key(self.port):
                    self.stop()
                    raise EngineError(
                        f"{self.program.name} n'exige pas sa clé ({KEY_VARIABLE}) : cette "
                        "version de llama.cpp ne la lit pas, il serait ouvert à tout programme "
                        "de cette machine. Il est arrêté : mets le moteur à jour.")
                return self
            time.sleep(0.25)
        self.stop()
        raise EngineError(f"{self.program.name} ne répond pas après {int(timeout)} s.")

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def tail(self, lines=4) -> str:
        journal = self._journal
        if journal is not None and (self.process is None or self.process.poll() is not None):
            journal.join(2)               # ses dernières lignes, écrites par le fil du journal
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "journal illisible"
        return " | ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())

    def stop(self):
        process, self.process = self.process, None
        with _live_lock:
            _live.discard(self.log_path)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def rpc_server(where: Path, device: str, cache_dir: Path) -> Process:
    """Serveur RPC prêtant `device` (« Vulkan1 », « CUDA0 », « CPU »…), poids gardés en
    cache dans `cache_dir` : un modèle déjà reçu une fois ne retraverse pas le réseau."""
    port = free_port()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return Process(f"llama-rpc-{port}", where / f"{RPC}{EXE}",
                   ["--host", "127.0.0.1", "--port", str(port), "--device", device, "--cache"],
                   port, env={"LLAMA_CACHE": str(cache_dir)})


def llama_server(where: Path, model: Path, rpc_ports: list, devices: str, split: str,
                 api_key: str, context: int, layers: int) -> Process:
    """llama-server qui répartit `model` entre ses appareils (`devices`, dans l'ordre du
    découpage, ex. « RPC0/Vulkan1 ») selon les proportions `split` (ex. « 5/3 »)."""
    port = free_port()
    # --rpc avant --device : les appareils RPC0, RPC1… n'existent qu'une fois déclarés
    args = ["--rpc", ",".join(f"127.0.0.1:{p}" for p in rpc_ports)] if rpc_ports else []
    args += ["--model", str(model), "--host", "127.0.0.1", "--port", str(port),
             "--ctx-size", str(context), "--parallel", "1",
             "--n-gpu-layers", str(layers), "--device", devices.replace("/", ","),
             "--tensor-split", split.replace("/", ","), "--no-webui"]
    # La clé par l'environnement, pas la ligne de commande (lisible par tout compte)
    return Process(f"llama-server-{port}", where / f"{SERVER}{EXE}", args, port,
                   env={KEY_VARIABLE: api_key})


def loaded(port: int) -> bool:
    """llama-server a fini de charger le modèle (/health répond 200, sans clé)."""
    try:
        return requests.get(f"http://127.0.0.1:{port}/health", timeout=2,
                            proxies={"http": None, "https": None}).status_code == 200
    except requests.RequestException:
        return False
