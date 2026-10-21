"""Identité et capacités de cette machine (le « profil » du nœud).

En phase 1, ce profil sera échangé entre nœuds pour que le planificateur sache
qui peut faire quoi : VRAM, modèles installés, machine inactive ou sur secteur…
L'alimentation (secteur, batterie, température) est lue par node/power.py.
"""
import ctypes
import functools
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import uuid

from aiodos import __version__, db, settings
from aiodos.cluster import engine
from aiodos.imaging import catalog as images_catalog
from aiodos.imaging import engine as images_engine
from aiodos.network import route
from aiodos.network.companion import SERVICE_HOLD, SERVICE_USAGE
from aiodos.node import android, android_app, power, priority
from aiodos.services import ollama
from aiodos.updates.store import Store

log = logging.getLogger("aiodos.cluster")

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # pas de console qui clignote
_cache = {"at": 0.0, "profile": None}
_cache_lock = threading.Lock()
_not_lent = {"reason": None}     # dernière raison écrite au journal (cf. _note_not_lent)


def node_id() -> str:
    row = db.query_one("SELECT value FROM settings WHERE key='node_id'")
    if row:
        return json.loads(row["value"])
    new_id = uuid.uuid4().hex
    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('node_id', ?)",
               (json.dumps(new_id),))
    return json.loads(db.query_one("SELECT value FROM settings WHERE key='node_id'")["value"])


def cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def memory() -> dict:
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"total": status.ullTotalPhys, "available": status.ullAvailPhys}
        return {"total": 0, "available": 0}
    return _meminfo()


def _meminfo(path="/proc/meminfo") -> dict:
    """Linux et Android : « MemTotal: 7854321 kB »…"""
    values = {}
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name in ("MemTotal", "MemAvailable"):
                    values[name] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return {"total": values.get("MemTotal", 0), "available": values.get("MemAvailable", 0)}


def gpus() -> list:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW | priority.CREATION_FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            result.append({"name": parts[0], "vram_total": int(parts[1]) * 1024 ** 2,
                           "vram_used": int(parts[2]) * 1024 ** 2,
                           "utilization": int(parts[3])})
    return result


SIMULATE_IDLE_ENV = "AIODOS_SIMULATE_IDLE"


def idle_seconds():
    """Temps depuis la dernière action clavier/souris (base du mode « économiseur »).
    AIODOS_SIMULATE_IDLE=secondes le remplace : essais et tests, qui ne doivent pas
    dépendre de ce que fait l'utilisateur de la machine pendant qu'ils tournent."""
    simulated = os.environ.get(SIMULATE_IDLE_ENV, "").strip()
    if simulated:
        try:
            return max(0.0, float(simulated))
        except ValueError:
            pass
    if sys.platform != "win32":
        return None

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]
    info = LastInput()
    info.cbSize = ctypes.sizeof(LastInput)
    user32, kernel32 = _input_api()
    if user32.GetLastInputInfo(ctypes.byref(info)):
        return idle_from_ticks(kernel32.GetTickCount(), info.dwTime)
    return None


def idle_from_ticks(now: int, last_input: int) -> float:
    """Secondes entre deux instants de GetTickCount, un compteur de millisecondes sur
    32 bits sans signe : il dépasse 2^31 après 24,9 jours de fonctionnement et repart de
    zéro après 49,7. La différence se calcule donc modulo 2^32."""
    return ((now - last_input) & 0xFFFFFFFF) / 1000


@functools.lru_cache(maxsize=1)
def _input_api():
    """user32 et kernel32 avec des types exacts : sans restype, ctypes lirait GetTickCount
    comme un entier signé, négatif au-delà de 24,9 jours (le PC passerait pour utilisé)."""
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    kernel32.GetTickCount.restype = wintypes.DWORD
    user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    return user32, kernel32


def _engine() -> str:
    """« llama.cpp » quand le moteur intégré fait tourner les modèles, et toujours dans
    l'appli Android (pas d'Ollama : un llama.cpp absent y reste « le moteur »), sinon
    « ollama »."""
    return "llama.cpp" if android_app.in_app() or ollama.use_integrated() else "ollama"


def _models(vram_total) -> list:
    try:
        models = ollama.list_models()
    except Exception:
        return []
    loaded = {m["name"]: m for m in ollama.loaded_models()}
    all_caps = ollama.capabilities_many([m["name"] for m in models])
    for m in models:
        m["caps"] = all_caps[m["name"]]
        m["fits_vram"] = bool(vram_total) and m["size"] <= vram_total * 0.92
        m["loaded"] = m["name"] in loaded
    return models


