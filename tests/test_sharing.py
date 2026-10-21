"""Partage avec les amis (étape 2.5) : à un ami, seules les bibliothèques partageables,
sous des noms neutres, avec un profil allégé de cette machine ; ce qu'il rend est
contrôlé par échantillon, et une machine prise en défaut est mise de côté. Côté
propriétaire du travail, sans réseau ni Ollama."""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from aiodos import db
from aiodos.api.server import create_app
from aiodos.jobs import registry
from aiodos.knowledge import store
from aiodos.network import p2p_app, pairing, peers, sharing, wire, work
from aiodos.network.client import PeerError
from aiodos.services import ollama
from tests.test_work_leases import B, C, _lease, _unit, owner  # noqa: F401  (owner : fixture)


@pytest.fixture(autouse=True)
def friends_forgotten():
    """Ce que le propriétaire retient des amis en mémoire (demandes comptées, unités rendues
    en échec, network/work.py) vit le temps du processus : un essai d'un autre fichier, sur
    la même machine B et les mêmes numéros d'unités, ne change pas ce qui est confié ici."""
    work.forget_friends()
    yield
    work.forget_friends()


@pytest.fixture
def friend(owner):
    """B est la machine d'un ami ; C reste une de mes machines."""
    peers.set_trust(B, sharing.FRIEND)
    return owner


def _result(owner, order, vectors, node=B):
    """B rend son calcul : le vrai texte du premier fichier confié (a.txt). Le contrôle
    refait le découpage ici : un autre texte serait refusé avant même les vecteurs."""
    pieces = ["Les chats dorment."]
    return work.result(owner["manager"], node, {
        "unit": order["id"], "compute_s": 1.0,
        "output": wire.pack({"pieces": pieces, "vectors": np.asarray(vectors, dtype=np.float32)})})


def test_peers_are_my_machines_unless_said_otherwise(owner):
    assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION >= 4
    assert sharing.level(B) == sharing.MINE
    assert sharing.level("pas-appairee") == sharing.FRIEND           # le plus étroit
    assert store.list_collections()[0]["scope"] == "mine"


def test_a_friend_gets_nothing_from_libraries_not_shared_with_friends(friend):
    assert _lease(friend)["unit"] is None
    assert _lease(friend, node=C)["unit"] is not None                 # une de mes machines


def test_a_friend_gets_shared_libraries_under_neutral_names_and_it_is_logged(friend):
    store.set_scope(friend["collection"], "friends")
    assert store.list_collections()[0]["scope"] == "friends"
    order = _lease(friend)["unit"]
    assert order["blob"]["name"] == f"fichier-{order['id']}.txt"
    assert order["payload"] == {"source": order["blob"]["name"]}      # ni chemin, ni bibliothèque
    entry = sharing.sent_log(B)[0]
    assert entry["what"].endswith("a.txt") and entry["sent_as"] == order["blob"]["name"]
    assert entry["size"] == order["blob"]["size"]
    assert sharing.sent_summary()[B]["count"] == 1 and C not in sharing.sent_summary()


def test_a_library_kept_on_this_pc_never_leaves(friend):
    store.set_scope(friend["collection"], "local")
    assert _lease(friend)["unit"] is None and _lease(friend, node=C)["unit"] is None
    with pytest.raises(ValueError):
        store.set_scope(friend["collection"], "partout")


def test_a_friend_result_is_checked_by_sample(friend, monkeypatch):
    store.set_scope(friend["collection"], "friends")
    order = _lease(friend)["unit"]
    vector = np.ones((1, 8), dtype=np.float32)
    asked = []
    monkeypatch.setattr(ollama, "embed",
                        lambda texts, model=None: asked.append(texts) or [vector[0].tolist()])
    assert _result(friend, order, vector) == {"accepted": True}
    assert asked == [["Les chats dorment."]]                          # le passage recalculé ici
    text = db.query_one("SELECT text FROM chunks ORDER BY id DESC LIMIT 1")["text"]
    assert text == "Les chats dorment."
    assert peers.list_peers()[0]["received_units"] == 1
    assert peers.list_peers()[0]["checked_units"] == 1                # fiabilité : contrôlé, conforme


def test_the_real_name_is_restored_in_the_text(friend):
    """Le nom neutre qu'un ami a pu écrire dans le texte (« [Image : fichier-12.png] »)
    redevient le vrai nom du fichier. Testé sur la sortie même : un résultat d'ami pour une
    image n'est plus jamais pris (ni même confié), il ne passe donc plus par work.result."""
    store.set_scope(friend["collection"], "friends")
    order = _lease(friend)["unit"]
    unit = _unit(order["id"])
    output = {"pieces": [f"[Image : {order['blob']['name']}] un chat"]}
    restored = sharing.restore_names(output, {"id": unit["id"],
                                              "payload": json.loads(unit["payload"])},
                                     registry.TASKS["index_file"])
    assert restored["pieces"] == ["[Image : a.txt] un chat"]


