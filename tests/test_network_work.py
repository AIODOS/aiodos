"""Deux vrais nœuds se partagent une indexation.

A ne calcule pas d'embeddings lui-même (--slots embed=0) ; B prête sa puissance.
L'indexation lancée sur A doit se faire entièrement sur B, et les résultats
n'être enregistrés que chez A. Il faut un Ollama local avec le modèle
d'embedding (sinon le test est ignoré).
"""
import time

import pytest
import requests

from aiodos import settings
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


def test_a_friend_computes_only_shared_libraries_and_never_sees_the_names(owner_and_lender,
                                                                         tmp_path):
    """Étape 2.5 : pour A, B est la machine d'un ami. Rien ne lui part tant que la
    bibliothèque n'est pas partagée avec les amis ; ensuite, les fichiers lui arrivent sous
    des noms neutres, et ce qu'il rend est contrôlé par échantillon (vrai Ollama ici)."""
    a, b = owner_and_lender
    pair(a, b, a_trusts="friend")
    b.api("PUT", "/settings", {"share_enabled": True, "share_when_idle": False,
                               "share_on_ac_only": False})
    folder = tmp_path / "notes"
    folder.mkdir()
    for name, text in NOTES.items():
        (folder / name).write_text(text, encoding="utf-8")
    collection = a.api("GET", "/collections")["collections"][0]["id"]
    job_id = a.api("POST", "/index", {"paths": [str(folder)],
                                      "collection_id": collection})["job_id"]
    b_id = b.node_id
    time.sleep(8)                         # B demande du travail : on ne lui confie rien
    assert _job(a, job_id)["done"] == 0
    assert a.api("GET", f"/network/peers/{b_id}/shared")["entries"] == []

    a.api("PATCH", f"/collections/{collection}", {"scope": "friends"})
    job = a.wait_for(lambda: (j := _job(a, job_id)) and j["status"] not in
                     ("queued", "running") and j, timeout=120, what="indexation terminée")
    assert job["status"] == "done" and job["done"] == 3, job
    sent = a.api("GET", f"/network/peers/{b_id}/shared")["entries"]
    assert len(sent) == 3 and all(e["sent_as"].startswith("fichier-") for e in sent)
    entries = a.api("GET", f"/entries?collection_id={collection}")
    assert sorted(e["source"] for e in entries) == sorted(f"notes/{n}" for n in NOTES)
    log = (tmp_path / "b" / "logs" / "aiodos.log").read_text(encoding="utf-8")
    assert "fichier-" in log and not any(name in log for name in NOTES)
    # Chacun montre à l'autre le profil que mérite ce que l'autre est pour lui
    of_a = b.wait_for(lambda: b.api("GET", "/network")["peers"][0].get("profile"), timeout=40,
                      what="profil de A vu par B")
    assert "memory" not in of_a and "idle_seconds" not in of_a   # pour A, B est un ami
    of_b = a.wait_for(lambda: a.api("GET", "/network")["peers"][0].get("profile"), timeout=40,
                      what="profil de B vu par A")
    assert "memory" in of_b                                      # pour B, A est à lui
