"""Étape C : conversation servie par une machine appairée.

Flux de morceaux scellés (ordre, troncature, altération), machine qui prête ses
modèles (refus lisibles, priorité à l'interactif), modèles proposés dans le
choix, routage de la génération, sur le réseau local ou par le compagnon (machine
appairée sur invitation). Sans réseau ni Ollama."""
import io
import json
import threading

import pytest
import requests
from fastapi.testclient import TestClient

from aiodos import settings
from aiodos.chat import pipeline
from aiodos.network import client, envelope, lending, p2p_app, peers, remote_models, route
from aiodos.network.client import PeerError
from aiodos.network.crypto import SealError
from aiodos.network.envelope import ReplayGuard
from aiodos.services import ollama

KEY = b"k" * 32
A, B = "machine-a", "machine-b"
NET_ID = "c" * 64


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
        from aiodos import db
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


def test_a_malformed_profile_never_breaks_the_model_list(monkeypatch):
    """Faille 16, défense en profondeur : le profil est typé à l'enregistrement et à la
    relecture (network/profile.py). S'il arrivait quand même mal formé jusqu'ici, les modèles
    mal formés sont ignorés, une valeur douteuse est neutralisée, et la liste se sérialise
    sans NaN : GET /api/models et la passerelle ne tombent plus pour toutes les machines."""
    base = {"node_id": B, "name": "PC fixe", "online": True, "trust": "mine",
            "address": "127.0.0.1", "port": 1, "net_id": None, "set_aside_at": None}
    models = [{"name": "qwen3.6:latest", "size": float("nan"), "caps": ["completion", 3],
               "fits_vram": "oui", "loaded": float("inf"), "digest": {"x": 1}, "family": None},
              "pas un modèle", ["liste"], {"name": {"<img>": 1}}, {"size": 3}, {"name": ""},
              {"name": "embeddinggemma:latest", "caps": ["embedding"]},
              {"name": "mistral:7b", "caps": "completion", "size": -5, "loaded": True}]
    monkeypatch.setattr(peers, "list_peers", lambda: [
        dict(base, profile={"lends_models": True, "ollama": {"models": models}}),
        dict(base, node_id="liste", profile=["pas", "un", "profil"]),
        dict(base, node_id="texte", profile={"lends_models": True, "ollama": "modèles"}),
        dict(base, node_id="oui", profile={"lends_models": "oui",
                                           "ollama": {"models": [{"name": "x"}]}})])
    found = remote_models.available()
    json.dumps(found, allow_nan=False)                     # ce que sert GET /api/models
    assert [(m["id"], m["size"], m["caps"], m["fits_vram"], m["loaded"], m["digest"],
             m["family"]) for m in found] == [
        ("peer:machine-b:qwen3.6:latest", 0, ["completion"], None, None, "", ""),
        ("peer:machine-b:mistral:7b", 0, [], None, True, "", "")]
    assert remote_models.chat_models(None) == []
    assert remote_models.chat_models({"ollama": {"models": {"length": "<img>"}}}) == []


def test_model_ids_keep_colons_in_model_names():
    assert remote_models.parse("peer:abc:qwen3.5:9b") == ("abc", "qwen3.5:9b")
    assert remote_models.parse("qwen3.5:9b") is None


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
    assert pipeline.runs_here("qwen3.6:latest") and not pipeline.runs_here("reparti:qwen3.6:latest")
    assert pipeline.preflight("Salut", "peer:machine-b:qwen3.6:latest")["remote"] == "PC fixe"


# ----------------------------------- machine jointe seulement par Internet (invitation)
class FakeCompanion:
    """Compagnon aiodos-net qui tourne, tel que route.paths_for le voit."""
    running = True
    headers = {"X-Aiodos-Net-Token": "jeton"}

    def proxy_base(self, net_id):
        return f"http://127.0.0.1:47800/peer/{net_id}"


class RawBody(io.BytesIO):
    """Corps brut d'une réponse, tel que client.py le lit (urllib3 : read1, sans décompresser)."""

    def read1(self, size=-1, decode_content=False):
        return super().read1(size)