def test_a_friend_whose_result_does_not_match_is_set_aside(friend, monkeypatch):
    store.set_scope(friend["collection"], "friends")
    order = _lease(friend)["unit"]
    monkeypatch.setattr(ollama, "embed", lambda texts, model=None: [[1.0] + [0.0] * 7])
    reply = _result(friend, order, np.ones((1, 8)))
    assert reply["accepted"] is False and "échantillon" in reply["reason"]
    assert "ne correspond pas" in sharing.set_aside_reason(B)
    assert peers.list_peers()[0]["refused_units"] == 1                # fiabilité : un résultat refusé
    assert _unit(order["id"])["status"] == "queued"                   # sera refaite ailleurs
    refused = _lease(friend)
    assert refused["unit"] is None and "ne te confie plus rien" in refused["reason"]
    peers.set_aside(B, None)                                          # rétablie par l'utilisateur
    assert _lease(friend)["unit"] is not None


def test_a_result_that_cannot_be_checked_here_goes_back_to_the_queue(friend, monkeypatch):
    """Ollama arrêté ici : le texte rendu est bien le découpage refait ici, mais ses
    vecteurs ne peuvent pas être recalculés. Ce n'est pas la faute de l'ami."""
    store.set_scope(friend["collection"], "friends")
    order = _lease(friend)["unit"]

    def down(texts, model=None):
        raise ConnectionError("Ollama arrêté")
    monkeypatch.setattr(ollama, "embed", down)
    reply = _result(friend, order, np.ones((1, 8)))
    assert reply["accepted"] is False and "Vérification impossible" in reply["reason"]
    assert sharing.set_aside_reason(B) is None                        # ce n'est pas sa faute
    unit = _unit(order["id"])
    assert unit["status"] == "queued" and unit["attempts"] == 0


def test_my_own_machines_are_not_sampled(owner, monkeypatch):
    order = _lease(owner)["unit"]
    monkeypatch.setattr(ollama, "embed", lambda *a, **k: pytest.fail("contrôle inattendu"))
    assert _result(owner, order, np.ones((1, 8))) == {"accepted": True}


def test_a_friend_sees_a_reduced_profile(owner, monkeypatch):
    full = {"node_id": "moi", "name": "PC", "version": "2.0.2", "cpu": {"threads": 32},
            "memory": {"total": 1}, "gpus": [{"name": "RTX"}], "power": {"plugged": True},
            "idle_seconds": 12, "os": "Windows", "lends_models": False,
            "updates": {"offer": "2.0.1", "files": {}},
            "ollama": {"version": "0.40", "models": [{"name": "mistral:7b"}]}}
    monkeypatch.setattr(p2p_app.identity, "profile", lambda: dict(full))
    # une de mes machines : tout, ce qui lui est ouvert à la consultation (rien ici), et
    # l'état du partage de puissance, avec sa raison (activity.py)
    assert p2p_app._profile(None, C, {}) == dict(
        full, consult={"open": 0}, sharing={"enabled": False, "active": False, "detail": ""})
    peers.set_trust(B, sharing.FRIEND)
    seen = p2p_app._profile(None, B, {})
    assert set(seen) == {"node_id", "name", "version", "lends_models", "updates", "ollama",
                         "consult", "sharing"}
    assert seen["ollama"] == {"models": []}                           # rien ne lui est prêté
    assert seen["sharing"] == {"enabled": False}         # pas l'activité de cette machine
    full["lends_models"] = True
    assert p2p_app._profile(None, B, {})["ollama"]["models"] == [{"name": "mistral:7b"}]


class Manager:
    node_id = "moi"

    def name(self):
        return "PC"

    def changed(self):
        pass

    def publish(self, event):
        pass

    def on_paired(self, node_id):
        pass

    def __init__(self):
        self.presented = []              # appairage par groupe (network/group.py)

    def present_later(self, friend_id, only=None):
        self.presented.append(("ami", friend_id))

    def present_new_machine(self, node_id):
        self.presented.append(("machine", node_id))


