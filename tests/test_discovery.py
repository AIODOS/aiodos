"""Adresse sur le réseau local, affichée au démarrage d'un nœud sans écran (téléphone)
pour l'ajouter depuis une autre machine."""
import ipaddress

from aiodos.network import discovery


def test_lan_address_is_a_real_ipv4_or_nothing():
    address = discovery.lan_address()
    if address is not None:                       # machine sans réseau : rien à afficher
        ip = ipaddress.IPv4Address(address)
        assert not ip.is_loopback and not ip.is_unspecified


def test_no_network_means_no_address(monkeypatch):
    class NoRoute:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, target):
            raise OSError("Network is unreachable")
    monkeypatch.setattr(discovery.socket, "socket", NoRoute)
    monkeypatch.setattr(discovery.interfaces, "lan_interfaces", lambda max_age=60: [])
    assert discovery.lan_address() is None


def test_loopback_is_not_an_address_for_other_machines(monkeypatch):
    class Loopback:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, target):
            pass

        def getsockname(self):
            return ("127.0.0.1", 40000)
    monkeypatch.setattr(discovery.socket, "socket", Loopback)
    monkeypatch.setattr(discovery.interfaces, "lan_interfaces", lambda max_age=60: [])
    assert discovery.lan_address() is None
