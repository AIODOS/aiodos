"""Découverte des nœuds Cœur IA du réseau local (diffusion UDP).

Chaque nœud annonce toutes les 5 s son identifiant, son nom et son port. Une
annonce n'est qu'un indice : rien ne se passe sans appairage confirmé des deux
côtés. Aucune donnée n'y circule, seulement « je suis là ».
"""
import asyncio
import json
import logging

from coeur.network.envelope import PROTOCOL

BEACON_EVERY = 5
log = logging.getLogger("coeur.discovery")


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_beacon):
        self.on_beacon = on_beacon

    def datagram_received(self, data, addr):
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if isinstance(message, dict) and message.get("coeur") == PROTOCOL:
            self.on_beacon(message, addr[0])


class Discovery:
    def __init__(self, port: int, beacon, on_beacon, extra_targets):
        self.port = port
        self.beacon = beacon                  # () -> dict à annoncer
        self.on_beacon = on_beacon            # (message, adresse) -> None
        self.extra_targets = extra_targets    # () -> [(ip, port)] ajoutées à la main
        self._transport = None
        self._task = None

    async def start(self):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self.on_beacon),
            local_addr=("0.0.0.0", self.port), allow_broadcast=True)
        self._task = asyncio.create_task(self._announce(), name="coeur-discovery")

    async def _announce(self):
        while True:
            data = json.dumps(self.beacon()).encode("utf-8")
            for target in [("255.255.255.255", self.port), *self.extra_targets()]:
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
