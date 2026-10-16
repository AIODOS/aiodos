"""Deux vrais nœuds AIODOS se parlent par leurs compagnons aiodos-net (étape 2.2).

Chaque nœud lance son compagnon en essai local (--internet-local : ni relais, ni
annuaire, ni box ; connexion directe sur 127.0.0.1). Les nœuds s'appairent sur le
réseau local comme aujourd'hui, échangent leurs clés Internet (action scellée
net/info), puis le chemin est forcé sur « internet » : toute requête entre eux passe
alors par les compagnons, et échouerait sans eux. Il faut le compagnon compilé ; le
partage d'une indexation demande en plus un Ollama local avec le modèle d'embedding.
"""
import pytest
import requests

from aiodos import settings
from aiodos.network import companion
from tests.nodes import Node, pair

pytestmark = pytest.mark.skipif(companion.find_binary() is None,
                                reason="compagnon aiodos-net non compilé (voir compagnon/README.md)")

EMBED_MODEL = settings.DEFAULTS["embed_model"]
NOTES = {
    "chats.txt": "Les chats dorment seize heures par jour, surtout l'après-midi.",
    "cafe.md": "# Café\nUn bon espresso se prépare en vingt-cinq secondes.",
    "pluie.txt": "En Bretagne, la pluie fine s'appelle le crachin.",
}


def _ollama_has_embed_model() -> bool:
    try:
        tags = requests.get("http://127.0.0.1:11434/api/tags", timeout=2).json()
    except (requests.RequestException, ValueError):
        return False
    return any(m["name"].startswith(EMBED_MODEL) for m in tags.get("models", []))


@pytest.fixture
def owner_and_lender(tmp_path):
    a = Node(tmp_path / "a", "PC portable", ["--slots", "embed=0", "--internet-local"]).start()
    b = Node(tmp_path / "b", "PC fixe", ["--internet-local"]).start()
    yield a, b
    a.stop()
    b.stop()


def _internet(node) -> dict:
    return node.api("GET", "/network/internet")


def _ready(node) -> dict:
    """Le compagnon démarre en tâche de fond, à côté du réseau local : on l'attend."""
    return node.wait_for(lambda: (state := _internet(node))["running"] and state,
                         what="compagnon prêt")


def _seen_by_companion(node, other) -> dict:
    """Ce que le compagnon de `node` sait de la machine `other` (connexion, compteurs)."""
    key = _internet(other)["id"]
    return next((p for p in _internet(node)["companion"]["peers"] if p["id"] == key), {})


def _connect_by_internet(a, b):
    """Appairage sur le réseau local, clés Internet échangées, puis tout par Internet."""
    _ready(a), _ready(b)
    a_id, b_id = pair(a, b)
    for node, other in ((a, b), (b, a)):
        key = _internet(other)["id"]
        node.wait_for(lambda: node.api("GET", "/network")["peers"][0].get("net_id") == key,
                      what="clé Internet reçue")
        node.wait_for(lambda: _seen_by_companion(node, other).get("allowed"),
                      what="liste blanche du compagnon")
    for node in (a, b):
        node.api("PUT", "/settings", {"p2p_route": "internet"})
    return a_id, b_id


def _job(node, job_id):
    return next((j for j in node.api("GET", "/jobs")["jobs"] if j["id"] == job_id), None)


def test_paired_nodes_talk_through_their_companions(owner_and_lender):
    a, b = owner_and_lender
    for node in (a, b):
        state = _ready(node)
        assert state["enabled"] and len(state["id"]) == 64, state
    _, b_id = _connect_by_internet(a, b)
    before = _seen_by_companion(a, b).get("requests_out", 0)
    # Profil demandé à B : la requête scellée passe par les deux compagnons
    peer = a.api("POST", f"/network/peers/{b_id}/refresh")["peers"][0]
    assert peer["online"] and peer["profile"]["name"] == "PC fixe"
    seen = _seen_by_companion(a, b)
    assert seen["requests_out"] > before
    assert seen["connected"] and seen["path"] == "direct" and seen["connections"] == 1
    assert _seen_by_companion(b, a)["requests_in"] >= 1
    # L'état de chaque machine dit par où elle est jointe
    assert a.api("GET", "/network")["peers"][0]["internet"]["path"] == "direct"


@pytest.mark.skipif(not _ollama_has_embed_model(), reason=f"Ollama avec {EMBED_MODEL} requis")
def test_indexing_is_shared_through_the_companions(owner_and_lender, tmp_path):
    a, b = owner_and_lender
    _connect_by_internet(a, b)
    b.api("PUT", "/settings", {"share_enabled": True, "share_when_idle": False,
                               "share_on_ac_only": False})

    folder = tmp_path / "notes"
    folder.mkdir()
    for name, text in NOTES.items():
        (folder / name).write_text(text, encoding="utf-8")
    collection = a.api("GET", "/collections")["collections"][0]["id"]
    job_id = a.api("POST", "/index", {"paths": [str(folder)],
                                      "collection_id": collection})["job_id"]
    job = a.wait_for(lambda: (j := _job(a, job_id)) and j["status"] not in
                     ("queued", "running") and j, timeout=180, what="indexation terminée")
    assert job["status"] == "done" and job["done"] == 3, job

    entries = a.api("GET", f"/entries?collection_id={collection}")
    assert sorted(e["source"] for e in entries) == sorted(f"notes/{n}" for n in NOTES)
    assert b.api("GET", "/entries") == []            # rien n'est resté chez B
    seen_by_a = a.api("GET", "/network")["peers"][0]
    assert seen_by_a["received_units"] == 3

    # Bail, fichier et résultat de chaque unité sont passés par les compagnons :
    # B demande, A répond (au moins 3 × lease, blob, result)
    assert _seen_by_companion(b, a)["requests_out"] >= 9
    assert _seen_by_companion(a, b)["requests_in"] >= 9
