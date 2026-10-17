"""Chercher chez mes machines et mes amis, côté demandeur, sans réseau ni Ollama : à qui
part la question, contrôle de ce qui revient, machine lente ou qui refuse, passages dans
la conversation (citations « chez … »), et l'API de l'interface."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aiodos import db
from aiodos.api.server import create_app
from aiodos.chat import pipeline
from aiodos.knowledge import store
from aiodos.network import client, consult, peers, remote_search, sharing
from aiodos.network.client import PeerError

KEY = b"k" * 32
LEA, PORTABLE, NOTHING_OPEN, OFFLINE = "pc-de-lea", "portable", "vieux-pc", "eteint"
TOKEN = {"X-Aiodos-Token": "jeton"}
PASSAGE = {"text": "La fusée lunaire décolle à l'aube.", "document": "fusee.txt",
           "library": "Cours", "score": 0.61}


def _paired(node_id, name, opened, trust=sharing.MINE, online=True):
    peers.save(node_id, name, KEY, "127.0.0.1", 1, trust=trust)
    peers.set_profile(node_id, {"name": name, "consult": {"open": opened}})
    if not online:
        db.execute("UPDATE peers SET last_seen=0 WHERE node_id=?", (node_id,))


@pytest.fixture
def circle():
    """Léa (une amie) et mon portable m'ouvrent des bibliothèques ; le vieux PC, rien ;
    une machine qui ouvre quelque chose est éteinte."""
    _paired(LEA, "PC de Léa", 1, trust=sharing.FRIEND)
    _paired(PORTABLE, "Portable", 2)
    _paired(NOTHING_OPEN, "Vieux PC", 0)
    _paired(OFFLINE, "Éteint", 3, online=False)


def _targets() -> set:
    return {p["node_id"] for p in remote_search.targets()}


def test_the_question_only_goes_to_machines_with_something_open(circle):
    assert _targets() == {LEA, PORTABLE}
    assert sorted((m["name"], m["friend"]) for m in remote_search.describe()) == [
        ("PC de Léa", True), ("Portable", False)]
    peers.set_aside(LEA, "contrôle échoué")                  # ses résultats seraient douteux
    assert _targets() == {PORTABLE}
    peers.set_aside(LEA, None)
    peers.save(PORTABLE, "Portable", KEY, None, None)       # ni adresse, ni clé Internet
    assert _targets() == {LEA}


def test_passages_come_back_checked_and_tagged_with_their_machine(circle, monkeypatch):
    sent = []

    def fake_call(my_id, peer, action, payload, key=None, timeout=10):
        sent.append((peer["node_id"], action, payload))
        if peer["node_id"] == PORTABLE:
            raise PeerError("« Portable » limite les recherches à 10 par minute.")
        return {"open": 1, "passages": [
            PASSAGE, {"text": 3}, "pas un passage", dict(PASSAGE, score="nan"),
            dict(PASSAGE, text="x" * 9000, document="d" * 999), dict(PASSAGE, text="   "),
            dict(PASSAGE, document="trop.txt"), dict(PASSAGE, document="encore.txt")]}
    monkeypatch.setattr(client, "call", fake_call)
    passages, machines = remote_search.search("fusée lunaire " * 500, k=3)
    assert {node for node, _, _ in sent} == {LEA, PORTABLE}
    assert all(action == "library/search" and payload["k"] == 3 for _, action, payload in sent)
    assert all(len(payload["question"]) == consult.MAX_QUESTION for _, _, payload in sent)
    short = len(PASSAGE["text"])                            # k = 3 : « encore.txt » reste là-bas
    assert [(p["document"][:5], len(p["text"]), p["rank"]) for p in passages] == [
        ("fusee", short, 0), ("ddddd", consult.MAX_TEXT, 1), ("trop.", short, 2)]
    assert len(passages[1]["document"]) == remote_search.MAX_DOCUMENT
    assert all(p["peer"] == "PC de Léa" and p["friend"] and p["node_id"] == LEA for p in passages)
    status = {m["node_id"]: m for m in machines}
    assert status[LEA]["count"] == 3 and status[LEA]["error"] is None
    assert "limite" in status[PORTABLE]["error"] and status[PORTABLE]["count"] == 0


def test_a_slow_machine_does_not_hold_the_answer(circle, monkeypatch):
    monkeypatch.setattr(remote_search, "GRACE", 0.2)
    release = threading.Event()

    def fake_call(my_id, peer, action, payload, key=None, timeout=10):
        if peer["node_id"] == PORTABLE:
            release.wait(5)
        return {"passages": [PASSAGE], "open": 1}
    monkeypatch.setattr(client, "call", fake_call)
    start = time.monotonic()
    passages, machines = remote_search.search("fusée", timeout=0.1)
    release.set()
    assert time.monotonic() - start < 2
    status = {m["node_id"]: m for m in machines}
    assert status[PORTABLE]["error"] == "pas de réponse à temps"
    assert status[LEA]["count"] == 1 and [p["peer"] for p in passages] == ["PC de Léa"]


def test_a_machine_that_closed_everything_is_no_longer_asked(circle, monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: {"passages": [], "open": 0})
    remote_search.search("fusée")
    assert _targets() == set()                  # sans attendre son prochain profil


def test_nothing_is_asked_without_a_question_or_a_machine(circle, monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("question envoyée"))
    assert remote_search.search("   ") == ([], [])
    assert remote_search.search("fusée", found=[]) == ([], [])


# ------------------------------------------------------------- la conversation
def _found():
    lea = {"peer": "PC de Léa", "node_id": LEA, "friend": True}
    return [dict(PASSAGE, rank=0, **lea),
            dict(PASSAGE, document="carburant.txt", score=0.5, rank=1, **lea),
            dict(PASSAGE, document="notes.md", library="Perso", score=0.9, rank=0,
                 peer="Portable", node_id=PORTABLE, friend=False)]


def test_the_conversation_cites_where_each_passage_was_found(circle, monkeypatch):
    monkeypatch.setattr(remote_search, "search", lambda question, **kw: (_found(), []))
    messages, sources = pipeline.build_messages(
        [], "La fusée ?", libraries=False, conversations=False, web_search=False, peers=True)
    extracts = next(m["content"] for m in messages if "chez qui" in m["content"])
    assert "[Source : fusee.txt — bibliothèque « Cours », chez « PC de Léa »]" in extracts
    # Le meilleur de chaque machine d'abord
    assert extracts.index("notes.md") < extracts.index("fusee.txt") < extracts.index("carburant")
    assert [(s["type"], s["label"], s["collection"], s["peer"]) for s in sources] == [
        ("remote", "notes.md", "Perso", "Portable"), ("remote", "fusee.txt", "Cours", "PC de Léa"),
        ("remote", "carburant.txt", "Cours", "PC de Léa")]


def test_nothing_leaves_unless_the_conversation_asks(circle, monkeypatch):
    monkeypatch.setattr(remote_search, "search", lambda *a, **k: pytest.fail("question envoyée"))
    messages, sources = pipeline.build_messages(
        [], "La fusée ?", libraries=False, conversations=False, web_search=False)
    assert sources == [] and not any("chez qui" in m["content"] for m in messages)


# --------------------------------------------------------------------- API
@pytest.fixture
def api(circle, fake_embed):
    store.ensure_defaults()
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        yield http


def _events(response):
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def test_the_chat_asks_the_others_only_when_the_message_says_so(api, monkeypatch):
    """Même avec le réglage activé : une application qui ne le demande pas (API compatible)
    n'envoie pas la question chez les autres."""
    asked = []
    monkeypatch.setattr(remote_search, "search",
                        lambda question, **kw: asked.append(question) or (_found()[:1], []))
    monkeypatch.setattr(pipeline, "generate", lambda *a, **k: iter([("content", "Réponse")]))
    api.put("/api/settings", json={"peer_libraries_enabled": True}, headers=TOKEN)
    body = {"text": "La fusée ?", "model": "local-test", "libraries": False}
    with api.stream("POST", "/api/chat", json=body, headers=TOKEN) as response:
        _events(response)
    assert asked == []
    with api.stream("POST", "/api/chat", json=dict(body, peer_libraries=True),
                    headers=TOKEN) as response:
        events = _events(response)
    assert asked == ["La fusée ?"]
    cited = [{"type": "remote", "label": "fusee.txt", "collection": "Cours", "peer": "PC de Léa",
              "score": 0.61}]
    assert next(e["sources"] for e in events if e["type"] == "sources") == cited
    # Gardés : la réponse et les citations ; le texte du passage, nulle part
    conversation = api.get(f"/api/conversations/{events[0]['conversation_id']}",
                           headers=TOKEN).json()
    assert conversation["messages"][-1]["sources"] == cited
    assert "décolle" not in json.dumps(conversation, ensure_ascii=False)
    assert not db.query("SELECT 1 FROM chunks WHERE text LIKE '%décolle%'")


