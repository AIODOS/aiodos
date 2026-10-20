"""Passerelle vers les autres outils (aiodos/gateway) servie par le moteur intégré : sans
Ollama sur la machine, les mêmes API pour les outils (gateway/engine.py).

Le routeur de llama.cpp est joué par un faux serveur aux formats de llama.cpp b11707
(tests/router_stub.py), les modèles par de faux fichiers ; un faux Ollama tourne à côté et ne
doit rien recevoir. Le nœud tourne dans le processus du test (TestClient), la passerelle sur
un vrai port de 127.0.0.1. Une machine appairée qui prête ses modèles est simulée
(client.stream). Le client officiel openai sert s'il est installé."""
import base64
import json
from dataclasses import dataclass

import numpy as np
import pytest
import requests
from fastapi.testclient import TestClient

from aiodos import settings
from aiodos.api.server import create_app
from aiodos.knowledge import chunking, store
from aiodos.llm import integrated, models
from aiodos.llm.engine import CHAT_CONTEXT, EMBED_CONTEXT, EngineUnavailable
from aiodos.network import client, peers, placement
from aiodos.node import identity
from tests.conftest import _bag_of_words
from tests.nodes import free_port
from tests.ollama_stub import OllamaStub
from tests.router_stub import (AFTER_TOOL, ANSWER, COMPLETION, INFILL, JSON_ANSWER, SEEN,
                               THOUGHT, RouterStub, install)

TOKEN = "jeton"
UI = {"X-Aiodos-Token": TOKEN}
LOCAL = "petit:1b"                # converse, réfléchit, appelle des outils
VISION = "voyant:4b"              # voit aussi les images (projecteur)
CODE = "code:7b"                  # a les jetons de complétion de code
STRICT = "strict:7b"              # gabarit qui exige l'alternance des rôles, comme mistral
EMBEDDING = "bge-m3:latest"       # rangé en premier par nom, comme le vrai
BROKEN = "casse:1b"               # ne se charge pas dans llama.cpp
OLLAMA_ONLY = "ollama-seul:1b"    # sur le faux Ollama, qui ne doit rien recevoir
LENT = "grand:7b"                 # seulement sur la machine appairée
PEER = "fixe"
TOOLS = [{"type": "function", "function": {
    "name": "meteo", "description": "La météo d'une ville",
    "parameters": {"type": "object", "properties": {"ville": {"type": "string"}}}}}]
HELLO = [{"role": "user", "content": "Salut"}]


def rid(name: str) -> str:
    return models.router_id(name)


def unit(vector) -> list:
    norm = float(np.linalg.norm(vector))
    return (np.asarray(vector) / norm).tolist() if norm else list(vector)


@dataclass
class Tool:
    """Un outil branché sur la passerelle (adresse, clé), et le faux routeur derrière."""
    base: str
    key: str
    router: RouterStub
    ui: TestClient

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def get(self, path, **kw):
        return requests.get(self.base + path, headers=self.auth, timeout=30, **kw)

    def post(self, path, body, **kw):
        return requests.post(self.base + path, json=body,
                             headers={**self.auth, **kw.pop("headers", {})}, timeout=30, **kw)

    def sent(self, path: str) -> list:
        """Ce que le routeur a reçu sur ce chemin."""
        return self.router.bodies(path)