def test_trust_is_chosen_when_confirming_the_pairing(data_dir, monkeypatch):
    db.connect()
    monkeypatch.setattr(pairing.client, "call", lambda *a, **k: {"accept": True})
    pairs = pairing.Pairing(Manager())
    for node_id, trust in (("lea", sharing.FRIEND), ("portable", sharing.MINE)):
        pairs._pending[node_id] = pairing.Pending(node_id, node_id, "127.0.0.1", 1, "responder",
                                                  b"k" * 32, "123456")
        pairs.answer(node_id, True, trust)
        assert peers.trust_of(node_id) == trust
    pairs._pending["x"] = pairing.Pending("x", "x", "127.0.0.1", 1, "responder", b"k" * 32, "1")
    with pytest.raises(PeerError):
        pairs.answer("x", True, "inconnu")
    peers.set_aside("lea", "essai")
    pairs._pending["lea"] = pairing.Pending("lea", "lea", "127.0.0.1", 1, "responder",
                                            b"k" * 32, "123456")
    with pytest.raises(PeerError, match="déjà appairée"):               # sa clé : seulement
        pairs.answer("lea", True, sharing.FRIEND)                      # si je le choisis
    pairs.answer("lea", True, sharing.FRIEND, replace=True)            # nouvel appairage :
    assert sharing.set_aside_reason("lea") is None                    # nouvelle chance
    # Appairage par groupe : l'amie rencontre mes autres machines, le portable mes amis
    assert pairs.m.presented == [("ami", "lea"), ("machine", "portable"), ("ami", "lea")]


def test_a_machine_paired_meanwhile_keeps_its_key_unless_i_choose(data_dir, monkeypatch):
    """La demande montrée ne la disait pas connue ; entre-temps, elle a été appairée (une
    présentation de groupe). Accepter sans « remplacer » est refusé, et sa clé reste ; de
    même si elle l'est devenue entre mon acceptation et celle de l'autre machine (relecture
    finale du 21/10/2026)."""
    db.connect()
    monkeypatch.setattr(pairing.client, "call", lambda *a, **k: {"accept": True})
    pairs = pairing.Pairing(Manager())
    pairs._pending["lea"] = pairing.Pending("lea", "lea", "127.0.0.1", 1, "responder",
                                            b"k" * 32, "123456")
    peers.save("lea", "PC de Léa", b"g" * 32, "127.0.0.1", 2, sharing.FRIEND)  # entre-temps
    with pytest.raises(PeerError, match="déjà appairée"):
        pairs.answer("lea", True, sharing.FRIEND)
    assert peers.get("lea")["key"] == b"g" * 32
    pairs.answer("lea", True, sharing.FRIEND, replace=True)            # choisi : remplacée
    assert peers.get("lea")["key"] == b"k" * 32
    monkeypatch.setattr(pairing.client, "call", lambda *a, **k: {})   # l'autre, pas encore
    pairs._pending["max"] = pairing.Pending("max", "max", "127.0.0.1", 3, "responder",
                                            b"m" * 32, "654321")
    pairs.answer("max", True, sharing.MINE)
    peers.save("max", "PC de Max", b"g" * 32, "127.0.0.1", 4, sharing.MINE)    # entre-temps
    pairs.remote_answer("max", True)
    assert peers.get("max")["key"] == b"g" * 32 and pairs.get("max") is None


def test_api_changes_trust_restores_and_shows_what_was_sent(owner):
    # embed=0 : l'exécuteur de ce nœud ne calcule pas lui-même les unités à confier à B
    # (avec un vrai Ollama sur la machine, il les prenait avant le bail : test instable)
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as client:
        headers = {"X-Aiodos-Token": "jeton"}
        snap = client.post(f"/api/network/peers/{B}/trust", json={"trust": "friend"},
                           headers=headers).json()
        assert next(p for p in snap["peers"] if p["node_id"] == B)["trust"] == "friend"
        assert client.post(f"/api/network/peers/{B}/trust", json={"trust": "x"},
                           headers=headers).status_code == 400
        assert client.post("/api/network/peers/inconnue/trust", json={"trust": "mine"},
                           headers=headers).status_code == 404
        peers.set_aside(B, "essai")
        snap = client.post(f"/api/network/peers/{B}/restore", headers=headers).json()
        assert not next(p for p in snap["peers"] if p["node_id"] == B)["set_aside_at"]
        collection = owner["collection"]
        assert client.patch(f"/api/collections/{collection}", json={"scope": "friends"},
                            headers=headers).status_code == 200
        sent = _lease(owner)["unit"]
        assert sent["blob"]["name"].startswith("fichier-")
        entries = client.get(f"/api/network/peers/{B}/shared", headers=headers).json()["entries"]
        assert entries[0]["sent_as"] == sent["blob"]["name"]
        snap = client.get("/api/network", headers=headers).json()
        assert next(p for p in snap["peers"] if p["node_id"] == B)["shared"]["count"] == 1
