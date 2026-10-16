"""API du nœud de bout en bout : sécurité, réglages, indexation, chat, cloud."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from aiodos.api.server import create_app
from aiodos.chat import pipeline
from aiodos.knowledge import store

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}


@pytest.fixture
def client(fake_embed):
    store.ensure_defaults()
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


def _events(response):
    return [json.loads(line[6:]) for line in response.iter_lines()
            if line.startswith("data: ")]


def test_token_and_host_are_required(client):
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers={"X-Aiodos-Token": "faux"}).status_code == 401
    assert client.get("/api/settings", headers={**AUTH, "Host": "evil.example"}).status_code == 400
    assert client.get("/api/settings", headers=AUTH).status_code == 200
    assert client.get("/").status_code == 200        # l'interface elle-même est publique


def test_api_keys_never_leave_in_clear(client):
    view = client.put("/api/settings", json={"mistral_key": "sk-test-12345678"},
                      headers=AUTH).json()
    assert view["mistral_key"] == "" and view["mistral_key_set"]
    assert view["mistral_key_hint"] == "••••5678"
    view = client.put("/api/settings", json={"mistral_key": ""}, headers=AUTH).json()
    assert view["mistral_key_set"]                       # chaîne vide = inchangé
    view = client.put("/api/settings", json={"mistral_key": None}, headers=AUTH).json()
    assert not view["mistral_key_set"]                   # null = effacé


def test_folder_indexing_runs_through_the_queue(client, tmp_path):
    folder = tmp_path / "Projet"
    (folder / "sous").mkdir(parents=True)
    (folder / "lisez-moi.md").write_text("Aiodos répartit le travail.", encoding="utf-8")
    (folder / "sous" / "notes.txt").write_text("Budget de 5000 euros.", encoding="utf-8")
    (folder / ".env").write_text("SECRET=1", encoding="utf-8")       # caché : ignoré
    (folder / "archive.zip").write_bytes(b"PK\x03\x04")              # binaire : ignoré
    general = store.list_collections()[0]["id"]
    job_id = client.post("/api/index", json={"paths": [str(folder)], "collection_id": general},
                         headers=AUTH).json()["job_id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        job = next(j for j in client.get("/api/jobs", headers=AUTH).json()["jobs"]
                   if j["id"] == job_id)
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "done", job
    assert job["result"]["indexed"] == 2
    assert job["result"]["skipped_types"] == {".zip": 1}
    sources = {e["source"] for e in client.get("/api/entries", headers=AUTH).json()}
    assert sources == {"Projet/lisez-moi.md", "Projet/sous/notes.txt"}


def test_chat_streams_and_saves(client, monkeypatch):
    def fake_generate(model, messages, think=True, stop=None):
        assert messages[-1] == {"role": "user", "content": "Salut"}
        assert "RAPPEL IMPORTANT" in messages[-2]["content"]       # règle anti-invention
        yield "thinking", "Je réfléchis…"
        yield "content", "Bonjour **monde**"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    with client.stream("POST", "/api/chat", json={"text": "Salut", "model": "local-test"},
                       headers=AUTH) as response:
        events = _events(response)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done" and "thinking" in kinds
    assert "<strong>monde</strong>" in events[-1]["html"]
    conv = client.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
    assert conv["title"] == "Salut"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][1]["thinking"] == "Je réfléchis…"


def test_cloud_needs_key_and_confirmation(client):
    body = {"text": "Bonjour", "model": "cloud:mistral"}
    assert client.post("/api/chat", json=body, headers=AUTH).status_code == 400   # pas de clé
    client.put("/api/settings", json={"mistral_key": "sk-test-12345678"}, headers=AUTH)
    assert client.post("/api/chat", json=body, headers=AUTH).status_code == 409   # pas validé
    pre = client.post("/api/chat/preflight", json=body, headers=AUTH).json()
    assert pre["cloud"] == "mistral" and pre["cloud_host"] == "api.mistral.ai"


def test_preflight_detects_web_intent(client):
    for text, expected in (("Peux-tu chercher la doc de Seedance ?", True),
                           ("rechercher en ligne", True), ("Quelle heure est-il ?", False)):
        pre = client.post("/api/chat/preflight", json={"text": text, "model": "x"},
                          headers=AUTH).json()
        assert pre["web_intent"] is expected, text


def test_conversation_folders(client):
    folder = client.post("/api/folders", json={"name": "Projet X"}, headers=AUTH).json()["id"]
    assert client.post("/api/folders", json={"name": "Projet X"}, headers=AUTH).status_code == 400
    from aiodos.chat import conversations
    conv = conversations.create("Essai")
    client.patch(f"/api/conversations/{conv}", json={"folder_id": folder}, headers=AUTH)
    tree = client.get("/api/conversations", headers=AUTH).json()
    assert tree["conversations"][0]["folder_id"] == folder
    client.delete(f"/api/folders/{folder}", headers=AUTH)            # la conversation reste
    tree = client.get("/api/conversations", headers=AUTH).json()
    assert tree["conversations"][0]["folder_id"] is None