def test_preflight_says_where_the_question_would_go(api):
    pre = api.post("/api/chat/preflight", json={"text": "Salut", "model": "m"},
                   headers=TOKEN).json()
    assert sorted((m["name"], m["friend"]) for m in pre["consult"]) == [
        ("PC de Léa", True), ("Portable", False)]
    assert pre["consult_allowed"] is True


def test_passages_of_others_go_neither_to_the_cloud_nor_to_a_friend_lent_model(api, monkeypatch):
    """Le propriétaire les a ouverts à cette machine, pas à des tiers."""
    assert pipeline.peers_allowed("qwen3.6:latest")                       # ce PC
    assert pipeline.peers_allowed(f"peer:{PORTABLE}:qwen3.6:latest")      # une de mes machines
    assert not pipeline.peers_allowed(f"peer:{LEA}:qwen3.6:latest")       # la machine d'une amie
    assert not pipeline.peers_allowed("peer:inconnue:qwen3.6:latest")
    assert not pipeline.peers_allowed("cloud:mistral")
    monkeypatch.setattr(remote_search, "search", lambda *a, **k: pytest.fail("question envoyée"))
    monkeypatch.setattr(pipeline, "generate", lambda *a, **k: iter([("content", "Réponse")]))
    body = {"text": "La fusée ?", "model": f"peer:{LEA}:qwen3.6:latest", "libraries": False,
            "peer_libraries": True}
    with api.stream("POST", "/api/chat", json=body, headers=TOKEN) as response:
        events = _events(response)
    assert next(e["sources"] for e in events if e["type"] == "sources") == []
    pre = api.post("/api/chat/preflight", json={"text": "x", "model": "cloud:mistral"},
                   headers=TOKEN).json()
    assert pre["consult_allowed"] is False


def test_the_library_view_searches_what_the_others_open(api, monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: {"passages": [PASSAGE], "open": 1})
    assert {m["name"] for m in api.get("/api/consult", headers=TOKEN).json()["machines"]} == {
        "PC de Léa", "Portable"}
    reply = api.post("/api/consult", json={"question": "fusée"}, headers=TOKEN).json()
    assert {(p["peer"], p["document"]) for p in reply["passages"]} == {
        ("PC de Léa", "fusee.txt"), ("Portable", "fusee.txt")}
    assert {m["name"]: m["count"] for m in reply["machines"]} == {"PC de Léa": 1, "Portable": 1}
    assert api.post("/api/consult", json={"question": "  "}, headers=TOKEN).status_code == 400
