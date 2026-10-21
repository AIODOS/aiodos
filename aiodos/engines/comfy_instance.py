"""Le ComfyUI que lance AIODOS quand celui de l'utilisateur n'est pas ouvert
(docs/conception/partage-puissance.md § 3.7, livraison L2c) : l'instance « mine », pour les
travaux des applis de ce PC et de mes machines. Seulement si l'utilisateur l'a permis
(engines_comfyui_launch, en plus de engines_comfyui_enabled).

- Lancée à la demande (une unité attend), sans fenêtre, sur un port de 127.0.0.1 tiré au
  hasard ; arrêtée IDLE_S après sa dernière unité, et avec AIODOS : elle est créée suspendue,
  rangée dans l'objet de tâche d'AIODOS (node/children.bind), puis relancée. Le Python d'un
  .venv (Desktop) n'est qu'un relais qui lance celui de standalone-env : créé suspendu, son
  enfant naît déjà dans l'objet de tâche.
- Ses dossiers : data/engines/comfyui/mine (entrées, sorties, temporaire, utilisateur), une
  base en mémoire. Les modèles de l'utilisateur en lecture (models.yaml : dossier partagé de
  Desktop, dossier models de l'installation) et ses nœuds ajoutés (custom_nodes de
  l'installation) : ce sont ses travaux, sur ses machines.
- Jamais --enable-manager ; --disable-api-nodes (nœuds qui appellent un service en ligne) ;
  environnement nettoyé (PYTHONPATH, PYTHONHOME, PYTHONSTARTUP, PYTHONUSERBASE retirés) et
  hors ligne pour Hugging Face : AIODOS ne télécharge rien.
- Son inventaire (version, cœur, classes) est gardé d'un lancement à l'autre
  (inventory.json) : la machine peut annoncer ce qu'elle sait faire sans le relancer, et ses
  modèles se lisent sur le disque (disk_models).
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from aiodos import logfiles, paths, settings
from aiodos.engines import comfy_find
from aiodos.i18n import t
from aiodos.node import children

log = logging.getLogger("aiodos.engines")

IDLE_S = 600
LAUNCH_TIMEOUT = 300             # premier lancement : torch, CUDA, nœuds ajoutés
LOG_BYTES = 4 * 1024 ** 2
DISK_TTL = 60.0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_SUSPENDED = 0x00000004
REMOVED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
               "DO_NOT_TRACK": "1"}
MODEL_FOLDERS = ("checkpoints", "vae", "loras", "clip", "text_encoders", "diffusion_models",
                 "unet", "upscale_models", "embeddings", "controlnet", "clip_vision",
                 "style_models", "hypernetworks", "gligen", "photomaker", "model_patches",
                 "audio_encoders", "latent_upscale_models", "vae_approx", "diffusers")
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
DEPTH = 4

_lock = threading.Lock()
_state = {"process": None, "url": None, "used": 0.0, "reaper": None, "journal": None}
_disk = {"at": 0.0, "value": None}


class Unavailable(Exception):
    """ComfyUI ne peut pas être lancé ici (pas d'installation, échec du démarrage)."""


# ------------------------------------------------------------------ réglages
def enabled() -> bool:
    return bool(settings.get("engines_comfyui_enabled")) and \
        bool(settings.get("engines_comfyui_launch"))


def install():
    """L'installation à lancer : la préférée qui a son Python."""
    return next((i for i in comfy_find.installs() if i.python is not None), None)


def launchable() -> bool:
    return enabled() and install() is not None


def base_dir() -> Path:
    base = paths.data_dir() / "engines" / "comfyui" / "mine"
    for part in ("input", "output", "temp", "user", "custom_nodes"):
        (base / part).mkdir(parents=True, exist_ok=True)
    return base


# ----------------------------------------------------------------- modèles
def model_roots(found) -> list:
    """Les dossiers de modèles de l'utilisateur : celui de Desktop (partagé entre ses
    installations), ceux que connaît AIODOS, celui de l'installation."""
    from aiodos.imaging import local_copies
    roots, seen = [], set()
    for root in [folder for folder, _ in local_copies._known()] + [found.comfyui / "models"]:
        key = os.path.normcase(str(root))
        if key not in seen and Path(root).is_dir():
            seen.add(key)
            roots.append(Path(root))
    return roots


def _quoted(path: Path) -> str:
    """Une chaîne YAML entre apostrophes : les barres obliques inverses y restent telles
    quelles."""
    return "'" + str(path).replace("'", "''") + "'"


def write_models_yaml(found, base: Path) -> Path:
    lines = []
    for n, root in enumerate(model_roots(found)):
        folders = [f for f in MODEL_FOLDERS if (root / f).is_dir()]
        if not folders:
            continue
        lines.append(f"modeles_{n}:")
        lines.append(f"    base_path: {_quoted(root)}")
        lines.extend(f"    {f}: {f}/" for f in folders)
    nodes = found.comfyui / "custom_nodes"
    if nodes.is_dir():                       # les nœuds ajoutés de l'utilisateur
        lines.append("noeuds_ajoutes:")
        lines.append(f"    base_path: {_quoted(found.comfyui)}")
        lines.append("    custom_nodes: custom_nodes/")
    target = base / "models.yaml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def disk_models() -> dict:
    """{dossier: [noms relatifs]} des modèles de l'utilisateur, lus sur le disque (gardé
    DISK_TTL s) : ce que l'instance verrait, sans la lancer."""
    now = time.monotonic()
    with _lock:
        if _disk["value"] is not None and now - _disk["at"] < DISK_TTL:
            return _disk["value"]
    found = install()
    out = {}
    if found is not None:
        for root in model_roots(found):
            for folder in MODEL_FOLDERS:
                top = root / folder
                if not top.is_dir():
                    continue
                names = out.setdefault(folder, [])
                for path in _walk(top, DEPTH):
                    name = path.relative_to(top).as_posix()
                    if name not in names:
                        names.append(name)
    with _lock:
        _disk.update(at=now, value=out)
    return out


def _walk(top: Path, depth: int):
    try:
        entries = list(os.scandir(top))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False) and depth > 1:
                yield from _walk(Path(entry.path), depth - 1)
            elif entry.is_file() and entry.name.lower().endswith(MODEL_EXT):
                yield Path(entry.path)
        except OSError:
            continue


