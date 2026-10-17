"""Un modèle réparti de bout en bout, sur ce seul PC : la machine A, dont la carte
graphique est cachée à llama.cpp (GGML_VK_VISIBLE_DEVICES), emprunte celle de la machine
B. Les deux s'appairent sur invitation : le serveur RPC de B n'est joint que par un
tunnel chiffré des compagnons, et le dernier étage du modèle reste chez A.

Il faut llama.cpp (variable AIODOS_LLAMACPP_DIR : le dossier de llama-server et de
ggml-rpc-server, version Vulkan), le compagnon compilé et le modèle mistral:7b dans
Ollama ; sinon le test est ignoré. Le premier chargement envoie ~4 Go de poids par le
tunnel : compter une à deux minutes.
"""
import json
import os

import pytest
import requests

from aiodos.network import companion
from tests.nodes import Node, pair_by_invitation
from tests.test_network_chat import _chat

LLAMA = os.environ.get("AIODOS_LLAMACPP_DIR")
MODEL = "mistral:7b"


def _installed(model) -> bool:
    try:
        tags = requests.get("http://127.0.0.1:11434/api/tags", timeout=2).json()
    except (requests.RequestException, ValueError):
        return False
    return any(m["name"] == model for m in tags.get("models", []))


pytestmark = pytest.mark.skipif(
    not LLAMA or companion.find_binary() is None or not _installed(MODEL),
    reason="il faut AIODOS_LLAMACPP_DIR (llama.cpp), le compagnon compilé et mistral:7b")


def _chat_events(node, model, text) -> list:
    """Tous les événements d'une conversation (comme _chat, qui ne rend que « done »)."""
    response = requests.post(
        f"http://127.0.0.1:{node.port}/api/chat",
        json={"text": text, "model": model, "think": False, "libraries": False,
              "conversations": False},
        headers={"X-Aiodos-Token": node.token}, stream=True, timeout=600)
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.iter_lines(decode_unicode=True)
            if line and line.startswith("data: ")]


@pytest.fixture
def machines(tmp_path, monkeypatch):
    b = Node(tmp_path / "b", "PC fixe", ["--internet-local"]).start()
    monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "99")        # A : aucune carte graphique
    a = Node(tmp_path / "a", "PC portable", ["--internet-local", "--slots", "embed=0"]).start()
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES")
    yield a, b
    a.stop()
    b.stop()


def test_a_machine_without_a_card_borrows_one(machines):
    a, b = machines
    b.api("PUT", "/settings", {"lend_gpu": True, "llamacpp_dir": LLAMA})
    a.api("PUT", "/settings", {"llamacpp_dir": LLAMA})
    _, b_id = pair_by_invitation(a, b, trust="mine")
    model_id = f"reparti:{MODEL}"

    def offered():
        a.api("POST", f"/network/peers/{b_id}/refresh")
        return {m["id"]: m for m in a.api("GET", "/models")}.get(model_id)
    offer = a.wait_for(offered, timeout=60, what="modèle réparti proposé")
    assert offer["group"] == "cluster" and offer["machines"] == ["PC fixe"]

    events = _chat_events(a, model_id, "Réponds seulement par le mot : bonjour")
    done = events[-1]
    assert done["type"] == "done" and done["error"] is None and done["html"].strip(), done
    # La mise en place s'est montrée : carte demandée à B, puis chargement
    steps = [e["text"] for e in events if e["type"] == "status"]
    assert any("PC fixe" in step for step in steps) and any("Chargement" in s for s in steps)
    # Le serveur RPC de B n'écoute que sur 127.0.0.1, ouvert à la seule clé de A
    exposed = b.wait_for(lambda: ((b.api("GET", "/network/internet").get("companion") or {})
                                  .get("tunnels") or {}).get("exposed"),
                         timeout=30, what="service ouvert par le compagnon de B")
    assert len(exposed) == 1 and exposed[0]["peers"] == 1
    # Une deuxième question réutilise le même groupe
    again = _chat(a, model_id, "Et en anglais ?")
    assert again["error"] is None

    # B ne prête plus : sa carte est reprise, et A ne propose plus le modèle (le profil
    # que B envoie est gardé 5 s : A le voit au plus tard au rafraîchissement suivant)
    b.api("PUT", "/settings", {"lend_gpu": False})
    a.wait_for(lambda: not offered(), timeout=30, what="modèle réparti retiré")
    b.wait_for(lambda: not ((b.api("GET", "/network/internet").get("companion") or {})
                            .get("tunnels") or {}).get("exposed"),
               timeout=60, what="carte de B reprise")


def test_the_model_is_ready_before_the_question(machines):
    """Choisi dans la conversation, le modèle réparti se met en place d'avance : la
    question qui suit répond sans nouvelle mise en place."""
    a, b = machines
    b.api("PUT", "/settings", {"lend_gpu": True, "llamacpp_dir": LLAMA})
    a.api("PUT", "/settings", {"llamacpp_dir": LLAMA})
    _, b_id = pair_by_invitation(a, b, trust="mine")
    model_id = f"reparti:{MODEL}"

    def offered():
        a.api("POST", f"/network/peers/{b_id}/refresh")
        return {m["id"]: m for m in a.api("GET", "/models")}.get(model_id)
    a.wait_for(offered, timeout=60, what="modèle réparti proposé")
    assert a.api("POST", "/cluster/prepare", {"model": model_id})["state"] == "preparing"
    state = a.wait_for(lambda: (s := a.api("GET", f"/cluster/prepare?model={model_id}"))[
        "state"] != "preparing" and s, timeout=600, what="modèle préparé d'avance")
    assert state["state"] == "ready", state
    events = _chat_events(a, model_id, "Réponds seulement par le mot : bonjour")
    assert events[-1]["type"] == "done" and events[-1]["error"] is None, events[-1]
    assert not [e for e in events if e["type"] == "status"]       # déjà en place
