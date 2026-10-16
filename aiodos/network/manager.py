"""Chef d'orchestre du réseau local : serveur entre nœuds, découverte, appairage,
surveillance des machines appairées, partage de puissance (cf. worker.py), et
compagnon Internet (cf. internet.py).

Tout tourne dans la boucle asyncio du nœud. Le serveur réseau écoute sur toutes
les interfaces (0.0.0.0) — séparé de l'API de l'interface, qui reste sur 127.0.0.1.
"""
import asyncio
import logging
import time

import uvicorn

from aiodos import settings
from aiodos.jobs import queue
from aiodos.network import client, invitations, peers, sharing
from aiodos.network.client import PeerError
from aiodos.network.discovery import Discovery, lan_address
from aiodos.network.envelope import PROTOCOL, ReplayGuard
from aiodos.network.internet import Internet
from aiodos.network.pairing import Pairing
from aiodos.network.worker import Worker

log = logging.getLogger("aiodos.network")

VISIBLE_TTL = 20            # une machine non annoncée depuis 20 s disparaît de la liste
REFRESH_EVERY = 15          # nouvelles des machines appairées
PROFILE_EVERY = 60
PORT_ATTEMPTS = 3           # port encore tenu par l'instance qui vient de s'arrêter (mise à jour)
PORT_RETRY_DELAY = 2


async def _serve(server):
    """uvicorn « quitte » (SystemExit) quand le port est déjà pris : sans cette garde,
    l'arrêt remonterait jusqu'à la boucle du nœud et l'arrêterait tout entier."""
    try:
        await server.serve()
    except SystemExit:
        pass


