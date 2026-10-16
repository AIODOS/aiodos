"""Deux vrais nœuds : une conversation de A est servie par un modèle de B.

B prête ses modèles ; A les voit dans son choix de modèles, sous l'identifiant
« peer:<B>:<modèle> », et la réponse arrive en flux chiffré. Quand B arrête de
prêter, A reçoit un refus lisible. Il faut un Ollama local avec un modèle de
conversation (sinon le test est ignoré) ; le plus petit installé est utilisé.
"""
import json

import pytest
import requests

from tests.nodes import Node, pair

OLLAMA = "http://127.0.0.1:11434"


def _smallest_chat_model():
    try:
        models = requests.get(f"{OLLAMA}/api/tags", timeout=2).json().get("models", [])
    except (requests.RequestException, ValueError):
        return None
    for m in sorted(models, key=lambda m: m.get("size", 0)):
        caps = requests.post(f"{OLLAMA}/api/show", json={"model": m["name"]},
                             timeout=10).json().get("capabilities") or []
        if "completion" in caps and "vision" not in caps:
            return m["name"]
    return None


MODEL = _smallest_chat_model()
pytestmark = pytest.mark.skipif(MODEL is None, reason="Ollama avec un modèle de conversation requis")


@pytest.fixture
def two_nodes(tmp_path):
    a = Node(tmp_path / "a", "PC portable").start()
    b = Node(tmp_path / "b", "PC fixe").start()
    yield a, b
    a.stop()
    b.stop()


def _chat(node, model, text):
    """Envoie un message et lit le flux SSE jusqu'à « done »."""
    response = requests.post(
        f"http://127.0.0.1:{node.port}/api/chat",
        json={"text": text, "model": model, "think": False,
              "libraries": False, "conversations": False},
        headers={"X-Aiodos-Token": node.token}, stream=True, timeout=300)
    assert response.status_code == 200, response.text
    for line in response.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            event = json.loads(line[6:])
            if event["type"] == "done":
                return event
    raise AssertionError("Flux terminé sans « done »")


def test_conversation_answered_by_a_model_of_the_paired_machine(two_nodes):
    a, b = two_nodes
    _, b_id = pair(a, b)
    b.api("PUT", "/settings", {"lend_models": True})
    a.api("POST", f"/network/peers/{b_id}/refresh")          # profil à jour tout de suite
    remote_id = f"peer:{b_id}:{MODEL}"
    offered = {m["id"]: m for m in a.api("GET", "/models")}
    assert offered[remote_id]["group"] == "remote" and offered[remote_id]["peer"] == "PC fixe"
    assert a.api("POST", "/chat/preflight", {"text": "Salut", "model": remote_id})["remote"] \
        == "PC fixe"

    done = _chat(a, remote_id, "Réponds seulement par le mot : bonjour")
    assert done["error"] is None and done["html"].strip()
    conversation = a.api("GET", "/conversations")["conversations"][0]
    messages = a.api("GET", f"/conversations/{conversation['id']}")["messages"]
    assert messages[-1]["role"] == "assistant" and messages[-1]["model"] == remote_id

    # B arrête de prêter : refus lisible, rien ne part
    b.api("PUT", "/settings", {"lend_models": False})
    refused = _chat(a, remote_id, "Encore ?")
    assert "ne prête pas" in (refused["error"] or "")
