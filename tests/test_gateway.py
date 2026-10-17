"""Passerelle vers les autres outils (aiodos/gateway) : sécurité, API compatible OpenAI et
Ollama, serveur MCP, pont stdio.

Le nœud tourne dans le processus du test (TestClient), la passerelle ouverte sur un vrai
port de 127.0.0.1 ; un faux Ollama (tests/ollama_stub.py) répond à la place du vrai. Une
machine appairée qui prête ses modèles est simulée (client.stream). Les clients officiels
(openai pour Python, SDK MCP) servent quand ils sont installés (requirements.txt, « Tests »),
curl quand il est là."""
import base64
import json
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

import numpy as np
import pytest
import requests
from fastapi.testclient import TestClient

from aiodos import paths, settings
from aiodos.api.server import create_app
from aiodos.knowledge import chunking, store
from aiodos.network import client, peers, placement, remote_models
from tests.conftest import _bag_of_words
from tests.nodes import free_port
from tests.ollama_stub import EMBEDDING, OllamaStub, answer_of

TOKEN = "jeton"
UI = {"X-Aiodos-Token": TOKEN}
LOCAL = "petit:1b"
LENT = "grand:7b"                    # seulement sur la machine appairée
PEER = "fixe"


@dataclass
class Gateway:
    ui: TestClient
    base: str
    key: str
    ollama: OllamaStub

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def get(self, path, **kw):
        return requests.get(self.base + path, headers={**self.auth, **kw.pop("headers", {})},
                            timeout=30, **kw)

    def post(self, path, body, **kw):
        return requests.post(self.base + path, json=body,
                             headers={**self.auth, **kw.pop("headers", {})}, timeout=30, **kw)


@pytest.fixture
def ollama():
    with OllamaStub([LOCAL]) as stub:
        yield stub


@pytest.fixture
def gateway(ollama, monkeypatch):
    placement.forget()
    port = free_port()
    settings.set_many({"ollama_url": ollama.url, "gateway_enabled": True, "gateway_port": port,
                       "embed_model": EMBEDDING, "rag_min_score": 0.3})
    store.ensure_defaults()
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        info = ui.get("/api/gateway", headers=UI).json()
        assert info["running"], info
        yield Gateway(ui, f"http://127.0.0.1:{port}", info["key"], ollama)
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
        yield {"thinking": "Je réfléchis. "}
        yield {"content": "Bonjour "}
        yield {"content": f"de {peer['name']}"}
        if payload.get("stats"):
            yield {"stats": {"prompt_tokens": 5, "completion_tokens": 3, "done_reason": "stop"}}
    monkeypatch.setattr(client, "stream", fake_stream)
    return sent


def _sse(response) -> list:
    return [line[6:] for line in response.iter_lines(decode_unicode=True)
            if line and line.startswith("data: ")]


# ------------------------------------------------------------------- sécurité
def test_closed_by_default_with_a_key_made_by_aiodos(ollama):
    port = free_port()
    settings.set_many({"ollama_url": ollama.url, "gateway_port": port})
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        info = ui.get("/api/gateway", headers=UI).json()
        assert not info["enabled"] and not info["running"]
        assert info["key"].startswith("aiodos-") and len(info["key"]) > 40
        with pytest.raises(requests.ConnectionError):
            requests.get(f"http://127.0.0.1:{port}/", timeout=2)
        # La clé n'est pas un réglage ordinaire : ni lisible ni remplaçable par /api/settings
        assert "gateway_key" not in ui.get("/api/settings", headers=UI).json()
        ui.put("/api/settings", json={"gateway_key": "choisie"}, headers=UI)
        assert ui.get("/api/gateway", headers=UI).json()["key"] == info["key"]


def test_the_key_is_required_in_the_format_of_each_family(gateway):
    for path, check in (("/v1/models", lambda b: b["error"]["code"] == "invalid_api_key"),
                        ("/api/tags", lambda b: "Clé" in b["error"]),
                        ("/mcp", lambda b: b["jsonrpc"] == "2.0" and b["id"] is None)):
        for headers in ({}, {"Authorization": "Bearer faux"}):
            response = requests.get(gateway.base + path, headers=headers, timeout=10)
            assert response.status_code == 401 and check(response.json()), path
    assert gateway.get("/api/tags").status_code == 200
    assert requests.get(gateway.base + "/api/tags", headers={"X-Api-Key": gateway.key},
                        timeout=10).status_code == 200
    root = requests.get(gateway.base + "/", timeout=10)            # sans clé : « répond-il ? »
    assert root.status_code == 200 and root.text == "Ollama is running"


