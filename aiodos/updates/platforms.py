"""Plateformes : « windows-x86_64 », « android-aarch64 », « linux-x86_64 »…

Un programme livré avec une version (le compagnon aiodos-net, cf. binaries.py) est,
dans le manifeste, un fichier de type « <programme>-<plateforme> » :
aiodos-net-windows-x86_64, aiodos-net-android-aarch64. Chaque machine ne prend que
celui de sa plateforme ; l'outil de publication (tools/publish.py) les construit avec
la cible Rust correspondante.
"""
import platform
import sys

from aiodos.node import android

# plateforme -> cible Rust qui la construit
RUST_TARGETS = {
    "windows-x86_64": "x86_64-pc-windows-msvc",
    "windows-aarch64": "aarch64-pc-windows-msvc",
    "android-aarch64": "aarch64-linux-android",
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "macos-aarch64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
}
_MACHINES = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64",
             "arm64": "aarch64", "aarch64": "aarch64"}


def tag() -> str | None:
    """La plateforme de cette machine, ou None si aucun programme n'est construit pour
    elle (processeur 32 bits, système inconnu)."""
    return name(sys.platform, platform.machine(), android.is_android())


def name(system: str, machine: str, on_android: bool = False) -> str | None:
    """Plateforme d'après sys.platform et platform.machine()."""
    if on_android:
        system = "android"
    elif system == "win32":
        system = "windows"
    elif system.startswith("linux"):
        system = "linux"
    elif system == "darwin":
        system = "macos"
    else:
        return None
    machine = _MACHINES.get(machine.lower())
    found = f"{system}-{machine}" if machine else None
    return found if found in RUST_TARGETS else None


def kind(program: str, where: str | None = None) -> str | None:
    """Type, dans le manifeste, du programme `program` construit pour la plateforme
    `where` (défaut : celle de cette machine)."""
    where = where or tag()
    return f"{program}-{where}" if where else None


def executable(program: str, where: str | None = None) -> str:
    """Nom du programme sur cette plateforme : « aiodos-net.exe » sous Windows."""
    where = where or tag() or ""
    windows = where.startswith("windows") if where else sys.platform == "win32"
    return f"{program}.exe" if windows else program
