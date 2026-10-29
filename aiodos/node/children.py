"""Programmes lancés par AIODOS qui s'arrêtent avec lui, même s'il est tué.

Sous Windows, ils sont rangés dans un « objet de travail » (job object) créé avec
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE : quand le dernier descripteur de cet objet se ferme
(fin d'AIODOS, normale ou brutale), Windows arrête tout ce qu'il contient. Sans cela, un
llama-server ou un serveur RPC resterait seul après un plantage, en gardant la mémoire de
la carte graphique.

Ailleurs, rien de plus : AIODOS les arrête lui-même à sa fermeture. (Sous Linux,
PR_SET_PDEATHSIG suit la fin du thread qui a lancé le programme, pas celle du processus :
un thread de passage l'arrêterait trop tôt.)

La mémoire vive qu'ils occupent, eux et ce qu'ils lancent (les modèles du routeur llama.cpp),
se lit aussi ici (resident) : celle des moteurs d'AIODOS, que node/places.py ne compte pas
contre ce qui est laissé à l'utilisateur (PC de l'auteur, 29/10/2026 : le modèle de
conversation chargé, 8,7 Go sur 32, et la machine se disait pleine pour le réseau public).
"""
import ctypes
import logging
import os
import sys
import threading

log = logging.getLogger("aiodos")

_lock = threading.Lock()
_job = {"handle": None}
_pids = set()          # ailleurs que sous Windows : les programmes lancés (resident)
MAX_LISTED = 512       # processus de l'objet de travail lus, au plus

if sys.platform == "win32":
    from ctypes import wintypes

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD,
                                                    ctypes.c_void_p]
    _kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    _kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                                  wintypes.DWORD]
    JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class _ProcessIds(ctypes.Structure):
        _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * MAX_LISTED)]

    class _MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    *((name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage"))]

    def _windows_resident() -> int:
        """Les pages en mémoire (working set) de tous les processus de l'objet de travail :
        les programmes rattachés et ceux qu'ils ont lancés, qui y entrent d'eux-mêmes."""
        with _lock:
            job = _job["handle"]
        if job is None:
            return 0
        listed = _ProcessIds()
        if not _kernel32.QueryInformationJobObject(job, JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                                                   ctypes.byref(listed), ctypes.sizeof(listed),
                                                   None):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject")
        total = 0
        for pid in listed.ProcessIdList[:listed.NumberOfProcessIdsInList]:
            process = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not process:
                continue                    # fini entre-temps
            try:
                counters = _MemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if _kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(counters),
                                                     counters.cb):
                    total += counters.WorkingSetSize
            finally:
                _kernel32.CloseHandle(process)
        return total

    def _windows_job():
        """L'objet de travail d'AIODOS, créé au premier programme ; son descripteur reste
        ouvert jusqu'à la fin du processus (c'est sa fermeture qui arrête les enfants)."""
        with _lock:
            if _job["handle"] is None:
                handle = _kernel32.CreateJobObjectW(None, None)
                if not handle:
                    raise OSError(ctypes.get_last_error(), "CreateJobObject")
                limits = _ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not _kernel32.SetInformationJobObject(
                        handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                        ctypes.byref(limits), ctypes.sizeof(limits)):
                    error = ctypes.get_last_error()
                    _kernel32.CloseHandle(handle)
                    raise OSError(error, "SetInformationJobObject")
                _job["handle"] = handle
            return _job["handle"]


def bind(pid: int) -> bool:
    """Le programme `pid` s'arrêtera avec AIODOS. Vrai si c'est garanti (Windows)."""
    if sys.platform != "win32":
        with _lock:
            _pids.add(pid)                  # sa mémoire, pour resident
        return False
    try:
        job = _windows_job()
        process = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess")
        try:
            if not _kernel32.AssignProcessToJobObject(job, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        finally:
            _kernel32.CloseHandle(process)
        return True
    except OSError as exc:                 # jamais bloquant : AIODOS l'arrêtera lui-même
        log.warning("Programme %s non rattaché à AIODOS : %s", pid, exc)
        return False


def resident() -> int:
    """La mémoire vive qu'occupent en ce moment les programmes lancés par AIODOS et ceux
    qu'ils ont lancés eux-mêmes, en octets ; 0 si elle ne se lit pas."""
    try:
        return _windows_resident() if sys.platform == "win32" else _proc_resident()
    except OSError as exc:
        log.debug("Mémoire des programmes d'AIODOS illisible : %s", exc)  # i18n: tel quel
        return 0


def _proc_resident(proc: str = "/proc") -> int:
    """Linux et Android : les programmes rattachés encore en vie et leurs descendants
    (/proc/<pid>/stat : le parent), pages résidentes de /proc/<pid>/statm."""
    with _lock:
        roots = set(_pids)
    if not roots:
        return 0
    parents = {}
    for entry in os.listdir(proc):
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc}/{entry}/stat", "rb") as f:
                stat = f.read()
            parents[int(entry)] = int(stat[stat.rindex(b")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue                        # fini entre-temps
    alive = roots & set(parents)
    with _lock:
        _pids.intersection_update(alive | (_pids - roots))    # les finis oubliés
    ours, grew = set(alive), bool(alive)
    while grew:
        found = {pid for pid, parent in parents.items() if parent in ours} - ours
        ours |= found
        grew = bool(found)
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    total = 0
    for pid in ours:
        try:
            with open(f"{proc}/{pid}/statm", encoding="ascii") as f:
                total += int(f.read().split()[1]) * page
        except (OSError, ValueError, IndexError):
            continue
    return total