@pytest.fixture
def router(tmp_path, monkeypatch):
    """Le moteur intégré de cette machine, joué par un faux routeur ; à côté, un faux Ollama
    qui ne doit rien recevoir."""
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "pas-d-ollama"))   # pas ceux du PC
    install([LOCAL, VISION, CODE, STRICT, EMBEDDING], vision=[VISION], embedding=[EMBEDDING],
            thinking=[LOCAL, VISION], tools=[LOCAL, VISION])
    identity._cache.update(at=0.0, profile=None)      # profil de cette machine : relu
    with RouterStub(integrated._router.key, vision={rid(VISION)}, fim={rid(CODE)},
                    embedding={rid(EMBEDDING)}, strict={rid(STRICT)},
                    broken={rid(BROKEN)}) as stub, OllamaStub([OLLAMA_ONLY]) as ollama:
        monkeypatch.setattr(integrated._router, "url", lambda: stub.url)
        monkeypatch.setattr(integrated._router, "status", lambda: dict(stub.loaded))
        monkeypatch.setattr(integrated._router, "unload",
                            lambda model_id: stub.unloaded.append(model_id) or True)
        settings.set_many({"llm_engine": "integrated", "ollama_url": ollama.url,
                           "embed_model": EMBEDDING, "rag_min_score": 0.3})
        yield stub
        assert ollama.requests == [], "la passerelle a parlé à Ollama"
    identity._cache.update(at=0.0, profile=None)
    models.forget_cache()


@pytest.fixture
def tool(router):
    placement.forget()
    port = free_port()
    settings.set_many({"gateway_enabled": True, "gateway_port": port})
    store.ensure_defaults()
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        info = ui.get("/api/gateway", headers=UI).json()
        assert info["running"], info
        yield Tool(f"http://127.0.0.1:{port}", info["key"], router, ui)
    placement.forget()


