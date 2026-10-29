"""audio.cpp : audiocpp_cli, le moteur de l'audio (docs/conception/audio.md § 1).

Le dossier du moteur : réglage « audiocpp_dir », sinon celui livré avec les versions signées
(updates/engines.py, comme stable-diffusion.cpp), sinon un audio.cpp posé à la main dans
data/engines/audio.cpp. L'appli Android n'en a pas encore (pas d'archive Android publiée).

Un processus par création : audiocpp_cli charge le modèle, écrit un WAV et s'arrête. Il
n'écoute sur aucun port (audiocpp_server, lui, le ferait : n'importe quel programme de la
machine pourrait s'adresser à lui). Avant, la carte graphique est rendue (engines/gpu_share.py :
les modèles de conversation chargés, sd-server s'il ne calcule pas). Il calcule sur la carte
choisie (réglage « audio_device », « Vulkan:1 » ou « CPU:0 »), sinon la meilleure : jamais celle
intégrée au processeur quand il y en a une autre, le processeur s'il n'y a pas de carte.
"""
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from aiodos import settings
from aiodos.audio import files
from aiodos.cluster.engine import NO_WINDOW
from aiodos.i18n import t
from aiodos.jobs.registry import Interrupted
from aiodos.updates import engines, platforms

log = logging.getLogger("aiodos.audio")

PROGRAM = "audiocpp_cli"
DEVICES_TTL = 1800                # --list-devices réveille la carte d'un portable : rare
_VERSION = re.compile(r"audio\.cpp\s+(\d+\.\d+\.\d+\S*)", re.I)
_COMMIT = re.compile(r"git:\s*([0-9a-f]{6,40})", re.I)
# « Vulkan:1 "NVIDIA GeForce RTX 4070 Laptop GPU" [GPU] »
_DEVICE = re.compile(r'^(?P<backend>[A-Za-z]+):(?P<index>\d+)\s+"(?P<label>[^"]*)"\s+'
                     r'\[(?P<kind>\w+)\]')

_versions = {}                    # (programme, date) -> version, lue une fois par programme
_devices = {"at": 0.0, "key": None, "value": []}


class EngineError(Exception):
    pass


def root() -> Path:
    return engines.root(engines.AUDIO)


def complete(folder: Path) -> bool:
    return engines.complete(folder, engines.AUDIO)


def folder() -> Path | None:
    """Dossier d'audio.cpp s'il contient audiocpp_cli, sinon None : celui du réglage, sinon
    celui livré avec les versions ou joint par l'installeur, sinon un dossier posé à la main."""
    if engines.packaged_only():          # appli Android : seulement ses programmes
        return engines.bundled(engines.AUDIO)
    configured = str(settings.get("audiocpp_dir") or "").strip()
    if configured:
        return Path(configured) if complete(Path(configured)) else None
    delivered = engines.find(engines.AUDIO) or engines.bundled(engines.AUDIO)
    if delivered is not None:
        return delivered
    manual = root()
    return manual if complete(manual) else None


def program() -> Path | None:
    where = folder()
    return where / platforms.executable(PROGRAM) if where is not None else None


def parse_version(text: str) -> str | None:
    """« audio.cpp 0.9.3 / git: 41c1f8ff 2026-10-26 » → « 0.9.3 (41c1f8ff) »."""
    found = _VERSION.search(text or "")
    if not found:
        return None
    commit = _COMMIT.search(text)
    return f"{found.group(1)} ({commit.group(1)})" if commit else found.group(1)


def parse_devices(text: str) -> list:
    """Appareils de --list-devices : [{"backend", "index", "label", "kind"}] (kind : GPU,
    IGPU, CPU)."""
    found = []
    for line in (text or "").splitlines():
        match = _DEVICE.match(line.strip())
        if match:
            found.append({"backend": match["backend"].lower(), "index": int(match["index"]),
                          "label": match["label"], "kind": match["kind"].upper()})
    return found


