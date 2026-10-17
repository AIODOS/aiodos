"""Deux vrais nœuds (processus séparés) et la passerelle de l'un d'eux.

Un outil extérieur branché sur la passerelle de A converse avec un modèle de A et avec un
modèle que B lui prête ; en mode automatique, la conversation part chez B quand A est
occupé ou n'a pas le modèle ; un client MCP cherche dans une bibliothèque de A.

Chaque nœud a son faux Ollama (tests/ollama_stub.py), qui signe ses réponses : A a
« petit:1b », B « petit:1b » et « grand:7b ». Le client openai pour Python et le SDK MCP
servent s'ils sont installés, curl s'il est là. Le dernier essai prend le vrai Ollama de
cette machine, avec son plus petit modèle de conversation (ignoré sans lui).
"""
import importlib.util
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import pytest
import requests

from tests.nodes import Node, free_port, pair
from tests.ollama_stub import EMBEDDING, OllamaStub

SMALL = "petit:1b"            # sur les deux machines
BIG = "grand:7b"              # seulement sur B


def installed(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


@dataclass
class Circle:
    a: Node
    b: Node
    b_id: str
    base: str
    key: str
    ollama_a: OllamaStub
    ollama_b: OllamaStub

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def post(self, path, body, **kw):
        return requests.post(self.base + path, json=body, headers=self.auth, timeout=60, **kw)


def _start_pair(root, a_settings: dict, b_settings: dict, started: list):
    """A (passerelle ouverte) et B (prête ses modèles), appairées ; started : les nœuds
    lancés, à arrêter même si la suite échoue."""
    port = free_port()
    a = Node(root / "a", "PC portable").preset(gateway_enabled=True, gateway_port=port,
                                               **a_settings)
    b = Node(root / "b", "PC fixe").preset(lend_models=True, **b_settings)
    for node in (a, b):
        started.append(node)
        node.start()
    _, b_id = pair(a, b)
    a.api("POST", f"/network/peers/{b_id}/refresh")          # profil de B à jour tout de suite
    return a, b, b_id, f"http://127.0.0.1:{port}", a.api("GET", "/gateway")["key"]


@pytest.fixture(scope="module")
def circle(tmp_path_factory):
    root = tmp_path_factory.mktemp("cercle")
    with OllamaStub([SMALL], name="A") as ollama_a, \
            OllamaStub([SMALL, BIG], name="B") as ollama_b:
        nodes = []
        try:
            a, b, b_id, base, key = _start_pair(
                root, {"ollama_url": ollama_a.url, "embed_model": EMBEDDING,
                       "rag_min_score": 0.3},
                {"ollama_url": ollama_b.url}, nodes)
            yield Circle(a, b, b_id, base, key, ollama_a, ollama_b)
        finally:
            for node in nodes:
                node.stop()


def _content(reply) -> str:
    return reply.json()["choices"][0]["message"]["content"]


def test_an_openai_client_talks_to_a_local_model_and_to_a_lent_one(circle):
    openai = pytest.importorskip("openai")
    tool = openai.OpenAI(base_url=f"{circle.base}/v1", api_key=circle.key, max_retries=0)
    ids = {m.id for m in tool.models.list()}
    assert {SMALL, BIG, f"peer:{circle.b_id}:{SMALL}", f"peer:{circle.b_id}:{BIG}"} <= ids
    for model, expected in ((SMALL, circle.ollama_a.answer(SMALL)),           # chez A
                            (f"peer:{circle.b_id}:{SMALL}", circle.ollama_b.answer(SMALL)),
                            (BIG, circle.ollama_b.answer(BIG))):               # seulement B
        reply = tool.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Salut"}])
        assert reply.choices[0].message.content == expected, model
        stream = tool.chat.completions.create(
            model=model, stream=True, messages=[{"role": "user", "content": "Salut"}])
        assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) \
            == expected, model


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl absent")
def test_curl_talks_to_a_local_model_and_to_a_lent_one(circle):
    def chat(model):
        out = subprocess.run(
            ["curl", "-s", "-H", f"Authorization: Bearer {circle.key}", "-d",
             json.dumps({"model": model, "messages": [{"role": "user", "content": "Salut"}]}),
             f"{circle.base}/api/chat"], capture_output=True, timeout=60)
        lines = [json.loads(line) for line in out.stdout.decode("utf-8").splitlines() if line]
        assert lines and lines[-1]["done"], out.stdout
        return "".join(line["message"]["content"] for line in lines)
    assert chat(SMALL) == circle.ollama_a.answer(SMALL)
    assert chat(f"peer:{circle.b_id}:{BIG}") == circle.ollama_b.answer(BIG)
    assert chat(BIG) == circle.ollama_b.answer(BIG)


def test_the_automatic_choice_hands_over_when_this_machine_is_busy(circle):
    """A a le modèle et le garde tant qu'elle est libre ; pendant qu'elle répond à une
    conversation, la suivante part chez B, libre, qui le prête aussi."""
    body = {"model": SMALL, "messages": [{"role": "user", "content": "Salut"}]}
    free = circle.post("/v1/chat/completions", body)
    assert free.headers["X-Aiodos-Model"] == SMALL
    assert _content(free) == circle.ollama_a.answer(SMALL)

    seen = len(circle.ollama_a.bodies("/v1/chat/completions"))
    circle.ollama_a.delay = 1.5                       # A répond lentement : elle est occupée
    first = {}
    worker = threading.Thread(target=lambda: first.update(
        reply=circle.post("/v1/chat/completions", body)))
    try:
        worker.start()
        deadline = time.time() + 10
        while len(circle.ollama_a.bodies("/v1/chat/completions")) == seen:
            assert time.time() < deadline, "A n'a pas commencé à répondre"
            time.sleep(0.05)
        second = circle.post("/v1/chat/completions", body)
        assert second.headers["X-Aiodos-Model"] == f"peer:{circle.b_id}:{SMALL}"
        assert _content(second) == circle.ollama_b.answer(SMALL)
    finally:
        worker.join(30)
        circle.ollama_a.delay = 0
    assert _content(first["reply"]) == circle.ollama_a.answer(SMALL)


