# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Trouver AIODOS sur ce PC (docs/api-applications.md § 1 et § 4.4).

- Le fichier de découverte `gateway.json` : `%LOCALAPPDATA%\\AIODOS\\gateway.json` sous Windows,
  `$XDG_STATE_HOME/aiodos/gateway.json` ailleurs, ou le chemin d'AIODOS_DISCOVERY_FILE (« - » :
  pas de fichier). Le client n'en retient que l'adresse, seulement si son hôte est local, et
  l'état de la passerelle ; il n'exécute jamais rien qui en viendrait.
- Le programme vivant : sous Windows, OpenProcess puis GetExitCodeProcess ; **jamais
  os.kill(pid, 0)**, qui y termine le programme.
- L'installation : AIODOS_HOME s'il est donné (et rien d'autre), sinon la clé de
  désinstallation (HKCU, puis HKLM), sinon `%LOCALAPPDATA%\\Programs\\AIODOS`. Une copie de
  développement (AIODOS_HOME contient `aiodos\\__init__.py`) se lance avec AIODOS_PYTHON ; sans
  lui, rien n'est lancé.
- `launch()` et le programme d'aide (`--app-request`) ne partent que sur un appel explicite
  de l'appli (un clic de l'utilisateur) : sans shell, sans fenêtre, avec une liste
  d'arguments calculée ici, jamais lue dans un fichier.
"""

import ctypes
import json
import locale
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

try:
    import winreg
except ImportError:                     # hors de Windows
    winreg = None

from . import errors, texts
from ._transport import Endpoint

log = logging.getLogger("aiodos_client")

UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}_is1")
MAX_FILE = 64 * 1024
MAX_OUTPUT = 1024 * 1024
HELPER_TIMEOUT = 90.0                   # le programme d'aide attend AIODOS 60 s au plus
WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# Variables qui changeraient l'interpréteur d'AIODOS (chemins de l'appli, environnement
# virtuel, PyInstaller…), et la clé de développement : elles ne passent pas aux programmes
# lancés.
_DROPPED_ENV = frozenset({
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH", "PYTHONUSERBASE",
    "PYTHONEXECUTABLE", "PYTHONPLATLIBDIR", "PYTHONINSPECT", "PYTHONNOUSERSITE",
    "__PYVENV_LAUNCHER__", "VIRTUAL_ENV", "TCL_LIBRARY", "TK_LIBRARY", "AIODOS_KEY",
})

# Windows : OpenProcess et GetExitCodeProcess, avec des fonctions à nous (une autre
# bibliothèque peut régler autrement celles de ctypes.windll).
if WINDOWS:
    _DWORD = ctypes.c_uint32
    _HANDLE = ctypes.c_void_p
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = [_DWORD, ctypes.c_int, _DWORD]
    _OpenProcess.restype = _HANDLE
    _GetExitCodeProcess = _kernel32.GetExitCodeProcess
    _GetExitCodeProcess.argtypes = [_HANDLE, ctypes.POINTER(_DWORD)]
    _GetExitCodeProcess.restype = ctypes.c_int
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [_HANDLE]
    _CloseHandle.restype = ctypes.c_int
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _AllowSetForegroundWindow = _user32.AllowSetForegroundWindow
        _AllowSetForegroundWindow.argtypes = [_DWORD]
        _AllowSetForegroundWindow.restype = ctypes.c_int
    except (OSError, AttributeError):   # Windows sans user32 (Nano Server)
        _AllowSetForegroundWindow = None

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


@dataclass(frozen=True)
class Discovery:
    """Ce que dit `gateway.json` (rien d'autre n'en est retenu)."""
    path: str
    url: str | None             # seulement si l'hôte est local
    running: bool
    pid: int | None
    instance: str | None
    state: str | None           # open, closed, error
    error: str | None           # le message d'AIODOS quand state vaut error
    apps: bool | None
    requests: bool | None
    approval_ui: bool | None
    version: str | None
    api: int | None


@dataclass(frozen=True)
class Installation:
    """Où est AIODOS, et de quoi le lancer."""
    root: str
    dev: bool                   # copie de développement (AIODOS_HOME)
    python: str | None          # interpréteur avec console (programme d'aide)
    pythonw: str | None         # interpréteur sans fenêtre (lancement)
    workdir: str                # dossier de travail du programme lancé


def discovery_path():
    """Le chemin du fichier de découverte, ou None (AIODOS_DISCOVERY_FILE vaut « - »)."""
    custom = os.environ.get("AIODOS_DISCOVERY_FILE")
    if custom is not None and custom.strip():
        return None if custom.strip() == "-" else custom
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(local, "AIODOS", "gateway.json")
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"),
                                                             ".local", "state")
    return os.path.join(state, "aiodos", "gateway.json")


def local_url(value):
    """L'adresse normalisée si son hôte est local, sinon None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return Endpoint(value).url
    except errors.AiodosError:
        return None


