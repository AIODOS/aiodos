"""Deux vrais compagnons aiodos-net, lancés et surveillés par AIODOS (network/companion.py),
relais et annuaires coupés : connexion directe sur 127.0.0.1. Chacun relaie vers un
faux serveur réseau d'AIODOS. Il faut le compagnon compilé (sinon le test est ignoré :
cd compagnon && cargo build --release).
"""
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from aiodos import instance, paths
from aiodos.network import companion as companion_mod
from aiodos.network.companion import Companion, CompanionError

pytestmark = pytest.mark.skipif(companion_mod.find_binary() is None,
                                reason="compagnon aiodos-net non compilé (voir compagnon/README.md)")

LOCAL = {"relays": ["none"], "directory": "none", "bind": "127.0.0.1:0", "portmap": False}


class FakeApp(BaseHTTPRequestHandler):
    """Faux p2p_app : renvoie ce qu'il voit ; /p2p/v1/stream/slow répond en deux morceaux."""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/p2p/v1/stream/slow":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b"un\n")
            self.server.release.wait(10)
            self._chunk(b"deux\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        reply = json.dumps({"path": self.path, "peer": self.headers.get("X-Aiodos-Peer"),
                            "token": self.headers.get("X-Aiodos-Net-Token"),
                            "body": body.decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def _chunk(self, data):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def _fake_app():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApp)
    server.release = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def two_companions(tmp_path):
    apps = [_fake_app(), _fake_app()]
    nodes = [Companion(f"127.0.0.1:{app.server_address[1]}",
                       dict(LOCAL, data_dir=tmp_path / name))
             for app, name in zip(apps, ("a", "b"))]
    for node in nodes:
        assert node.start(), node.error
    yield nodes, apps
    for node in nodes:
        node.stop()
    for app in apps:
        app.shutdown()


def _introduce(a, b):
    """Chacun met l'autre dans sa liste blanche, avec ses adresses (sans annuaire)."""
    for me, other in ((a, b), (b, a)):
        info = other.info()
        me.set_peers([{"id": info["id"], "addrs": info["addrs"]}])


def _via(me, other, path, body=None, **kwargs):
    return requests.post(me.proxy_base(other.id) + path, json=body, headers=me.headers,
                         timeout=20, **kwargs)


def test_companions_start_with_a_stable_identity(two_companions, tmp_path):
    (a, b), _ = two_companions
    assert a.running and b.running and a.id != b.id and len(a.id) == 64
    assert (tmp_path / "a" / "aiodos-net.key").exists()
    # Le jeton est exigé, et la clé secrète n'apparaît nulle part dans l'état
    assert requests.get(f"http://{a.listen}/net/v1/status", timeout=5).status_code == 401
    status = a.status()
    secret = (tmp_path / "a" / "aiodos-net.key").read_text().strip()
    assert status["id"] == a.id and secret not in json.dumps(status)
    # Relancé sur le même dossier, le compagnon garde la même clé
    first_id = a.id
    a.stop()
    assert a.start(), a.error
    assert a.id == first_id


def test_requests_and_streams_pass_through(two_companions):
    (a, b), (_, app_b) = two_companions
    _introduce(a, b)
    for i in range(3):
        reply = _via(a, b, "/p2p/v1/work/lease", {"n": i})
        assert reply.status_code == 200, reply.text
        seen = reply.json()
        assert seen["path"] == "/p2p/v1/work/lease" and json.loads(seen["body"]) == {"n": i}
        assert seen["peer"] == a.id and seen["token"] is None
    peer = next(p for p in a.status()["peers"] if p["id"] == b.id)
    assert peer["connected"] and peer["path"] == "direct" and peer["dials"] == 1

    # Réponse en flux : le premier morceau arrive avant que le second soit produit
    with _via(a, b, "/p2p/v1/stream/slow", {}, stream=True) as response:
        pieces = response.iter_content(chunk_size=None)
        assert next(pieces) == b"un\n"
        app_b.release.set()
        assert b"".join(pieces) == b"deux\n"


def test_machines_outside_the_allowlist_are_refused(two_companions):
    (a, b), _ = two_companions
    info = b.info()
    a.set_peers([{"id": info["id"], "addrs": info["addrs"]}])
    b.set_peers([])                                  # B ne connaît pas A
    refused = _via(a, b, "/p2p/v1/ping", {})
    assert refused.status_code >= 500
    assert b.status()["refused"]["count"] >= 1
    with pytest.raises(CompanionError):
        a.set_peers([{"id": "pas une clé"}])


NODE_THEN_KILLED = """
import sys, time
from aiodos.network.companion import Companion
node = Companion("127.0.0.1:9", {"relays": ["none"], "directory": "none",
                                 "bind": "127.0.0.1:0", "portmap": False,
                                 "data_dir": sys.argv[1]})
assert node.start(), node.error
print(node.process.pid, flush=True)
time.sleep(120)
"""


def test_the_companion_stops_when_aiodos_is_killed(tmp_path):
    """AIODOS arrêté brutalement (plantage, gestionnaire des tâches) : son compagnon ne
    reste pas en vie, orphelin (il voit son entrée standard se fermer)."""
    node = subprocess.Popen([sys.executable, "-c", NODE_THEN_KILLED, str(tmp_path / "net")],
                            cwd=paths.APP_ROOT, stdout=subprocess.PIPE, text=True)
    pid = int(node.stdout.readline())
    assert instance._alive(pid)
    node.kill()
    node.wait(10)
    deadline = time.time() + 15
    while time.time() < deadline and instance._alive(pid):
        time.sleep(0.2)
    assert not instance._alive(pid), "compagnon resté en vie après la mort d'AIODOS"


def test_stopping_during_launch_leaves_no_companion_behind(tmp_path, monkeypatch):
    """Internet activé puis désactivé aussitôt : le compagnon en train de démarrer ne
    reste pas en vie, ni maintenant, ni après la fin de son lancement."""
    launched = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(companion_mod.subprocess, "Popen", recording_popen)
    node = Companion("127.0.0.1:9", dict(LOCAL, data_dir=tmp_path / "net"))
    starter = threading.Thread(target=node.start)
    starter.start()
    while not launched:
        time.sleep(0.01)
    node.stop()                                      # avant, ou juste après, « prêt »
    starter.join(40)
    deadline = time.time() + 15
    while time.time() < deadline and any(p.poll() is None for p in launched):
        time.sleep(0.1)
    assert all(p.poll() is not None for p in launched)
    assert not node.running


def test_a_crashed_companion_is_restarted(two_companions):
    (a, b), _ = two_companions
    generation = a.generation
    a.process.kill()
    deadline = time.time() + 20
    while time.time() < deadline and not (a.generation > generation and a.running):
        time.sleep(0.2)
    assert a.running and a.generation == generation + 1
    _introduce(a, b)                                 # liste blanche à redonner après relance
    assert _via(a, b, "/p2p/v1/ping", {}).status_code == 200