@pytest.fixture
def lender(monkeypatch):
    """Machine appairée « PC fixe » qui prête LENT et LOCAL ; ses réponses sont simulées."""
    peers.save(PEER, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.set_profile(PEER, {"lends_models": True, "ollama": {"models": [
        {"name": m, "caps": ["completion"], "size": 7, "family": "qwen", "parameters": "7B",
         "quantization": "Q4_K_M", "fits_vram": True} for m in (LENT, LOCAL)]}})
    sent = []

    def fake_stream(my_id, peer, action, payload, stop=None, **kw):
        sent.append(payload)
        yield {"content": "Bonjour "}
        yield {"content": f"de {peer['name']}"}
    monkeypatch.setattr(client, "stream", fake_stream)
    return sent


def _lines(response) -> list:
    return [json.loads(line) for line in response.text.splitlines() if line]


def _sse(response) -> list:
    return [line[6:] for line in response.iter_lines(decode_unicode=True)
            if line and line.startswith("data: ")]


# ------------------------------------------------------------------- catalogue
def test_the_models_of_the_integrated_engine_without_ollama(tool):
    listed = {m["id"]: m for m in tool.get("/v1/models").json()["data"]}
    assert set(listed) == {LOCAL, VISION, CODE, STRICT, EMBEDDING}      # pas OLLAMA_ONLY
    assert listed[LOCAL]["object"] == "model" and listed[LOCAL]["owned_by"] == "aiodos"
    tags = {t["name"]: t for t in tool.get("/api/tags").json()["models"]}
    assert set(tags) == set(listed) and all(t["model"] == n for n, t in tags.items())
    assert tags[LOCAL]["capabilities"] == ["completion", "thinking", "tools"]
    assert tags[VISION]["capabilities"] == ["completion", "vision", "thinking", "tools"]
    assert tags[EMBEDDING]["capabilities"] == ["embedding"]
    assert tags[LOCAL]["digest"] == f"empreinte-{LOCAL}"
    assert tags[LOCAL]["details"]["quantization_level"] == "Q4_K_M"
    # La version d'Ollama dont la passerelle suit les formats (Open WebUI la lit par nombres)
    assert tool.get("/api/version").json() == {"version": "0.40.5"}
    shown = tool.post("/api/show", {"model": LOCAL}).json()
    assert shown["model_info"]["llama.context_length"] == CHAT_CONTEXT
    assert shown["parameters"] == f"num_ctx {CHAT_CONTEXT}"
    assert shown["capabilities"] == tags[LOCAL]["capabilities"] and "tools" in shown["template"]
    shown = tool.post("/api/show", {"name": "bge-m3"}).json()      # ancien « name », sans tag
    assert shown["model_info"]["llama.context_length"] == EMBED_CONTEXT
    tool.router.loaded.clear()
    tool.router.loaded[rid(LOCAL)] = "loaded"
    running = tool.get("/api/ps").json()["models"]
    assert [m["name"] for m in running] == [LOCAL]
    assert running[0]["context_length"] == CHAT_CONTEXT and running[0]["expires_at"]


# --------------------------------------------------------- API compatible Ollama
def test_an_ollama_chat_in_text_goes_through_the_facade(tool):
    question = [{"role": "user", "content": "Salut"}, {"role": "system", "content": "Sois bref."},
                {"role": "user", "content": "Ça va ?"}]
    streamed = tool.post("/api/chat", {"model": LOCAL, "messages": question, "options": {
        "num_predict": 64, "temperature": 0.2, "num_ctx": 4096}})
    assert streamed.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(streamed)
    assert all(l["message"]["role"] == "assistant" and "content" in l["message"] for l in lines)
    assert "".join(l["message"]["content"] for l in lines) == ANSWER
    assert "".join(l["message"].get("thinking", "") for l in lines) == THOUGHT
    assert lines[-1]["done"] and lines[-1]["done_reason"] == "stop"
    assert lines[-1]["prompt_eval_count"] == 7 and lines[-1]["eval_count"] == 4
    sent = tool.sent("/v1/chat/completions")[-1]
    assert sent["model"] == rid(LOCAL)
    # Mis en forme pour les gabarits stricts ; num_ctx reste au réglage du routeur
    assert sent["messages"] == [{"role": "user", "content": "Salut\n\nSois bref.\n\nÇa va ?"}]
    assert sent["max_tokens"] == 64 and sent["temperature"] == 0.2 and "num_ctx" not in sent
    assert "chat_template_kwargs" not in sent

    whole = tool.post("/api/chat", {"model": LOCAL, "stream": False, "think": False,
                                    "messages": HELLO}).json()
    assert whole["message"] == {"role": "assistant", "content": ANSWER} and whole["done"]
    assert tool.sent("/v1/chat/completions")[-1]["chat_template_kwargs"] == {
        "enable_thinking": False}


def test_an_ollama_chat_with_tools_and_their_results(tool):
    question = [{"role": "user", "content": "Météo à Lyon ?"}]
    lines = _lines(tool.post("/api/chat", {"model": LOCAL, "messages": question,
                                           "tools": TOOLS}))
    calls = [l["message"]["tool_calls"] for l in lines if l["message"].get("tool_calls")]
    assert calls == [[{"id": "appel-du-routeur", "function": {
        "index": 0, "name": "meteo", "arguments": {"ville": "Lyon"}}}]]  # arguments en objet
    assert lines[-1]["done"] and lines[-1]["done_reason"] == "stop"
    assert tool.sent("/v1/chat/completions")[-1]["tools"] == TOOLS
    whole = tool.post("/api/chat", {"model": LOCAL, "messages": question, "tools": TOOLS,
                                    "stream": False}).json()
    assert whole["message"]["tool_calls"][0]["function"]["arguments"] == {"ville": "Lyon"}

    # Le résultat de l'outil, au format d'Ollama : arguments en objet, sans identifiant
    follow = question + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "meteo", "arguments": {"ville": "Lyon"}}}]},
        {"role": "tool", "content": "Soleil, 21 °C", "tool_name": "meteo"}]
    reply = tool.post("/api/chat", {"model": LOCAL, "messages": follow, "stream": False}).json()
    assert reply["message"]["content"] == AFTER_TOOL
    sent = tool.sent("/v1/chat/completions")[-1]["messages"]
    assert sent[1]["tool_calls"] == [{"id": "call00001", "type": "function", "function": {
        "name": "meteo", "arguments": '{"ville": "Lyon"}'}}]
    assert sent[2] == {"role": "tool", "content": "Soleil, 21 °C", "tool_call_id": "call00001"}