def _hold_problem() -> str | None:
    """Pourquoi le compagnon de cette machine ne tiendrait pas le serveur RPC d'un prêt, ou
    None s'il le peut. Mêmes conditions que cluster/helper.lend, qui refuse le prêt sans
    elles : sans compagnon qui tient ce serveur (SERVICE_HOLD, compagnon/src/hold.rs) et
    dit ses flux (SERVICE_USAGE), n'importe quel programme de cette machine y serait servi."""
    companion = route.companion()
    if companion is None or not companion.running:
        return ("le compagnon aiodos-net ne tourne pas (réglage « Relier mes machines par "
                "Internet »)")
    if not (companion.supports(SERVICE_USAGE) and companion.supports(SERVICE_HOLD)):
        return ("le compagnon aiodos-net est trop ancien pour tenir le serveur prêté, ou ne "
                "peut pas lire la table des connexions de ce système : mets AIODOS à jour")
    return None


def _note_not_lent(reason: str | None):
    """Écrit une fois au journal (et de nouveau si elle change) pourquoi la carte n'est pas
    proposée à mes machines alors que le réglage le demande : sans quoi elle cesserait
    d'être annoncée sans un mot."""
    if reason and reason != _not_lent["reason"]:
        log.info("Carte graphique pas proposée à mes machines malgré le réglage « Prêter ma "
                 "carte graphique pour les gros modèles » : %s", reason)
    _not_lent["reason"] = reason


def _cluster() -> dict:
    """Carte graphique prêtée à mes machines pour les gros modèles répartis
    (cluster/helper.py) : version de llama.cpp, mémoire libre de la carte prêtée.

    « lends » n'est vrai que si helper.lend accepterait le prêt : mes machines choisissent
    leurs prêteurs d'après lui (cluster/group.lenders), et un refus n'arriverait sinon
    qu'à l'usage, au premier message d'une conversation. Donc jamais dans l'appli Android
    (toute appli du téléphone joindrait le serveur RPC), ni sans un compagnon qui tient ce
    serveur (_hold_problem). Rien n'est demandé à llama.cpp tant que le prêt est coupé, ni
    sur un téléphone."""
    if not settings.get("lend_gpu") or android_app.in_app():
        return {"lends": False}
    found = engine.info()
    best = engine.lent_device(found["devices"]) if found else None
    problem = _hold_problem() if best else None
    _note_not_lent(problem)
    return {"lends": bool(best) and problem is None,
            "version": found["version"] if found else None,
            "device": best["label"] if best else None,
            "free_mib": best["free_mib"] if best else 0}


def _images() -> dict:
    """Images (imaging/) : version de stable-diffusion.cpp, modèles complets ici. Une de
    mes machines voit ainsi qui peut calculer ses images (moteur, modèle, prêt de
    puissance), et à qui copier un modèle (le modèle suffit)."""
    found = images_engine.info()
    return {"engine": found["version"] if found else None,
            "models": images_catalog.available(),
            "shares": bool(settings.get("share_enabled"))}


def _updates() -> dict:
    """offer : la version la plus récente que cette machine transmet (une machine 2.0.x
    ne lit que cette clé) ; files : les fichiers qu'elle a de chaque paquet, compagnons
    compris, pour qu'une machine sache à qui demander celui de sa plateforme."""
    store = Store()
    return {"offer": store.offer(), "files": store.available()}


def profile(max_age=5.0) -> dict:
    with _cache_lock:
        if _cache["profile"] and time.monotonic() - _cache["at"] < max_age:
            return _cache["profile"]
    cards = gpus()
    vram_total = max((g["vram_total"] for g in cards), default=0)
    supply = power.read()
    data = {
        "node_id": node_id(),
        "name": settings.get("node_name"),
        "version": __version__,
        "os": (android.system() if android.is_android()
               else f"{platform.system()} {platform.release()}"),
        "cpu": {"name": cpu_name(), "threads": os.cpu_count()},
        "memory": memory(),
        "gpus": cards,
        "on_ac_power": supply["plugged"],
        "power": supply,
        "idle_seconds": idle_seconds(),
        # « engine » : llama.cpp intégré (llm/) ou Ollama ; la clé garde son ancien nom
        "ollama": {"version": ollama.version(), "models": _models(vram_total),
                   "engine": _engine()},
        "lends_models": bool(settings.get("lend_models")),
        # Réponses en flux avec signes de vie (network/p2p_app.ALIVE_S) : la machine qui
        # attend peut lire avec un délai court (remote_models.chat_stream)
        "stream_alive": True,
        "cluster": _cluster(),                   # gros modèles répartis (cluster/)
        "images": _images(),                     # images réparties (imaging/)
        "updates": _updates(),                   # versions transmissibles (updates/p2p.py)
    }
    with _cache_lock:
        _cache.update(at=time.monotonic(), profile=data)
    return data