# --------------------------------------------------------------- inventaire
def save_inventory(inv: dict):
    """Ce que ComfyUI sait faire, gardé pour l'annoncer sans le relancer."""
    data = {"version": inv.get("version"), "core": inv.get("core"),
            "vram_mib": inv.get("vram_mib"), "classes": sorted(inv.get("classes") or {})}
    try:
        (base_dir() / "inventory.json").write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        log.debug("Inventaire de ComfyUI non gardé", exc_info=True)


def cached_inventory() -> dict | None:
    try:
        data = json.loads((base_dir() / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    found = install()
    if not isinstance(data, dict) or found is None or data.get("version") != found.version:
        return None                          # une autre version : à relire en la lançant
    return data


# ------------------------------------------------------------------ lancer
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def command(found, port: int, base: Path, models_yaml: Path) -> list:
    return [str(found.python), "-s", str(found.comfyui / "main.py"), "--listen", "127.0.0.1",
            "--port", str(port), "--base-directory", str(base),
            "--database-url", "sqlite:///:memory:", "--extra-model-paths-config",
            str(models_yaml), "--disable-auto-launch", "--disable-api-nodes",
            "--max-upload-size", "100"]


def environment() -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() not in REMOVED_ENV}
    env.update(OFFLINE_ENV)
    return env


def _spawn(argv: list, cwd: Path, log_path: Path):
    """Lancé suspendu, rangé dans l'objet de tâche d'AIODOS, puis relancé (Windows) : ce
    qu'il lancera lui-même (l'enfant du relais .venv) y naît aussi."""
    windows = sys.platform == "win32"
    flags = NO_WINDOW | (CREATE_SUSPENDED if windows else 0)
    process = subprocess.Popen(argv, cwd=str(cwd), env=environment(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               creationflags=flags)
    children.bind(process.pid)
    if windows:
        import ctypes
        ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(int(process._handle)))
    journal = logfiles.capture(process.stdout, log_path, LOG_BYTES)
    return process, journal


def _alive() -> bool:
    process = _state["process"]
    return process is not None and process.poll() is None


def current():
    """L'instance d'AIODOS si elle tourne et répond, sinon None."""
    if not _alive() or not _state["url"]:
        return None
    return comfy_find.probe(_state["url"])


def ensure(stop=None, timeout: float = LAUNCH_TIMEOUT):
    """L'instance prête (lancée s'il le faut) → comfy_find.Instance ; Unavailable sinon."""
    with _lock:
        if _alive() and _state["url"]:
            running = comfy_find.probe(_state["url"])
            if running is not None:
                _state["used"] = time.monotonic()
                return running
        _stop_locked()
        found = install()
        if found is None:
            raise Unavailable(t("comfy.no_install"))
        base = base_dir()
        port = _free_port()
        log_path = paths.log_path().parent / "comfyui.log"
        log.info("Lancement de ComfyUI %s sans fenêtre (port %s)", found.version or "", port)
        try:
            process, journal = _spawn(command(found, port, base, write_models_yaml(found, base)),
                                      found.comfyui, log_path)
        except OSError as exc:
            raise Unavailable(t("comfy.start_failed", error=str(exc))) from exc
        _state.update(process=process, journal=journal, url=f"http://127.0.0.1:{port}",
                      used=time.monotonic())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            stop_instance()
            raise Unavailable(t("comfy.launch_interrupted"))
        if not _alive():
            raise Unavailable(t("comfy.stopped_at_start", log=_tail(log_path)))
        running = comfy_find.probe(_state["url"])
        if running is not None:
            _start_reaper()
            log.info("ComfyUI prêt : %s", running.url)
            return running
        time.sleep(1.0)
    stop_instance()
    raise Unavailable(t("comfy.no_answer_after", seconds=int(timeout)))


def _tail(path: Path, lines: int = 3) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return t("comfy.log_unreadable")
    return " | ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())


def touch():
    _state["used"] = time.monotonic()


def learn():
    """Lance l'instance le temps de lire ce que ComfyUI sait faire (version, classes), pour
    l'annoncer à mes machines sans la garder lancée : arrêtée aussitôt si elle ne servait
    pas déjà. Appelé quand l'utilisateur permet le lancement, et par « Chercher de nouveau »."""
    from aiodos.engines import comfy_client, comfyui
    was_running = current() is not None
    found = ensure()
    try:
        save_inventory(comfyui.inventory(found.url, refresh=True))
        log.info("Ce que sait faire ComfyUI %s : lu et gardé", found.version or "")
    except comfy_client.Unreachable as exc:
        log.warning("ComfyUI lancé, mais son inventaire est illisible : %s", exc)
    finally:
        if not was_running and not _busy():
            stop_instance()


def learn_soon():
    """learn() dans un fil à part, si le lancement est permis et l'inventaire inconnu."""
    if launchable() and cached_inventory() is None:
        def run():
            try:
                learn()
            except Unavailable as exc:
                log.warning("ComfyUI n'a pas pu être lancé : %s", exc)
            except Exception:
                log.exception("Lecture de ComfyUI impossible")
        threading.Thread(target=run, daemon=True, name="aiodos-comfyui-lecture").start()


def _kill_tree(pid: int):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=NO_WINDOW)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _stop_locked():
    process, _state["process"], _state["url"] = _state["process"], None, None
    if process is not None and process.poll() is None:
        _kill_tree(process.pid)
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()


def stop_instance():
    with _lock:
        _stop_locked()


def _busy() -> bool:
    from aiodos import db
    return db.query_one("SELECT 1 FROM units WHERE task='engine_unit' AND status='running' "
                        "LIMIT 1") is not None


def _start_reaper():
    if _state["reaper"] is not None and _state["reaper"].is_alive():
        return
    _state["reaper"] = threading.Thread(target=_reap, daemon=True, name="aiodos-comfyui-veille")
    _state["reaper"].start()


def _reap():
    while True:
        time.sleep(30)
        with _lock:
            if not _alive():
                _state["process"] = None
                return
            idle = time.monotonic() - _state["used"] > IDLE_S
        try:
            busy = _busy()
        except Exception:
            busy = True
        if idle and not busy:
            log.info("ComfyUI arrêté : plus de travail depuis %s min", IDLE_S // 60)
            stop_instance()
            return
        if busy:
            touch()
