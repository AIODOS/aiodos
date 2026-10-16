"""Particularités d'Android (Termux) : le reconnaître, et décrire l'appareil.

Sous Android, le nom de la machine vaut « localhost » : le nœud prend le modèle
du téléphone à la place, pour être reconnaissable dans la liste des machines.
"""
import functools
import os
import shutil
import subprocess
import sys

_GETPROP = "/system/bin/getprop"


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
