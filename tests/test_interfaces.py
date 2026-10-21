"""Le réseau local sous un VPN (network/interfaces.py, network/discovery.py) : les annonces
partent aussi à l'adresse de diffusion de chaque vrai réseau local, et l'adresse montrée
pour ajouter cette machine est celle de sa vraie carte, pas celle du VPN qui tient la route
par défaut. Cartes simulées ; seul le dernier essai lit les vraies cartes de Windows."""
import json
import sys
import types

import pytest

from aiodos.network import discovery, interfaces
from aiodos.network.interfaces import Lan
from aiodos.node import android_app


@pytest.fixture(autouse=True)
def fresh():
    interfaces.forget()
    yield
    interfaces.forget()


@pytest.mark.parametrize("address, prefix, broadcast", [
    ("192.168.1.58", 24, "192.168.1.255"),
    ("172.17.5.9", 20, "172.17.15.255"),
    ("10.0.0.7", 8, "10.255.255.255"),
    ("192.168.1.58", 31, None),                 # point à point : pas de diffusion
    ("192.168.1.58", 32, None),
])
def test_the_broadcast_address_of_a_network(address, prefix, broadcast):
    assert Lan("Wi-Fi", address, prefix).broadcast == broadcast


@pytest.mark.parametrize("name, virtual", [
    ("Wi-Fi", False), ("Ethernet", False), ("wlan0", False), ("enp3s0", False),
    ("eth0", False), ("NordLynx", True), ("TAP-NordVPN Windows Adapter V9", True),
    ("WireGuard Tunnel", True), ("Wintun Userspace Tunnel", True), ("tun0", True),
    ("wg0", True), ("nordlynx", True), ("tailscale0", True), ("vEthernet (WSL)", True),
    ("Hyper-V Virtual Ethernet Adapter", True), ("VirtualBox Host-Only Network", True),
    ("docker0", True), ("veth12ab", True), ("br-5f1c", True),
])
def test_vpn_and_virtual_cards_are_recognised(name, virtual):
    assert interfaces.is_virtual(name) is virtual


def test_unusable_addresses_are_dropped_and_the_reading_is_kept_a_while(monkeypatch):
    calls = []

    def read():
        calls.append(1)
        return [Lan("Wi-Fi", "192.168.1.58", 24), Lan("x", "169.254.3.4", 16),
                Lan("y", "100.64.0.9", 10), Lan("z", "127.0.0.1", 8)]
    monkeypatch.setattr(interfaces.sys, "platform", "win32")
    monkeypatch.setattr(interfaces, "_from_windows", read)
    assert [lan.address for lan in interfaces.lan_interfaces()] == ["192.168.1.58"]
    interfaces.lan_interfaces()
    assert len(calls) == 1                       # relu au plus toutes les TTL secondes


def test_an_unreadable_card_list_stops_nothing(monkeypatch):
    def broken():
        raise OSError("refusé")
    monkeypatch.setattr(interfaces.sys, "platform", "win32")
    monkeypatch.setattr(interfaces, "_from_windows", broken)
    assert interfaces.lan_interfaces() == []


def test_the_address_shown_is_the_real_card_not_the_vpn(monkeypatch):
    monkeypatch.setattr(discovery, "_route_address", lambda: "10.5.0.2")   # NordLynx
    monkeypatch.setattr(interfaces, "lan_interfaces",
                        lambda max_age=interfaces.TTL: [Lan("Wi-Fi", "192.168.1.58", 24)])
    assert discovery.lan_address() == "192.168.1.58"
    # Sans VPN, la route sort par une vraie carte : c'est elle
    monkeypatch.setattr(interfaces, "lan_interfaces", lambda max_age=interfaces.TTL: [
        Lan("Ethernet", "192.168.0.3", 24), Lan("Wi-Fi", "192.168.1.58", 24)])
    monkeypatch.setattr(discovery, "_route_address", lambda: "192.168.1.58")
    assert discovery.lan_address() == "192.168.1.58"
    # Aucune carte lisible (autre système) : la route, comme avant
    monkeypatch.setattr(interfaces, "lan_interfaces", lambda max_age=interfaces.TTL: [])
    assert discovery.lan_address() == "192.168.1.58"


def test_announcements_go_to_each_real_network_then_everywhere(monkeypatch):
    monkeypatch.setattr(interfaces, "lan_interfaces", lambda max_age=interfaces.TTL: [
        Lan("Wi-Fi", "192.168.1.58", 24), Lan("Ethernet", "10.0.0.7", 8),
        Lan("point à point", "192.168.9.1", 32)])
    found = discovery.Discovery(47700, dict, lambda m, a: None,
                                lambda: [("192.168.1.20", 47700), ("192.168.1.255", 47700)])
    assert found._targets() == [("192.168.1.255", 47700), ("10.255.255.255", 47700),
                                ("255.255.255.255", 47700), ("192.168.1.20", 47700)]


def test_in_the_android_app_the_addresses_come_from_android(monkeypatch):
    class Device:
        @staticmethod
        def lanAddresses():
            return json.dumps([{"name": "wlan0", "address": "192.168.1.77", "prefix": 24},
                               {"name": "bad"}, "pas un objet"])
    java = types.ModuleType("java")
    java.jclass = lambda name: Device
    monkeypatch.setitem(sys.modules, "java", java)
    monkeypatch.setenv(android_app.APP_ENV, "1")
    assert interfaces.lan_interfaces() == [Lan("wlan0", "192.168.1.77", 24)]
    monkeypatch.delenv(android_app.APP_ENV)
    assert android_app.lan_addresses() == []                # hors de l'appli : rien


@pytest.mark.skipif(sys.platform != "win32", reason="cartes réseau de Windows")
def test_the_real_windows_cards_are_read():
    for lan in interfaces.lan_interfaces(max_age=0):
        assert interfaces._usable(lan.address) and 0 <= lan.prefix <= 32
        assert not interfaces.is_virtual(lan.name)
