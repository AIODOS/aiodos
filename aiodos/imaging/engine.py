"""stable-diffusion.cpp : sd-server, le moteur des images, lancé par AIODOS.

Le dossier du moteur : réglage « sdcpp_dir », sinon celui livré avec les versions signées
(updates/engines.py, comme llama.cpp), sinon un stable-diffusion.cpp posé à la main dans
data/engines/stable-diffusion.cpp.

Un seul sd-server à la fois, pour un modèle : il garde le modèle chargé entre deux images
(le charger prend plusieurs secondes), et s'arrête IDLE_S après la dernière, ou dès
qu'une conversation commence ici sans image en cours : la carte graphique revient à
Ollama. Avant de le lancer, les modèles de conversation d'Ollama sont déchargés de la
carte (essai du 17/10/2026 : Ollama en gardait 4 Go, et la première image échouait faute
de mémoire). Il calcule sur la carte graphique choisie
(réglage « images_device », sinon la meilleure : jamais celle intégrée au processeur quand
il y en a une autre).

sd-server n'a pas de clé d'accès, et il répond à toute page web qui le demande : il
renvoie l'origine de la page dans Access-Control-Allow-Origin, avec Allow-Credentials
(versions f0aac52 et e7b7ec6, revu le 29/10/2026 ; aucune option ne l'en empêche, cf.
sd-server --help). Depuis master-1027 (e7b7ec6), son API sait aussi charger et décharger un
modèle : son dossier de modèles est vide (empty_models), rien d'autre ne peut y être chargé ;
le décharger reste possible, comme le faire tomber (cf. plus bas). Il n'écoute donc
que sur 127.0.0.1, et sur un port que les navigateurs refusent de joindre (listen_port) :
aucune page web, même ouverte sur cette machine, ne peut lui parler (sauf si une stratégie
d'entreprise ou une préférence du navigateur rouvre ce port : Firefox le permet pour
n'importe lequel). Si tous ces ports sont pris, il n'est pas lancé (pas de repli sur un
port quelconque). À chaque demande (acquire), même quand il tourne déjà, AIODOS vérifie
que c'est bien lui, et lui seul, qui écoute sur ce port (_owned) : un autre programme qui
l'aurait pris pendant le chargement du modèle, ou qui s'y serait ajouté, recevrait les
demandes, image de départ d'une retouche comprise ; sinon sd-server est arrêté, et relancé
sur un autre port. serving(base) refait cette vérification pour qui suit un calcul en
cours. Ce qui reste : pendant qu'il tourne, un autre programme de la machine (un autre
compte Windows, une autre appli du téléphone) peut encore s'adresser à lui, lire les
tâches et leurs images (/sdcpp/v1/jobs/…), ou le faire tomber ; il faudrait pour cela une
clé dans sd-server lui-même.
"""
import logging
import os
import random
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from aiodos import paths, settings
from aiodos.cluster.engine import NO_WINDOW, EngineError, Process
from aiodos.i18n import t
from aiodos.imaging import catalog, local_copies
from aiodos.jobs import pacing
from aiodos.jobs.registry import Interrupted
from aiodos.services import ollama
from aiodos.updates import engines, platforms

log = logging.getLogger("aiodos.images")

SERVER = "sd-server"
IDLE_S = 90                       # sd-server arrêté 1 min 30 après la dernière image
LOAD_TIMEOUT = 600                # premier chargement : plusieurs Go lus sur le disque
DEVICES_TTL = 1800                # --list-devices réveille la carte d'un portable : rare
_COMMIT = re.compile(r"commit\s+([0-9a-f]{6,40})", re.I)
_DEVICE = re.compile(r"^(\S+)\t(.+)$")
_INTEGRATED = re.compile(r"(?i)\b(UHD|Iris|Radeon\(TM\) Graphics|Graphics Controller)\b")
NO_PROXY = {"http": None, "https": None}

_versions = {}                    # (programme, date) -> version : lue une fois par programme
_helps = {}                       # (programme, date) -> texte de --help : lu une fois aussi
_devices = {"at": 0.0, "key": None, "value": []}
_lock = threading.Lock()          # un lancement à la fois
_state = {"process": None, "model": None, "busy": 0, "used": 0.0, "reaper": None}


def root() -> Path:
    return engines.root(engines.IMAGES)


def complete(folder: Path) -> bool:
    return engines.complete(folder, engines.IMAGES)


