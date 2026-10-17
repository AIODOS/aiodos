"""Mesures du processeur sous Windows, sans dépendance (ctypes), pour
tools/bench_background.py.

D'où viennent les temps (en centaines de nanosecondes) :
  - GetSystemTimes : inactivité, noyau (inactivité comprise) et utilisateur, pour
    toute la machine ;
  - NtQuerySystemInformation(SystemProcessorPerformanceInformation) : les mêmes, par
    processeur logique ;
  - GetSystemCpuSetInformation : classe d'efficacité de chaque processeur logique
    (processeur hybride : la plus basse désigne les cœurs efficaces, « E », les
    autres les cœurs performants, « P ») ;
  - CreateToolhelp32Snapshot : processus (nom, parent) et threads (priorité de base) ;
  - GetProcessTimes, GetThreadTimes : temps de calcul d'un processus, d'un thread.

Sampler.sample() renvoie, depuis l'appel précédent, la charge de la machine (en %
de tous les processeurs logiques), celle des cœurs P et E, et pour chaque groupe de
processus suivi le nombre de « cœurs occupés » (1,0 = un processeur logique à plein).
"""
import ctypes
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPTHREAD = 0x4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION = 8
ALL_PROCESSOR_GROUPS = 0xFFFF
INVALID_HANDLE = ctypes.c_void_p(-1).value


class FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", wintypes.DWORD)]


class PROCESSOR_TIMES(ctypes.Structure):           # SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION
    _fields_ = [("IdleTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
                ("UserTime", ctypes.c_longlong), ("DpcTime", ctypes.c_longlong),
                ("InterruptTime", ctypes.c_longlong), ("InterruptCount", ctypes.c_ulong)]


class CPU_SET(ctypes.Structure):
    _fields_ = [("Id", wintypes.DWORD), ("Group", wintypes.WORD),
                ("LogicalProcessorIndex", ctypes.c_ubyte), ("CoreIndex", ctypes.c_ubyte),
                ("LastLevelCacheIndex", ctypes.c_ubyte), ("NumaNodeIndex", ctypes.c_ubyte),
                ("EfficiencyClass", ctypes.c_ubyte), ("AllFlags", ctypes.c_ubyte),
                ("Reserved", wintypes.DWORD), ("AllocationTag", ctypes.c_ulonglong)]


class SYSTEM_CPU_SET_INFORMATION(ctypes.Structure):
    _fields_ = [("Size", wintypes.DWORD), ("Type", wintypes.DWORD), ("CpuSet", CPU_SET)]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenThread.restype = wintypes.HANDLE
kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
kernel32.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]


def _ticks(ft: FILETIME) -> int:
    return (ft.high << 32) | ft.low


def system_times() -> tuple:
    """(inactivité, occupé) de toute la machine, cumulés depuis le démarrage."""
    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    total = _ticks(kernel) + _ticks(user)            # le temps noyau compte l'inactivité
    return _ticks(idle), total - _ticks(idle)


def processor_times() -> list:
    """[(inactivité, occupé)] par processeur logique du groupe courant."""
    count = kernel32.GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)
    buffer = (PROCESSOR_TIMES * count)()
    length = wintypes.ULONG()
    status = ntdll.NtQuerySystemInformation(SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION, buffer,
                                            ctypes.sizeof(buffer), ctypes.byref(length))
    if status != 0:
        raise OSError(f"NtQuerySystemInformation : statut {status:#x}")
    n = length.value // ctypes.sizeof(PROCESSOR_TIMES)
    return [(buffer[i].IdleTime, buffer[i].KernelTime + buffer[i].UserTime - buffer[i].IdleTime)
            for i in range(n)]


