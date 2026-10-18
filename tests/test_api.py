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
    (folder / "lisez-moi.md").write_text("AIODOS répartit le travail.", encoding="utf-8")
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
                           ("rechercher en ligne", True), ("Quelle météo demain à Lyon ?", True),
                           ("Qui a gagné le match hier soir ?", True),
                           ("Quelle heure est-il ?", False), ("Bonjour !", False)):
        pre = client.post("/api/chat/preflight", json={"text": text, "model": "x"},
                          headers=AUTH).json()
        assert pre["web_intent"] is expected, text


def test_the_last_rule_says_whether_the_web_was_searched(client, monkeypatch):
    """« Web » activé ne cherche que si la question le demande : sans recherche, le modèle
    ne doit ni inventer d'adresse ni renvoyer vers une option déjà activée."""
    from aiodos import settings

    def rule(web_allowed, searched=None):
        settings.set_value("web_search_enabled", web_allowed)
        web = None if searched is None else (searched, [])
        messages, _ = pipeline.build_messages([], "Bonjour", libraries=False,
                                              conversations=False,
                                              web_search=searched is not None, web=web)
        return messages[-2]["content"]

    assert "aucune recherche web n'a été faite" in rule(True)
    assert "Active la recherche web" in rule(False)
    assert "n'a rien donné" in rule(True, searched=[])
    assert rule(True, searched=["[Web : page — https://exemple.org]\ntexte"]).startswith(
        "RAPPEL : les résultats web")


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


def test_interface_files_are_revalidated_after_an_update(data_dir):
    """Un navigateur ouvert sur le même port ne doit pas garder l'ancien code de
    l'interface après une mise à jour : chaque fichier est revalidé (304 s'il n'a pas
    changé)."""
    from fastapi.testclient import TestClient

    from aiodos.api.server import create_app
    with TestClient(create_app("jeton", {"testserver"})) as client:
        first = client.get("/js/app.js")
        assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
        again = client.get("/js/app.js", headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304


def test_conversations_are_found_by_what_their_messages_say(client):
    from aiodos.chat import conversations
    first = conversations.create(title="Voyage")
    conversations.add_message(first, "user", "Quel temps fait-il à Brest en novembre ?")
    conversations.add_message(first, "assistant", "À Brest, novembre est doux et pluvieux.")
    other = conversations.create(title="Cuisine")
    conversations.add_message(other, "user", "Une recette de crêpes, 100% sarrasin")
    found = client.get("/api/conversations/search?q=brest", headers=AUTH).json()
    assert [f["id"] for f in found] == [first] and "Brest" in found[0]["snippet"]
    assert [f["id"] for f in client.get("/api/conversations/search?q=100%25",
                                        headers=AUTH).json()] == [other]
    assert client.get("/api/conversations/search?q=_", headers=AUTH).json() == []
