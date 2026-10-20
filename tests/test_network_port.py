"""Port entre nœuds déjà pris (instance qui finit de s'arrêter lors d'une mise à jour,
autre programme) : on réessaie, et sinon on le dit, sans arrêter le nœud. uvicorn
« quitte » (SystemExit) dans ce cas : cela ne doit pas remonter jusqu'à la boucle."""
import asyncio
import socket
import threading

import pytest

from aiodos import db
from aiodos.network import manager as manager_module
from aiodos.network.manager import NetworkManager


class Bus:
    def publish(self, event):
        pass


@pytest.fixture
def busy_port(data_dir, monkeypatch):
    monkeypatch.setattr(manager_module, "PORT_RETRY_DELAY", 0.3)
    db.connect()
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    yield holder
    holder.close()


def start(port, release_after=None, holder=None):
    net = NetworkManager("moi", Bus(), {"force": True, "host": "127.0.0.1", "port": port,
                                        "discovery": False})

    async def scenario():
        if release_after is not None:
            threading.Timer(release_after, holder.close).start()
        await net._start()
        outcome = (net.running, net.error)
        if net.running:
            await net._stop()
        return outcome
    return asyncio.run(scenario())


def test_port_freed_meanwhile_is_taken_on_a_later_attempt(busy_port):
    running, error = start(busy_port.getsockname()[1], release_after=0.1, holder=busy_port)
    assert running and error is None


def test_port_kept_busy_is_reported_without_stopping_the_node(busy_port):
    running, error = start(busy_port.getsockname()[1])
    assert not running and "indisponible" in error


def _start_on_settings_port(taken, monkeypatch, holder=None):
    """Nœud sur le port réglé `taken` (pas de --p2p-port), démarré deux fois de suite."""
    from aiodos import settings
    settings.set_value("p2p_port", taken)
    monkeypatch.setattr(manager_module, "_holder", lambda port: holder)
    net = NetworkManager("moi", Bus(), {"force": True, "host": "127.0.0.1", "discovery": False})

    async def scenario():
        seen = []
        for _ in range(2):                         # réseau coupé puis rallumé (apply)
            await net._start()
            seen.append((net.running, net.error, net.p2p_port, net._beacon()["port"]))
            if net.running:
                await net._stop()
        return seen
    return asyncio.run(scenario())


def test_port_of_the_settings_taken_for_good_gives_way_to_a_spare_one(busy_port, monkeypatch):
    """Un autre programme garde le port réglé : le réseau passe par le suivant (le même d'un
    démarrage à l'autre), que les annonces donnent aux autres machines, et le dit. Un port
    demandé explicitement (--p2p-port, essais) n'est jamais changé, lui (essais ci-dessus)."""
    taken = busy_port.getsockname()[1]
    if taken + 1 > 65535:
        pytest.skip("port en fin de plage")
    for running, error, port, announced in _start_on_settings_port(taken, monkeypatch):
        assert running and port == announced and port != taken
        assert port == taken + 1 or port > taken + manager_module.SPARE_PORTS
        assert f"port {taken} est pris" in error and f"port {port}" in error


def test_an_other_aiodos_node_holding_the_port_is_named(busy_port, monkeypatch):
    """L'ancien nœud de Termux garde le port sur le même téléphone : on dit lequel, et quoi
    faire, plutôt qu'un « autre programme »."""
    taken = busy_port.getsockname()[1]
    (running, error, port, _), _ = _start_on_settings_port(
        taken, monkeypatch, holder={"node_id": "autre", "name": "Samsung SM-S911B"})
    assert running and "Un autre nœud AIODOS (« Samsung SM-S911B »)" in error
    assert "redémarrer le téléphone" in error and f"port {port}" in error


def test_a_slow_server_is_waited_for_not_mistaken_for_a_taken_port(data_dir, monkeypatch):
    """Un serveur qui met du temps à démarrer n'est pas un port pris : pas de port de
    secours, pas de second serveur, et l'arrêt ferme bien le port réglé."""
    from aiodos import settings
    db.connect()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    settings.set_value("p2p_port", free)
    real_serve = manager_module._serve

    async def slow_serve(server):
        await asyncio.sleep(0.6)
        await real_serve(server)
    monkeypatch.setattr(manager_module, "_serve", slow_serve)
    monkeypatch.setattr(manager_module, "LISTEN_TIMEOUT", 0.2)    # attente écoulée avant
    net = NetworkManager("moi", Bus(), {"force": True, "host": "127.0.0.1", "discovery": False})

    async def scenario():
        await net._start()
        outcome = (net.running, net.spare_port, net.p2p_port)
        await asyncio.sleep(1.0)                    # le serveur finit par démarrer
        started = net._server.started
        await net._stop()
        await asyncio.sleep(0.2)
        return outcome, started
    (running, spare, port), started = asyncio.run(scenario())
    assert running and spare is None and port == free and started
    with socket.socket() as s:                      # rendu à l'arrêt
        s.bind(("127.0.0.1", free))


def test_announcements_go_out_even_when_their_port_is_taken():
    """Le port des annonces pris (ancien nœud de Termux) : on s'annonce quand même, depuis
    un port libre, sans écouter."""
    from aiodos.network.discovery import Discovery
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("0.0.0.0", 0))
    port = holder.getsockname()[1]
    sent = []

    async def scenario():
        discovery = Discovery(port, lambda: {"id": "moi"}, lambda m, a: None, lambda: [])
        await discovery.start()
        original = discovery._transport.sendto
        discovery._transport.sendto = lambda data, target: sent.append(target) or original(data, target)
        await asyncio.sleep(0.1)
        listening = discovery.listening
        await discovery.stop()
        return listening
    try:
        listening = asyncio.run(scenario())
    finally:
        holder.close()
    assert listening is False
