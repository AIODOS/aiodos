"""Internet (phase 2) vu du nœud : le compagnon aiodos-net, et ce qu'il faut lui dire.

Le gestionnaire du réseau (manager.py) délègue ici : lancer le compagnon quand le
réseau démarre et que l'utilisateur a activé Internet ; lui donner sa liste blanche
(les machines appairées dont on connaît la clé Internet) ; échanger ces clés avec les
machines appairées (action scellée « net/info », cf. netinfo.py) ; décrire l'état pour
l'interface (chemin direct ou relayé, latence, pour chaque machine).
"""
import asyncio
import json
import logging
import time

from aiodos import settings
from aiodos.network import client, netinfo, peers, route
from aiodos.network.client import PeerError
from aiodos.network.companion import Companion, CompanionError

log = logging.getLogger("aiodos.net")


class Internet:
    def __init__(self, manager, options: dict):
        self.m = manager
        self.force = bool(options.get("internet"))            # --internet
        self.local = bool(options.get("internet_local"))      # --internet-local (tests)
        self.companion = None
        self._exchanged = {}       # machine -> (instant, infos qu'on lui a données)
        self._sent = None          # (lancement du compagnon, liste blanche envoyée)
        self._status = None        # dernier état du compagnon (pour l'interface)

    @property
    def enabled(self) -> bool:
        return self.force or bool(settings.get("internet_enabled"))

    def options(self) -> dict:
        if self.local:             # deux nœuds sur une machine : ni relais, ni annuaire, ni box
            return {"relays": ["none"], "directory": "none", "bind": "127.0.0.1:0",
                    "portmap": False}
        relays = [str(r) for r in settings.get("internet_relays") or []] or ["none"]
        directory = ",".join(str(d) for d in settings.get("internet_directory") or [])
        return {"relays": relays, "directory": directory or "none",
                "portmap": bool(settings.get("internet_portmap"))}

    def forward(self) -> str:
        """Adresse du serveur réseau local, vers lequel le compagnon relaie."""
        host = self.m.host
        if host in ("", "0.0.0.0"):
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{self.m.p2p_port}"

    # ------------------------------------------------------------ cycle de vie
    async def start(self):
        if not self.enabled or self.companion is not None:
            return
        companion = Companion(self.forward(), self.options())
        self.companion = companion
        route.attach(companion)        # utilisé seulement quand il tourne
        self._sent = None
        if await asyncio.to_thread(companion.start):
            await self.sync()
            await self.refresh_status()

    async def stop(self):
        companion, self.companion = self.companion, None
        self._status = None
        if companion is not None:
            route.detach(companion)
            await asyncio.to_thread(companion.stop)

    async def apply(self):
        """Réglage « Internet » changé : compagnon lancé ou arrêté (réseau local requis)."""
        if self.m.running and self.enabled:
            await self.start()
        else:
            await self.stop()

    # ---------------------------------------------------------- liste blanche
    async def sync(self):
        """Liste blanche du compagnon = machines appairées dont on connaît la clé ;
        envoyée quand elle change, et après chaque relance du compagnon."""
        companion = self.companion
        if companion is None or not companion.running:
            return
        entries = netinfo.allowlist(await asyncio.to_thread(peers.net_list))
        state = (companion.generation, json.dumps(entries, sort_keys=True))
        if state == self._sent:
            return
        try:
            await asyncio.to_thread(companion.set_peers, entries)
        except CompanionError as exc:
            log.warning("Liste blanche du compagnon non mise à jour : %s", exc)
            return
        self._sent = state

    def changed_from_thread(self):
        """Clé Internet d'une machine reçue (handler réseau, dans un thread)."""
        loop = self.m._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.sync()))

    async def exchange(self, peer: dict):
        """Échange des clés Internet avec une machine appairée qui vient de répondre :
        la première fois, puis toutes les 5 minutes ou quand les nôtres changent."""
        node_id = peer["node_id"]
        mine = await asyncio.to_thread(netinfo.mine)
        last = self._exchanged.get(node_id)
        if last and last[1] == mine and time.time() - last[0] < netinfo.EXCHANGE_EVERY:
            return
        self._exchanged[node_id] = (time.time(), mine)
        try:
            reply = await asyncio.to_thread(client.call, self.m.node_id, peer, "net/info",
                                            {"net": mine})
        except PeerError:
            return                 # version précédente d'AIODOS (action inconnue) : plus tard
        if await asyncio.to_thread(netinfo.remember, node_id, reply.get("net")):
            await self.sync()

    def forget(self, node_id):
        self._exchanged.pop(node_id, None)

    # ------------------------------------------------------------------ état
    async def refresh_status(self):
        companion = self.companion
        if companion is None or not companion.running:
            self._status = None
            return
        try:
            self._status = await asyncio.to_thread(companion.status)
        except CompanionError:
            self._status = None

    async def live_status(self) -> dict:
        """État complet du compagnon, lu à l'instant (API de l'interface)."""
        await self.refresh_status()
        return {**self.snapshot(), "companion": self._status}

    def snapshot(self) -> dict:
        companion = self.companion
        status = self._status or {}
        return {
            "enabled": self.enabled,
            "running": bool(companion and companion.running),
            "error": companion.error if companion else None,
            "id": companion.id if companion else None,
            "route": route.mode(),
            "relays": [r.get("url") for r in status.get("home_relays") or []
                       if r.get("connected")],
        }

    def annotate(self, paired: list):
        """Ajoute à chaque machine appairée l'état de sa connexion Internet, s'il y en a."""
        by_key = {p.get("id"): p for p in (self._status or {}).get("peers") or []}
        for peer in paired:
            seen = by_key.get(peer.get("net_id"))
            peer["internet"] = None if seen is None else {
                "connected": seen.get("connected"), "path": seen.get("path"),
                "rtt_ms": seen.get("rtt_ms"), "direction": seen.get("direction")}
