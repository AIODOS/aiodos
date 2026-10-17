"""Deux vrais nœuds appairés : la liste « Machines connectées » de l'un montre l'autre en
direct, avec son chemin, son temps de réponse et l'état de son partage (network/activity.py)."""
import pytest

from tests.nodes import Node, pair


@pytest.fixture
def two_nodes(tmp_path):
    a = Node(tmp_path / "a", "PC portable").preset(share_enabled=True).start()
    b = Node(tmp_path / "b", "PC fixe").start()
    yield a, b
    a.stop()
    b.stop()


def test_a_paired_machine_is_seen_live_with_its_sharing_state(two_nodes):
    a, b = two_nodes
    a_id, b_id = pair(a, b)

    def live_of_a():
        peer = next(p for p in b.api("GET", "/network")["peers"] if p["node_id"] == a_id)
        live = peer["live"]
        # le temps de réponse vient d'un ping, 15 s après le profil de l'appairage
        return live if live["sharing"] and live["rtt_ms"] is not None else None

    live = b.wait_for(live_of_a, timeout=45, what="nouvelles de A en direct")
    assert live["route"] == "lan"
    assert 0 <= live["rtt_ms"] < 5000
    sharing = live["sharing"]
    assert sharing["enabled"] is True and isinstance(sharing["active"], bool) and sharing["detail"]
    assert live["for_me"] == [] and live["for_it"] == []
    assert live["chats_for_me"] == 0 and live["chats_for_it"] == 0

    # B ne prête pas sa puissance : A le sait aussi
    peer_b = a.wait_for(lambda: next((p for p in a.api("GET", "/network")["peers"]
                                      if p["node_id"] == b_id and p["live"]["sharing"]), None),
                        timeout=45, what="nouvelles de B")
    assert peer_b["live"]["sharing"]["enabled"] is False
