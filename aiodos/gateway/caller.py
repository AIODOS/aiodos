"""Qui appelle la passerelle : le programme et le compte Windows derrière une connexion
(docs/api-applications.md § 4 ; conception : docs/conception/applications-v1.md § 3.5).

Vérifié sous Windows, par ctypes, sans dépendance :
1. la connexion vient de ce PC (127.0.0.1) ;
2. le programme : GetExtendedTcpTable(TCP_TABLE_OWNER_PID_CONNECTIONS) donne la ligne
   127.0.0.1:<port du client> → 127.0.0.1:<port de la passerelle> et son numéro de
   programme, puis QueryFullProcessImageNameW son chemin ;
3. le compte : OpenProcessToken, GetTokenInformation(TokenUser), comparé à celui d'AIODOS.

À chaque requête qui porte une clé d'appli et à chaque demande d'accès, jamais en cache par
port (un port éphémère peut passer à un autre programme). Hors de Windows : inconnu (None).

Ce qui ne se vérifie pas : le nom que l'appli se donne, le script que fait tourner un
interpréteur (python.exe, node.exe…), l'authenticité du programme. Une clé d'appli ne
protège pas d'un programme malveillant du même compte : elle dit qui fait quoi, limite les
droits, et ferme la porte aux autres comptes, aux pages web et au réseau.
"""
import ctypes
import functools
import logging
import os
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath

log = logging.getLogger("aiodos.gateway")

_TCP_TABLE_OWNER_PID_CONNECTIONS = 4
_ERROR_INSUFFICIENT_BUFFER = 122
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TH32CS_SNAPPROCESS = 0x2
# Programmes qui font tourner n'importe quel script : la carte d'accord le dit, et une clé
# ne peut pas leur être liée
GENERIC = {"python.exe", "pythonw.exe", "py.exe", "pyw.exe", "node.exe", "electron.exe",
           "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "bash.exe",
           "wsl.exe", "curl.exe", "java.exe", "javaw.exe", "deno.exe", "bun.exe"}


@dataclass(frozen=True)
class Caller:
    pid: int | None
    program: str | None             # chemin complet du programme
    same_account: bool | None       # None : inconnu (hors de Windows, droits refusés)

    @property
    def generic(self) -> bool:
        return bool(self.program) and PureWindowsPath(self.program).name.lower() in GENERIC

    @property
    def name(self) -> str:
        return PureWindowsPath(self.program).name if self.program else ""


UNKNOWN = Caller(None, None, None)


def _windows() -> bool:
    return sys.platform == "win32"


# ------------------------------------------------------------- connexion → pid
def connection_pid(client_port: int, server_port: int) -> int | None:
    """Le programme qui tient la connexion 127.0.0.1:client_port → 127.0.0.1:server_port."""
    if not _windows():
        return None
    from ctypes import wintypes
    try:
        get = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    except OSError:
        return None
    get.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    get.restype = wintypes.DWORD
    size = wintypes.DWORD(0)
    table = None
    for _ in range(4):               # la table peut grandir entre deux appels
        table = ctypes.create_string_buffer(max(size.value, 4))
        error = get(table, ctypes.byref(size), False, socket.AF_INET,
                    _TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if error != _ERROR_INSUFFICIENT_BUFFER:
            break
    if error != 0:
        return None
    # MIB_TCPROW_OWNER_PID : état, adresse et port locaux, adresse et port distants, pid
    for i in range(struct.unpack_from("<I", table, 0)[0]):
        at = 4 + i * 24
        _, local_addr, local_port, remote_addr, remote_port, pid = struct.unpack_from(
            "<IIIIII", table, at)
        if (socket.ntohs(local_port & 0xFFFF) == client_port
                and socket.ntohs(remote_port & 0xFFFF) == server_port
                and local_addr == remote_addr == 0x0100007F):          # 127.0.0.1
            return pid
    return None


# --------------------------------------------------------------- pid → programme
def _kernel():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return kernel


def program_of(pid) -> str | None:
    """Chemin complet du programme de ce numéro, ou None."""
    if not _windows() or not pid:
        return None
    from ctypes import wintypes
    kernel = _kernel()
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


def sid_of(pid) -> str | None:
    """SID du compte de ce programme, ou None (droits refusés, programme fini)."""
    if not _windows() or not pid:
        return None
    from ctypes import wintypes
    kernel = _kernel()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
            buffer = ctypes.create_string_buffer(max(needed.value, 1))
            if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
                return None
            sid = ctypes.c_void_p.from_buffer(buffer).value
            out = ctypes.c_void_p()
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(out)):
                return None
            try:
                return ctypes.wstring_at(out.value)
            finally:
                kernel.LocalFree(out)
        finally:
            kernel.CloseHandle(token)
    finally:
        kernel.CloseHandle(handle)


@functools.cache
def my_sid() -> str | None:
    return sid_of(os.getpid())


def parent_of(pid) -> int | None:
    """Le programme qui a lancé celui-ci (instantané des programmes de Windows)."""
    if not _windows() or not pid:
        return None
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == int(pid):
                return int(entry.th32ParentProcessID) or None
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        kernel.CloseHandle(snapshot)


# ------------------------------------------------------------------ l'appelant
def of_pid(pid) -> Caller:
    """Le programme de ce numéro et son compte, comparé à celui d'AIODOS."""
    if not pid:
        return UNKNOWN
    program = program_of(pid)
    sid, mine = sid_of(pid), my_sid()
    same = None if (sid is None or mine is None) else sid == mine
    return Caller(int(pid), program, same)


def identify(scope) -> Caller:
    """L'appelant d'une requête (scope ASGI) : son programme et son compte ; UNKNOWN hors de
    Windows ou si la table ne le dit pas."""
    client, server = scope.get("client"), scope.get("server")
    if not _windows() or not client or not server:
        return UNKNOWN
    if client[0] not in ("127.0.0.1", "::ffff:127.0.0.1"):
        return Caller(None, None, False)          # pas de ce PC : refusé plus haut
    try:
        pid = connection_pid(int(client[1]), int(server[1]))
    except (OSError, ValueError, struct.error) as exc:
        log.debug("Table TCP illisible : %s", exc)
        return UNKNOWN
    return of_pid(pid) if pid else UNKNOWN


def holder_of(port: int) -> str | None:
    """Le programme qui écoute sur ce port (message « port déjà pris »)."""
    from aiodos.imaging import engine as images_engine
    try:
        pids = images_engine._listeners(port) or set()
    except Exception:
        return None
    for pid in pids:
        if isinstance(pid, int) and pid != os.getpid():
            program = program_of(pid)
            if program:
                return program
    return None
