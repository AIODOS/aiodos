"""Lancement de vrais nœuds AIODOS dans des processus séparés (tests réseau).

Chaque nœud a son propre dossier de données et écoute sur 127.0.0.1 : pas de
pare-feu à franchir, pas de diffusion UDP (ajout par adresse).
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

APP_ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Node:
    def __init__(self, data_dir: Path, name: str, args=(), root: Path = APP_ROOT):
        self.data_dir = data_dir
        self.name = name
        self.args = list(args)            # options supplémentaires (ex. --slots embed=0)
        self.root = root                  # code lancé (une copie : machine installée)
        self.port = free_port()
        self.p2p_port = free_port()
        self.token = None
        self.process = None
        self.output = []

    def preset(self, **values):
        """Réglages écrits dans la base du nœud avant son démarrage (adresse d'un faux
        Ollama, passerelle ouverte…) : il démarre directement avec."""
        from aiodos import db, settings
        self.data_dir.mkdir(parents=True, exist_ok=True)
        previous = os.environ.get("AIODOS_DATA_DIR")
        os.environ["AIODOS_DATA_DIR"] = str(self.data_dir)
        try:
            settings.set_many(values)
        finally:
            db.close()
            if previous is None:
                os.environ.pop("AIODOS_DATA_DIR", None)
            else:
                os.environ["AIODOS_DATA_DIR"] = previous
        return self

    def start(self, timeout=40):
        # Jamais de fenêtre d'erreur sur le bureau : un nœud d'essai n'a personne devant lui.
        # Secteur et inactivité simulés, comme dans conftest.py, aussi pour les nœuds lancés
        # avant lui (fixtures de module) : sinon, PC sur batterie ou utilisé, ils ne
        # prêteraient rien et l'essai dépendrait de l'état du PC
        env = dict(os.environ, AIODOS_DATA_DIR=str(self.data_dir), PYTHONIOENCODING="utf-8",
                   AIODOS_NO_DIALOG="1",
                   AIODOS_SIMULATE_POWER=os.environ.get("AIODOS_SIMULATE_POWER", "ac"),
                   AIODOS_SIMULATE_IDLE=os.environ.get("AIODOS_SIMULATE_IDLE", "86400"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "aiodos", "--headless", "--no-migrate",
             "--port", str(self.port), "--network", "--p2p-host", "127.0.0.1",
             "--p2p-port", str(self.p2p_port), "--no-discovery", *self.args],
            cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        ready = threading.Event()

        def read():
            for line in self.process.stdout:
                self.output.append(line)
                found = re.search(r"\?t=([\w-]+)", line)
                if found:
                    self.token = found.group(1)
                    ready.set()
        threading.Thread(target=read, daemon=True).start()
        if not ready.wait(timeout):
            self.stop()
            raise RuntimeError("Le nœud n'a pas démarré :\n" + "".join(self.output[-30:]))
        self.api("PUT", "/settings", {"node_name": self.name})
        return self

    def api(self, method, path, body=None, expect=200):
        response = requests.request(method, f"http://127.0.0.1:{self.port}/api{path}",
                                    json=body, headers={"X-Aiodos-Token": self.token},
                                    timeout=30)
        assert response.status_code == expect, response.text
        return response.json()

    def wait_for(self, predicate, timeout=20, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        raise AssertionError(f"Délai dépassé en attendant : {what}")

    @property
    def node_id(self) -> str:
        return self.api("GET", "/network")["node_id"]

    def stop(self):
        """Arrête le nœud et tout ce qu'il a lancé. Après une mise à jour, le nœud tourne
        dans un autre processus (instance.json), surveillé par un lanceur qui, s'il le
        voyait s'arrêter seul, relancerait la version précédente : resté seul, ce nœud
        retenterait sa mise à jour une heure plus tard, longtemps après l'essai. On arrête
        donc d'abord ce lanceur, puis le nœud, puis le processus lancé par l'essai, chacun
        avec ses enfants (compagnon)."""
        pids = []
        holder = self._holder()
        if holder:
            parent = _parent_python(holder)
            if parent:
                pids.append(parent)
            pids.append(holder)
        if self.process:
            pids.append(self.process.pid)
        for pid in dict.fromkeys(pids):
            _kill_tree(pid)
        if self.process:
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _holder(self) -> int | None:
        try:
            return int(json.loads((self.data_dir / "instance.json")
                                  .read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None


def _kill_tree(pid: int):
    """Le processus et ses enfants ; sans erreur s'il n'est plus là."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _parent_python(pid: int) -> int | None:
    """Le lanceur d'AIODOS qui surveille `pid` : son parent, s'il est un Python autre que
    celui des essais. Sous Windows seulement (ailleurs, le nœud relancé remplace son
    processus : execve)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    processes = {}
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
    while ok:
        processes[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
        ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    kernel32.CloseHandle(snapshot)
    parent = processes.get(pid, (None, ""))[0]
    if not parent or parent == os.getpid() or parent not in processes:
        return None
    return parent if processes[parent][1] in ("python.exe", "pythonw.exe") else None


def pair(a: Node, b: Node, a_trusts="mine", b_trusts="mine"):
    """Appairage complet de deux nœuds (B trouve A, codes confirmés des deux côtés).
    a_trusts : ce que B est pour A (« mine », ou « friend » : un ami), et inversement."""
    a_id, b_id = a.node_id, b.node_id
    b.api("POST", "/network/add", {"address": f"127.0.0.1:{a.p2p_port}"})
    b.api("POST", "/network/pair", {"node_id": a_id})
    a.wait_for(lambda: a.api("GET", "/network")["pairing"], what="demande reçue")
    b.api("POST", "/network/pair/answer", {"node_id": a_id, "accept": True, "trust": b_trusts})
    a.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": True, "trust": a_trusts})
    b.wait_for(lambda: [p for p in b.api("GET", "/network")["peers"] if p["online"]],
               what="appairage côté B")
    a.wait_for(lambda: [p for p in a.api("GET", "/network")["peers"] if p["online"]],
               what="appairage côté A")
    return a_id, b_id


def pair_by_invitation(inviter: Node, friend: Node, trust="friend"):
    """Appairage sans réseau local (étape 2.4), par les compagnons (nœuds lancés avec
    --internet-local) : invitation, réponse, puis le même code confirmé des deux côtés.
    trust : ce que chacune est pour l'autre (« friend » : un ami, ou « mine »)."""
    for node in (inviter, friend):
        node.wait_for(lambda: node.api("GET", "/network/internet")["running"],
                      what="compagnon prêt")
    a_id, b_id = inviter.node_id, friend.node_id
    invitation = inviter.api("POST", "/network/invitations")
    reply = friend.api("POST", "/network/invitations/accept", {"code": invitation["code"]})
    started = inviter.api("POST", f"/network/invitations/{invitation['id']}/reply",
                          {"code": reply["reply"]})
    pending = friend.wait_for(lambda: friend.api("GET", "/network")["pairing"],
                              what="demande d'appairage reçue")
    assert pending[0]["code"] == started["code"]
    friend.api("POST", "/network/pair/answer", {"node_id": a_id, "accept": True, "trust": trust})
    inviter.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": True, "trust": trust})
    for node, other_id in ((inviter, b_id), (friend, a_id)):
        node.wait_for(lambda: [p for p in node.api("GET", "/network")["peers"]
                               if p["node_id"] == other_id and p["online"]],
                      timeout=40, what="appairage sur invitation")
    return a_id, b_id
