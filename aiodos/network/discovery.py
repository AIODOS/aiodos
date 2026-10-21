"""Découverte des nœuds AIODOS du réseau local (diffusion UDP).

Chaque nœud annonce toutes les 5 s son identifiant, son nom et son port. Une
annonce n'est qu'un indice : rien ne se passe sans appairage confirmé des deux
côtés. Aucune donnée n'y circule, seulement « je suis là ». Sous un VPN, l'annonce part
aussi à l'adresse de diffusion de chaque vrai réseau local (network/interfaces.py).
"""
import asyncio
import json
import logging
import socket

from aiodos.network import interfaces
from aiodos.network.envelope import PROTOCOL

BEACON_EVERY = 5
log = logging.getLogger("aiodos.discovery")


def _route_address() -> str | None:
    """L'adresse IPv4 de la route par défaut. Un connect() UDP ne fait que choisir la
    route : aucun paquet ne part. Sous un VPN, c'est l'adresse du VPN."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))          # adresse réservée à la documentation
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith(("0.", "127.")) else address


def lan_address() -> str | None:
    """Adresse IPv4 de cette machine sur le réseau local : celle qu'une autre
    machine saisit pour l'ajouter (nœud sans écran, téléphone…). Celle d'une vraie
    carte (Wi-Fi, Ethernet), même sous un VPN qui tient la route par défaut
    (network/interfaces.py) : celle de la route si c'en est une, sinon la première ;
    sans carte lisible, celle de la route."""
    route = _route_address()
    lans = [lan.address for lan in interfaces.lan_interfaces()]
    if route in lans or not lans:
        return route
    return lans[0]


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_beacon):
        self.on_beacon = on_beacon

    def datagram_received(self, data, addr):
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if isinstance(message, dict) and message.get("aiodos") == PROTOCOL:
            self.on_beacon(message, addr[0])


class Discovery:
    def __init__(self, port: int, beacon, on_beacon, extra_targets):
        self.port = port
        self.beacon = beacon                  # () -> dict à annoncer
        self.on_beacon = on_beacon            # (message, adresse) -> None
        self.extra_targets = extra_targets    # () -> [(ip, port)] ajoutées à la main
        self._transport = None
        self._task = None
        self.listening = False                # entend-on les annonces des autres ?

    async def start(self):
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self.on_beacon),
                local_addr=("0.0.0.0", self.port), allow_broadcast=True)
            self.listening = True
        except OSError as exc:
            # Port des annonces pris (un autre nœud AIODOS sur cet appareil, l'ancien nœud
            # de Termux…) : on s'annonce quand même, depuis un port libre, sans entendre les
            # autres ; les autres machines voient donc ce nœud et son port
            log.warning("Port %s des annonces pris (%s) : annonces envoyées sans écoute",
                        self.port, exc)
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self.on_beacon), local_addr=("0.0.0.0", 0),
                allow_broadcast=True)
        self._task = asyncio.create_task(self._announce(), name="aiodos-discovery")

    def _targets(self) -> list:
        """Où annoncer : l'adresse de diffusion de chaque vrai réseau local (sa route sort
        par la bonne carte, même sous un VPN qui tient la route par défaut), la diffusion
        générale en secours, puis les adresses ajoutées à la main."""
        targets = [(lan.broadcast, self.port) for lan in interfaces.lan_interfaces()
                   if lan.broadcast]
        targets += [("255.255.255.255", self.port), *self.extra_targets()]
        return list(dict.fromkeys(targets))

    async def _announce(self):
        while True:
            data = json.dumps(self.beacon()).encode("utf-8")
            for target in self._targets():
                try:
                    self._transport.sendto(data, target)
                except OSError as exc:
                    log.debug("Annonce vers %s impossible : %s", target, exc)
            await asyncio.sleep(BEACON_EVERY)

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._transport:
            self._transport.close()