def test_web_pages_and_other_hosts_are_refused(gateway):
    port = gateway.base.rsplit(":", 1)[1]
    assert gateway.get("/api/tags", headers={"Host": f"evil.example:{port}"}).status_code == 400
    assert gateway.get("/api/tags", headers={"Origin": "https://evil.example"}).status_code == 403
    assert gateway.get("/api/tags", headers={"Origin": "http://localhost:3000"}).status_code == 200
    assert gateway.get("/api/tags", headers={"Host": f"localhost:{port}"}).status_code == 200
    docker = {"Host": f"host.docker.internal:{port}"}        # conteneur sous Docker Desktop
    assert gateway.get("/api/tags", headers=docker).status_code == 200
    server = gateway.ui.app.app.state.gateway._server
    assert server.config.host == "127.0.0.1"                        # jamais sur le réseau


def test_a_renewed_key_replaces_the_old_one_at_once(gateway):
    old = gateway.key
    new = gateway.ui.post("/api/gateway/key", headers=UI).json()["key"]
    assert new != old
    assert gateway.get("/api/tags").status_code == 401
    gateway.key = new
    assert gateway.get("/api/tags").status_code == 200


def test_opened_closed_and_moved_without_restarting(gateway):
    old_port = int(gateway.base.rsplit(":", 1)[1])
    closed = gateway.ui.post("/api/gateway", json={"enabled": False}, headers=UI).json()
    assert not closed["running"]
    with pytest.raises(requests.ConnectionError):
        requests.get(gateway.base + "/", timeout=2)
    new_port = free_port()
    opened = gateway.ui.post("/api/gateway", json={"enabled": True, "port": new_port},
                             headers=UI).json()
    assert opened["running"] and opened["urls"]["openai"] == f"http://127.0.0.1:{new_port}/v1"
    assert requests.get(f"http://127.0.0.1:{new_port}/", timeout=5).status_code == 200
    with socket.socket() as taken:                                  # port déjà pris
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = taken.getsockname()[1]
        refused = gateway.ui.post("/api/gateway", json={"port": busy}, headers=UI).json()
        assert not refused["running"] and str(busy) in refused["error"]
    assert gateway.ui.post("/api/gateway", json={"port": 80}, headers=UI).status_code == 400
    assert old_port != new_port


# --------------------------------------------------------- API compatible OpenAI
def test_models_of_this_machine_and_lent_ones(gateway, lender):
    listed = {m["id"]: m for m in gateway.get("/v1/models").json()["data"]}
    assert set(listed) == {LOCAL, EMBEDDING, LENT, f"peer:{PEER}:{LENT}", f"peer:{PEER}:{LOCAL}"}
    assert listed[LOCAL]["object"] == "model" and isinstance(listed[LOCAL]["created"], int)
    assert listed[f"peer:{PEER}:{LENT}"]["owned_by"] == "PC fixe"
    assert listed[f"peer:{PEER}:{LENT}"]["name"] == f"{LENT} · PC fixe"
    one = gateway.get(f"/v1/models/peer:{PEER}:{LENT}").json()
    assert one["id"] == f"peer:{PEER}:{LENT}"
    missing = gateway.get("/v1/models/inconnu:1b")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "model_not_found"


def test_a_local_conversation_is_relayed_to_ollama_as_it_is(gateway):
    body = {"model": LOCAL, "messages": [{"role": "user", "content": "Salut"}],
            "tools": [{"type": "function", "function": {"name": "meteo", "parameters": {}}}],
            "temperature": 0.2}
    reply = gateway.post("/v1/chat/completions", body)
    assert reply.status_code == 200, reply.text
    assert reply.json()["choices"][0]["message"]["content"] == answer_of(LOCAL)
    assert reply.headers["X-Aiodos-Model"] == LOCAL
    sent = gateway.ollama.bodies("/v1/chat/completions")[-1]
    assert sent["tools"] == body["tools"] and sent["temperature"] == 0.2   # tout y passe


