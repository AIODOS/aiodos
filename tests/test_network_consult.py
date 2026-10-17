"""Deux vrais nœuds : B cherche dans la bibliothèque que A lui ouvre, sans copie.

A indexe des notes, dont un dossier secret et un document fermés, puis ouvre sa
bibliothèque à ses amis. B, la machine d'un ami pour A, y cherche depuis la vue
Bibliothèques et depuis une conversation : seuls les passages ouverts reviennent, avec le
nom de A, et A note chaque recherche. Gardée sur ce PC, la bibliothèque ne répond plus.
Il faut un Ollama local avec le modèle d'embedding (sinon le test est ignoré). Pour deux
machines appairées sur invitation (étape 2.4), il faut en plus le compagnon compilé.
"""
import json

import pytest
import requests

from aiodos.network import companion
from tests.nodes import Node, pair, pair_by_invitation
from tests.test_network_work import EMBED_MODEL, _job, _ollama_has_embed_model

pytestmark = pytest.mark.skipif(not _ollama_has_embed_model(),
                                reason=f"Ollama avec {EMBED_MODEL} requis")

NOTES = {
    "fusee.txt": "La fusée lunaire décolle à l'aube depuis la base de Kourou, en Guyane.",
    "cafe.md": "# Café\nUn bon espresso se prépare en vingt-cinq secondes.",
    "secret/journal.txt": "Mon journal secret : le code du coffre-fort est 4512.",
    "budget.txt": "Le budget de la famille pour l'année 2027 est de trois mille euros.",
}
ROCKET = "Quand et d'où la fusée lunaire décolle-t-elle ?"


def _library_of_a(a, tmp_path) -> int:
    """« Cours » chez A : les notes indexées, le dossier secret et le budget fermés."""
    folder = tmp_path / "notes"
    for name, text in NOTES.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")
    courses = a.api("POST", "/collections", {"name": "Cours"})["id"]
    job_id = a.api("POST", "/index", {"paths": [str(folder)], "collection_id": courses})["job_id"]
    job = a.wait_for(lambda: (j := _job(a, job_id)) and j["status"] not in
                     ("queued", "running") and j, timeout=120, what="indexation chez A")
    assert job["status"] == "done" and job["done"] == len(NOTES), job
    a.api("POST", f"/collections/{courses}/closed", {"path": "notes/secret", "folder": True})
    a.api("POST", f"/collections/{courses}/closed", {"path": "notes/budget.txt"})
    return courses


def _ask(node, question):
    return node.api("POST", "/consult", {"question": question})


def _chat_sources(node, text):
    """Sources d'une réponse qui cherche aussi chez les autres (lues avant la génération :
    le modèle n'a pas besoin d'exister)."""
    response = requests.post(
        f"http://127.0.0.1:{node.port}/api/chat",
        json={"text": text, "model": "modele-absent", "libraries": False,
              "conversations": False, "peer_libraries": True},
        headers={"X-Aiodos-Token": node.token}, stream=True, timeout=120)
    assert response.status_code == 200, response.text
    with response:
        for line in response.iter_lines(decode_unicode=True):
            if line and line.startswith("data: "):
                event = json.loads(line[6:])
                if event["type"] == "sources":
                    return event["sources"]
    raise AssertionError("Flux terminé sans « sources »")


def _searches_in_open_library(a, b, a_id, b_id, courses):
    # Rien d'ouvert : la question ne part pas
    b.api("POST", f"/network/peers/{a_id}/refresh")
    assert b.api("GET", "/consult")["machines"] == []
    assert _ask(b, ROCKET) == {"passages": [], "machines": []}
    assert a.api("GET", f"/network/peers/{b_id}/consulted")["entries"] == []

    a.api("PATCH", f"/collections/{courses}", {"open_to": "friends"})
    b.api("POST", f"/network/peers/{a_id}/refresh")              # son profil le dit
    assert [m["name"] for m in b.api("GET", "/consult")["machines"]] == ["PC de Marc"]

    found = _ask(b, ROCKET)
    assert found["machines"][0]["error"] is None, found
    best = found["passages"][0]
    assert best["peer"] == "PC de Marc" and best["library"] == "Cours"
    assert best["document"] == "fusee.txt" and "Kourou" in best["text"]
    # Le dossier secret et le document fermés ne sortent jamais, même demandés exactement
    for question in ("Quel est le code du coffre-fort ? Mon journal secret",
                     "Le budget de la famille pour l'année 2027"):
        texts = " ".join(p["text"] for p in _ask(b, question)["passages"])
        assert "4512" not in texts and "trois mille" not in texts

    # Dans une conversation : les citations disent chez qui
    sources = _chat_sources(b, ROCKET)
    assert {"type": "remote", "label": "fusee.txt", "collection": "Cours",
            "peer": "PC de Marc"} in [{k: s[k] for k in ("type", "label", "collection", "peer")}
                                      for s in sources]

    # A a noté chaque recherche : qui, quand, combien de passages, de quels documents
    log = a.api("GET", f"/network/peers/{b_id}/consulted")["entries"]
    assert len(log) == 4 and all(e["refused"] is None for e in log)
    documents = {d["document"] for e in log for d in e["documents"]}
    assert "notes/fusee.txt" in documents
    assert not documents & {"notes/secret/journal.txt", "notes/budget.txt"}
    seen_by_a = next(p for p in a.api("GET", "/network")["peers"] if p["node_id"] == b_id)
    assert seen_by_a["consulted"]["count"] == 4

    # Gardée sur ce PC : plus rien, et B cesse aussitôt d'envoyer ses questions
    a.api("PATCH", f"/collections/{courses}", {"scope": "local"})
    closed = _ask(b, ROCKET)
    assert closed["passages"] == [] and closed["machines"][0]["open"] == 0
    assert b.api("GET", "/consult")["machines"] == []
    assert b.api("GET", "/entries") == []                         # rien n'a été copié chez B


@pytest.fixture
def two_nodes(tmp_path):
    a = Node(tmp_path / "a", "PC de Marc").start()
    b = Node(tmp_path / "b", "PC de Léa").start()
    yield a, b
    a.stop()
    b.stop()


def test_a_friend_searches_my_open_library_without_copying_it(two_nodes, tmp_path):
    a, b = two_nodes
    a_id, b_id = pair(a, b, a_trusts="friend", b_trusts="friend")
    courses = _library_of_a(a, tmp_path)
    _searches_in_open_library(a, b, a_id, b_id, courses)


@pytest.fixture
def two_friends(tmp_path):
    """Deux nœuds qui ne se connaissent pas sur le réseau local : leurs compagnons, en
    essai local (--internet-local), sont leur seul chemin."""
    a = Node(tmp_path / "a", "PC de Marc", ["--internet-local"]).start()
    b = Node(tmp_path / "b", "PC de Léa", ["--internet-local"]).start()
    yield a, b
    a.stop()
    b.stop()


@pytest.mark.skipif(companion.find_binary() is None,
                    reason="compagnon aiodos-net non compilé (voir compagnon/README.md)")
def test_a_friend_far_away_searches_my_open_library_by_internet(two_friends, tmp_path):
    """Appairés sur invitation : B ne connaît de A que sa clé Internet."""
    a, b = two_friends
    a_id, b_id = pair_by_invitation(a, b)
    peer = next(p for p in b.api("GET", "/network")["peers"] if p["node_id"] == a_id)
    assert peer["net_id"] and not peer["address"]
    courses = _library_of_a(a, tmp_path)
    _searches_in_open_library(a, b, a_id, b_id, courses)
