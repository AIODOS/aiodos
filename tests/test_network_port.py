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
