"""Lancement de vrais nœuds AIODOS dans des processus séparés (tests réseau).

Chaque nœud a son propre dossier de données et écoute sur 127.0.0.1 : pas de
pare-feu à franchir, pas de diffusion UDP (ajout par adresse).
"""
import os
import re
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
    def __init__(self, data_dir: Path, name: str, args=()):
        self.data_dir = data_dir
        self.name = name
        self.args = list(args)            # options supplémentaires (ex. --slots embed=0)
        self.port = free_port()
        self.p2p_port = free_port()
        self.token = None
        self.process = None
        self.output = []

    def start(self, timeout=40):
        env = dict(os.environ, AIODOS_DATA_DIR=str(self.data_dir), PYTHONIOENCODING="utf-8")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "aiodos", "--headless", "--no-migrate",
             "--port", str(self.port), "--network", "--p2p-host", "127.0.0.1",
             "--p2p-port", str(self.p2p_port), "--no-discovery", *self.args],
            cwd=APP_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def pair(a: Node, b: Node):
    """Appairage complet de deux nœuds (B trouve A, codes confirmés des deux côtés)."""
    a_id, b_id = a.node_id, b.node_id
    b.api("POST", "/network/add", {"address": f"127.0.0.1:{a.p2p_port}"})
    b.api("POST", "/network/pair", {"node_id": a_id})
    a.wait_for(lambda: a.api("GET", "/network")["pairing"], what="demande reçue")
    b.api("POST", "/network/pair/answer", {"node_id": a_id, "accept": True})
    a.api("POST", "/network/pair/answer", {"node_id": b_id, "accept": True})
    b.wait_for(lambda: [p for p in b.api("GET", "/network")["peers"] if p["online"]],
               what="appairage côté B")
    a.wait_for(lambda: [p for p in a.api("GET", "/network")["peers"] if p["online"]],
               what="appairage côté A")
    return a_id, b_id
