"""Alimentation de cette machine : secteur ou batterie, niveau de charge, température.

Sert aux règles du partage de puissance (cf. network/worker.py) : un PC portable
ne prête rien sur batterie ; un téléphone ne calcule qu'en charge, assez chargé
et pas trop chaud, comme BOINC sous Android.

D'où viennent les mesures :
  - Windows : GetSystemPowerStatus ;
  - Linux et Android : /sys/class/power_supply/ (souvent fermé aux applications
    par SELinux sur les Android récents) ;
  - Android, à défaut : la commande termux-battery-status (paquet termux-api et
    application Termux:API).

read() renvoie un dict :
  plugged     True en charge / sur secteur, False sur batterie, None si inconnu ;
  level       charge de la batterie en %, None si inconnue (ou pas de batterie) ;
  temperature température de la batterie en °C, None si inconnue ;
  mobile      True sur un téléphone ou une tablette (Android) ;
  source      d'où vient la mesure (windows, sysfs, termux-api) ou None.
"""
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from coeur.node import android

POWER_SUPPLY = Path("/sys/class/power_supply")
TERMUX_COMMAND = "termux-battery-status"
TERMUX_TIMEOUT = 10    # sans l'application Termux:API, la commande ne répond jamais
TERMUX_TTL = 20        # elle passe par une application Android : lente, gardée 20 s
TERMUX_RETRY = 300     # commande absente ou muette : nouvel essai dans 5 min
_SOURCES = ("Mains", "USB", "Wireless")      # chargeurs (« USB_PD »… sur d'anciens noyaux)
_termux = {"until": 0.0, "value": None}
_termux_lock = threading.Lock()


def _status(plugged=None, level=None, temperature=None, mobile=False, source=None) -> dict:
    return {"plugged": plugged, "level": level, "temperature": temperature,
            "mobile": mobile, "source": source}


def read() -> dict:
    if sys.platform == "win32":
        return _windows()
    return _linux(POWER_SUPPLY, android.is_android())


# --------------------------------------------------------------------- Windows
def _windows() -> dict:
    class PowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("Reserved1", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    status = PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return _status()
    has_battery = not status.BatteryFlag & 128      # 128 : pas de batterie, 255 : inconnu
    level = status.BatteryLifePercent if has_battery and status.BatteryLifePercent <= 100 else None
    return _status({1: True, 0: False}.get(status.ACLineStatus), level, source="windows")


# ----------------------------------------------------------- Linux et Android
def _linux(root: Path, mobile: bool) -> dict:
    found = _sysfs(root)
    found = dict(found, mobile=mobile) if found else None
    if not mobile:
        return found or _status()
    if found and None not in (found["plugged"], found["level"], found["temperature"]):
        return found
    # Téléphone : il faut avoir lu la batterie elle-même. Un dossier vide ou fermé
    # par SELinux ne veut pas dire « machine de bureau, toujours sur secteur »
    if found and found["level"] is None:
        found = None
    return _termux_status() or found or _status(mobile=True)


def _attr(folder: Path, name: str):
    try:
        return (folder / name).read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _number(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _sysfs(root: Path) -> dict | None:
    """Lecture de /sys/class/power_supply (format : Documentation/ABI/testing/
    sysfs-class-power du noyau). None si rien n'y est lisible."""
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return None
    online, statuses, levels, temperatures = [], [], [], []
    unreadable = False
    for supply in supplies:
        kind = _attr(supply, "type")
        if kind is None:
            unreadable = True
            continue
        if _attr(supply, "scope") == "Device":     # souris, manette… : pas l'alimentation
            continue
        if kind == "Battery":
            if _attr(supply, "present") == "0":
                continue
            statuses.append(_attr(supply, "status"))
            level = _number(_attr(supply, "capacity"))
            if level is not None and 0 <= level <= 100:
                levels.append(level)
            tenths = _number(_attr(supply, "temp"))           # dixièmes de degré
            if tenths is not None and -400 <= tenths <= 1200:
                temperatures.append(tenths / 10)
        elif kind.startswith(_SOURCES):
            state = _attr(supply, "online")
            if state is not None:
                online.append(state != "0")
    if not statuses and not online and unreadable:
        return None
    if "Charging" in statuses or any(online):
        plugged = True
    elif "Discharging" in statuses or online:      # chargeurs connus, tous débranchés
        plugged = False
    elif "Full" in statuses or "Not charging" in statuses:
        plugged = True
    elif not statuses:
        plugged = True                             # pas de batterie : machine de bureau
    else:
        plugged = None
    level = round(sum(levels) / len(levels)) if levels else None
    return _status(plugged, level, max(temperatures, default=None), source="sysfs")


# ------------------------------------------------------- Termux:API (Android)
def _termux_status() -> dict | None:
    with _termux_lock:
        if time.monotonic() < _termux["until"]:
            return _termux["value"]
        value = from_termux(_run_termux())
        _termux.update(value=value,
                       until=time.monotonic() + (TERMUX_TTL if value else TERMUX_RETRY))
        return value


def _run_termux():
    """Sortie JSON de termux-battery-status, ou None (commande absente, muette…)."""
    command = shutil.which(TERMUX_COMMAND)
    if command is None:
        return None
    try:
        # Groupe de processus à part : en cas de silence, la commande et ce
        # qu'elle a lancé sont arrêtés ensemble, rien ne reste en attente
        process = subprocess.Popen([command], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = process.communicate(timeout=TERMUX_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill(process)
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _kill(process):
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass
    process.wait()


def from_termux(data) -> dict | None:
    """Mesure tirée de termux-battery-status (application Termux:API)."""
    if not isinstance(data, dict):
        return None
    plugged = data.get("plugged")
    if isinstance(plugged, str):
        plugged = plugged.upper() != "UNPLUGGED"          # PLUGGED_AC, _USB, _WIRELESS…
    else:
        plugged = {"CHARGING": True, "FULL": True,
                   "DISCHARGING": False}.get(str(data.get("status") or "").upper())
    level = _number(data.get("percentage"))
    if level is not None and not 0 <= level <= 100:
        level = None
    temperature = data.get("temperature")             # 30.700000762939453 (float 32 bits)
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not -40 <= temperature <= 120):
        temperature = None
    else:
        temperature = round(temperature, 1)
    if plugged is None and level is None:
        return None
    return _status(plugged, level, temperature, mobile=True, source="termux-api")