def _bool(value):
    return value if isinstance(value, bool) else None


def _text(value, limit=500):
    if not isinstance(value, str):
        return None
    clean = "".join(ch for ch in value if ch.isprintable())
    return clean[:limit] or None


def read_discovery():
    """Le fichier de découverte lu et vérifié, ou None (absent, illisible, d'un autre produit)."""
    path = discovery_path()
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE + 1)
    except OSError:
        return None
    if len(raw) > MAX_FILE:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("product") != "aiodos":
        return None
    gateway = data.get("gateway") if isinstance(data.get("gateway"), dict) else {}
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2 ** 32:
        pid = None
    api = data.get("api") if isinstance(data.get("api"), int) else None
    state = gateway.get("state")
    return Discovery(path=path, url=local_url(gateway.get("url")),
                     running=data.get("running") is True, pid=pid,
                     instance=_text(data.get("instance"), 64),
                     state=state if state in ("open", "closed", "error") else None,
                     error=_text(gateway.get("error")), apps=_bool(gateway.get("apps")),
                     requests=_bool(gateway.get("requests")),
                     approval_ui=_bool(gateway.get("approval_ui")),
                     version=_text(data.get("version"), 40), api=api)


def pid_alive(pid) -> bool:
    """Le programme `pid` tourne-t-il ? (Windows : OpenProcess + GetExitCodeProcess)"""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2 ** 32:
        return False
    if WINDOWS:
        handle = _OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            # Un programme d'un autre compte (ou protégé) existe mais ne s'ouvre pas.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = _DWORD()
            if not _GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _CloseHandle(handle)
    try:
        os.kill(pid, 0)                 # hors de Windows, le signal 0 ne fait que tester
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def allow_foreground(pid) -> None:
    """Permet à AIODOS (`pid`) de passer au premier plan (Windows ; sans effet ailleurs)."""
    if not WINDOWS or _AllowSetForegroundWindow is None:
        return
    if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2 ** 32:
        try:
            _AllowSetForegroundWindow(pid)
        except OSError:
            pass


def _registry_roots():
    if winreg is None:
        return []
    places = [(winreg.HKEY_CURRENT_USER, 0),
              (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
              (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)]
    roots = []
    for hive, view in places:
        try:
            with winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | view) as key:
                value, kind = winreg.QueryValueEx(key, "InstallLocation")
        except OSError:
            continue
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
            continue
        if kind == winreg.REG_EXPAND_SZ:
            value = os.path.expandvars(value)
        value = value.strip()
        if value and value not in roots:
            roots.append(value)
    return roots


def _resolve_program(value):
    if not value:
        return None
    if os.path.isfile(value):
        return os.path.abspath(value)
    found = shutil.which(value)
    return os.path.abspath(found) if found else None


def _from_root(root, allow_dev=False):
    if not root:
        return None
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return None
    if allow_dev and os.path.isfile(os.path.join(root, "aiodos", "__init__.py")):
        program = _resolve_program(os.environ.get("AIODOS_PYTHON"))
        return Installation(root=root, dev=True, python=program, pythonw=program, workdir=root)
    python = os.path.join(root, "runtime", "python.exe")
    if not os.path.isfile(python):
        return None
    pythonw = os.path.join(root, "runtime", "pythonw.exe")
    return Installation(root=root, dev=False, python=python,
                        pythonw=pythonw if os.path.isfile(pythonw) else None,
                        workdir=os.path.join(root, "app"))