class NetworkManager:
    def __init__(self, node_id: str, bus, options: dict | None = None, executor=None):
        options = options or {}
        self.node_id = node_id
        self.bus = bus
        self.executor = executor          # file locale : unités confiées, ressources prêtées
        self.worker = Worker(self) if executor else None
        self.force = bool(options.get("force"))           # --network
        self.host = options.get("host") or "0.0.0.0"       # --p2p-host
        self.port_override = options.get("port")          # --p2p-port
        self.discovery_on = options.get("discovery", True)
        self.guard = ReplayGuard()
        self.pairing = Pairing(self)
        self.internet = Internet(self, options)            # --internet, --internet-local
        self.visible = {}
        self.error = None
        self._server = None
        self._server_task = None
        self._discovery = None
        self._refresh_task = None
        self._last_profile = {}
        self._loop = None

    # ------------------------------------------------------------- réglages
    def name(self) -> str:
        return settings.get("node_name")

    @property
    def enabled(self) -> bool:
        return self.force or bool(settings.get("network_enabled"))

    @property
    def p2p_port(self) -> int:
        return int(self.port_override or settings.get("p2p_port"))

    @property
    def running(self) -> bool:
        return self._server_task is not None and not self._server_task.done()

    # --------------------------------------------------------- cycle de vie
    async def apply(self):
        """Démarre ou arrête le réseau selon le réglage."""
        if self.enabled and not self.running:
            await self._start()
        elif not self.enabled and self.running:
            await self._stop()
        self.changed()

    async def _start(self):
        from aiodos.network.p2p_app import create_p2p_app
        self._loop = asyncio.get_running_loop()
        self.error = None
        app = create_p2p_app(self)
        for attempt in range(PORT_ATTEMPTS):
            config = uvicorn.Config(app, host=self.host, port=self.p2p_port,
                                    log_config=None, log_level="warning", access_log=False)
            self._server = uvicorn.Server(config)
            self._server_task = asyncio.create_task(_serve(self._server), name="aiodos-p2p")
            for _ in range(100):
                if self._server.started or self._server_task.done():
                    break
                await asyncio.sleep(0.05)
            if self._server.started or not self._server_task.done():
                break
            if attempt + 1 < PORT_ATTEMPTS:
                await asyncio.sleep(PORT_RETRY_DELAY)
        if not self._server.started:
            self.error = (f"Le port {self.p2p_port} est indisponible : le réseau n'a pas pu "
                          "démarrer.")
            log.warning(self.error)
            self._server_task = None
            return
        if self.discovery_on:
            self._discovery = Discovery(int(settings.get("discovery_port")), self._beacon,
                                        self._on_beacon, self._manual_targets)
            try:
                await self._discovery.start()
            except OSError as exc:
                self._discovery = None
                self.error = f"Découverte automatique indisponible ({exc}). Ajout par adresse possible."
        self.internet.launch()                            # si activé ; prêt un peu plus tard
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="aiodos-peers")
        if self.worker:
            self.worker.start()
        address = lan_address() if self.host == "0.0.0.0" else None
        if address:
            log.info("Réseau local actif sur %s:%s (pour ajouter cette machine depuis une "
                     "autre : %s:%s)", self.host, self.p2p_port, address, self.p2p_port)
        else:
            log.info("Réseau local actif sur %s:%s", self.host, self.p2p_port)

    async def _stop(self):
        if self.worker:
            await self.worker.stop()
        if self._refresh_task:
            self._refresh_task.cancel()
        await self.internet.stop()
        if self._discovery:
            await self._discovery.stop()
            self._discovery = None
        if self._server:
            self._server.should_exit = True
            try:
                await asyncio.wait_for(self._server_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._server = self._server_task = self._refresh_task = None
        self.visible.clear()
        log.info("Réseau local arrêté")

    async def shutdown(self):
        if self.running:
            await self._stop()

    # ----------------------------------------------------------- découverte
    def _beacon(self) -> dict:
        return {"aiodos": PROTOCOL, "id": self.node_id, "name": self.name(), "port": self.p2p_port}

    def _manual_targets(self) -> list:
        targets = []
        for item in settings.get("manual_peers") or []:
            host, _, port = str(item).partition(":")
            if host:
                targets.append((host, int(settings.get("discovery_port"))))
        return targets

    def _on_beacon(self, message: dict, address: str):
        node_id = str(message.get("id") or "")
        if not node_id or node_id == self.node_id:
            return
        try:
            port = int(message.get("port"))
        except (TypeError, ValueError):
            return
        name = str(message.get("name") or "?")[:60]
        is_new = node_id not in self.visible
        self.visible[node_id] = {"node_id": node_id, "name": name, "address": address,
                                 "port": port, "last_seen": time.time()}
        if peers.get(node_id):
            peers.touch(node_id, address, port, name)
        if is_new:
            self.changed()

    async def add_manual(self, address: str) -> dict:
        """Ajout d'une machine par son adresse (si la diffusion ne passe pas)."""
        host, _, port = address.strip().partition(":")
        port = int(port or settings.get("p2p_port"))
        info = await asyncio.to_thread(client.hello, host, port)
        if info["node_id"] == self.node_id:
            raise PeerError("C'est l'adresse de cette machine-ci.")
        self.visible[info["node_id"]] = {"node_id": info["node_id"], "name": info["name"],
                                         "address": host, "port": port, "last_seen": time.time(),
                                         "manual": True}
        saved = list(settings.get("manual_peers") or [])
        if f"{host}:{port}" not in saved:
            settings.set_value("manual_peers", saved + [f"{host}:{port}"])
        self.changed()
        return info

    # ----------------------------------------------- machines appairées
    async def _refresh_loop(self):
        while True:
            known = await asyncio.to_thread(peers.list_peers)
            # En parallèle : une machine injoignable (jusqu'à 30 s par Internet) ne retarde
            # pas les nouvelles des autres, qui passeraient sinon pour hors ligne
            results = await asyncio.gather(*(self._refresh_peer(p) for p in known),
                                           return_exceptions=True)
            for peer, result in zip(known, results):
                if isinstance(result, Exception):
                    log.warning("Nouvelles de « %s » : %s", peer["name"], result)
            await self.internet.sync()
            await self.internet.refresh_status()
            await asyncio.sleep(REFRESH_EVERY)

    async def _refresh_peer(self, peer: dict):
        full = await asyncio.to_thread(peers.get, peer["node_id"])
        if not full or not (full.get("address") or full.get("net_id")):
            return
        want_profile = time.time() - self._last_profile.get(peer["node_id"], 0) > PROFILE_EVERY
        try:
            reply = await asyncio.to_thread(
                client.call, self.node_id, full, "profile" if want_profile else "ping", {})
        except Exception:
            return              # hors ligne : « vue il y a … » le dira
        await asyncio.to_thread(peers.touch, peer["node_id"], None, None, reply.get("name"))
        if want_profile:
            self._last_profile[peer["node_id"]] = time.time()
            await asyncio.to_thread(peers.set_profile, peer["node_id"], reply)
        await self.internet.exchange(full)                # clés Internet (compagnon)
        self.changed()

    def net_changed(self, removed: str | None = None):
        """Clé Internet d'une machine appairée reçue, ou machine désappairée par l'autre
        côté (`removed`) : liste blanche du compagnon à revoir (depuis n'importe quel thread)."""
        self.internet.changed_from_thread(forget=removed)

    def on_paired(self, node_id: str):
        """Nouvelle machine appairée : on demande son profil tout de suite
        (appelable depuis n'importe quel thread)."""
        self.internet.forget(node_id)                     # clés Internet à échanger de nouveau
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.refresh_now(node_id)))

    async def refresh_now(self, node_id: str):
        self._last_profile.pop(node_id, None)
        peer = await asyncio.to_thread(peers.get, node_id)
        if peer:
            await self._refresh_peer(peer)

    async def unpair(self, node_id: str):
        peer = await asyncio.to_thread(peers.get, node_id)
        if peer and peer.get("address"):
            try:                # on prévient l'autre machine, sans l'exiger
                await asyncio.to_thread(client.call, self.node_id, peer, "unpair", {}, None, 4)
            except Exception:
                pass
        await asyncio.to_thread(peers.remove, node_id)
        self.internet.forget(node_id)
        await self.internet.sync()                        # retirée de la liste blanche
        self.changed()

    # ----------------------------------------------------------- interface
    def snapshot(self) -> dict:
        now = time.time()
        for node_id, item in list(self.visible.items()):
            if not item.get("manual") and now - item["last_seen"] > VISIBLE_TTL:
                del self.visible[node_id]
        paired = peers.list_peers()
        paired_ids = {p["node_id"] for p in paired}
        helping = {}                      # machine -> unités qu'elle calcule pour moi
        for counts in queue.remote_counts().values():
            for node_id, n in counts.items():
                helping[node_id] = helping.get(node_id, 0) + n
        shared = sharing.sent_summary()
        for peer in paired:
            peer["working_for_me"] = helping.get(peer["node_id"], 0)
            peer["shared"] = shared.get(peer["node_id"])         # parti chez cet ami
        self.internet.annotate(paired)
        return {
            "enabled": self.enabled, "running": self.running, "error": self.error,
            "node_id": self.node_id, "name": self.name(), "port": self.p2p_port,
            "discovery": self._discovery is not None,
            "peers": paired,
            "visible": [v for v in self.visible.values() if v["node_id"] not in paired_ids],
            "pairing": self.pairing.snapshot(),
            "invitations": invitations.snapshot(),
            "internet": self.internet.snapshot(),
            "offload": bool(settings.get("offload_enabled")),
            "sharing": self.worker.snapshot() if self.worker and self.running else None,
        }

    def publish(self, event: dict):
        self.bus.publish(event)

    def changed(self):
        self.bus.publish({"type": "network"})