def test_ollama_images_and_imposed_format(tool):
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 16).decode()
    seen = tool.post("/api/chat", {"model": VISION, "stream": False, "messages": [
        {"role": "user", "content": "Que vois-tu ?", "images": [image]}]}).json()
    assert seen["message"]["content"] == SEEN
    assert tool.sent("/v1/chat/completions")[-1]["messages"][0]["content"] == [
        {"type": "text", "text": "Que vois-tu ?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}]
    described = tool.post("/api/generate", {"model": VISION, "prompt": "Décris.",
                                            "images": [image], "stream": False}).json()
    assert described["response"] == SEEN
    before = len(tool.router.requests)
    blind = tool.post("/api/chat", {"model": LOCAL, "messages": [
        {"role": "user", "content": "Que vois-tu ?", "images": [image]}]})
    assert blind.status_code == 400 and "ne voit pas les images" in blind.json()["error"]
    assert len(tool.router.requests) == before            # rien n'est parti au routeur

    shaped = tool.post("/api/chat", {"model": LOCAL, "stream": False, "format": "json",
                                     "messages": HELLO}).json()
    assert json.loads(shaped["message"]["content"]) == json.loads(JSON_ANSWER)
    assert tool.sent("/v1/chat/completions")[-1]["response_format"] == {"type": "json_object"}
    schema = {"type": "object", "properties": {"ville": {"type": "string"}}}
    tool.post("/api/generate", {"model": LOCAL, "prompt": "Une ville ?", "format": schema,
                                "stream": False})
    assert tool.sent("/v1/chat/completions")[-1]["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "format", "schema": schema}}


def test_ollama_generate_raw_text_code_completion_and_loading(tool):
    lines = _lines(tool.post("/api/generate", {"model": LOCAL, "prompt": "Salut",
                                               "system": "Bref"}))
    assert "".join(l["response"] for l in lines) == ANSWER and lines[-1]["done"]
    assert tool.sent("/v1/chat/completions")[-1]["messages"] == [
        {"role": "system", "content": "Bref"}, {"role": "user", "content": "Salut"}]
    raw = _lines(tool.post("/api/generate", {"model": LOCAL, "prompt": "<|fim_prefix|>def f(",
                                             "raw": True,
                                             "options": {"num_predict": 8, "stop": ["\n"]}}))
    assert "".join(l["response"] for l in raw) == COMPLETION
    sent = tool.sent("/v1/completions")[-1]
    assert sent["prompt"] == "<|fim_prefix|>def f(" and sent["max_tokens"] == 8
    assert sent["stop"] == ["\n"]
    # Complétion de code (Continue) : les jetons de complétion du modèle, par /infill
    fim = tool.post("/api/generate", {"model": CODE, "prompt": "def fib(n):\n",
                                      "suffix": "\n    return a\n", "stream": False}).json()
    assert fim["response"] == INFILL and fim["done_reason"] == "stop"
    assert tool.sent("/infill")[-1] == {"model": rid(CODE), "input_prefix": "def fib(n):\n",
                                        "input_suffix": "\n    return a\n", "stream": True}
    refused = tool.post("/api/generate", {"model": LOCAL, "prompt": "def", "suffix": "pass"})
    assert refused.status_code == 400 and "compléter du code" in refused.json()["error"]
    for extra in ({"template": "{{ .Prompt }}"}, {"context": [1, 2, 3]}):
        unknown = tool.post("/api/generate", {"model": LOCAL, "prompt": "x", **extra})
        assert unknown.status_code == 400 and "/api/chat" in unknown.json()["error"]
    # Chargement seul (rien à calculer), puis déchargement
    tool.router.loaded.clear()
    for _ in range(2):                                     # la seconde fois : déjà chargé
        loaded = tool.post("/api/generate", {"model": LOCAL}).json()
        assert loaded["done"] and loaded["done_reason"] == "load"
    assert tool.sent("/models/load") == [{"model": rid(LOCAL)}] * 2
    unloaded = tool.post("/api/chat", {"model": LOCAL, "messages": [], "keep_alive": 0}).json()
    assert unloaded["done_reason"] == "unload" and tool.router.unloaded == [rid(LOCAL)]


# --------------------------------------------------------- API compatible OpenAI
def test_an_openai_chat_is_relayed_to_the_router_as_it_is(tool):
    body = {"model": LOCAL, "messages": [{"role": "user", "content": "Météo à Lyon ?"}],
            "tools": TOOLS, "temperature": 0.2, "top_k": 20, "parallel_tool_calls": False}
    reply = tool.post("/v1/chat/completions", body)
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data["model"] == LOCAL and data["object"] == "chat.completion"   # pas celui du routeur
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert data["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "meteo", "arguments": '{"ville": "Lyon"}'}
    assert reply.headers["X-Aiodos-Model"] == LOCAL
    assert tool.sent("/v1/chat/completions")[-1] == {**body, "model": rid(LOCAL)}   # tel quel

    streamed = tool.post("/v1/chat/completions", {**body, "stream": True,
                                                  "stream_options": {"include_usage": True}},
                         stream=True)
    assert streamed.headers["content-type"].startswith("text/event-stream")
    events = _sse(streamed)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["model"] == LOCAL for c in chunks)
    calls = [t for c in chunks for ch in c["choices"] for t in ch["delta"].get("tool_calls") or []]
    assert "".join(t["function"].get("arguments", "") for t in calls) == '{"ville": "Lyon"}'
    assert "".join(ch["delta"].get("reasoning_content") or "" for c in chunks
                   for ch in c["choices"]) == THOUGHT
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["prompt_tokens"] == 7

    # Ce que seule cette machine sait faire : plusieurs réponses, probabilités, schéma imposé
    several = tool.post("/v1/chat/completions", {
        "model": LOCAL, "messages": HELLO, "n": 2, "logprobs": True,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "ville", "schema": {"type": "object"}}}}).json()
    assert len(several["choices"]) == 2 and several["choices"][0]["logprobs"]
    assert several["choices"][1]["message"]["content"] == JSON_ANSWER


def test_a_text_conversation_is_shaped_for_strict_templates(tool):
    """Un gabarit strict (mistral) refuse deux messages de suite du même rôle : la
    conversation en texte lui arrive mise en forme ; avec des outils, elle part telle quelle,
    et son refus est dit clairement (400)."""
    body = {"model": STRICT, "reasoning_effort": "none", "messages": [
        {"role": "developer", "content": "Sois bref."}, {"role": "user", "content": "Salut"},
        {"role": "user", "content": [{"type": "text", "text": "Ça va ?"}]}]}
    reply = tool.post("/v1/chat/completions", body)
    assert reply.status_code == 200, reply.text
    assert reply.json()["choices"][0]["message"]["content"] == ANSWER
    sent = tool.sent("/v1/chat/completions")[-1]
    assert sent["messages"] == [{"role": "system", "content": "Sois bref."},
                                {"role": "user", "content": "Salut\n\nÇa va ?"}]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}     # sans réflexion
    refused = tool.post("/v1/chat/completions", {**body, "tools": TOOLS})
    assert refused.status_code == 400
    assert "exige que les rôles alternent" in refused.json()["error"]["message"]


