"""AIODOS relancé alors qu'il tourne déjà, fenêtre cachée dans la zone de notification :
l'instance en place montre sa fenêtre (instance.wake_running, POST /instance/show), avec
une clé propre à cette demande : le jeton de l'API ne quitte pas la mémoire du nœud."""
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
            seen.append((self.path, self.headers.get("X-Aiodos-Show"),
                         self.headers.get("X-Aiodos-Token")))
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
                                "show_key": "cle-de-test"}), encoding="utf-8")
    yield {"seen": seen, "reply": reply, "lock": lock, "port": server.server_port}
    server.shutdown()


def test_the_lock_keeps_the_port_and_a_key_of_its_own(data_dir):
    assert instance.acquire(8123, "cle-de-test")
    lock = json.loads((data_dir / "instance.json").read_text(encoding="utf-8"))
    assert lock == {"pid": os.getpid(), "port": 8123, "show_key": "cle-de-test"}
    instance.release()
    assert not (data_dir / "instance.json").exists()


def test_a_second_launch_asks_the_first_to_show_itself(running):
    assert instance.wake_running()
    assert running["seen"] == [("/instance/show", "cle-de-test", None)]   # jamais le jeton


def test_no_window_there_means_the_usual_message(running):
    running["reply"]["body"] = b'{"shown": false}'          # nœud sans fenêtre
    assert not instance.wake_running()


def test_an_old_lock_without_key_or_a_dead_port(running):
    running["lock"].write_text(json.dumps({"pid": os.getpid(), "port": running["port"],
                                           "token": "jeton"}), encoding="utf-8")
    assert not instance.wake_running()
    running["lock"].write_text(json.dumps({"pid": os.getpid(), "port": 1,
                                           "show_key": "cle-de-test"}), encoding="utf-8")
    assert not instance.wake_running(timeout=2)
    assert running["seen"] == []


def test_the_route_wants_its_own_key_not_the_api_token(fake_embed):
    with TestClient(create_app("jeton", {"testserver"}, show_key="cle-de-test")) as client:
        def show(**headers):
            return client.post("/instance/show", headers=headers)
        assert show().status_code == 403
        assert show(**{"X-Aiodos-Token": "jeton"}).status_code == 403
        assert show(**{"X-Aiodos-Show": "autre"}).status_code == 403
        assert show(**{"X-Aiodos-Show": "cle-de-test"}).json() == {"shown": False}
        calls = []
        instance.on_show(lambda: calls.append("montrée") or True)
        try:
            assert show(**{"X-Aiodos-Show": "cle-de-test"}).json() == {"shown": True}
        finally:
            instance.on_show(None)
        assert calls == ["montrée"]
        assert show(**{"X-Aiodos-Show": "cle-de-test", "Host": "evil.example"}).status_code == 400


def test_without_a_key_nothing_can_bring_the_window_back(fake_embed):
    with TestClient(create_app("jeton", {"testserver"})) as client:
        assert client.post("/instance/show", headers={"X-Aiodos-Show": ""}).status_code == 403


def test_defaults_closing_keeps_aiodos_and_battery_is_spared():
    assert settings.get("close_to_tray") is True
    assert settings.get("battery_mode") == "eco"
