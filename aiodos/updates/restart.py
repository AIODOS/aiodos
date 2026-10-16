"""Redémarrer AIODOS sur une version qui vient d'être installée.

request() arrête la boucle principale du nœud (ferme la fenêtre, ou sort de l'attente
du nœud sans fenêtre) ; une fois le nœud arrêté, relaunch() passe la main :

- sous le lanceur (cf. launcher.py), on sort avec le code RESTART, et le lanceur
  lance la nouvelle version ;
- sans lanceur (version d'origine), on relance AIODOS, dont le lanceur trouvera la
  nouvelle version : même processus sous Linux et Android (exec), nouveau sous Windows.
"""
import logging
import os
import subprocess
import sys
import threading

from aiodos import paths
from aiodos.updates import launcher

log = logging.getLogger("aiodos.updates")

_requested = threading.Event()
_stoppers = []


def on_request(stop):
    """stop() sera appelée à la demande de redémarrage (ex. fermer la fenêtre)."""
    _stoppers.append(stop)


def request():
    if _requested.is_set():
        return
    log.info("Redémarrage pour passer à la nouvelle version")
    _requested.set()
    for stop in list(_stoppers):
        try:
            stop()
        except Exception:
            log.exception("Arrêt pour redémarrage")


def requested() -> bool:
    return _requested.is_set()


def _stream(stream):
    """Sortie à transmettre au nouveau processus, si elle en est une vraie."""
    try:
        stream.fileno()
        return stream
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def relaunch(argv: list) -> int:
    """À appeler une fois le nœud arrêté et le verrou d'instance rendu."""
    if launcher.supervised():
        return launcher.RESTART
    command = [sys.executable, "-m", "aiodos", *launcher.child_argv(argv)]
    env = dict(os.environ, AIODOS_DATA_DIR=str(paths.data_dir()))
    for handler in logging.getLogger().handlers:
        handler.flush()
    if sys.platform != "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream:
                stream.flush()
        os.chdir(paths.APP_ROOT)
        os.execve(sys.executable, command, env)
    subprocess.Popen(command, cwd=paths.APP_ROOT, env=env, stdin=subprocess.DEVNULL,
                     stdout=_stream(sys.stdout), stderr=_stream(sys.stderr))
    return 0