def test_openai_images_and_clear_refusals(tool):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    question = [{"role": "user", "content": [{"type": "text", "text": "Que vois-tu ?"}, image]}]
    seen = tool.post("/v1/chat/completions", {"model": VISION, "messages": question})
    assert seen.status_code == 200 and seen.json()["choices"][0]["message"]["content"] == SEEN
    before = len(tool.router.requests)
    blind = tool.post("/v1/chat/completions", {"model": LOCAL, "messages": question})
    assert blind.status_code == 400
    assert "ne voit pas les images" in blind.json()["error"]["message"]
    assert len(tool.router.requests) == before            # refusé avant le routeur
    # Le routeur répond 500 à ce qu'il ne prend pas : pour l'outil, un refus clair
    audio = [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}]}]
    unheard = tool.post("/v1/chat/completions", {"model": VISION, "messages": audio})
    assert unheard.status_code == 400 and "not supported" in unheard.json()["error"]["message"]
    embedding = tool.post("/v1/chat/completions", {"model": f"local:{EMBEDDING}",
                                                   "messages": HELLO})
    assert embedding.status_code == 400 and "embedding" in embedding.json()["error"]["message"]
    absent = tool.post("/v1/chat/completions", {"model": "local:absent:1b", "messages": HELLO})
    assert absent.status_code == 404 and absent.json()["error"]["code"] == "model_not_found"


