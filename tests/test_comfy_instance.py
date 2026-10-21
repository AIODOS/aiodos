"""Le ComfyUI que lance AIODOS quand celui de l'utilisateur n'est pas ouvert
(engines/comfy_instance.py, livraison L2c) : avec une fausse installation dont le main.py
répond comme ComfyUI (/system_stats). Lancé sans fenêtre dans son dossier, rattaché à AIODOS,
arrêté sur demande ; ses modèles lus sur le disque ; son inventaire gardé d'un lancement à
l'autre ; annoncé à mes machines seulement s'il connaît ses classes."""
import os
import sys
from pathlib import Path

import pytest

from aiodos import settings
from aiodos.engines import comfy_find, comfy_instance, lending

FAKE_MAIN = '''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        answers = {
            "/system_stats": {"system": {"comfyui_version": "0.35.1", "argv": sys.argv},
                              "devices": [{"name": "cuda:0 essai", "vram_total": 8 * 2 ** 30}]},
            "/object_info": {"KSampler": {"input": {}, "output": ["LATENT"],
                                          "python_module": "nodes"}},
            "/models": ["checkpoints"], "/models/checkpoints": ["sous/base.safetensors"]}
        if self.path not in answers:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(answers[self.path]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


@pytest.fixture
def installed(tmp_path, monkeypatch):
    code = tmp_path / "ComfyUI"
    (code / "comfy").mkdir(parents=True)
    (code / "custom_nodes").mkdir()
    (code / "main.py").write_text(FAKE_MAIN, encoding="utf-8")
    models = tmp_path / "modeles"
    (models / "checkpoints" / "sous").mkdir(parents=True)
    (models / "checkpoints" / "sous" / "base.safetensors").write_bytes(b"0")
    (models / "checkpoints" / "notes.txt").write_text("pas un modèle", encoding="utf-8")
    found = comfy_find.Install(comfyui=code, python=Path(sys.executable), kind="setting",
                               version="0.35.1")
    monkeypatch.setattr(comfy_instance, "install", lambda: found)
    monkeypatch.setattr(comfy_instance, "model_roots", lambda inst: [models])
    comfy_instance._disk.update(at=0.0, value=None)
    settings.set_many({"engines_comfyui_enabled": True, "engines_comfyui_launch": True})
    yield found
    comfy_instance.stop_instance()


def test_launched_quietly_in_its_own_folder_then_stopped(installed):
    assert comfy_instance.launchable() and comfy_instance.current() is None
    running = comfy_instance.ensure(timeout=60)
    assert running.version == "0.35.1" and running.url.startswith("http://127.0.0.1:")
    argv = list(running.argv)
    base = str(comfy_instance.base_dir())
    assert argv[argv.index("--base-directory") + 1] == base
    assert "--disable-api-nodes" in argv and "--enable-manager" not in argv
    assert argv[argv.index("--database-url") + 1] == "sqlite:///:memory:"
    text = (comfy_instance.base_dir() / "models.yaml").read_text(encoding="utf-8")
    assert "checkpoints: checkpoints/" in text and "custom_nodes: custom_nodes/" in text
    assert comfy_instance.current() is not None
    assert comfy_instance.ensure(timeout=5).url == running.url        # la même, déjà prête
    process = comfy_instance._state["process"]
    comfy_instance.stop_instance()
    assert comfy_instance.current() is None and process.poll() is not None


def test_its_models_are_read_on_the_disk_and_its_inventory_kept(installed):
    assert comfy_instance.disk_models() == {"checkpoints": ["sous/base.safetensors"]}
    view = lending.local_view()
    assert view["launch"] and view["classes"] is None
    assert lending.capabilities() == {}             # ses classes pas encore lues : rien
    assert lending.summary()["comfyui"]["models"] == {"checkpoints": ["sous/base.safetensors"]}
    comfy_instance.save_inventory({"version": "0.35.1", "core": "c0a1", "vram_mib": 8192,
                                   "classes": {"KSampler": "a", "SaveImage": "b"}})
    caps = lending.capabilities()["comfyui"]
    assert caps["classes"] == ["KSampler", "SaveImage"] and caps["state"] == "ready"
    assert caps["models"] == {"checkpoints": ["sous/base.safetensors"]}
    comfy_instance.save_inventory({"version": "0.34.0", "classes": {"KSampler": "a"}})
    assert comfy_instance.cached_inventory() is None          # une autre version : relue


def test_launched_once_to_learn_what_it_can_do(installed):
    """À l'activation du lancement : lancé le temps de lire version et classes, puis arrêté ;
    la machine peut alors l'annoncer à mes machines sans le garder lancé."""
    assert comfy_instance.cached_inventory() is None
    comfy_instance.learn()
    assert comfy_instance.current() is None                     # arrêté aussitôt
    cached = comfy_instance.cached_inventory()
    assert cached["version"] == "0.35.1" and cached["classes"] == ["KSampler"]
    assert lending.capabilities()["comfyui"]["classes"] == ["KSampler"]


def test_nothing_launches_without_both_settings(installed):
    settings.set_value("engines_comfyui_launch", False)
    assert not comfy_instance.launchable() and lending.local_view() is None
    settings.set_many({"engines_comfyui_launch": True, "engines_comfyui_enabled": False})
    assert not comfy_instance.launchable()


def test_a_clean_offline_environment(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\piege")
    monkeypatch.setenv("PYTHONSTARTUP", r"C:\piege\demarrage.py")
    env = comfy_instance.environment()
    assert "PYTHONPATH" not in env and "PYTHONSTARTUP" not in env
    assert env["HF_HUB_OFFLINE"] == "1" and env["DO_NOT_TRACK"] == "1"
    assert env.get("PATH") == os.environ.get("PATH")
