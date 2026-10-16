"""Étape C : conversation servie par une machine appairée.

Flux de morceaux scellés (ordre, troncature, altération), machine qui prête ses
modèles (refus lisibles, priorité à l'interactif), modèles proposés dans le
choix, routage de la génération. Sans réseau ni Ollama."""
import threading

import pytest
from fastapi.testclient import TestClient

from coeur import settings
from coeur.chat import pipeline
from coeur.network import client, envelope, lending, p2p_app, peers, remote_models
from coeur.network.client import PeerError
from coeur.network.crypto import SealError
from coeur.network.envelope import ReplayGuard
from coeur.services import ollama

KEY = b"k" * 32
A, B = "machine-a", "machine-b"


class FakeExecutor:
    def __init__(self):
        self.interactive = 0
        self.history = []

    def interactive_begin(self):
        self.interactive += 1
        self.history.append("begin")

    def interactive_end(self):
        self.interactive -= 1
        self.history.append("end")


class FakeManager:
    node_id = B
    p2p_port = 1

    def __init__(self):
        self.guard = ReplayGuard()
        self.executor = FakeExecutor()

    def name(self):
        return "PC fixe"

    def changed(self):
        pass


# ------------------------------------------------------------------- flux
def test_frames_are_bound_to_their_rank_and_request():
    line = envelope.wrap_frame(KEY, "chat", B, A, 1.5, 0, {"content": "Bon"})
    assert envelope.unwrap_frame(KEY, "chat", B, A, 1.5, 0, line) == {"content": "Bon"}
    with pytest.raises(SealError):                    # morceau déplacé
        envelope.unwrap_frame(KEY, "chat", B, A, 1.5, 1, line)
    with pytest.raises(SealError):                    # rejoué dans une autre réponse
        envelope.unwrap_frame(KEY, "chat", B, A, 2.5, 0, line)
    with pytest.raises(SealError):
        envelope.unwrap_frame(KEY, "chat", B, A, 1.5, 0, "pas du base64 !")


@pytest.fixture
def server(monkeypatch):
    peers.save(A, "PC portable", KEY, "127.0.0.1", 1)
    manager = FakeManager()
    return manager, TestClient(p2p_app.create_p2p_app(manager))


def _read(manager, response, env):
    frames = [envelope.unwrap_frame(KEY, "test", manager.node_id, A, env["ts"], seq, line)
              for seq, line in enumerate(l for l in response.iter_lines() if l)]
    return frames


def test_stream_route_seals_each_chunk_and_ends_explicitly(server, monkeypatch):
    manager, http = server
    monkeypatch.setitem(p2p_app.STREAM_HANDLERS, "test",
                        lambda m, sender, payload, stop: iter([{"n": 1}, {"n": 2}]))
    env = envelope.wrap(KEY, "test", A, B, {"x": 1})
    with http.stream("POST", "/p2p/v1/stream/test", json=env) as response:
        assert response.status_code == 200
        assert _read(manager, response, env) == [{"n": 1}, {"n": 2}, {"end": True}]


def test_stream_errors_are_reported_in_the_stream(server, monkeypatch):
    manager, http = server

    def failing(m, sender, payload, stop):
        yield {"n": 1}
        raise RuntimeError("modèle déchargé")
    monkeypatch.setitem(p2p_app.STREAM_HANDLERS, "test",
                        lambda m, s, p, stop: failing(m, s, p, stop))
    env = envelope.wrap(KEY, "test", A, B, {})
    with http.stream("POST", "/p2p/v1/stream/test", json=env) as response:
        assert _read(manager, response, env) == [{"n": 1}, {"error": "modèle déchargé"}]


def test_stream_refusal_and_unknown_machine(server, monkeypatch):
    manager, http = server

    def refuse(m, sender, payload, stop):
        raise PeerError("Cette machine ne prête pas ses modèles.")
    monkeypatch.setitem(p2p_app.STREAM_HANDLERS, "test", refuse)
    response = http.post("/p2p/v1/stream/test", json=envelope.wrap(KEY, "test", A, B, {}))
    assert response.status_code == 400 and "ne prête pas" in response.json()["detail"]
    intruder = envelope.wrap(b"x" * 32, "test", "intrus", B, {})
    assert http.post("/p2p/v1/stream/test", json=intruder).status_code == 403


