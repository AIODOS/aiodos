"""Deux vrais nœuds se partagent une indexation.

A ne calcule pas d'embeddings lui-même (--slots embed=0) ; B prête sa puissance.
L'indexation lancée sur A doit se faire entièrement sur B, et les résultats
n'être enregistrés que chez A. Il faut un Ollama local avec le modèle
d'embedding (sinon le test est ignoré).
"""
import pytest
import requests

from coeur import settings
from tests.nodes import Node, pair

EMBED_MODEL = settings.DEFAULTS["embed_model"]       # celui des nœuds lancés ici


def _ollama_has_embed_model() -> bool:
    try:
        tags = requests.get("http://127.0.0.1:11434/api/tags", timeout=2).json()
    except (requests.RequestException, ValueError):
        return False
    return any(m["name"].startswith(EMBED_MODEL) for m in tags.get("models", []))


pytestmark = pytest.mark.skipif(not _ollama_has_embed_model(),
                                reason=f"Ollama avec {EMBED_MODEL} requis")

NOTES = {
    "chats.txt": "Les chats dorment seize heures par jour, surtout l'après-midi.",
    "cafe.md": "# Café\nUn bon espresso se prépare en vingt-cinq secondes.",
    "pluie.txt": "En Bretagne, la pluie fine s'appelle le crachin.",
}


@pytest.fixture
def owner_and_lender(tmp_path):
    a = Node(tmp_path / "a", "PC portable", ["--slots", "embed=0"]).start()
    b = Node(tmp_path / "b", "PC fixe").start()
    yield a, b
    a.stop()
    b.stop()


def _job(node, job_id):
    return next((j for j in node.api("GET", "/jobs")["jobs"] if j["id"] == job_id), None)


def test_indexing_is_computed_by_the_paired_machine_and_kept_by_the_owner(owner_and_lender,
                                                                           tmp_path):
    a, b = owner_and_lender
    pair(a, b)
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
                     ("queued", "running") and j, timeout=120, what="indexation terminée")
    assert job["status"] == "done" and job["done"] == 3, job

    entries = a.api("GET", f"/entries?collection_id={collection}")
    assert sorted(e["source"] for e in entries) == sorted(f"notes/{n}" for n in NOTES)
    assert all(e["chunks"] >= 1 for e in entries)
    assert b.api("GET", "/entries") == []            # rien n'est resté chez B

    # Réciprocité : chacun tient le compte du travail échangé
    seen_by_a = a.api("GET", "/network")["peers"][0]
    assert seen_by_a["received_units"] == 3 and seen_by_a["received_s"] > 0
    seen_by_b = b.wait_for(lambda: [p for p in b.api("GET", "/network")["peers"]
                                    if p["given_units"] == 3], what="compte côté B")[0]
    assert seen_by_b["given_s"] > 0

    # B n'a gardé aucun fichier reçu
    assert not any((tmp_path / "b" / "tmp" / "work").rglob("*.*"))
