"""Se connecter à AIODOS quand la passerelle est fermée, ou AIODOS arrêté
(docs/api-applications.md § 4.4 ; conception : partage de puissance, § 6.2).

Côté appli, sur un clic de l'utilisateur, le client lance le programme d'aide :

    <racine>\\runtime\\python.exe -m aiodos --app-request     (depuis <racine>\\app)

avec la demande d'accès (§ 4.3) en une ligne JSON sur l'entrée standard. Le programme d'aide
(run_helper) :
1. lance AIODOS en arrière-plan s'il ne tourne pas, et l'attend READY_TIMEOUT s ;
2. transmet la demande à l'instance par son canal privé (POST /instance/app-request sur le
   port de l'interface, avec la clé show_key d'instance.json, que seul ce compte lit) :
   jamais par la passerelle, qui reste fermée ;
3. écrit une ligne JSON sur sa sortie standard ({device_code, user_code, interval,
   expires_in, request_id, url}) et s'arrête (0) ; en cas d'échec, {"error": {code,
   message}} et 1.

Côté instance (instance_request) : le programme qui a lancé le programme d'aide (l'appli) est
identifié par son numéro, son chemin et son compte Windows, puis la demande suit le chemin
ordinaire (access_flow.start) : carte d'accord, et « Autoriser » ouvre la passerelle aux
applications. Le client relève ensuite sa clé sur la passerelle, à l'adresse rendue.

Le programme d'aide n'est jamais passé par le lanceur des mises à jour (updates/launcher.py) :
il ne fait que parler à l'instance en marche, quelle que soit sa version.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from aiodos import instance, paths
from aiodos.gateway import caller as callers
from aiodos.gateway.errors import ApiError

READY_TIMEOUT = 60
STDIN_LIMIT = 64 * 1024
_LAUNCHERS = {"py.exe", "pyw.exe"}       # le lanceur de Python intercalé en développement


# ------------------------------------------------------------ programme d'aide
def _emit(out, data: dict):
    # En ASCII (accents échappés) : la sortie d'un programme lancé par une appli peut être
    # en cp1252, qui ne sait pas écrire « → »
    out.write(json.dumps(data, ensure_ascii=True) + "\n")
    out.flush()


def _fail(out, code: str, message: str) -> int:
    _emit(out, {"error": {"code": code, "message": message}})
    return 1


def app_pid() -> int | None:
    """Le programme de l'appli : celui qui a lancé ce programme d'aide, en passant le
    lanceur py.exe s'il s'est intercalé (copie de développement)."""
    pid = os.getppid()
    for _ in range(3):
        program = callers.program_of(pid)
        if not program or Path(program).name.lower() not in _LAUNCHERS:
            return pid
        parent = callers.parent_of(pid)
        if not parent:
            return pid
        pid = parent
    return pid


def _launch():
    """AIODOS en arrière-plan, comme le raccourci « en arrière-plan » de l'installeur."""
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    command = [str(quiet if quiet.exists() else exe), "-m", "aiodos", "--background"]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, cwd=str(paths.APP_ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=flags)


def _lock() -> dict | None:
    try:
        lock = json.loads((paths.data_dir() / "instance.json").read_text(encoding="utf-8"))
        return lock if lock.get("port") and lock.get("show_key") else None
    except (OSError, ValueError, AttributeError):
        return None


def run_helper(stdin=None, stdout=None) -> int:
    """Le programme d'aide (« python -m aiodos --app-request »). Jamais de fenêtre : toute
    erreur sort en JSON."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        return _helper(stdin, stdout)
    except Exception as exc:              # l'appli lit la sortie : jamais de boîte d'erreur
        return _fail(stdout, "internal_error", f"Programme d'aide en échec : {exc}")


def _helper(stdin, stdout) -> int:
    import requests
    line = stdin.readline(STDIN_LIMIT)
    try:
        body = json.loads(line)
    except ValueError:
        return _fail(stdout, "invalid_request", "Demande illisible : une ligne JSON attendue "
                     "sur l'entrée standard.")
    if not isinstance(body, dict):
        return _fail(stdout, "invalid_request", "Demande illisible : un objet JSON attendu.")
    pid = app_pid()
    if not instance.running():
        try:
            _launch()
        except OSError as exc:
            return _fail(stdout, "not_running", f"AIODOS ne démarre pas : {exc}")
    deadline = time.monotonic() + READY_TIMEOUT
    last = "AIODOS ne répond pas."
    while time.monotonic() < deadline:
        lock = _lock() if instance.running() else None
        if lock:
            try:
                reply = requests.post(
                    f"http://127.0.0.1:{int(lock['port'])}/instance/app-request",
                    json={"request": body, "pid": pid}, timeout=15,
                    headers={"X-Aiodos-Show": str(lock["show_key"])},
                    proxies={"http": None, "https": None})
                data = reply.json()
                if reply.ok and isinstance(data, dict) and data.get("device_code"):
                    _emit(stdout, data)
                    return 0
                error = data.get("error") if isinstance(data, dict) else None
                if isinstance(error, dict) and error.get("code"):
                    _emit(stdout, {"error": {"code": error["code"],
                                             "message": error.get("message") or ""}})
                    return 1
                last = f"Réponse inattendue d'AIODOS (HTTP {reply.status_code})."
            except (requests.RequestException, ValueError) as exc:
                last = f"AIODOS ne répond pas encore ({exc.__class__.__name__})."
        time.sleep(0.5)
    return _fail(stdout, "not_running", last)


# ------------------------------------------------------------------- instance
def instance_request(body, gateway_port: int) -> tuple:
    """POST /instance/app-request (après la clé show_key) → (statut HTTP, corps)."""
    from aiodos.gateway import access_flow
    if not isinstance(body, dict) or not isinstance(body.get("request"), dict):
        return 400, {"error": {"code": "invalid_request", "message": "Demande illisible."}}
    pid = body.get("pid")
    who = callers.of_pid(pid) if isinstance(pid, int) and pid > 0 else callers.UNKNOWN
    try:
        reply = access_flow.start(body["request"], who, via="instance")
    except ApiError as exc:
        return exc.status, {"error": {"code": exc.code, "message": str(exc)}}
    return 200, {**reply, "url": f"http://127.0.0.1:{gateway_port}"}