def test_raw_text_and_code_completion_for_code_editors(tool):
    done = tool.post("/v1/completions", {"model": CODE, "prompt": "<|fim_prefix|>def f(",
                                         "max_tokens": 20, "stop": ["\n"]})
    assert done.status_code == 200, done.text
    assert done.json()["object"] == "text_completion"
    assert done.json()["choices"][0]["text"] == COMPLETION
    assert done.json()["usage"]["completion_tokens"] == 2
    sent = tool.sent("/v1/completions")[-1]
    assert sent["prompt"] == "<|fim_prefix|>def f(" and sent["max_tokens"] == 20
    assert sent["stop"] == ["\n"]
    fim = tool.post("/v1/completions", {"model": CODE, "prompt": "def fib(n):\n",
                                        "suffix": "\n    return a\n", "max_tokens": 16}).json()
    assert fim["choices"][0]["text"] == INFILL and fim["choices"][0]["finish_reason"] == "length"
    assert tool.sent("/infill")[-1]["input_suffix"] == "\n    return a\n"
    events = _sse(tool.post("/v1/completions", {"model": CODE, "prompt": "x", "stream": True},
                            stream=True))
    assert events[-1] == "[DONE]"
    assert "".join(json.loads(e)["choices"][0]["text"] for e in events[:-1]) == COMPLETION
    refused = tool.post("/v1/completions", {"model": LOCAL, "prompt": "def", "suffix": "pass"})
    assert refused.status_code == 400
    assert "compléter du code" in refused.json()["error"]["message"]


# ---------------------------------------------------------------- embeddings
def test_embeddings_by_the_integrated_engine(tool):
    texts = ["fusée lunaire", "chat noir"]
    floats = tool.post("/v1/embeddings", {"model": EMBEDDING, "input": texts}).json()
    assert [d["index"] for d in floats["data"]] == [0, 1]
    assert floats["data"][0]["embedding"] == pytest.approx(unit(_bag_of_words(texts)[0]))
    packed = tool.post("/v1/embeddings", {"model": "bge-m3", "input": texts,
                                          "encoding_format": "base64"}).json()
    vector = np.frombuffer(base64.b64decode(packed["data"][1]["embedding"]), dtype="<f4")
    assert vector.tolist() == pytest.approx(unit(_bag_of_words(texts)[1]), abs=1e-6)
    assert tool.sent("/v1/embeddings")[-1] == {"model": rid(EMBEDDING), "input": texts}
    # « dimensions » : les premières seulement, normées de nouveau, comme Ollama
    text = "fusée lunaire du chat noir sur le canapé rouge"
    short = tool.post("/v1/embeddings", {"model": EMBEDDING, "input": text,
                                         "dimensions": 16}).json()["data"][0]["embedding"]
    assert short == pytest.approx(unit(_bag_of_words([text])[0][:16]))

    embedded = tool.post("/api/embed", {"model": EMBEDDING, "input": "fusée"}).json()
    assert embedded["model"] == EMBEDDING
    assert embedded["embeddings"][0] == pytest.approx(unit(_bag_of_words(["fusée"])[0]))
    listed = tool.post("/api/embed", {"model": EMBEDDING, "input": texts}).json()
    assert len(listed["embeddings"]) == 2
    legacy = tool.post("/api/embeddings", {"model": EMBEDDING, "prompt": "fusée"}).json()
    assert legacy["embedding"] == pytest.approx(unit(_bag_of_words(["fusée"])[0]))
    chat = tool.post("/api/embed", {"model": LOCAL, "input": "x"})
    assert chat.status_code == 400 and "pas un modèle d'embedding" in chat.json()["error"]
    long = tool.post("/v1/embeddings", {"model": EMBEDDING, "input": ["mot " * 80]})
    assert long.status_code == 400 and "Texte trop long" in long.json()["error"]["message"]
    assert f"{EMBED_CONTEXT} au plus" in long.json()["error"]["message"]
    lent = tool.post("/v1/embeddings", {"model": f"peer:{PEER}:{EMBEDDING}", "input": "x"})
    assert lent.status_code == 400                    # jamais ailleurs que sur cette machine