def test_a_lent_model_answers_in_the_format_of_openai(gateway, lender):
    body = {"model": LENT, "messages": [{"role": "developer", "content": "Sois bref."},
                                        {"role": "user", "content": [
                                            {"type": "text", "text": "Salut"}]}],
            "max_completion_tokens": 50, "stop": ["\n\n"], "temperature": 0.3}
    whole = gateway.post("/v1/chat/completions", body)
    assert whole.status_code == 200, whole.text
    data = whole.json()
    assert data["object"] == "chat.completion" and data["model"] == LENT
    assert data["choices"][0]["message"]["content"] == "Bonjour de PC fixe"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    assert whole.headers["X-Aiodos-Model"] == f"peer:{PEER}:{LENT}"
    assert lender[-1]["messages"] == [{"role": "system", "content": "Sois bref."},
                                      {"role": "user", "content": "Salut"}]
    assert lender[-1]["options"] == {"num_predict": 50, "temperature": 0.3, "stop": ["\n\n"]}

    streamed = gateway.post("/v1/chat/completions", {**body, "stream": True,
                                                     "stream_options": {"include_usage": True}},
                            stream=True)
    assert streamed.headers["content-type"].startswith("text/event-stream")
    events = _sse(streamed)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert "".join(d.get("content", "") for d in deltas) == "Bonjour de PC fixe"
    assert any(d.get("reasoning_content") == "Je réfléchis. " for d in deltas)
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] == 8
    assert all(c["usage"] is None for c in chunks[:-1])
    assert len({c["id"] for c in chunks}) == 1


def test_what_only_this_machine_can_do_stays_here(gateway, lender):
    image = {"model": LENT, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Que vois-tu ?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    refused = gateway.post("/v1/chat/completions", image)
    assert refused.status_code == 400 and "cette machine" in refused.json()["error"]["message"]
    # Avec des outils, cette machine d'abord (ici LOCAL est sur les deux machines)
    tools = {"model": LOCAL, "messages": [{"role": "user", "content": "Salut"}],
             "tools": [{"type": "function", "function": {"name": "meteo", "parameters": {}}}]}
    assert gateway.post("/v1/chat/completions", tools).headers["X-Aiodos-Model"] == LOCAL
    # Une machine appairée précise, sans outils : elle répond en texte
    tools["model"] = f"peer:{PEER}:{LOCAL}"
    assert gateway.post("/v1/chat/completions", tools).json()["choices"][0]["message"][
        "content"] == "Bonjour de PC fixe"


def test_unknown_models_and_bad_requests(gateway):
    missing = gateway.post("/v1/chat/completions",
                           {"model": "inconnu:1b", "messages": [{"role": "user", "content": "x"}]})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "model_not_found"
    assert gateway.post("/v1/chat/completions", {"model": LOCAL}).status_code == 400
    cloud = gateway.post("/v1/chat/completions",
                         {"model": "cloud:mistral", "messages": [{"role": "user", "content": "x"}]})
    assert cloud.status_code == 404                     # le cloud ne passe pas par ici


def test_raw_text_completion_for_code_editors(gateway):
    done = gateway.post("/v1/completions", {"model": LOCAL, "prompt": "<|fim_prefix|>def f(",
                                            "max_tokens": 20, "stop": ["\n"]})
    assert done.status_code == 200, done.text
    assert done.json()["object"] == "text_completion"
    assert done.json()["choices"][0]["text"] == answer_of(LOCAL)
    sent = gateway.ollama.bodies("/api/generate")[-1]
    assert sent["raw"] is True and sent["prompt"] == "<|fim_prefix|>def f("
    assert sent["options"] == {"num_predict": 20, "stop": ["\n"]}
    events = _sse(gateway.post("/v1/completions", {"model": LOCAL, "prompt": "x",
                                                   "stream": True}, stream=True))
    assert events[-1] == "[DONE]"
    assert "".join(json.loads(e)["choices"][0]["text"] for e in events[:-1]) == answer_of(LOCAL)


def test_embeddings_in_base64_as_the_openai_clients_ask(gateway):
    texts = ["fusée lunaire", "chat noir"]
    floats = gateway.post("/v1/embeddings", {"model": EMBEDDING, "input": texts}).json()
    assert [d["index"] for d in floats["data"]] == [0, 1]
    assert floats["data"][0]["embedding"] == pytest.approx(_bag_of_words(texts)[0])
    packed = gateway.post("/v1/embeddings", {"model": EMBEDDING, "input": texts,
                                             "encoding_format": "base64"}).json()
    vector = np.frombuffer(base64.b64decode(packed["data"][1]["embedding"]), dtype="<f4")
    assert vector.tolist() == pytest.approx(_bag_of_words(texts)[1])
    assert packed["usage"]["prompt_tokens"] == 2
    lent = gateway.post("/v1/embeddings", {"model": f"peer:{PEER}:{EMBEDDING}", "input": "x"})
    assert lent.status_code == 400                        # jamais ailleurs que sur cette machine


def test_the_official_openai_client(gateway, lender):
    openai = pytest.importorskip("openai")
    tool = openai.OpenAI(base_url=f"{gateway.base}/v1", api_key=gateway.key, max_retries=0)
    assert {m.id for m in tool.models.list()} >= {LOCAL, LENT, f"peer:{PEER}:{LENT}"}
    for model, expected in ((LOCAL, answer_of(LOCAL)), (LENT, "Bonjour de PC fixe"),
                            (f"peer:{PEER}:{LOCAL}", "Bonjour de PC fixe")):
        reply = tool.chat.completions.create(model=model,
                                             messages=[{"role": "user", "content": "Salut"}])
        assert reply.choices[0].message.content == expected, model
        stream = tool.chat.completions.create(model=model, stream=True,
                                              messages=[{"role": "user", "content": "Salut"}])
        assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) \
            == expected, model
    vectors = tool.embeddings.create(model=EMBEDDING, input=["fusée lunaire"])   # en base64
    assert vectors.data[0].embedding == pytest.approx(_bag_of_words(["fusée lunaire"])[0])
    with pytest.raises(openai.AuthenticationError):
        openai.OpenAI(base_url=f"{gateway.base}/v1", api_key="faux",
                      max_retries=0).models.list()


