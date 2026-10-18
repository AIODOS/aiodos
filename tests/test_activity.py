"""Activité en direct avec mes machines appairées (network/activity.py), pour la liste
« Machines connectées » : ce que chacune annonce de son partage, par quel chemin on la
joint, ce qu'elle fait pour moi et moi pour elle en ce moment."""
from types import SimpleNamespace

import pytest

from aiodos import settings
from aiodos.jobs import queue
from aiodos.network import activity, client, lending, p2p_app, peers, remote_models, sharing
from aiodos.services import ollama
from tests.test_work_leases import B, C, _lease, owner  # noqa: F401  (owner : fixture)

MESSAGES = [{"role": "user", "content": "Bonjour"}]


def _ledger(node_id):
    peer = next(p for p in peers.list_peers() if p["node_id"] == node_id)
    return peer["given_chats"], peer["received_chats"]


@pytest.fixture(autouse=True)
def fresh():
    activity._heard.clear()
    yield
    activity._heard.clear()


def _live(running=None):
    paired = peers.list_peers()
    for peer in paired:
        peer["online"] = True
    activity.annotate(paired, running)
    return {p["node_id"]: p["live"] for p in paired}


def test_my_machines_hear_the_sharing_state_a_friend_only_whether_it_is_on(owner):
    settings.set_value("share_enabled", True)
    manager = SimpleNamespace(worker=SimpleNamespace(
        state={"active": False, "state": "paused", "detail": "En pause : sur batterie."}))
    assert activity.field(manager, C) == {"sharing": {
        "enabled": True, "active": False, "detail": "En pause : sur batterie."}}
    peers.set_trust(B, sharing.FRIEND)
    assert activity.field(manager, B) == {"sharing": {"enabled": True}}   # pas son activité
    assert activity.field(SimpleNamespace(), C)["sharing"]["active"] is False   # réseau arrêté


def test_a_ping_answers_with_the_sharing_state(owner):
    reply = p2p_app._ping(owner["manager"], C, {})
    assert reply["sharing"] == {"enabled": False, "active": False, "detail": ""}


def test_what_a_machine_says_is_kept_with_its_response_time(owner):
    activity.note(B, {"sharing": {"enabled": True, "active": True, "detail": "Prêt."}}, 3.14159)
    activity.note(B, {"name": "PC fixe"})       # une version précédente n'en dit rien : gardé
    live = _live()
    assert live[B]["sharing"] == {"enabled": True, "active": True, "detail": "Prêt."}
    assert live[B]["rtt_ms"] == 3.1
    assert live[B]["route"] == "lan"            # adresse sur le réseau local, chemin automatique
    assert live[C]["sharing"] is None           # rien entendu
    activity.forget(B)
    assert _live()[B]["sharing"] is None


def test_an_offline_machine_has_no_route_nor_response_time(owner):
    activity.note(B, {}, 2.0)
    paired = peers.list_peers()
    for peer in paired:
        peer["online"] = False
    activity.annotate(paired, None)
    live = next(p["live"] for p in paired if p["node_id"] == B)
    assert live["route"] is None and live["rtt_ms"] is None


def test_internet_route_direct_or_by_a_relay(owner):
    peer = {"node_id": "x", "online": True, "net_id": "cle",
            "internet": {"connected": True, "path": "relay", "rtt_ms": 82.0}}
    activity.annotate([peer], None)
    assert peer["live"]["route"] == "relay" and peer["live"]["internet_rtt_ms"] == 82.0
    peer["internet"]["path"] = "direct"
    activity.annotate([peer], None)
    assert peer["live"]["route"] == "direct"


def test_units_computed_for_me_and_by_me_right_now(owner):
    assert _lease(owner)["unit"]                    # B calcule une unité de l'indexation
    title = queue.job_summary(owner["job"])["title"]
    live = _live(running=[{"peer": C, "label": "image 2/8"}])
    assert live[B]["for_me"] == [{"title": title, "units": 1}]
    assert live[C]["for_it"] == ["image 2/8"]
    assert live[C]["for_me"] == [] and live[B]["for_it"] == []


def test_conversations_served_both_ways_count_while_they_last(owner):
    with activity.chat("by", B):
        with activity.chat("by", B), activity.chat("for", C):
            live = _live()
            assert live[B]["chats_for_me"] == 2 and live[C]["chats_for_it"] == 1
    live = _live()
    assert all(item["chats_for_me"] == 0 and item["chats_for_it"] == 0 for item in live.values())


def test_a_lent_model_counts_its_conversation_until_the_answer_ends(owner, monkeypatch):
    settings.set_value("lend_models", True)
    monkeypatch.setattr(ollama, "chat_stream",
                        lambda *a, **k: iter([("content", "Bon"), ("content", "jour")]))
    manager = SimpleNamespace(name=lambda: "PC", executor=SimpleNamespace(
        interactive_begin=lambda: None, interactive_end=lambda: None))
    answer = lending.chat(manager, C, {"model": "nomic-embed-text:latest",
                                       "messages": MESSAGES}, None)
    assert next(answer) == {"content": "Bon"}
    assert _live()[C]["chats_for_it"] == 1
    assert _ledger(C) == (0, 0)                      # comptée une fois la réponse finie
    assert list(answer) == [{"content": "jour"}]
    assert _live()[C]["chats_for_it"] == 0
    assert _ledger(C) == (1, 0)                      # servie par cette machine, pour C


def test_my_conversation_answered_by_a_machine_counts_until_its_end(owner, monkeypatch):
    monkeypatch.setattr(client, "stream", lambda *a, **k: iter([{"content": "Salut"}]))
    answer = remote_models.chat_stream(remote_models.model_id(B, "mistral:7b"), MESSAGES)
    assert next(answer) == ("content", "Salut")
    assert _live()[B]["chats_for_me"] == 1
    assert list(answer) == []
    assert _live()[B]["chats_for_me"] == 0
    assert _ledger(B) == (0, 1)                      # servie par B, pour cette machine


def test_an_interrupted_answer_is_not_counted(owner, monkeypatch):
    monkeypatch.setattr(client, "stream", lambda *a, **k: iter([{"content": "Sa"}, {"content": "lut"}]))
    answer = remote_models.chat_stream(remote_models.model_id(B, "mistral:7b"), MESSAGES)
    assert next(answer) == ("content", "Sa")
    answer.close()                                   # arrêtée par l'utilisateur
    assert _live()[B]["chats_for_me"] == 0
    assert _ledger(B) == (0, 0)


def test_a_chat_stopped_on_the_way_is_not_counted():
    import threading
    from aiodos.network import activity, peers
    peers.save("arretee", "Arrêtée", b"s" * 32, "127.0.0.1", 9, trust="friend")
    stop = threading.Event()
    with activity.chat("for", "arretee", stop):
        stop.set()                                                   # bouton Stop, machine partie
    with activity.chat("by", "arretee"):
        pass
    row = next(p for p in peers.list_peers() if p["node_id"] == "arretee")
    assert (row["given_chats"], row["received_chats"]) == (0, 1)