class StreamReply:
    """Réponse en flux telle que requests la rend : un morceau scellé par ligne."""
    status_code = 200
    headers = {}

    def __init__(self, lines):
        self.raw = RawBody(b"".join(line + b"\n" for line in lines))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def companion():
    fake = FakeCompanion()
    route.attach(fake)
    yield fake
    route.detach(fake)


def test_a_machine_paired_by_invitation_answers_through_the_companion(companion, monkeypatch):
    """Appairée sur invitation (étape 2.4), la machine qui prête n'a pas d'adresse sur le
    réseau local, seulement sa clé Internet : la conversation passe par le compagnon."""
    peers.save(B, "PC de Léa", KEY, None, None, trust="friend")
    peers.set_net(B, NET_ID, {"relays": [], "addrs": []})
    sent = []

    def fake_post(url, json=None, headers=None, proxies=None, timeout=None, stream=False):
        sent.append(url)
        frames = [{"content": "Bonjour"}, {"end": True}]
        return StreamReply([envelope.wrap_frame(KEY, "chat", B, json["from"], json["ts"], seq,
                                                frame).encode()
                            for seq, frame in enumerate(frames)])
    monkeypatch.setattr(requests, "post", fake_post)
    answer = remote_models.chat_stream("peer:machine-b:qwen3.6:latest", QUESTION, think=False)
    assert list(answer) == [("content", "Bonjour")]
    assert sent == [f"http://127.0.0.1:47800/peer/{NET_ID}/p2p/v1/stream/chat"]


def test_a_machine_reachable_in_no_way_is_refused_clearly(companion, monkeypatch):
    peers.save(B, "PC de Léa", KEY, None, None, trust="friend")      # ni adresse, ni clé
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("rien ne doit partir"))
    with pytest.raises(PeerError) as refused:
        list(remote_models.chat_stream("peer:machine-b:qwen3.6:latest", QUESTION))
    assert str(refused.value) == ("« PC de Léa » est injoignable pour l'instant (ni adresse "
                                  "sur le réseau local, ni clé Internet).")


def test_a_silent_stream_sends_signs_of_life_the_requester_ignores(server, monkeypatch):
    """Modèle qui se charge, longue réflexion : la machine qui calcule le dit à intervalles
    (p2p_app.ALIVE_S), sans quoi celle qui attend ne saurait pas si elle a disparu."""
    manager, http = server
    monkeypatch.setattr(p2p_app, "ALIVE_S", 0.05)

    def slow(m, sender, payload, stop):
        threading.Event().wait(0.4)
        yield {"content": "Bonjour"}
    monkeypatch.setitem(p2p_app.STREAM_HANDLERS, "test", slow)
    env = envelope.wrap(KEY, "test", A, B, {})
    with http.stream("POST", "/p2p/v1/stream/test", json=env) as response:
        frames = _read(manager, response, env)
    assert frames[-2:] == [{"content": "Bonjour"}, {"end": True}]
    assert len(frames) > 3 and all(f == {"alive": True} for f in frames[:-2])

    _peer_with_models()
    monkeypatch.setattr(client, "stream", lambda *a, **k: iter(
        [{"alive": True}, {"content": "Bon"}, {"alive": True}, {"content": "jour"}]))
    assert list(pipeline.generate("peer:machine-b:qwen3.6:latest", QUESTION, think=False)) == [
        ("content", "Bon"), ("content", "jour")]


def test_a_previous_version_is_read_with_the_long_delay(monkeypatch):
    """Une machine qui annonce des signes de vie (profil « stream_alive ») se lit avec un
    délai de 3 minutes ; une version précédente, qui se tait pendant qu'un gros modèle se
    charge, garde les 10 minutes d'avant."""
    delays = []
    monkeypatch.setattr(client, "stream", lambda *a, read_timeout=None, **k:
                        delays.append(read_timeout) or iter([{"content": "Oui"}]))
    _peer_with_models()
    list(pipeline.generate("peer:machine-b:qwen3.6:latest", QUESTION, think=False))
    peers.update_profile(B, "stream_alive", True)
    list(pipeline.generate("peer:machine-b:qwen3.6:latest", QUESTION, think=False))
    assert delays == [remote_models.READ_SILENT_S, remote_models.READ_ALIVE_S]
    from aiodos.node import identity
    from aiodos.network import sharing
    assert identity.profile()["stream_alive"] is True and "stream_alive" in sharing.FRIEND_PROFILE