# -------------------------------------------------- machine qui prête ses modèles
@pytest.fixture
def lender(monkeypatch):
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": "qwen3.6:latest"}])
    seen = {}

    def fake_stream(model, messages, think=None, stop=None):
        seen.update(model=model, messages=messages, think=think)
        yield "thinking", "Je réfléchis"
        yield "content", "Bonjour"
    monkeypatch.setattr(ollama, "chat_stream", fake_stream)
    return FakeManager(), seen


QUESTION = [{"role": "system", "content": "Sois bref."}, {"role": "user", "content": "Salut"}]


def test_lending_is_off_by_default(lender):
    manager, _ = lender
    with pytest.raises(PeerError, match="ne prête pas"):
        lending.chat(manager, A, {"model": "qwen3.6:latest", "messages": QUESTION},
                     threading.Event())


def test_lending_answers_with_priority_to_interactive(lender):
    manager, seen = lender
    settings.set_value("lend_models", True)
    answer = lending.chat(manager, A, {"model": "qwen3.6:latest", "messages": QUESTION,
                                       "think": False}, threading.Event())
    assert list(answer) == [{"thinking": "Je réfléchis"}, {"content": "Bonjour"}]
    assert manager.executor.history == ["begin", "end"]          # GPU de fond suspendu
    assert seen == {"model": "qwen3.6:latest", "messages": QUESTION, "think": False}


def test_lending_refuses_unknown_models_and_odd_messages(lender):
    manager, _ = lender
    settings.set_value("lend_models", True)
    with pytest.raises(PeerError, match="plus installé"):
        lending.chat(manager, A, {"model": "llama-inconnu", "messages": QUESTION}, None)
    for bad in ([], [{"role": "tool", "content": "x"}], [{"role": "user"}], "texte"):
        with pytest.raises(PeerError):
            lending.chat(manager, A, {"model": "qwen3.6:latest", "messages": bad}, None)


# ------------------------------------------------------ modèles proposés ici
def _peer_with_models(lends=True, online=True):
    peers.save(B, "PC fixe", KEY, "127.0.0.1", 1)
    peers.set_profile(B, {"lends_models": lends, "ollama": {"models": [
        {"name": "qwen3.6:latest", "size": 22, "caps": ["completion", "thinking"],
         "fits_vram": True},
        {"name": "embeddinggemma:latest", "size": 1, "caps": ["embedding"]}]}})
    if not online:
        from coeur import db
        db.execute("UPDATE peers SET last_seen=0 WHERE node_id=?", (B,))


def test_remote_models_listed_only_when_lent_and_online():
    _peer_with_models()
    models = remote_models.available()
    assert [(m["id"], m["label"], m["peer"]) for m in models] == [
        ("peer:machine-b:qwen3.6:latest", "qwen3.6:latest", "PC fixe")]
    _peer_with_models(lends=False)
    assert remote_models.available() == []
    _peer_with_models(online=False)
    assert remote_models.available() == []


def test_model_ids_keep_colons_in_model_names():
    assert remote_models.parse("peer:abc:qwen3.5:9b") == ("abc", "qwen3.5:9b")
    assert remote_models.parse("qwen3.5:9b") is None
    assert remote_models.parse("cloud:mistral") is None


def test_generation_is_routed_to_the_machine_that_lends(monkeypatch):
    _peer_with_models()
    calls = {}

    def fake_stream(my_id, peer, action, payload, stop=None, **kw):
        calls.update(peer=peer["node_id"], action=action, payload=payload)
        yield {"thinking": "Hmm"}
        yield {"content": "Bonjour"}
    monkeypatch.setattr(client, "stream", fake_stream)
    out = list(pipeline.generate("peer:machine-b:qwen3.6:latest", QUESTION, think=False))
    assert out == [("thinking", "Hmm"), ("content", "Bonjour")]
    assert calls == {"peer": B, "action": "chat",
                     "payload": {"model": "qwen3.6:latest", "messages": QUESTION, "think": False}}
    assert not pipeline.runs_here("peer:machine-b:qwen3.6:latest")
    assert pipeline.runs_here("qwen3.6:latest") and not pipeline.runs_here("cloud:mistral")
    assert pipeline.preflight("Salut", "peer:machine-b:qwen3.6:latest")["remote"] == "PC fixe"
