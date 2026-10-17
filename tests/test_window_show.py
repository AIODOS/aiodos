"""AIODOS relancé alors qu'il tourne déjà, fenêtre cachée dans la zone de notification :
l'instance en place montre sa fenêtre (instance.wake_running, POST /api/window/show)."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from aiodos import instance, settings
from aiodos.api.server import create_app


@pytest.fixture
def running(data_dir):
    """Une « instance en place » : un petit serveur qui note ce qu'on lui demande."""
    seen = []
    reply = {"body": b'{"shown": true}'}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("X-Aiodos-Token")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply["body"])

        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lock = data_dir / "instance.json"
    lock.write_text(json.dumps({"pid": os.getpid(), "port": server.server_port,
                                "token": "jeton"}), encoding="utf-8")
    yield {"seen": seen, "reply": reply, "lock": lock, "port": server.server_port}
    server.shutdown()


def test_the_lock_keeps_port_and_token(data_dir):
    assert instance.acquire(8123, "jeton")
    lock = json.loads((data_dir / "instance.json").read_text(encoding="utf-8"))
    assert lock == {"pid": os.getpid(), "port": 8123, "token": "jeton"}
    instance.release()
    assert not (data_dir / "instance.json").exists()


def test_a_second_launch_asks_the_first_to_show_itself(running):
    assert instance.wake_running()
    assert running["seen"] == [("/api/window/show", "jeton")]


def test_no_window_there_means_the_usual_message(running):
    running["reply"]["body"] = b'{"shown": false}'          # nœud sans fenêtre
    assert not instance.wake_running()


def test_an_old_lock_without_token_or_a_dead_port(running):
    running["lock"].write_text(json.dumps({"pid": os.getpid(), "port": running["port"]}),
                               encoding="utf-8")
    assert not instance.wake_running()
    running["lock"].write_text(json.dumps({"pid": os.getpid(), "port": 1, "token": "jeton"}),
                               encoding="utf-8")
    assert not instance.wake_running(timeout=2)
    assert running["seen"] == []


def test_the_route_shows_the_window_of_this_instance(fake_embed):
    with TestClient(create_app("jeton", {"testserver"})) as client:
        auth = {"X-Aiodos-Token": "jeton"}
        assert client.post("/api/window/show").status_code == 401
        assert client.post("/api/window/show", headers=auth).json() == {"shown": False}
        calls = []
        instance.on_show(lambda: calls.append("montrée") or True)
        try:
            assert client.post("/api/window/show", headers=auth).json() == {"shown": True}
        finally:
            instance.on_show(None)
        assert calls == ["montrée"]


def test_closing_the_window_keeps_aiodos_running_by_default():
    assert settings.get("close_to_tray") is True
    assert settings.get("battery_mode") == "eco"
