"""Nœud sans fenêtre (téléphone, machine de calcul) : il ne doit dépendre ni de la
fenêtre native (pywebview, pythonnet), ni de la transcription (faster-whisper),
absentes de requirements-node.txt, ni des paquets de lecture des documents (un
nœud minimal s'en passe : il annonce alors seulement les formats qu'il lit).
Vérifié dans un processus à part, où ces modules sont introuvables."""
import os
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent

WITHOUT_GUI = r'''
import importlib, pkgutil, sys

ABSENT = {"webview", "clr", "clr_loader", "pythonnet", "faster_whisper", "ctranslate2", "av",
          "pypdf", "docx", "pptx", "openpyxl", "striprtf", "ebooklib"}

class Absent:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ABSENT:
            raise ModuleNotFoundError(f"No module named {name!r} (simulé)", name=name)
        return None

sys.meta_path.insert(0, Absent())

import coeur
for module in pkgutil.walk_packages(coeur.__path__, "coeur."):
    if module.name != "coeur.ui.window":          # la fenêtre, seulement sans --headless
        importlib.import_module(module.name)

from fastapi.testclient import TestClient
from coeur.api.server import create_app
from coeur.network.worker import Worker
from coeur.services import extract

assert extract.readable_documents() == [".htm", ".html", ".odt"]

with TestClient(create_app("jeton", {"testserver"})) as client:
    reply = client.get("/api/settings", headers={"X-Coeur-Token": "jeton"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["share_min_battery"] == 80
assert Worker.capabilities.__name__                # la machine qui prête se charge aussi
print("nœud sans fenêtre : ok")
'''


def test_headless_node_needs_neither_window_nor_whisper(tmp_path):
    env = dict(os.environ, COEUR_DATA_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "-c", WITHOUT_GUI], cwd=APP_ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "nœud sans fenêtre : ok" in done.stdout