# ------------------------------------------------------------ pannes et règles
def test_a_model_that_does_not_load_is_set_aside(tool):
    install([BROKEN])
    identity._cache.update(at=0.0, profile=None)
    assert BROKEN in {m["id"] for m in tool.get("/v1/models").json()["data"]}
    reply = tool.post("/v1/chat/completions", {"model": BROKEN, "messages": HELLO})
    assert reply.status_code == 404 and "ne se charge pas" in reply.json()["error"]["message"]
    assert BROKEN not in {m["id"] for m in tool.get("/v1/models").json()["data"]}
    assert tool.post("/api/chat", {"model": f"local:{BROKEN}",
                                   "messages": HELLO}).status_code == 404


def test_an_engine_that_does_not_start_is_said_clearly(tool, monkeypatch):
    def stopped():
        raise EngineUnavailable("Le moteur intégré ne démarre pas : llama-server s'est arrêté.")
    monkeypatch.setattr(integrated._router, "url", stopped)
    for path in ("/v1/chat/completions", "/api/chat"):
        reply = tool.post(path, {"model": LOCAL, "messages": HELLO})
        assert reply.status_code == 503 and "ne démarre pas" in reply.text, path


def test_lent_models_and_the_rules_of_the_circle(tool, lender):
    listed = {m["id"] for m in tool.get("/v1/models").json()["data"]}
    assert {LOCAL, LENT, f"peer:{PEER}:{LENT}", f"peer:{PEER}:{LOCAL}"} <= listed
    # Avec des outils, cette machine d'abord (LOCAL est sur les deux)
    called = tool.post("/v1/chat/completions", {"model": LOCAL, "messages": HELLO,
                                                "tools": TOOLS})
    assert called.headers["X-Aiodos-Model"] == LOCAL
    lent = tool.post("/api/chat", {"model": LENT, "stream": False, "messages": HELLO}).json()
    assert lent["message"]["content"] == "Bonjour de PC fixe"
    there = tool.post("/v1/chat/completions", {"model": f"peer:{PEER}:{LOCAL}",
                                               "messages": HELLO})
    assert there.json()["choices"][0]["message"]["content"] == "Bonjour de PC fixe"
    # Une image ne part pas chez la machine appairée, qui seule a LENT
    image = [{"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "data:image/png;base64,iVBORw0KGgo="}}]}]
    refused = tool.post("/v1/chat/completions", {"model": LENT, "messages": image})
    assert refused.status_code == 400 and "cette machine" in refused.json()["error"]["message"]
    for model in ("cloud:mistral", "local:cloud:mistral"):
        assert tool.post("/v1/chat/completions", {"model": model,
                                                  "messages": HELLO}).status_code == 404


def test_a_tool_that_leaves_stops_the_router(tool):
    """L'outil part en route : le routeur cesse de calculer pour lui, par le relais tel quel
    comme par la façade, et pour une réponse attendue sans flux."""
    tool.router.delay = 0.3
    for path in ("/v1/chat/completions", "/api/chat"):
        tool.router.cut.clear()
        response = requests.post(tool.base + path, headers=tool.auth, stream=True, timeout=30,
                                 json={"model": LOCAL, "messages": HELLO, "stream": True})
        next(line for line in response.iter_lines() if line)      # un premier morceau
        response.close()
        assert tool.router.cut.wait(10), f"le routeur calcule encore pour un outil parti ({path})"
    tool.router.cut.clear()
    with pytest.raises(requests.Timeout):
        requests.post(tool.base + "/api/chat", headers=tool.auth, timeout=0.8, json={
            "model": LOCAL, "messages": HELLO, "stream": False})
    assert tool.router.cut.wait(10), "la réponse attendue sans flux n'a pas été arrêtée"


# ------------------------------------------------------------------ serveur MCP
DOCS = {"budget.txt": "Le budget 2024 du projet est de cinquante mille euros.",
        "chat.txt": "Le chat noir dort sur le canapé rouge du salon."}


def _rpc(tool, method, params):
    return tool.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                     headers={"Accept": "application/json, text/event-stream",
                              "MCP-Protocol-Version": "2025-11-25"}).json()["result"]


def test_mcp_with_the_integrated_engine(tool):
    general = store.list_collections()[0]["id"]
    for source, text in DOCS.items():
        pieces = chunking.chunk_text(text)
        store.upsert_entry(general, source, "file", None, list(zip(pieces, _bag_of_words(pieces))))
    found = _rpc(tool, "tools/call", {"name": "rechercher_bibliotheques",
                                      "arguments": {"question": "budget 2024 du projet"}})
    assert found["structuredContent"]["passages"][0]["source"] == "budget.txt"
    assert tool.sent("/v1/embeddings")[-1]["input"] == ["budget 2024 du projet"]
    said = _rpc(tool, "tools/call", {"name": "converser",
                                     "arguments": {"message": "Salut", "modele": LOCAL}})
    assert said["structuredContent"] == {"reponse": ANSWER, "modele": LOCAL,
                                         "machine": "cette machine"}
    # Sans modèle d'ici par défaut : le premier qui converse, pas le modèle d'embedding
    settings.set_value("default_model", "cloud:mistral")
    default = _rpc(tool, "tools/call", {"name": "converser", "arguments": {"message": "Salut"}})
    assert not default["isError"] and default["structuredContent"]["reponse"] == ANSWER
    assert default["structuredContent"]["modele"] != EMBEDDING


# ---------------------------------------------------------------- client officiel
def test_the_official_openai_client(tool):
    openai = pytest.importorskip("openai")
    aiodos = openai.OpenAI(base_url=f"{tool.base}/v1", api_key=tool.key, max_retries=0)
    assert {m.id for m in aiodos.models.list()} >= {LOCAL, VISION, EMBEDDING}
    reply = aiodos.chat.completions.create(model=LOCAL, messages=HELLO)
    assert reply.choices[0].message.content == ANSWER and reply.model == LOCAL
    stream = aiodos.chat.completions.create(model=LOCAL, messages=HELLO, stream=True)
    assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) == ANSWER
    called = aiodos.chat.completions.create(model=LOCAL, messages=HELLO, tools=TOOLS)
    assert called.choices[0].message.tool_calls[0].function.name == "meteo"
    vectors = aiodos.embeddings.create(model=EMBEDDING, input=["fusée lunaire"])   # en base64
    assert vectors.data[0].embedding == pytest.approx(unit(_bag_of_words(["fusée lunaire"])[0]),
                                                      abs=1e-6)
