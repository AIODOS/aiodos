"""Installer un modèle de conversation (services/model_install.py) : le modèle conseillé selon
l'appareil (téléphone, carte graphique, mémoire), un seul téléchargement à la fois, le flux
d'Ollama lu jusqu'au bout, et l'API qui en publie l'avancement. Sans Ollama."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aiodos.api.events import EventBus
from aiodos.api.server import create_app
from aiodos.node import android, identity
from aiodos.services import model_install, ollama

TOKEN = {"X-Aiodos-Token": "jeton"}
GB = 1_000_000_000


def _device(monkeypatch, phone=False, vram=0, ram=16 * GB, installed=()):
    monkeypatch.setattr(android, "is_android", lambda: phone)
    monkeypatch.setattr(identity, "profile", lambda max_age=5.0: {
        "gpus": [{"vram_total": vram}] if vram else [], "memory": {"total": ram}})
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": n} for n in installed])


@pytest.mark.parametrize("phone, vram, ram, best", [
    (True, 0, 8 * GB, "qwen3.5:2b"),           # S23 : 8 Go
    (True, 0, 4 * GB, "qwen3.5:0.8b"),
    (False, 8 * GB, 32 * GB, "qwen3.5:9b"),    # RTX 4070 Laptop
    (False, 6 * GB, 16 * GB, "qwen3.5:4b"),
    (False, 0, 16 * GB, "qwen3.5:4b"),         # sans carte graphique
    (False, 0, 8 * GB, "qwen3.5:2b"),
    (False, 2 * GB, 4 * GB, "qwen3.5:0.8b"),
])
def test_the_advice_fits_the_device(monkeypatch, phone, vram, ram, best):
    _device(monkeypatch, phone, vram, ram)
    found = model_install.suggestions()
    assert found["recommended"] == best and found["models"][0]["name"] == best
    assert found["phone"] is phone and found["ollama"] is True
    if phone:                                   # rien de plus de 3 Go sur un téléphone
        assert all(m["size"] <= 3 * GB for m in found["models"])
    # Licences libres pour converser ; le modèle des bibliothèques, lui, affiche la sienne
    chat = [m for m in found["models"] if m.get("kind") != "embedding"]
    assert {m["license"] for m in chat} <= {"Apache-2.0", "MIT"}
    assert [m["license"] for m in found["models"] if m.get("kind") == "embedding"] == ["Gemma"]


def test_what_fits_and_what_is_already_there(monkeypatch):
    _device(monkeypatch, vram=8 * GB, ram=32 * GB, installed=("qwen3.5:9b", "phi4-mini:latest"))
    models = {m["name"]: m for m in model_install.suggestions()["models"]}
    assert models["qwen3.5:9b"]["installed"] and models["phi4-mini"]["installed"]
    assert models["qwen3.5:4b"]["fits"] == "gpu" and not models["qwen3.5:4b"]["installed"]


def test_without_ollama_nothing_can_be_installed(monkeypatch):
    _device(monkeypatch)

    def down(max_age=10):
        raise ConnectionError("Ollama arrêté")
    monkeypatch.setattr(ollama, "list_models", down)
    assert model_install.suggestions()["ollama"] is False


class _Reply:
    """Réponse en flux d'Ollama à /api/pull."""
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self, decode_unicode=True):
        return iter(self.lines)


PULL = [json.dumps(x) for x in (
    {"status": "pulling manifest"},
    {"status": "pulling a1", "digest": "sha256:a1", "total": 600, "completed": 0},
    {"status": "pulling a1", "digest": "sha256:a1", "total": 600, "completed": 300},
    {"status": "pulling b2", "digest": "sha256:b2", "total": 400, "completed": 400},
    {"status": "pulling a1", "digest": "sha256:a1", "total": 600, "completed": 600},
    {"status": "verifying sha256 digest"}, {"status": "writing manifest"},
    {"status": "success"})]


def test_the_pull_stream_is_followed_to_the_end(monkeypatch):
    sent, seen = [], []
    monkeypatch.setattr(ollama.requests, "post", lambda url, json, stream, timeout:
                        sent.append((url, json)) or _Reply(PULL))
    assert ollama.pull("qwen3.5:9b", progress=lambda c, t, s: seen.append((c, t, s)))
    assert sent[0][0].endswith("/api/pull") and sent[0][1] == {"model": "qwen3.5:9b",
                                                               "stream": True}
    assert seen[2][:2] == (300, 600) and seen[3][:2] == (700, 1000) and seen[-1][:2] == (1000, 1000)
    stop = threading.Event()
    stop.set()
    assert ollama.pull("qwen3.5:9b", stop=stop) is False
    monkeypatch.setattr(ollama.requests, "post", lambda *a, **k: _Reply(
        [json.dumps({"error": "pull model manifest: file does not exist"})]))
    with pytest.raises(RuntimeError, match="does not exist"):
        ollama.pull("inconnu")


