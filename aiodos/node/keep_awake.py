"""Pas de mise en veille pendant les travaux des applications (docs/conception/partage-
puissance.md § 2.5) : sur secteur, tant qu'une unité de moteur calcule (ici, chez une de mes
machines pour ce PC, ou ici pour une autre de mes machines), AIODOS demande à Windows de ne
pas mettre le PC en veille (SetThreadExecutionState, ES_SYSTEM_REQUIRED : l'écran, lui, peut
s'éteindre). Sur batterie, Windows fait comme d'habitude. Réglage keep_awake_jobs (vrai par
défaut). C'est une demande d'application, comme celle d'un lecteur vidéo : aucun réglage de
Windows ne change, et elle tombe avec le processus d'AIODOS.

La demande vaut pour le fil qui la fait : un fil à part la tient et la revoit toutes les
EVERY secondes.
"""
import logging
import sys
import threading

from aiodos import db, settings
from aiodos.node import power

log = logging.getLogger("aiodos.power")

EVERY = 30
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_holds = {}                         # raison -> nombre (unités calculées ici pour une autre)
_state = {"thread": None, "held": False, "stop": None}


def hold(reason: str):
    with _lock:
        _holds[reason] = _holds.get(reason, 0) + 1


def release(reason: str):
    with _lock:
        if _holds.get(reason, 0) <= 1:
            _holds.pop(reason, None)
        else:
            _holds[reason] -= 1


def wanted() -> bool:
    """Faut-il garder le PC éveillé en ce moment ?"""
    if not settings.get("keep_awake_jobs"):
        return False
    if not power.read().get("plugged"):
        return False
    with _lock:
        if _holds:
            return True
    row = db.query_one("SELECT 1 FROM units WHERE task='engine_unit' AND status='running' "
                       "LIMIT 1")
    return row is not None


def _apply(on: bool):
    if sys.platform != "win32":
        return
    import ctypes
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))


def _loop(stop: threading.Event):
    while not stop.is_set():
        try:
            on = wanted()
        except Exception:
            log.debug("Mise en veille : état illisible", exc_info=True)
            on = False
        if on != _state["held"]:
            _apply(on)
            _state["held"] = on
            # i18n: tel quel (journal)
            log.info("Mise en veille %s : %s", "retenue" if on else "rendue à Windows",
                     "travail d'application en cours, sur secteur" if on else "plus rien")
        stop.wait(EVERY)
    if _state["held"]:
        _apply(False)
        _state["held"] = False


def start():
    if sys.platform != "win32" or _state["thread"] is not None:
        return
    stop = threading.Event()
    _state["stop"] = stop
    _state["thread"] = threading.Thread(target=_loop, args=(stop,), daemon=True,
                                        name="aiodos-eveil")
    _state["thread"].start()


def stop():
    if _state["stop"] is not None:
        _state["stop"].set()
    _state.update(thread=None, stop=None)
