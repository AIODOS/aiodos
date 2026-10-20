"""Particularités d'Android (l'appli AIODOS ou Termux) : le reconnaître, et décrire
l'appareil.

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

from aiodos.node import android_app

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
    """Le téléphone est-il connecté à un réseau Wi-Fi ? Android lui-même dans l'appli
    AIODOS ; sous Termux, termux-wifi-connectioninfo (Termux:API ; depuis Android 10,
    /sys/class/net n'est plus lisible par une appli), ou à défaut l'interface « wlan0 » en
    service ; None si on ne peut pas le savoir. Le partage
    de connexion n'est pas du Wi-Fi : il passe par les données mobiles. Un téléphone ne
    calcule pour un ami qu'en Wi-Fi (network/worker.py)."""
    now = time.monotonic()
    if _wifi["at"] is not None and now - _wifi["at"] < WIFI_TTL:
        return _wifi["value"]
    value = android_app.on_wifi() if android_app.in_app() else _wifi_termux()
    if value is None:
        value = _wifi_interface()
    _wifi.update(at=now, value=value)
    return value


def _wifi_interface() -> bool | None:
    """wlan0 seulement : wlan1 ou swlan0 servent souvent au partage de connexion."""
    try:
        with open("/sys/class/net/wlan0/operstate", encoding="ascii") as f:
            return f.read().strip() == "up"
    except OSError:
        return None                  # illisible : pas une preuve d'absence de Wi-Fi


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
