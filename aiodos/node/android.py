"""Particularités d'Android (Termux) : le reconnaître, et décrire l'appareil.

Sous Android, le nom de la machine vaut « localhost » : le nœud prend le modèle
du téléphone à la place, pour être reconnaissable dans la liste des machines.
"""
import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import time

_GETPROP = "/system/bin/getprop"
WIFI_TTL = 30.0                     # le Wi-Fi est relu au plus toutes les 30 s
_wifi = {"at": None, "value": None}


def is_android() -> bool:
    """Android, Termux compris : le Python de Termux se dit « linux » (avant 3.13)."""
    return (sys.platform == "android" or hasattr(sys, "getandroidapilevel")
            or "ANDROID_ROOT" in os.environ or "TERMUX_VERSION" in os.environ)


def prop(name: str) -> str:
    """Propriété du système (getprop), ou "" si elle est illisible."""
    command = _GETPROP if os.path.exists(_GETPROP) else shutil.which("getprop")
    if not command:
        return ""
    try:
        return subprocess.run([command, name], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def on_wifi() -> bool | None:
    """Le téléphone est-il sur un réseau Wi-Fi ? Une interface « wlan… » en service, ou, à
    défaut, termux-wifi-connectioninfo (Termux:API) ; None si on ne peut pas le savoir.
    Un téléphone ne calcule pour un ami qu'en Wi-Fi (network/worker.py)."""
    now = time.monotonic()
    if _wifi["at"] is not None and now - _wifi["at"] < WIFI_TTL:
        return _wifi["value"]
    value = _wifi_interface()
    if value is None:
        value = _wifi_termux()
    _wifi.update(at=now, value=value)
    return value


def _wifi_interface() -> bool | None:
    base = "/sys/class/net"
    try:
        names = [n for n in os.listdir(base) if n.startswith("wlan")]
    except OSError:
        return None
    if not names:
        return None                  # rien de visible : pas une preuve d'absence de Wi-Fi
    for name in names:
        try:
            with open(os.path.join(base, name, "operstate"), encoding="ascii") as f:
                if f.read().strip() == "up":
                    return True
        except OSError:
            return None
    return False


def _wifi_termux() -> bool | None:
    command = shutil.which("termux-wifi-connectioninfo")
    if command is None:
        return None
    try:
        # Groupe à part : en cas de silence, la commande et ce qu'elle lance s'arrêtent
        process = subprocess.Popen([command], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()
        return None
    try:
        return json.loads(out).get("supplicant_state") == "COMPLETED"
    except (ValueError, AttributeError):
        return None


@functools.lru_cache(maxsize=1)
def model() -> str:
    """« Google Pixel 7 », « Samsung SM-A546B »… ou "" si inconnu."""
    maker, name = prop("ro.product.manufacturer"), prop("ro.product.model")
    if maker and not name.lower().startswith(maker.lower()):
        name = f"{maker[:1].upper()}{maker[1:]} {name}".strip()
    return name


@functools.lru_cache(maxsize=1)
def system() -> str:
    """« Android 14 », pour le profil du nœud."""
    release = prop("ro.build.version.release")
    return f"Android {release}" if release else "Android"