def best_device(devices: list) -> dict | None:
    """L'appareil qui calculera : celui du réglage s'il existe (« Vulkan:1 », « CPU:0 »),
    sinon la première carte qui n'est pas intégrée au processeur, sinon la première carte,
    sinon None (le processeur)."""
    wanted = str(settings.get("audio_device") or "").strip().lower()
    if wanted:
        return next((d for d in devices if f"{d['backend']}:{d['index']}" == wanted), None)
    cards = [d for d in devices if d["kind"] != "CPU"]
    dedicated = [d for d in cards if d["kind"] == "GPU"]
    return (dedicated or cards or [None])[0]


def _run_quick(path: Path, *args) -> str:
    try:
        done = subprocess.run([str(path), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # i18n: tel quel (journal : info() et devices() ne font que le noter)
        raise EngineError(f"audio.cpp ne répond pas ({exc.__class__.__name__}).") from exc
    return (done.stdout or "") + (done.stderr or "")


def info() -> dict | None:
    """{"folder", "version"} du moteur de cette machine, ou None s'il manque. « --version »
    ne charge aucun pilote, et n'est lu qu'une fois par programme."""
    path = program()
    if path is None:
        return None
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _versions:
        try:
            _versions[key] = parse_version(_run_quick(path, "--version"))
        except EngineError as exc:
            log.warning("audio.cpp illisible dans %s : %s", path.parent, exc)
            return None
    version = _versions[key]
    return {"folder": str(path.parent), "version": version} if version else None


def devices(max_age=DEVICES_TTL) -> list:
    """Appareils de calcul vus par audio.cpp, lus au plus toutes les 30 min."""
    found = info()
    if found is None:
        return []
    if _devices["key"] == found["folder"] and time.monotonic() - _devices["at"] < max_age:
        return _devices["value"]
    try:
        value = parse_devices(_run_quick(program(), "--list-devices"))
    except EngineError as exc:
        log.warning("Appareils d'audio.cpp illisibles : %s", exc)
        return []
    _devices.update(at=time.monotonic(), key=found["folder"], value=value)
    return value


def device_args() -> list:
    """--backend et --device de l'appareil qui calculera (le processeur sans carte)."""
    best = best_device(devices())
    if best is None:
        return ["--backend", "cpu"]
    return ["--backend", best["backend"], "--device", str(best["index"])]


def capabilities() -> dict | None:
    """Ce que cette machine sait calculer en audio (demande de travail) : version d'audio.cpp,
    empreintes des fichiers vérifiés, modèles complets. None sans audio.cpp."""
    found = info()
    if found is None:
        return None
    return {"engine": found["version"], "files": files.verified_hashes(),
            "models": files.available()}


# ------------------------------------------------------------------ un calcul
def run(args: list, stop=None, timeout: float = 1800, log_file: Path | None = None) -> str:
    """audiocpp_cli avec ces arguments, jusqu'au bout : sa sortie (texte). L'arrêt (stop) le
    tue et lève Interrupted ; EngineError s'il manque, dépasse timeout ou échoue."""
    path = program()
    if path is None:
        raise EngineError(t("audio.engine_missing"))
    try:
        process = subprocess.Popen([str(path), *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   cwd=str(path.parent), creationflags=NO_WINDOW)
    except OSError as exc:
        raise EngineError(t("audio.engine_failed", error=exc)) from exc
    output = []
    reader = threading.Thread(target=lambda: output.append(process.stdout.read() or b""),
                              daemon=True, name="aiodos-audio-sortie")
    reader.start()
    began = time.monotonic()
    try:
        while process.poll() is None:
            if stop is not None and stop.wait(0.2):
                raise Interrupted(t("audio.stopped"))
            if stop is None:
                time.sleep(0.2)
            if time.monotonic() - began > timeout:
                raise EngineError(t("audio.too_long", minutes=round(timeout / 60)))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
        reader.join(10)
    text = b"".join(output).decode("utf-8", "replace")
    if log_file is not None:
        try:
            log_file.write_text(text[-200_000:], encoding="utf-8")
        except OSError:
            pass
    if process.returncode != 0:
        last = [line for line in text.splitlines() if line.strip()][-3:]
        raise EngineError(t("audio.engine_failed", error=" / ".join(last)[-400:]
                            or process.returncode))
    return text