def test_a_conversation_in_automatic_mode_says_which_machine_answered(circle):
    response = requests.post(
        f"http://127.0.0.1:{circle.a.port}/api/chat",
        json={"text": "Salut", "model": f"auto:{BIG}", "libraries": False,
              "conversations": False}, headers={"X-Aiodos-Token": circle.a.token},
        stream=True, timeout=60)
    events = [json.loads(line[6:]) for line in response.iter_lines(decode_unicode=True)
              if line and line.startswith("data: ")]
    route = next(e for e in events if e["type"] == "route")
    assert route["model"] == f"peer:{circle.b_id}:{BIG}" and route["machine"] == "PC fixe"
    assert events[-1]["type"] == "done" and circle.ollama_b.answer(BIG) in events[-1]["html"]
    conversation = circle.a.api("GET", f"/conversations/{events[0]['conversation_id']}")
    assert conversation["model"] == f"auto:{BIG}"
    assert conversation["messages"][-1]["model"] == f"peer:{circle.b_id}:{BIG}"


def _index_note(circle, folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "recette.txt").write_text(
        "La recette de la tarte aux pommes demande six pommes et une pâte brisée.",
        encoding="utf-8")
    (folder / "voiture.txt").write_text(
        "La voiture rouge est garée devant le garage du voisin.", encoding="utf-8")
    general = circle.a.api("GET", "/collections")["collections"][0]["id"]
    job_id = circle.a.api("POST", "/index", {"paths": [str(folder)],
                                             "collection_id": general})["job_id"]
    circle.a.wait_for(lambda: next(j for j in circle.a.api("GET", "/jobs")["jobs"]
                                   if j["id"] == job_id)["status"] == "done",
                      timeout=60, what="indexation sur A")


def test_an_mcp_client_searches_the_libraries_of_this_machine(circle, tmp_path):
    _index_note(circle, tmp_path / "notes")
    headers = {**circle.auth, "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-11-25"}
    reply = requests.post(f"{circle.base}/mcp", headers=headers, timeout=60, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "rechercher_bibliotheques",
                   "arguments": {"question": "recette de la tarte aux pommes"}}}).json()
    passages = reply["result"]["structuredContent"]["passages"]
    assert passages[0]["source"].endswith("recette.txt")
    if not installed("mcp"):                          # le SDK officiel, s'il est là
        return
    import anyio
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async def search():
        async with httpx2.AsyncClient(headers=circle.auth, timeout=60) as http:
            async with Client(streamable_http_client(f"{circle.base}/mcp",
                                                     http_client=http)) as session:
                return await session.call_tool("rechercher_bibliotheques",
                                               {"question": "voiture rouge du voisin"})
    found = anyio.run(search)
    assert found.structured_content["passages"][0]["source"].endswith("voiture.txt")


# ------------------------------------------------------------- avec le vrai Ollama
OLLAMA = "http://127.0.0.1:11434"


def _smallest_chat_model():
    try:
        models = requests.get(f"{OLLAMA}/api/tags", timeout=2).json().get("models", [])
    except (requests.RequestException, ValueError):
        return None
    for m in sorted(models, key=lambda m: m.get("size", 0)):
        caps = m.get("capabilities") or requests.post(
            f"{OLLAMA}/api/show", json={"model": m["name"]}, timeout=10).json().get(
            "capabilities") or []
        if "completion" in caps and "vision" not in caps and "embedding" not in caps:
            return m["name"]
    return None


REAL = _smallest_chat_model()


@pytest.mark.skipif(REAL is None, reason="Ollama avec un modèle de conversation requis")
def test_with_the_real_ollama_a_local_and_a_lent_model_answer(tmp_path):
    nodes = []
    try:
        a, b, b_id, base, key = _start_pair(tmp_path, {}, {}, nodes)
        auth = {"Authorization": f"Bearer {key}"}
        question = [{"role": "user", "content": "Réponds seulement par le mot : bonjour"}]
        for model in (f"local:{REAL}", f"peer:{b_id}:{REAL}"):
            reply = requests.post(f"{base}/v1/chat/completions", headers=auth, timeout=300,
                                  json={"model": model, "messages": question,
                                        "max_tokens": 20})
            assert reply.status_code == 200, reply.text
            assert _content(reply).strip(), model
        if shutil.which("curl"):
            out = subprocess.run(
                ["curl", "-s", "-H", f"Authorization: Bearer {key}", "-d",
                 json.dumps({"model": f"peer:{b_id}:{REAL}", "messages": question,
                             "options": {"num_predict": 20}}), f"{base}/api/chat"],
                capture_output=True, timeout=300)
            lines = [json.loads(l) for l in out.stdout.decode("utf-8").splitlines() if l]
            assert lines[-1]["done"] and "".join(l["message"]["content"] for l in lines).strip()
        if installed("openai"):
            import openai
            tool = openai.OpenAI(base_url=f"{base}/v1", api_key=key, max_retries=0,
                                 timeout=300)
            for model in (REAL, f"peer:{b_id}:{REAL}"):
                reply = tool.chat.completions.create(model=model, messages=question,
                                                     max_tokens=20)
                assert reply.choices[0].message.content.strip(), model
    finally:
        for node in nodes:
            node.stop()