def folder() -> Path | None:
    """Dossier de stable-diffusion.cpp s'il contient sd-server, sinon None : celui du
    réglage, sinon celui livré avec les versions ou joint par l'installeur, sinon un
    dossier posé à la main."""
    if engines.packaged_only():          # appli Android : seulement ses programmes
        return engines.bundled(engines.IMAGES)
    configured = str(settings.get("sdcpp_dir") or "").strip()
    if configured:
        return Path(configured) if complete(Path(configured)) else None
    delivered = engines.find(engines.IMAGES) or engines.bundled(engines.IMAGES)
    if delivered is not None:
        return delivered
    manual = root()
    return manual if complete(manual) else None


def parse_version(text: str) -> str | None:
    """« stable-diffusion.cpp version unknown, commit f0aac52 » → « f0aac52 »."""
    found = _COMMIT.search(text or "")
    return found.group(1) if found else None


def parse_devices(text: str) -> list:
    """Appareils de --list-devices (« nom<TAB>description » par ligne)."""
    devices = []
    for line in (text or "").splitlines():
        found = _DEVICE.match(line.rstrip("\r"))
        if found:
            devices.append({"name": found.group(1), "label": found.group(2).strip()})
    return devices


def best_device(devices: list) -> dict | None:
    """La carte graphique qui calculera : celle du réglage si elle existe, sinon la
    première qui n'est pas intégrée au processeur, sinon la première carte, sinon None
    (processeur)."""
    wanted = str(settings.get("images_device") or "").strip()
    if wanted:
        return next((d for d in devices if d["name"] == wanted), None)
    cards = [d for d in devices if d["name"].upper() != "CPU"]
    dedicated = [d for d in cards if not _INTEGRATED.search(d["label"])]
    return (dedicated or cards or [None])[0]


