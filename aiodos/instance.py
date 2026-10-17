"""Une seule instance d'AIODOS à la fois par dossier de données.

Deux nœuds sur la même base fonctionneraient (les réservations d'unités sont
atomiques), mais deux fenêtres identiques prêteraient à confusion. AIODOS relancé
alors qu'il tourne déjà (fenêtre cachée dans la zone de notification…) demande à
l'instance en place de montrer sa fenêtre (wake_running) : le verrou garde pour cela
le port de son API locale et une clé propre à cette demande (pas le jeton de l'API).
"""
import ctypes
import json
import os
import sys

from aiodos import paths

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_show = []                 # fonction qui montre la fenêtre de cette instance (ui/window.py)


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


def running() -> bool:
    """Une autre instance vivante tient-elle le verrou de ce dossier de données ?"""
    try:
        other = json.loads(_lock_file().read_text(encoding="utf-8"))
        return _alive(int(other.get("pid", 0)))
    except (OSError, ValueError):
        return False


def acquire(port: int, show_key: str | None = None) -> bool:
    """Réserve l'instance. Faux si une autre instance vivante tient déjà le verrou.
    show_key : clé qui fera revenir sa fenêtre (cf. wake_running, POST /instance/show)."""
    if running():
        return False
    lock = {"pid": os.getpid(), "port": port}
    if show_key:
        lock["show_key"] = show_key
    _lock_file().write_text(json.dumps(lock), encoding="utf-8")
    return True


def on_show(fn):
    """fn() -> bool montrera la fenêtre de cette instance (cf. show_window) ; None : aucune."""
    _show[:] = [fn] if fn else []


def show_window() -> bool:
    """Montre la fenêtre de cette instance, s'il y en a une (appel de l'API locale)."""
    return bool(_show and _show[0]())


def wake_running(timeout: float = 5) -> bool:
    """Demande à l'instance qui tient le verrou de montrer sa fenêtre. Vrai si c'est fait."""
    try:
        other = json.loads(_lock_file().read_text(encoding="utf-8"))
        pid, port, key = int(other["pid"]), int(other["port"]), str(other["show_key"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if sys.platform == "win32":
        # Elle a le droit de passer au premier plan : c'est ce lancement qui le lui donne
        ctypes.windll.user32.AllowSetForegroundWindow(pid)
    import requests
    try:
        reply = requests.post(f"http://127.0.0.1:{port}/instance/show", timeout=timeout,
                              headers={"X-Aiodos-Show": key},
                              proxies={"http": None, "https": None})
        return reply.ok and reply.json().get("shown") is True
    except (requests.RequestException, ValueError):
        return False


def release():
    try:
        lock = _lock_file()
        if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
            lock.unlink()
    except (OSError, ValueError):
        pass


def alert(message: str):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "AIODOS", 0x40)
    else:
        print(message)
