"""Une seule instance de Cœur IA à la fois par dossier de données.

Deux nœuds sur la même base fonctionneraient (les réservations d'unités sont
atomiques), mais deux fenêtres identiques prêteraient à confusion.
"""
import ctypes
import json
import os
import sys

from coeur import paths

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _lock_file():
    return paths.data_dir() / "instance.json"


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def acquire(port: int) -> bool:
    """Réserve l'instance. Faux si une autre instance vivante tient déjà le verrou."""
    lock = _lock_file()
    try:
        other = json.loads(lock.read_text(encoding="utf-8"))
        if _alive(int(other.get("pid", 0))):
            return False
    except (OSError, ValueError):
        pass
    lock.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
    return True


def release():
    try:
        lock = _lock_file()
        if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
            lock.unlink()
    except (OSError, ValueError):
        pass


def alert(message: str):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "Cœur IA", 0x40)
    else:
        print(message)