def _run(program: Path, *args) -> str:
    try:
        done = subprocess.run([str(program), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # i18n: tel quel (journal : info() et devices() ne font que le noter)
        raise EngineError(f"stable-diffusion.cpp ne répond pas ({exc.__class__.__name__}).") from exc
    return (done.stdout or "") + (done.stderr or "")


def info() -> dict | None:
    """{"folder", "version"} du moteur de cette machine, ou None s'il manque. Le profil de
    la machine la demande souvent : « --version » ne charge aucun pilote (la carte d'un
    portable reste en veille), et n'est lu qu'une fois par programme."""
    where = folder()
    if where is None:
        return None
    program = where / platforms.executable(SERVER)
    try:
        key = (str(program), program.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _versions:
        try:
            _versions[key] = parse_version(_run(program, "--version"))
        except EngineError as exc:
            log.warning("stable-diffusion.cpp illisible dans %s : %s", where, exc)
            return None
    version = _versions[key]
    return {"folder": str(where), "version": version} if version else None


def devices(max_age=DEVICES_TTL) -> list:
    """Appareils de calcul vus par stable-diffusion.cpp. « --list-devices » charge les
    pilotes (et réveille la carte graphique d'un portable) : lu au plus toutes les 30 min,
    quand il faut choisir la carte (lancement, vue Images)."""
    found = info()
    if found is None:
        return []
    if _devices["key"] == found["folder"] and time.monotonic() - _devices["at"] < max_age:
        return _devices["value"]
    try:
        value = parse_devices(_run(Path(found["folder"]) / platforms.executable(SERVER),
                                   "--list-devices"))
    except EngineError as exc:
        log.warning("Appareils de stable-diffusion.cpp illisibles : %s", exc)
        return []
    _devices.update(at=time.monotonic(), key=found["folder"], value=value)
    return value


def capabilities() -> dict | None:
    """Ce que cette machine sait calculer en images (demande de travail, profil) : version
    de stable-diffusion.cpp, empreintes des fichiers de modèles vérifiés, modèles complets.
    None sans stable-diffusion.cpp."""
    found = info()
    if found is None:
        return None
    files = {spec["sha256"] for m in catalog.MODELS.values() for spec in m["files"].values()
             if catalog.file_state(spec) == "ok"}
    return {"engine": found["version"], "files": sorted(files), "models": catalog.available(),
            "img2img": True}         # retouche à partir d'une image (jobs/kinds/images.py)


# --------------------------------------------------------------- port d'écoute
# Ports que les navigateurs refusent de joindre (« bad ports » de la norme Fetch, appliqués
# par Chrome, Edge et la fenêtre d'AIODOS, Firefox et Safari), au-dessus de 1024 : un
# programme peut s'y installer sans droits particuliers. 4190 et 6679, ajoutés à la liste
# plus tard, n'y sont pas : un navigateur pas tout à fait à jour les laisserait passer.
# 6566 et 10080 non plus : une stratégie d'entreprise de Chrome et d'Edge
# (ExplicitlyAllowedNetworkPorts) peut les rouvrir.
BROWSER_BLOCKED_PORTS = (1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000,
                         6665, 6666, 6667, 6668, 6669, 6697)
_TCP_TABLE_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122


def listen_port() -> int:
    """Port de sd-server : un port libre parmi BROWSER_BLOCKED_PORTS, tiré au hasard.
    EngineError s'ils sont tous pris : se replier sur un port quelconque rendrait sd-server
    joignable par une page web (un programme de la machine peut les occuper exprès)."""
    for port in random.sample(BROWSER_BLOCKED_PORTS, len(BROWSER_BLOCKED_PORTS)):
        if _free(port):
            return port
    ports = ", ".join(map(str, BROWSER_BLOCKED_PORTS))
    log.warning("Aucun des ports fermés aux navigateurs n'est libre ici (%s) : sd-server "
                "n'est pas lancé", ports)
    raise EngineError(t("imaging.no_safe_port", ports=ports))


def _free(port: int) -> bool:
    """Personne n'écoute sur ce port (toutes adresses), et sd-server pourra s'y installer
    sur 127.0.0.1. Sous Windows, un programme installé sur 0.0.0.0 sans exclusivité
    n'empêche pas de s'installer sur 127.0.0.1 : d'où la table des ports."""
    listening = _listeners(port)
    if listening:
        return False
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    if listening is None:            # ce système ne dit pas qui écoute : on essaie
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return False
        except OSError:
            pass
    return True


def _listeners(port: int):
    """Qui écoute sur ce port TCP, en IPv4 et IPv6 : numéros des programmes sous Windows,
    numéros de sockets (inodes) sous Linux et Android ; None si ce système ne le dit pas
    (Android 10 et après ferment /proc/net aux applis, macOS)."""
    if sys.platform == "win32":
        return _windows_listeners(port)
    if sys.platform.startswith("linux"):
        return _proc_listeners(port)
    return None


def _windows_listeners(port: int):
    import ctypes
    from ctypes import wintypes
    try:
        get = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    except OSError:
        return None
    get.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    get.restype = wintypes.DWORD
    found = set()
    # MIB_TCPROW_OWNER_PID (24 octets : port à 8, programme à 20) et
    # MIB_TCP6ROW_OWNER_PID (56 octets : port à 20, programme à 52)
    for family, row, port_at, pid_at in ((socket.AF_INET, 24, 8, 20),
                                         (socket.AF_INET6, 56, 20, 52)):
        size = wintypes.DWORD(0)
        for _ in range(4):           # la table peut grandir entre deux appels
            table = ctypes.create_string_buffer(max(size.value, 4))
            error = get(table, ctypes.byref(size), False, family,
                        _TCP_TABLE_OWNER_PID_LISTENER, 0)
            if error != _ERROR_INSUFFICIENT_BUFFER:
                break
        if error != 0:
            if family == socket.AF_INET:
                return None
            continue                 # IPv6 absent de cette machine
        for i in range(struct.unpack_from("<I", table, 0)[0]):
            at = 4 + i * row
            if socket.ntohs(struct.unpack_from("<I", table, at + port_at)[0] & 0xFFFF) == port:
                found.add(struct.unpack_from("<I", table, at + pid_at)[0])
    return found


def _proc_listeners(port: int, net: Path = Path("/proc/net")):
    found = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (net / name).read_text(encoding="ascii", errors="replace").splitlines()
        except FileNotFoundError:
            if name == "tcp":
                return None
            continue                 # IPv6 absent
        except OSError:
            return None
        for line in lines[1:]:
            # sl, adresse:port locale, distante, état (0A : à l'écoute), …, inode
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" \
                    and int(fields[1].rpartition(":")[2], 16) == port:
                found.add(fields[9])
    return found


def _sockets_of(pid: int):
    """Ce qui désigne, dans _listeners, ce qu'écoute le programme `pid` ; None si on ne
    peut pas le savoir."""
    if sys.platform == "win32":
        return {pid}
    if sys.platform.startswith("linux"):
        found = set()
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                if target.startswith("socket:["):
                    found.add(target[len("socket:["):-1])
        except OSError:
            return None
        return found
    return None


def _owned(process, port: int) -> bool:
    """sd-server, et lui seul, écoute sur son port : faux tant qu'il charge le modèle ;
    EngineError si un autre programme y écoute (il recevrait les demandes et rendrait ses
    propres images). Vrai sans vérifier quand ce système ne dit pas qui écoute."""
    child = getattr(process, "process", None)
    pid = getattr(child, "pid", None)
    listening = _listeners(port)
    mine = _sockets_of(pid) if pid else None
    if listening is None or mine is None:
        return True
    if listening - mine:
        raise EngineError(t("imaging.port_taken", port=port))
    return bool(listening & mine)


def serving(base: str) -> bool:
    """Le sd-server lancé par AIODOS est-il toujours celui qui répond à `base` ? Pour qui
    suit un calcul (avant la demande, puis à chaque relecture de son état) : s'il s'est
    arrêté en route (un programme de la machine peut le faire tomber, puisqu'il peut lui
    parler), son port est libre, et qui s'y installe répondrait « terminé » avec sa propre
    image. Faux aussi si un autre programme écoute sur ce port."""
    process = _state["process"]
    if process is None or not process.alive or base != f"http://127.0.0.1:{process.port}":
        return False
    try:
        return _owned(process, process.port)
    except EngineError:
        return False


def _still_alone(process) -> bool:
    """sd-server, déjà chargé, écoute-t-il encore seul sur son port ? Revu avant de lui
    confier une nouvelle image : un programme qui s'y serait ajouté (ou qui l'aurait
    remplacé) recevrait sa demande. Faux : il est arrêté puis relancé, sur un autre port."""
    try:
        if _owned(process, process.port):
            return True
        problem = "il n'écoute plus sur son port"                   # i18n: tel quel (journal)
    except EngineError:
        problem = "un autre programme écoute aussi sur son port"    # i18n: tel quel (journal)
    log.warning("sd-server relancé (port %s) : %s", process.port, problem)
    return False


# --------------------------------------------------------------- sd-server
def _answers(port: int) -> bool:
    """sd-server a chargé le modèle et répond."""
    try:
        return requests.get(f"http://127.0.0.1:{port}/sdcpp/v1/capabilities", timeout=3,
                            proxies=NO_PROXY).status_code == 200
    except requests.RequestException:
        return False


def accepts(option: str) -> bool:
    """Le sd-server de cette machine connaît-il cette option (son --help, lu une fois par
    programme) ? Une version plus ancienne (réglage sdcpp_dir) refuserait de démarrer avec
    une option qu'elle ne connaît pas."""
    found = info()
    if found is None:
        return False
    program = Path(found["folder"]) / platforms.executable(SERVER)
    try:
        key = (str(program), program.stat().st_mtime_ns)
    except OSError:
        return False
    if key not in _helps:
        try:
            _helps[key] = _run(program, "--help")
        except EngineError as exc:
            log.warning("Aide de stable-diffusion.cpp illisible : %s", exc)
            return False
    return option in _helps[key]


def empty_models() -> Path:
    """Le dossier de modèles de sd-server (--model-dir) : vide, dans les données d'AIODOS.
    Depuis master-1027, son API charge et décharge des modèles pris dans ce dossier
    (/sdcpp/v1/models/load ; « ./models » par défaut, relatif au dossier courant d'AIODOS) :
    rien d'autre que le modèle d'AIODOS ne peut ainsi y être chargé (cf. le début du module)."""
    folder = paths.data_dir() / "images" / "sd-server-models"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def arguments(model_id: str, port: int, device: str | None) -> list:
    args = ["--listen-ip", "127.0.0.1", "--listen-port", str(port), "--log-level", "info"]
    if accepts("--model-dir"):
        args += ["--model-dir", str(empty_models())]
    for part, path in catalog.files(model_id).items():
        args += [catalog.FLAGS[part], str(path)]
    args += list(catalog.model(model_id).get("server_args") or [])
    if device:
        args += ["--backend", device]
    return args


def acquire(model_id: str, stop=None, waiting=None) -> str:
    """Adresse d'un sd-server prêt pour ce modèle (lancé s'il le faut, l'autre modèle
    arrêté). À rendre avec release() après l'image. `stop` interrompt le chargement ;
    `waiting(secondes)` donne des nouvelles d'un long chargement."""
    with _lock:
        process = _state["process"]
        if process is not None and process.alive and _state["model"] == model_id:
            if _still_alone(process):
                _state["busy"] += 1
                _state["used"] = time.monotonic()
                return f"http://127.0.0.1:{process.port}"
        _stop_locked()
        found = info()
        if found is None:
            raise EngineError(t("imaging.engine_missing"))
        # Un fichier que d'autres comptes peuvent modifier (ancien lien physique, modèle
        # déplacé depuis un dossier commun) : copié d'abord (une fois), sinon sd-server
        # chargerait ce qu'ils y auraient mis ; ModelError, et rien de lancé, si c'est
        # impossible
        specs = catalog.model(model_id)["files"]
        for part, path in catalog.files(model_id).items():
            local_copies.detach(specs[part], path, stop)
        if stop is not None and stop.is_set():
            raise Interrupted()
        device = best_device(devices())
        freed = ollama.unload_chat_models()       # la carte aux images, le temps du lot
        if freed:
            log.info("Carte graphique libérée pour les images : %s déchargé(s) d'Ollama",
                     ", ".join(freed))
        port = listen_port()
        # Un seul journal, réécrit à chaque lancement : sd-server est relancé souvent
        process = Process("sd-server", Path(found["folder"]) / platforms.executable(SERVER),
                          arguments(model_id, port, device["name"] if device else None), port)
        log.info("Chargement de %s dans sd-server (%s)", model_id,
                 device["label"] if device else "processeur")
        began = time.monotonic()
        try:
            process.start(LOAD_TIMEOUT, ready=lambda: _owned(process, port) and _answers(port),
                          stop=stop, waiting=waiting)
        except EngineError:
            process.stop()                        # port pris par un autre : sd-server aussi
            if stop is not None and stop.is_set():
                # Arrêté en route (conversation, utilisateur revenu, AIODOS qui s'arrête) :
                # l'unité est rendue, elle n'a pas échoué
                raise Interrupted() from None
            raise
        log.info("%s prêt en %.1f s", model_id, time.monotonic() - began)
        _state.update(process=process, model=model_id, busy=1, used=time.monotonic())
        if _state["reaper"] is None or not _state["reaper"].is_alive():
            _state["reaper"] = threading.Thread(target=_reap, daemon=True,
                                                name="aiodos-images-arret")
            _state["reaper"].start()
        return f"http://127.0.0.1:{port}"


def release():
    with _lock:
        _state["busy"] = max(0, _state["busy"] - 1)
        _state["used"] = time.monotonic()


def loaded() -> str | None:
    """Modèle chargé en ce moment, s'il y en a un."""
    process = _state["process"]
    return _state["model"] if process is not None and process.alive else None


def _stop_locked():
    process, _state["process"], _state["model"] = _state["process"], None, None
    _state["busy"] = 0
    if process is not None:
        process.stop()


def stop():
    """Arrête sd-server (arrêt d'AIODOS, ou mémoire à rendre)."""
    with _lock:
        _stop_locked()


def _reap():
    while True:
        time.sleep(5)
        with _lock:
            process = _state["process"]
            if process is None:
                _state["reaper"] = None
                return
            if not process.alive:
                _stop_locked()
            elif _state["busy"] == 0 and time.monotonic() - _state["used"] > IDLE_S:
                log.info("sd-server arrêté : plus d'image à calculer")
                _stop_locked()
            elif _state["busy"] == 0 and pacing.chatting():
                log.info("sd-server arrêté : la carte graphique revient à la conversation")
                _stop_locked()


def yield_gpu() -> bool:
    """La carte graphique à un autre moteur (une unité ComfyUI d'une appli,
    engines/gpu_share.py) : sd-server inoccupé s'arrête. → vrai s'il tournait."""
    with _lock:
        if _state["process"] is not None and _state["busy"] == 0:
            _stop_locked()
            return True
    return False


def _yield_to_chat():
    """Une conversation commence ici : sd-server inoccupé rend aussitôt la carte (une
    image en cours finit d'abord ; _reap l'arrête ensuite)."""
    with _lock:
        if _state["process"] is not None and _state["busy"] == 0:
            log.info("sd-server arrêté : la carte graphique revient à la conversation")
            _stop_locked()


pacing.on_chat(_yield_to_chat)
