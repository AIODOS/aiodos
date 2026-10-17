"""Programmes lancés par AIODOS qui s'arrêtent avec lui, même s'il est tué.

Sous Windows, ils sont rangés dans un « objet de travail » (job object) créé avec
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE : quand le dernier descripteur de cet objet se ferme
(fin d'AIODOS, normale ou brutale), Windows arrête tout ce qu'il contient. Sans cela, un
llama-server ou un serveur RPC resterait seul après un plantage, en gardant la mémoire de
la carte graphique.

Ailleurs, rien de plus : AIODOS les arrête lui-même à sa fermeture. (Sous Linux,
PR_SET_PDEATHSIG suit la fin du thread qui a lancé le programme, pas celle du processus :
un thread de passage l'arrêterait trop tôt.)
"""
import ctypes
import logging
import sys
import threading

log = logging.getLogger("aiodos")

_lock = threading.Lock()
_job = {"handle": None}

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