def installation():
    """L'installation d'AIODOS sur ce PC, ou None.

    AIODOS_HOME, s'il est donné, est la seule racine regardée (essais, copie de développement).
    """
    home = os.environ.get("AIODOS_HOME")
    if home is not None and home.strip():
        return _from_root(home.strip(), allow_dev=True)
    for root in _registry_roots():
        found = _from_root(root)
        if found:
            return found
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return _from_root(os.path.join(base, "Programs", "AIODOS"))
    return None


def child_env() -> dict:
    """L'environnement d'un programme lancé : celui de l'appli, sans ce qui changerait
    l'interpréteur d'AIODOS ni la clé de développement."""
    env = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _DROPPED_ENV or upper.startswith("_PYI") or upper.startswith("_MEI"):
            continue
        env[name] = value
    return env


def _program(inst, windowless, lang):
    program = inst.pythonw if windowless else inst.python
    if inst.dev and not program:
        raise errors.make("not_running", lang, message=texts.error("dev_copy", lang,
                                                                   path=inst.root),
                          hint=texts.hint("dev_copy", lang))
    missing = None
    if not program or not os.path.isfile(program):
        missing = program or os.path.join(inst.root, "runtime")
    elif not os.path.isdir(inst.workdir):
        missing = inst.workdir
    if missing:
        raise errors.make("not_installed", lang,
                          message=texts.error("program_missing", lang, path=missing))
    return program


def launch_command(inst, lang="fr"):
    """La commande qui lance AIODOS en arrière-plan : (arguments, dossier de travail)."""
    return [_program(inst, True, lang), "-m", "aiodos", "--background"], inst.workdir


def helper_command(inst, lang="fr"):
    """La commande du programme d'aide (§ 4.4) : (arguments, dossier de travail)."""
    return [_program(inst, False, lang), "-m", "aiodos", "--app-request"], inst.workdir


def launch(inst, lang="fr") -> None:
    """Lance AIODOS en arrière-plan (pythonw.exe -m aiodos --background), sans attendre."""
    args, workdir = launch_command(inst, lang)
    log.info("lancement d'AIODOS (%s)", "copie de développement" if inst.dev else inst.root)
    options = {"creationflags": NO_WINDOW | NEW_GROUP} if WINDOWS else \
        {"start_new_session": True}
    subprocess.Popen(args, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, env=child_env(), **options)


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "latin-1", "replace")


def _kill(proc):
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=2)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def app_request(inst, body: dict, lang="fr", cancel=None, timeout=HELPER_TIMEOUT) -> dict:
    """Le programme d'aide d'AIODOS (§ 4.4) : la demande d'accès sur son entrée standard, en
    une ligne ; sa réponse, une ligne JSON sur sa sortie standard. Rend cette réponse (code
    de sortie 0), ou lève l'erreur qu'il a écrite (code 1)."""
    args, workdir = helper_command(inst, lang)
    line = (json.dumps(body, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    options = {"creationflags": NO_WINDOW} if WINDOWS else {}
    env = child_env()
    # Sa réponse en UTF-8 : sous Windows, une sortie redirigée serait sinon en cp1252, où un
    # message d'AIODOS qui contient « → » ne s'écrit pas.
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(args, cwd=workdir, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                close_fds=True, env=env, **options)
    except OSError as exc:
        log.warning("programme d'aide impossible à lancer (%s)", type(exc).__name__)
        raise errors.make("not_installed", lang) from exc
    deadline = time.monotonic() + timeout
    send = line
    while True:
        try:
            out, err = proc.communicate(input=send, timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            send = None
        if cancel is not None and cancel():
            _kill(proc)
            raise errors.make("cancelled", lang)
        if time.monotonic() > deadline:
            _kill(proc)
            raise errors.make("timeout", lang, detail={"seconds": timeout})
    text = _decode(out[:MAX_OUTPUT])
    payload = None
    for candidate in reversed(text.splitlines()):
        candidate = candidate.strip()
        if candidate.startswith("{"):
            try:
                payload = json.loads(candidate)
                break
            except ValueError:
                continue
    if proc.returncode == 0 and isinstance(payload, dict) \
            and isinstance(payload.get("device_code"), str) \
            and isinstance(payload.get("user_code"), str):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        raise errors.from_response(None, None, payload, lang)
    log.debug("programme d'aide : code %s, réponse incomprise (%s)", proc.returncode,
              _decode(err[:300]).strip())
    raise errors.make("protocol", lang)
