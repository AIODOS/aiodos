"""Priorité du travail de fond : l'utilisateur passe toujours avant (docs/arriere-plan.md).

Ce qui sert directement l'utilisateur (interface, conversations) garde la priorité
normale. Ce qui travaille en fond la cède :
  - les threads du travail de fond : unités de la file et calcul pour d'autres machines,
    exécutés par un groupe de threads à part (run) ;
  - nvidia-smi, que le profil de la machine lance souvent (CREATION_FLAGS) ; lower_process
    sert à un programme de calcul lancé en fond.
Restent en priorité normale : le compagnon aiodos-net, qui relaie aussi des
conversations, et l'essai d'une nouvelle version, dont le délai ne doit pas dépendre de
la charge du PC (une version qui le dépasse est refusée 24 h).

Windows (documentation de Microsoft, relue le 17/10/2026) :
  - un thread de fond passe en priorité « inférieure à la normale »
    (SetThreadPriority, THREAD_PRIORITY_BELOW_NORMAL) : quand le processeur est plein,
    les programmes de l'utilisateur passent d'abord ; en EcoQoS (SetThreadInformation,
    ThreadPowerThrottling, THREAD_POWER_THROTTLING_EXECUTION_SPEED) : Windows 11 lui
    donne la fréquence la plus économe et, sur un processeur hybride, les cœurs
    efficaces ; en priorité mémoire « inférieure à la normale » (ThreadMemoryPriority) :
    les pages qu'il lit pour indexer sont retirées de la mémoire avant celles des
    autres programmes ;
  - un programme lancé en fond est créé en classe « inférieure à la normale »
    (BELOW_NORMAL_PRIORITY_CLASS), puis passé en EcoQoS (SetProcessInformation,
    ProcessPowerThrottling).
  Pas de THREAD_MODE_BACKGROUND_BEGIN : il ralentit aussi les accès disque du thread, et
  Microsoft le déconseille pour un thread qui partage des verrous avec d'autres
  (inversion de priorité) : ici le verrou de l'interpréteur Python et la base SQLite.

Linux et Android : nice 10 (THREAD_PRIORITY_BACKGROUND d'Android). Sous Linux, le nice
est propre à chaque thread, et un thread le transmet à ceux qu'il crée. Un thread qui l'a
pris ne peut plus le rendre (il faudrait des droits) : d'où un groupe de threads à part,
réservé au travail de fond.

Rien d'autre n'est touché : ni réglage du système (plan d'alimentation…), ni Ollama, qui
tourne à part. Un appel refusé (ancien Windows…) est sans conséquence.
"""
import asyncio
import concurrent.futures
import ctypes
import functools
import logging
import os
import sys
import threading
from pathlib import Path

from aiodos.node import android

log = logging.getLogger("aiodos.priority")

NICE = 10                      # Android : THREAD_PRIORITY_BACKGROUND
POOL_SIZE = 32                 # au-delà de la somme des unités simultanées (--slots)
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATION_FLAGS = BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0

_THREAD_PRIORITY_BELOW_NORMAL = -1
_THREAD_MEMORY_PRIORITY, _THREAD_POWER_THROTTLING = 0, 3     # THREAD_INFORMATION_CLASS
_PROCESS_POWER_THROTTLING = 4                                # PROCESS_INFORMATION_CLASS
_MEMORY_PRIORITY_BELOW_NORMAL = 4
_THROTTLING_VERSION, _EXECUTION_SPEED = 1, 0x1
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_pool = None
_pool_lock = threading.Lock()
_logged = set()


class _Throttling(ctypes.Structure):
    """THREAD_POWER_THROTTLING_STATE et PROCESS_POWER_THROTTLING_STATE (même forme)."""
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong)]


def _linux() -> bool:
    return sys.platform.startswith("linux") or android.is_android()


@functools.lru_cache(maxsize=1)
def _kernel32():
    """kernel32 avec des types exacts : un HANDLE fait 64 bits (sans argtypes, ctypes
    passerait le pseudo-handle du thread courant tronqué, et l'appel échouerait)."""
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, size = wintypes.HANDLE, wintypes.DWORD
    k.GetCurrentThread.restype = handle
    k.SetThreadPriority.argtypes = [handle, ctypes.c_int]
    k.GetThreadPriority.argtypes = [handle]
    k.SetThreadInformation.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, size]
    k.GetThreadInformation.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, size]
    k.OpenProcess.restype = handle
    k.OpenProcess.argtypes = [size, wintypes.BOOL, size]
    k.SetPriorityClass.argtypes = [handle, size]
    k.GetPriorityClass.argtypes = [handle]
    k.SetProcessInformation.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, size]
    k.GetProcessInformation.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, size]
    k.CloseHandle.argtypes = [handle]
    return k


def _note(kind: str, applied: list):
    """Au journal une fois : ce qui a pu être appliqué aux threads, aux programmes."""
    if applied and kind not in _logged:
        _logged.add(kind)
        log.info("Travail de fond (%s) : %s", kind, ", ".join(applied))


# ------------------------------------------------------------------ threads
def lower_current_thread() -> list:
    """Abaisse le thread courant (cf. en-tête). Renvoie ce qui a été appliqué. Ne lève
    jamais : appelé à la création de chaque thread de fond, une exception rendrait tout
    le groupe inutilisable (ThreadPoolExecutor)."""
    try:
        if sys.platform == "win32":
            applied = _windows_thread()
        elif _linux():
            applied = _nice_thread(threading.get_native_id())
        else:
            applied = []
    except Exception as exc:       # fonction absente (ancien système), refus…
        log.debug("Priorité du thread inchangée : %s", exc)
        applied = []
    _note("threads", applied)
    return applied