def efficiency_classes() -> list:
    """Classe d'efficacité de chaque processeur logique (groupe 0), dans l'ordre."""
    length = wintypes.ULONG()
    kernel32.GetSystemCpuSetInformation(None, 0, ctypes.byref(length), None, 0)
    buffer = ctypes.create_string_buffer(length.value)
    if not kernel32.GetSystemCpuSetInformation(buffer, length, ctypes.byref(length), None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    found, offset = {}, 0
    while offset + ctypes.sizeof(SYSTEM_CPU_SET_INFORMATION) <= length.value:
        entry = SYSTEM_CPU_SET_INFORMATION.from_buffer_copy(buffer, offset)
        if entry.Type == 0 and entry.CpuSet.Group == 0:          # CpuSetInformation
            found[entry.CpuSet.LogicalProcessorIndex] = entry.CpuSet.EfficiencyClass
        offset += entry.Size or ctypes.sizeof(SYSTEM_CPU_SET_INFORMATION)
    return [found[i] for i in sorted(found)]


def processes() -> list:
    """[(pid, parent, nom de l'exécutable)] de tous les processus."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE):
        raise ctypes.WinError(ctypes.get_last_error())
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def threads_of(pid: int) -> list:
    """[(thread, priorité de base)] des threads du processus `pid`."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot in (None, INVALID_HANDLE):
        raise ctypes.WinError(ctypes.get_last_error())
    out = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                out.append((entry.th32ThreadID, entry.tpBasePri))
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def _cpu(handle, getter) -> int | None:
    times = [FILETIME() for _ in range(4)]
    if not getter(handle, *(ctypes.byref(t) for t in times)):
        return None
    return _ticks(times[2]) + _ticks(times[3])             # noyau + utilisateur


def process_cpu(pid: int) -> int | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        return _cpu(handle, kernel32.GetProcessTimes)
    finally:
        kernel32.CloseHandle(handle)


def thread_cpu(tid: int) -> int | None:
    handle = kernel32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not handle:
        return None
    try:
        return _cpu(handle, kernel32.GetThreadTimes)
    finally:
        kernel32.CloseHandle(handle)


def descendants(roots: set, table: list) -> set:
    """Les processus de `roots` et tous leurs descendants."""
    found = set(roots)
    grew = True
    while grew:
        grew = False
        for pid, parent, _name in table:
            if parent in found and pid not in found:
                found.add(pid)
                grew = True
    return found


class Sampler:
    """Charge entre deux appels de sample().

    groups : {nom: fonction(table des processus) -> ensemble de pid}. Un processus apparu
    entre deux mesures n'est compté qu'à partir de la mesure suivante.
    watch_threads : pid dont les threads sont suivis un par un (priorité de base,
    temps de calcul), pour séparer le travail de fond du reste."""

    def __init__(self, groups: dict, watch_threads: int | None = None):
        self.groups = groups
        self.watch_threads = watch_threads
        classes = efficiency_classes()
        lowest = min(classes) if classes and len(set(classes)) > 1 else None
        self.kinds = ["E" if lowest is not None and c == lowest else "P" for c in classes]
        self._last = None

    def _read(self) -> dict:
        table = processes()
        pids = {name: pick(table) for name, pick in self.groups.items()}
        cpu = {pid: process_cpu(pid) for members in pids.values() for pid in members}
        threads = {}
        if self.watch_threads:
            for tid, priority in threads_of(self.watch_threads):
                threads[tid] = (priority, thread_cpu(tid))
        return {"at": time.perf_counter(), "system": system_times(),
                "processors": processor_times(), "pids": pids, "cpu": cpu,
                "threads": threads}

    def sample(self) -> dict | None:
        now = self._read()
        last, self._last = self._last, now
        if last is None:
            return None
        wall = (now["at"] - last["at"]) * 1e7               # en centaines de ns
        idle = now["system"][0] - last["system"][0]
        busy = now["system"][1] - last["system"][1]
        result = {"system": 100 * busy / max(1, idle + busy)}
        per_kind = {}
        for i, ((idle1, busy1), (idle0, busy0)) in enumerate(zip(now["processors"],
                                                              last["processors"])):
            kind = self.kinds[i] if i < len(self.kinds) else "P"
            total = (idle1 - idle0) + (busy1 - busy0)
            per_kind.setdefault(kind, []).append(100 * (busy1 - busy0) / max(1, total))
        for kind, values in per_kind.items():
            result[f"cores_{kind}"] = sum(values) / len(values)
        for name, members in now["pids"].items():
            used = 0
            for pid in members:
                before, after = last["cpu"].get(pid), now["cpu"].get(pid)
                if before is not None and after is not None and after >= before:
                    used += after - before
            result[name] = used / wall
        if self.watch_threads:
            lowered = normal = 0
            for tid, (priority, after) in now["threads"].items():
                before = last["threads"].get(tid, (None, None))[1]
                if before is None or after is None or after < before:
                    continue
                if priority < 8:
                    lowered += after - before
                else:
                    normal += after - before
            result["threads_lowered"] = lowered / wall
            result["threads_normal"] = normal / wall
            result["threads_lowered_count"] = sum(1 for p, _ in now["threads"].values() if p < 8)
        return result