def test_one_installation_at_a_time_and_only_what_is_offered(monkeypatch):
    gate = threading.Event()

    def slow(name, progress=None, stop=None):
        progress(1, 2, "pulling")
        gate.wait(5)
        return True
    monkeypatch.setattr(ollama, "pull", slow)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [])
    with pytest.raises(model_install.InstallError):
        model_install.start("n-importe-quoi:70b")
    model_install.start("qwen3.5:2b")
    with pytest.raises(model_install.InstallError, match="déjà en cours"):
        model_install.start("qwen3.5:4b")
    gate.set()
    deadline = time.monotonic() + 5
    while model_install.progress()["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert model_install.progress()["done"] is True


def _wait_until_done():
    deadline = time.monotonic() + 5
    while model_install.progress()["running"] and time.monotonic() < deadline:
        time.sleep(0.02)
    return model_install.progress()


def test_several_models_one_after_the_other(monkeypatch):
    """Plusieurs modèles cochés ensemble (demandé par l'auteur le 27/10/2026) : installés l'un
    après l'autre ; un échec n'empêche pas les suivants ; « Annuler » arrête la suite."""
    pulled, events = [], []

    def pull(name, progress=None, stop=None):
        pulled.append(name)
        if name == "qwen3.5:4b":
            raise RuntimeError("file does not exist")
        progress(1, 1, "success")
        return True
    monkeypatch.setattr(ollama, "pull", pull)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [])
    with pytest.raises(model_install.InstallError):          # un seul hors catalogue : rien
        model_install.start(["qwen3.5:2b", "n-importe-quoi:70b"])
    started = model_install.start(["qwen3.5:2b", "qwen3.5:4b", "qwen3.5:2b", "qwen3.5:9b"],
                                  changed=lambda: events.append(model_install.progress()))
    assert started["steps"] == 3                               # en double : une seule fois
    final = _wait_until_done()
    assert pulled == ["qwen3.5:2b", "qwen3.5:4b", "qwen3.5:9b"]
    assert final["installed"] == ["qwen3.5:2b", "qwen3.5:9b"] and final["done"] is False
    assert "qwen3.5:4b" in final["error"] and final["queue"] == []
    assert any(e["running"] and e["installed"] == ["qwen3.5:2b"] for e in events)

    gate = threading.Event()

    def slow(name, progress=None, stop=None):
        pulled.append(name)
        gate.wait(5)
        return not stop.is_set()
    monkeypatch.setattr(ollama, "pull", slow)
    pulled.clear()
    model_install.start(["qwen3.5:2b", "qwen3.5:9b"])
    deadline = time.monotonic() + 5
    while not pulled and time.monotonic() < deadline:          # le premier a commencé
        time.sleep(0.01)
    model_install.cancel()
    gate.set()
    final = _wait_until_done()
    assert pulled == ["qwen3.5:2b"] and final["installed"] == [] and final["done"] is False


def test_the_api_publishes_the_progress(monkeypatch):
    _device(monkeypatch, vram=8 * GB)

    def quick(name, progress=None, stop=None):
        progress(500, 1000, "pulling")
        time.sleep(0.6)                              # deux événements au plus par seconde
        progress(1000, 1000, "success")
        return True
    monkeypatch.setattr(ollama, "pull", quick)
    events = []
    real = EventBus.publish
    monkeypatch.setattr(EventBus, "publish", lambda self, event: (
        events.append(event) if event.get("type") == "model_install" else None,
        real(self, event)))
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        found = http.get("/api/models/install", headers=TOKEN).json()
        assert found["recommended"] == "qwen3.5:9b"
        assert http.post("/api/models/install", headers=TOKEN,
                         json={"name": "un-modele-pas-propose"}).status_code == 409
        started = http.post("/api/models/install", headers=TOKEN, json={"name": "qwen3.5:9b"})
        assert started.status_code == 200 and started.json()["running"] is True
        deadline = time.monotonic() + 5
        while http.get("/api/models/install", headers=TOKEN).json()["progress"]["running"] \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        final = http.get("/api/models/install", headers=TOKEN).json()["progress"]
    assert final["done"] is True and final["completed"] == final["total"] == 1000
    assert events[0]["running"] is True and events[-1]["running"] is False
    assert events[-1]["done"] is True and events[-1]["model"] == "qwen3.5:9b"


def test_gemma4_and_qwen36_come_through_the_integrated_engine(monkeypatch):
    """Ajoutés le 21/10/2026 : seulement par le moteur intégré (fichiers épinglés de
    llm/catalog.py), jamais sur un téléphone ; le modèle conseillé ne change pas."""
    from aiodos.llm import catalog
    _device(monkeypatch, vram=8 * GB, ram=32 * GB)
    monkeypatch.setattr(ollama, "use_integrated", lambda: True)
    found = model_install.suggestions()
    models = {m["name"]: m for m in found["models"]}
    assert found["recommended"] == "qwen3.5:9b"
    assert models["gemma4:e4b"]["fits"] == "gpu" and models["gemma4:e4b"]["license"] == "Apache-2.0"
    # Mixture d'experts : 23 Go entre la carte (8 Go) et la mémoire vive (32 Go)
    assert models["qwen3.6:35b-a3b"]["fits"] == "moe"
    for name in ("gemma4:e4b", "qwen3.6:35b-a3b"):
        spec = catalog.entry(name)
        assert spec and spec["license"] == "Apache-2.0"
        assert catalog.total_size(spec) == models[name]["size"]
    _device(monkeypatch, vram=8 * GB, ram=16 * GB)
    assert {m["name"]: m for m in model_install.suggestions()["models"]}[
        "qwen3.6:35b-a3b"]["fits"] == "heavy"
    _device(monkeypatch, phone=True, ram=8 * GB)
    names = {m["name"] for m in model_install.suggestions()["models"]}
    assert not names & {"gemma4:e4b", "qwen3.6:35b-a3b"}


def test_without_the_integrated_engine_they_are_not_offered(monkeypatch):
    _device(monkeypatch, vram=8 * GB, ram=32 * GB)
    monkeypatch.setattr(ollama, "use_integrated", lambda: False)
    names = {m["name"] for m in model_install.suggestions()["models"]}
    assert not names & {"gemma4:e4b", "qwen3.6:35b-a3b"}