# --------------------------------------------------------- API compatible Ollama
def test_ollama_listing_version_and_details(gateway, lender):
    tags = {m["name"]: m for m in gateway.get("/api/tags").json()["models"]}
    assert set(tags) == {LOCAL, EMBEDDING, LENT, f"peer:{PEER}:{LENT}", f"peer:{PEER}:{LOCAL}"}
    assert all(t["model"] == name for name, t in tags.items())
    assert tags[LENT]["details"]["parameter_size"] == "7B"
    assert gateway.get("/api/version").json() == {"version": "0.40.5"}
    shown = gateway.post("/api/show", {"name": LOCAL}).json()            # ancien « name »
    assert shown["model_info"]["llama.context_length"] == 4096
    lent = gateway.post("/api/show", {"model": f"peer:{PEER}:{LENT}"}).json()
    assert lent["capabilities"] == ["completion"] and "model_info" in lent
    assert gateway.post("/api/show", {"model": "inconnu:1b"}).status_code == 404
    assert gateway.get("/api/ps").json() == {"models": []}
    pull = gateway.post("/api/pull", {"model": LOCAL})
    assert pull.status_code == 404 and "Ollama" in pull.json()["error"]


def test_ollama_chat_from_this_machine_and_from_the_lender(gateway, lender):
    local = gateway.post("/api/chat", {"model": LOCAL,
                                       "messages": [{"role": "user", "content": "Salut"}]})
    assert local.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in local.text.splitlines() if line]
    assert "".join(l["message"]["content"] for l in lines) == answer_of(LOCAL)

    lent = gateway.post("/api/chat", {"model": LENT, "options": {"num_ctx": 8192, "seed": 3},
                                      "messages": [{"role": "user", "content": "Salut"}]})
    lines = [json.loads(line) for line in lent.text.splitlines() if line]
    assert all(l["message"]["role"] == "assistant" and "content" in l["message"] for l in lines)
    assert "".join(l["message"]["content"] for l in lines) == "Bonjour de PC fixe"
    assert any(l["message"].get("thinking") for l in lines)
    assert lines[-1]["done"] and lines[-1]["done_reason"] == "stop"
    assert lines[-1]["eval_count"] == 3 and lines[-1]["prompt_eval_count"] == 5
    assert lender[-1]["options"] == {"num_ctx": 8192, "seed": 3}   # trié par la machine qui prête

    whole = gateway.post("/api/chat", {"model": LENT, "stream": False,
                                       "messages": [{"role": "user", "content": "Salut"}]}).json()
    assert whole["message"]["content"] == "Bonjour de PC fixe" and whole["done"]


def test_ollama_generate_embed_and_loading(gateway, lender):
    lines = [json.loads(l) for l in gateway.post(
        "/api/generate", {"model": LENT, "prompt": "Salut", "system": "Bref"}).text.splitlines()]
    assert "".join(l["response"] for l in lines) == "Bonjour de PC fixe"
    assert lender[-1]["messages"] == [{"role": "system", "content": "Bref"},
                                      {"role": "user", "content": "Salut"}]
    loaded = gateway.post("/api/generate", {"model": LENT}).json()      # chargement seul
    assert loaded["done"] and loaded["done_reason"] == "load"
    fim = gateway.post("/api/generate", {"model": LENT, "prompt": "def", "suffix": "pass"})
    assert fim.status_code == 400                       # complétion de code : ici seulement
    embedded = gateway.post("/api/embed", {"model": EMBEDDING, "input": "fusée"}).json()
    assert embedded["embeddings"][0] == pytest.approx(_bag_of_words(["fusée"])[0])
    legacy = gateway.post("/api/embeddings", {"model": EMBEDDING, "prompt": "fusée"}).json()
    assert legacy["embedding"] == pytest.approx(_bag_of_words(["fusée"])[0])


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl absent")
def test_curl_talks_to_the_ollama_api(gateway, lender):
    def curl(path, body):
        out = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {gateway.key}",
                              "-d", json.dumps(body), gateway.base + path],
                             capture_output=True, timeout=30)
        return [json.loads(line) for line in out.stdout.decode("utf-8").splitlines() if line]
    for model, expected in ((LOCAL, answer_of(LOCAL)), (f"peer:{PEER}:{LENT}",
                                                       "Bonjour de PC fixe")):
        lines = curl("/api/chat", {"model": model,
                                   "messages": [{"role": "user", "content": "Salut"}]})
        assert "".join(l["message"]["content"] for l in lines) == expected


