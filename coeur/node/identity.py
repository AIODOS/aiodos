"""Identité et capacités de cette machine (le « profil » du nœud).

En phase 1, ce profil sera échangé entre nœuds pour que le planificateur sache
qui peut faire quoi : VRAM, modèles installés, machine inactive ou sur secteur…
"""
import ctypes
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid

from coeur import __version__, db, settings
from coeur.services import ollama

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # pas de console qui clignote
_cache = {"at": 0.0, "profile": None}
_cache_lock = threading.Lock()


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


def gpus() -> list:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW).stdout
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


def on_ac_power():
    """True sur secteur, False sur batterie, None si inconnu."""
    if sys.platform != "win32":
        return None

    class PowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    status = PowerStatus()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return {1: True, 0: False}.get(status.ACLineStatus)
    return None


def idle_seconds():
    """Temps depuis la dernière action clavier/souris (base du mode « économiseur »)."""
    if sys.platform != "win32":
        return None

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]
    info = LastInput()
    info.cbSize = ctypes.sizeof(LastInput)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return max(0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000)
    return None


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


def profile(max_age=5.0) -> dict:
    with _cache_lock:
        if _cache["profile"] and time.monotonic() - _cache["at"] < max_age:
            return _cache["profile"]
    cards = gpus()
    vram_total = max((g["vram_total"] for g in cards), default=0)
    data = {
        "node_id": node_id(),
        "name": settings.get("node_name"),
        "version": __version__,
        "os": f"{platform.system()} {platform.release()}",
        "cpu": {"name": cpu_name(), "threads": os.cpu_count()},
        "memory": memory(),
        "gpus": cards,
        "on_ac_power": on_ac_power(),
        "idle_seconds": idle_seconds(),
        "ollama": {"version": ollama.version(), "models": _models(vram_total)},
    }
    with _cache_lock:
        _cache.update(at=time.monotonic(), profile=data)
    return data