def _windows_thread() -> list:
    k, applied = _kernel32(), []
    thread = k.GetCurrentThread()
    if k.SetThreadPriority(thread, _THREAD_PRIORITY_BELOW_NORMAL):
        applied.append("priorité inférieure à la normale")      # i18n: tel quel (journal)
    eco = _Throttling(_THROTTLING_VERSION, _EXECUTION_SPEED, _EXECUTION_SPEED)
    if k.SetThreadInformation(thread, _THREAD_POWER_THROTTLING, ctypes.byref(eco),
                              ctypes.sizeof(eco)):
        applied.append("EcoQoS")
    memory = ctypes.c_ulong(_MEMORY_PRIORITY_BELOW_NORMAL)
    if k.SetThreadInformation(thread, _THREAD_MEMORY_PRIORITY, ctypes.byref(memory),
                              ctypes.sizeof(memory)):
        applied.append("mémoire de priorité inférieure")        # i18n: tel quel (journal)
    return applied


def _nice_thread(tid: int) -> list:
    """Linux, Android : le thread (ou processus) `tid` passe à nice 10, sans jamais
    remonter un nice déjà plus grand."""
    current = os.getpriority(os.PRIO_PROCESS, tid)
    if current < NICE:
        os.setpriority(os.PRIO_PROCESS, tid, NICE)
    return [f"nice {max(current, NICE)}"]


def thread_state() -> dict:
    """Priorité du thread courant, EcoQoS et priorité mémoire (Windows ; tests, journal)."""
    if sys.platform != "win32":
        if _linux():
            return {"nice": os.getpriority(os.PRIO_PROCESS, threading.get_native_id())}
        return {}
    k = _kernel32()
    thread = k.GetCurrentThread()
    eco, memory = _Throttling(_THROTTLING_VERSION, 0, 0), ctypes.c_ulong(0)
    k.GetThreadInformation(thread, _THREAD_POWER_THROTTLING, ctypes.byref(eco),
                           ctypes.sizeof(eco))
    k.GetThreadInformation(thread, _THREAD_MEMORY_PRIORITY, ctypes.byref(memory),
                           ctypes.sizeof(memory))
    return {"priority": k.GetThreadPriority(thread),
            "ecoqos": bool(eco.ControlMask & eco.StateMask & _EXECUTION_SPEED),
            "memory": memory.value}


# ----------------------------------------------------- groupe de threads de fond
def pool() -> concurrent.futures.ThreadPoolExecutor:
    """Les threads du travail de fond, abaissés dès leur création."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(
                POOL_SIZE, thread_name_prefix="aiodos-fond", initializer=lower_current_thread)
        return _pool


async def run(fn, *args):
    """fn(*args) dans un thread de fond, depuis la boucle asyncio (comme
    asyncio.to_thread, qui sert, lui, aussi l'interface et les conversations)."""
    return await asyncio.get_running_loop().run_in_executor(pool(), functools.partial(fn, *args))


# ---------------------------------------------------------------- programmes
def lower_process(pid: int) -> list:
    """Abaisse un programme lancé par AIODOS pour son travail de fond. Sous Windows, à
    créer de préférence avec CREATION_FLAGS (classe de priorité dès le départ) ; sous
    Linux, à rappeler une fois le programme prêt, pour les threads qu'il a créés
    entre-temps. Renvoie ce qui a été appliqué."""
    try:
        if sys.platform == "win32":
            applied = _windows_process(pid)
        elif _linux():
            applied = _nice_process(pid)
        else:
            applied = []
    except Exception as exc:       # jamais au point d'empêcher le lancement du programme
        log.debug("Priorité du programme %s inchangée : %s", pid, exc)
        applied = []
    _note("programmes", applied)
    return applied


def _windows_process(pid: int) -> list:
    k, applied = _kernel32(), []
    process = k.OpenProcess(_PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION,
                            False, pid)
    if not process:
        return applied
    try:
        if k.SetPriorityClass(process, BELOW_NORMAL_PRIORITY_CLASS):
            applied.append("priorité inférieure à la normale")  # i18n: tel quel (journal)
        eco = _Throttling(_THROTTLING_VERSION, _EXECUTION_SPEED, _EXECUTION_SPEED)
        if k.SetProcessInformation(process, _PROCESS_POWER_THROTTLING, ctypes.byref(eco),
                                   ctypes.sizeof(eco)):
            applied.append("EcoQoS")
    finally:
        k.CloseHandle(process)
    return applied


def _nice_process(pid: int, proc: Path = Path("/proc")) -> list:
    """Tous les threads du programme (sous Linux, setpriority sur un processus ne vise
    que son thread principal)."""
    applied = []
    try:
        tasks = [int(t.name) for t in (proc / str(pid) / "task").iterdir() if t.name.isdigit()]
    except OSError:
        tasks = [pid]
    for tid in tasks:
        try:
            applied = _nice_thread(tid)
        except OSError:                  # thread terminé entre-temps
            continue
    return applied


def process_state(pid: int) -> dict:
    """Classe de priorité et EcoQoS d'un programme (Windows ; tests)."""
    if sys.platform != "win32":
        return {"nice": os.getpriority(os.PRIO_PROCESS, pid)} if _linux() else {}
    k = _kernel32()
    process = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return {}
    try:
        eco = _Throttling(_THROTTLING_VERSION, 0, 0)
        k.GetProcessInformation(process, _PROCESS_POWER_THROTTLING, ctypes.byref(eco),
                                ctypes.sizeof(eco))
        return {"priority_class": k.GetPriorityClass(process),
                "ecoqos": bool(eco.ControlMask & eco.StateMask & _EXECUTION_SPEED)}
    finally:
        k.CloseHandle(process)