# ------------------------------------------------------------------ serveur MCP
def _library(fake_texts: dict):
    general = store.list_collections()[0]["id"]
    for source, text in fake_texts.items():
        pieces = chunking.chunk_text(text)
        store.upsert_entry(general, source, "file", None, list(zip(pieces, _bag_of_words(pieces))))


DOCS = {"budget.txt": "Le budget 2024 du projet est de cinquante mille euros.",
        "chat.txt": "Le chat noir dort sur le canapé rouge du salon."}


def _rpc(gateway, method, params=None, modern=False, headers=None, message_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if message_id is not None:
        message["id"] = message_id
    params = dict(params or {})
    sent = {"Accept": "application/json, text/event-stream", **(headers or {})}
    if modern:
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                           "io.modelcontextprotocol/clientCapabilities": {}}
        sent.setdefault("MCP-Protocol-Version", "2026-07-28")
        sent.setdefault("Mcp-Method", method)
        if method == "tools/call":
            sent.setdefault("Mcp-Name", params.get("name"))
    if params:
        message["params"] = params
    return gateway.post("/mcp", message, headers=sent)


def test_mcp_with_the_initialize_handshake(gateway):
    _library(DOCS)
    init = _rpc(gateway, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "essai", "version": "1"}})
    result = init.json()["result"]
    assert result["protocolVersion"] == "2025-06-18" and "Mcp-Session-Id" not in init.headers
    assert result["serverInfo"]["name"] == "aiodos" and result["capabilities"]["tools"]
    assert _rpc(gateway, "notifications/initialized", message_id=None).status_code == 202
    version = {"MCP-Protocol-Version": "2025-06-18"}
    assert _rpc(gateway, "ping", headers=version).json()["result"] == {}
    tools = _rpc(gateway, "tools/list", headers=version).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["lister_bibliotheques", "rechercher_bibliotheques",
                                          "converser"]
    found = _rpc(gateway, "tools/call", {"name": "rechercher_bibliotheques",
                                         "arguments": {"question": "budget 2024 du projet"}},
                 headers=version).json()["result"]
    assert not found["isError"]
    assert found["structuredContent"]["passages"][0]["source"] == "budget.txt"
    assert "cinquante mille euros" in found["content"][0]["text"]
    future = _rpc(gateway, "initialize", {"protocolVersion": "2099-01-01", "capabilities": {},
                                          "clientInfo": {"name": "x", "version": "1"}})
    assert future.json()["result"]["protocolVersion"] == "2025-11-25"
    unknown = _rpc(gateway, "tools/list", headers={"MCP-Protocol-Version": "1999-01-01"})
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == -32022


