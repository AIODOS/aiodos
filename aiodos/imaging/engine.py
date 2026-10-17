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
il y en a une autre). Il n'écoute que sur 127.0.0.1, sur un port tiré au hasard : sd-server
n'a pas de clé d'accès.
"""
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

from aiodos import settings
from aiodos.cluster.engine import NO_WINDOW, EngineError, Process, free_port
from aiodos.imaging import catalog
from aiodos.jobs import pacing
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
_devices = {"at": 0.0, "key": None, "value": []}
_lock = threading.Lock()          # un lancement à la fois
_state = {"process": None, "model": None, "busy": 0, "used": 0.0, "reaper": None}


def root() -> Path:
    return engines.root(engines.IMAGES)


def complete(folder: Path) -> bool:
    return engines.complete(folder, engines.IMAGES)


def folder() -> Path | None:
    """Dossier de stable-diffusion.cpp s'il contient sd-server, sinon None : celui du
    réglage, sinon celui livré avec les versions, sinon un dossier posé à la main."""
    configured = str(settings.get("sdcpp_dir") or "").strip()
    if configured:
        return Path(configured) if complete(Path(configured)) else None
    delivered = engines.find(engines.IMAGES)
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
    return {"engine": found["version"], "files": sorted(files), "models": catalog.available()}


# --------------------------------------------------------------- sd-server
def _answers(port: int) -> bool:
    """sd-server a chargé le modèle et répond."""
    try:
        return requests.get(f"http://127.0.0.1:{port}/sdcpp/v1/capabilities", timeout=3,
                            proxies=NO_PROXY).status_code == 200
    except requests.RequestException:
        return False


def arguments(model_id: str, port: int, device: str | None) -> list:
    args = ["--listen-ip", "127.0.0.1", "--listen-port", str(port), "--log-level", "info"]
    for part, path in catalog.files(model_id).items():
        args += [catalog.FLAGS[part], str(path)]
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
            _state["busy"] += 1
            _state["used"] = time.monotonic()
            return f"http://127.0.0.1:{process.port}"
        _stop_locked()
        found = info()
        if found is None:
            raise EngineError("stable-diffusion.cpp n'est pas installé sur cette machine "
                              "(réglage « Dossier de stable-diffusion.cpp »).")
        device = best_device(devices())
        freed = ollama.unload_chat_models()       # la carte aux images, le temps du lot
        if freed:
            log.info("Carte graphique libérée pour les images : %s déchargé(s) d'Ollama",
                     ", ".join(freed))
        port = free_port()
        process = Process(f"sd-server-{port}", Path(found["folder"]) / platforms.executable(SERVER),
                          arguments(model_id, port, device["name"] if device else None), port)
        log.info("Chargement de %s dans sd-server (%s)", model_id,
                 device["label"] if device else "processeur")
        began = time.monotonic()
        process.start(LOAD_TIMEOUT, ready=lambda: _answers(port), stop=stop, waiting=waiting)
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


def _yield_to_chat():
    """Une conversation commence ici : sd-server inoccupé rend aussitôt la carte (une
    image en cours finit d'abord ; _reap l'arrête ensuite)."""
    with _lock:
        if _state["process"] is not None and _state["busy"] == 0:
            log.info("sd-server arrêté : la carte graphique revient à la conversation")
            _stop_locked()


pacing.on_chat(_yield_to_chat)