def test_mcp_stateless_2026(gateway):
    _library(DOCS)
    discover = _rpc(gateway, "server/discover", modern=True).json()["result"]
    assert discover["resultType"] == "complete" and "2026-07-28" in discover["supportedVersions"]
    assert discover["ttlMs"] > 0 and discover["cacheScope"] in ("public", "private")
    assert discover["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "aiodos"
    listed = _rpc(gateway, "tools/list", modern=True).json()["result"]
    assert listed["resultType"] == "complete" and len(listed["tools"]) == 3
    called = _rpc(gateway, "tools/call", {"name": "lister_bibliotheques", "arguments": {}},
                  modern=True).json()["result"]
    assert called["resultType"] == "complete"
    assert called["structuredContent"]["bibliotheques"][0]["elements"] == 2
    mismatch = _rpc(gateway, "tools/call", {"name": "lister_bibliotheques", "arguments": {}},
                    modern=True, headers={"Mcp-Name": "converser"})
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == -32020
    missing = _rpc(gateway, "tools/list", headers={"MCP-Protocol-Version": "2026-07-28"})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == -32602
    unknown = _rpc(gateway, "resources/list", modern=True)
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == -32601
    assert requests.get(gateway.base + "/mcp", headers=gateway.auth, timeout=10).status_code == 405


def test_mcp_tool_errors_are_told_to_the_assistant(gateway):
    version = {"MCP-Protocol-Version": "2025-11-25"}
    empty = _rpc(gateway, "tools/call", {"name": "rechercher_bibliotheques",
                                         "arguments": {"question": "budget"}},
                 headers=version).json()["result"]
    assert not empty["isError"] and empty["structuredContent"]["passages"] == []
    wrong = _rpc(gateway, "tools/call", {"name": "rechercher_bibliotheques",
                                         "arguments": {"question": "x", "bibliotheque": "Nulle"}},
                 headers=version).json()["result"]
    assert wrong["isError"] and "Nulle" in wrong["content"][0]["text"]
    unknown = _rpc(gateway, "tools/call", {"name": "effacer_tout", "arguments": {}},
                   headers=version).json()
    assert unknown["error"]["code"] == -32602


def test_mcp_conversation_through_the_circle(gateway, lender):
    version = {"MCP-Protocol-Version": "2025-11-25"}
    here = _rpc(gateway, "tools/call", {"name": "converser",
                                        "arguments": {"message": "Salut", "modele": LOCAL}},
                headers=version).json()["result"]
    assert here["structuredContent"] == {"reponse": answer_of(LOCAL), "modele": LOCAL,
                                         "machine": "cette machine"}
    there = _rpc(gateway, "tools/call", {"name": "converser",
                                         "arguments": {"message": "Salut", "modele": LENT}},
                 headers=version).json()["result"]
    assert there["content"][0]["text"] == "Bonjour de PC fixe"
    assert there["structuredContent"]["machine"] == "PC fixe"


def test_mcp_batches_of_2025_03_26(gateway):
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    replies = gateway.post("/mcp", batch).json()
    assert [r["id"] for r in replies] == [1, 2]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_the_official_mcp_client(gateway, mode):
    """SDK officiel : époque 2026-07-28 (sonde server/discover) ou poignée de main."""
    pytest.importorskip("mcp")
    import anyio
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    _library(DOCS)

    async def search():
        async with httpx2.AsyncClient(headers=gateway.auth, timeout=30) as http:
            transport = streamable_http_client(f"{gateway.base}/mcp", http_client=http)
            async with Client(transport, mode=mode) as session:
                names = [t.name for t in (await session.list_tools()).tools]
                found = await session.call_tool("rechercher_bibliotheques",
                                                {"question": "chat noir sur le canapé"})
                return session.protocol_version, names, found
    version, names, found = anyio.run(search)
    assert version == ("2026-07-28" if mode == "auto" else "2025-11-25")
    assert "rechercher_bibliotheques" in names and not found.is_error
    assert found.structured_content["passages"][0]["source"] == "chat.txt"


def test_the_stdio_bridge_with_the_official_client(gateway):
    """Comme Claude Desktop : un programme lancé par l'assistant, qui relaie vers AIODOS en
    lisant lui-même l'adresse et la clé dans les réglages."""
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client, StdioServerParameters
    _library(DOCS)
    config = gateway.ui.get("/api/gateway", headers=UI).json()["mcp_stdio"]["mcpServers"]["aiodos"]
    assert config["args"] == ["-m", "aiodos.gateway.mcp_stdio"]
    env = {**config["env"], "AIODOS_DATA_DIR": str(paths.data_dir()),
           "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    server = StdioServerParameters(command=sys.executable, args=config["args"], env=env,
                                   cwd=str(paths.APP_ROOT))

    async def search():
        async with Client(server) as session:
            return await session.call_tool("rechercher_bibliotheques",
                                           {"question": "budget 2024", "nombre": 1})
    found = anyio.run(search)
    assert found.structured_content["passages"][0]["source"] == "budget.txt"


def test_the_stdio_bridge_relays_line_by_line(gateway):
    """Le pont lancé comme le ferait un assistant, sans SDK : un message JSON-RPC par ligne,
    dans les deux époques du protocole ; il ajoute lui-même les en-têtes du transport HTTP."""
    import os
    _library(DOCS)
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {}}
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "rechercher_bibliotheques", "arguments": {"question": "budget 2024"},
            "_meta": meta}},
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "essai", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
    ]
    env = {**os.environ, "AIODOS_DATA_DIR": str(paths.data_dir()),
           "PYTHONPATH": str(paths.APP_ROOT), "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, "-m", "aiodos.gateway.mcp_stdio"],
                          input="".join(json.dumps(m) + "\n" for m in messages).encode(),
                          capture_output=True, env=env, cwd=paths.APP_ROOT, timeout=60)
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines() if line]
    assert [r["id"] for r in replies] == [1, 2, 3, 4], done.stderr
    assert replies[0]["result"]["resultType"] == "complete"
    assert replies[1]["result"]["structuredContent"]["passages"][0]["source"] == "budget.txt"
    assert replies[2]["result"]["protocolVersion"] == "2025-11-25"
    assert len(replies[3]["result"]["tools"]) == 3


def test_the_stdio_bridge_says_when_aiodos_is_closed(gateway):
    from aiodos.gateway import mcp_stdio
    gateway.ui.post("/api/gateway", json={"enabled": False}, headers=UI)
    bridge = mcp_stdio.Bridge()
    reply = bridge.forward({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert reply["id"] == 7 and "AIODOS ne répond pas" in reply["error"]["message"]
    assert bridge.forward({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_remote_models_report_the_lender(gateway, lender):
    """Le choix automatique de la passerelle est celui des conversations : un modèle qui
    n'est que sur la machine appairée y part, une demande d'image reste ici."""
    assert remote_models.parse(f"peer:{PEER}:{LENT}") == (PEER, LENT)
    reply = gateway.post("/api/chat", {"model": LENT, "stream": False,
                                       "messages": [{"role": "user", "content": "Salut"}]})
    assert reply.json()["message"]["content"] == "Bonjour de PC fixe"


def test_large_models_split_between_my_machines(gateway, monkeypatch):
    """Un gros modèle réparti (« reparti:… », cluster/group.py) est proposé tel quel et
    répond par son groupe, au format de chaque API ; pas d'embeddings avec lui."""
    from aiodos.cluster import group as cluster
    monkeypatch.setattr(cluster, "available", lambda: [
        {"id": "reparti:qwen3.6", "label": "qwen3.6", "group": "cluster",
         "machines": ["PC fixe"], "complete": True, "size": 22}])
    asked = []

    def fake_group(model, messages, think=True, stop=None, options=None, stats=False):
        asked.append((model, messages, options))
        yield "content", "Réponse du groupe"
        if stats:
            yield "stats", {"prompt_tokens": 4, "completion_tokens": 2, "done_reason": "length"}
    monkeypatch.setattr(cluster, "chat_stream", fake_group)
    listed = {m["id"]: m for m in gateway.get("/v1/models").json()["data"]}
    assert listed["reparti:qwen3.6"]["owned_by"] == "réparti"
    reply = gateway.post("/v1/chat/completions", {"model": "reparti:qwen3.6", "messages": [
        {"role": "user", "content": "Salut"}], "max_tokens": 20, "stop": ["FIN"]}).json()
    assert reply["choices"][0]["message"]["content"] == "Réponse du groupe"
    assert reply["choices"][0]["finish_reason"] == "length"
    assert reply["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    assert asked == [("reparti:qwen3.6", [{"role": "user", "content": "Salut"}],
                      {"num_predict": 20, "stop": ["FIN"]})]
    lines = [json.loads(l) for l in gateway.post("/api/chat", {
        "model": "reparti:qwen3.6", "messages": [{"role": "user", "content": "Salut"}]}
    ).text.splitlines() if l]
    assert "".join(l["message"]["content"] for l in lines) == "Réponse du groupe"
    assert gateway.post("/v1/embeddings", {"model": "reparti:qwen3.6",
                                           "input": "x"}).status_code == 400


def test_cloud_models_never_pass_even_disguised(gateway, monkeypatch):
    """Un modèle du cloud ne passe pas par la passerelle, même déguisé (« local:cloud:… ») :
    son envoi se valide dans l'interface, message par message."""
    from aiodos.services import cloud
    settings.set_value("mistral_key", "sk-test-12345678")
    sent = []
    monkeypatch.setattr(cloud, "chat_stream",
                        lambda *a, **k: sent.append(a) or iter([("content", "parti !")]))
    question = [{"role": "user", "content": "Salut"}]
    for model in ("cloud:mistral", "local:cloud:mistral", "auto:cloud:mistral",
                  "local:peer:x:y", "local:auto:x", "local:reparti:x"):
        reply = gateway.post("/v1/chat/completions", {"model": model, "messages": question})
        assert reply.status_code == 404, model
        assert gateway.post("/api/chat", {"model": model,
                                          "messages": question}).status_code == 404, model
    version = {"MCP-Protocol-Version": "2025-11-25"}
    refused = _rpc(gateway, "tools/call", {"name": "converser", "arguments": {
        "message": "Salut", "modele": "local:cloud:mistral"}}, headers=version).json()["result"]
    assert refused["isError"]
    assert sent == []


def test_libraries_kept_on_this_pc_are_never_shown_to_assistants(gateway):
    """Le cadenas (portée « local ») : ni listée, ni cherchée, ni nommable par un assistant,
    dont le modèle est souvent dans le cloud."""
    _library(DOCS)
    secret = store.create_collection("Secret")
    store.set_scope(secret, "local")
    pieces = ["Le code du coffre-fort du budget 2024 est 1234."]
    store.upsert_entry(secret, "coffre.txt", "file", None, list(zip(pieces, _bag_of_words(pieces))))
    version = {"MCP-Protocol-Version": "2025-11-25"}
    listed = _rpc(gateway, "tools/call", {"name": "lister_bibliotheques", "arguments": {}},
                  headers=version).json()["result"]["structuredContent"]["bibliotheques"]
    assert [b["nom"] for b in listed] == [store.DEFAULT_COLLECTION]
    found = _rpc(gateway, "tools/call", {"name": "rechercher_bibliotheques", "arguments": {
        "question": "code du coffre-fort du budget 2024", "nombre": 10}},
        headers=version).json()["result"]["structuredContent"]["passages"]
    assert found and all(p["source"] != "coffre.txt" for p in found)
    named = _rpc(gateway, "tools/call", {"name": "rechercher_bibliotheques", "arguments": {
        "question": "code du coffre", "bibliotheque": "Secret"}}, headers=version).json()
    assert named["result"]["isError"] and "gardée sur ce PC" in named["result"]["content"][0]["text"]


def test_untagged_model_names_like_ollama(gateway):
    """« petit » désigne « petit:latest » comme pour Ollama (ici, le faux Ollama n'a que
    « petit:1b » : « petit » n'existe donc pas)."""
    assert gateway.get("/v1/models/plongement").json()["id"] == EMBEDDING
    assert gateway.post("/api/show", {"model": "plongement"}).status_code == 200
    missing = gateway.post("/v1/chat/completions", {"model": "petit", "messages": [
        {"role": "user", "content": "Salut"}]})
    assert missing.status_code == 404


@pytest.fixture
def slow_lender(monkeypatch):
    """Machine appairée qui répond lentement, mot à mot, et dit quand on l'a arrêtée."""
    import threading
    import time
    peers.save(PEER, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.set_profile(PEER, {"lends_models": True, "ollama": {"models": [
        {"name": LENT, "caps": ["completion"], "size": 7}]}})
    stopped = threading.Event()
    stopped.spoke = threading.Event()     # un premier mot est parti

    def fake_stream(my_id, peer, action, payload, stop=None, **kw):
        finished = False
        try:                              # arrêtée : stop levé, ou flux fermé en route
            for word in ("Un", " deux", " trois", " quatre", " cinq", " six"):
                if stop is not None and stop.is_set():
                    return
                time.sleep(0.5)
                yield {"content": word}
                stopped.spoke.set()
            finished = True
        finally:
            if not finished:
                stopped.set()
    monkeypatch.setattr(client, "stream", fake_stream)
    return stopped


def test_a_tool_that_leaves_before_a_whole_answer_stops_it(gateway, slow_lender):
    """Réponse attendue sans flux : l'outil part, la machine qui calcule est arrêtée."""
    with pytest.raises(requests.Timeout):
        requests.post(gateway.base + "/v1/chat/completions", headers=gateway.auth, timeout=0.8,
                      json={"model": LENT, "messages": [{"role": "user", "content": "Salut"}]})
    assert slow_lender.wait(8), "la génération n'a pas été arrêtée"


def test_mcp_cancelled_notification_stops_the_conversation(gateway, slow_lender):
    import threading
    version = {"MCP-Protocol-Version": "2025-11-25"}
    result = {}
    call = threading.Thread(target=lambda: result.update(reply=_rpc(
        gateway, "tools/call", {"name": "converser", "arguments": {
            "message": "Compte", "modele": LENT}}, headers=version, message_id=41).json()))
    call.start()
    # Quelques mots sont partis (le choix de la machine peut prendre plus d'une seconde :
    # nvidia-smi met 1,5 s à réveiller la carte d'un portable en veille)
    assert slow_lender.spoke.wait(15)
    cancel = _rpc(gateway, "notifications/cancelled", {"requestId": 41, "reason": "assez"},
                  headers=version, message_id=None)
    assert cancel.status_code == 202
    call.join(10)
    assert slow_lender.is_set()
    assert result["reply"]["result"]["structuredContent"]["reponse"].startswith("Un")
