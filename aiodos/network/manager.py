"""Chef d'orchestre du réseau local : serveur entre nœuds, découverte, appairage,
surveillance des machines appairées, partage de puissance (cf. worker.py), et
compagnon Internet (cf. internet.py).

Tout tourne dans la boucle asyncio du nœud. Le serveur réseau écoute sur toutes
les interfaces (0.0.0.0) — séparé de l'API de l'interface, qui reste sur 127.0.0.1.
"""
import asyncio
import logging
import socket
import threading
import time

import uvicorn

from aiodos import settings
from aiodos.jobs import queue
from aiodos.network import (activity, client, consult, invitations, peers, placement, profile,
                            route, sharing)
from aiodos.network.beacons import BeaconChecks
from aiodos.network.client import PeerError
from aiodos.network.connections import GuardedH11
from aiodos.network.discovery import Discovery, lan_address
from aiodos.network.envelope import PROTOCOL, ReplayGuard
from aiodos.network.group import Group
from aiodos.network.internet import Internet
from aiodos.network.public import Public
from aiodos.network.pairing import Pairing
from aiodos.network.worker import Worker

log = logging.getLogger("aiodos.network")

VISIBLE_TTL = 20            # une machine non annoncée depuis 20 s disparaît de la liste
REFRESH_EVERY = 15          # nouvelles des machines appairées
PROFILE_EVERY = 60
PORT_ATTEMPTS = 3           # port encore tenu par l'instance qui vient de s'arrêter (mise à jour)
PORT_RETRY_DELAY = 2


LISTEN_TIMEOUT = 30         # s : un serveur qui démarre lentement n'est pas un port pris
SPARE_PORTS = 9             # ports de secours essayés à la suite du port réglé
MAX_VISIBLE = 64            # machines non appairées montrées à la fois (balises)


def _spare_ports(host: str, wanted: int) -> list:
    """Ports de secours : les suivants du port réglé (le même d'un lancement à l'autre tant
    qu'il est libre), puis un port choisi par le système."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as s:
        s.bind((host, 0))
        spare = s.getsockname()[1]
    return [wanted + n for n in range(1, SPARE_PORTS + 1) if wanted + n < 65536] + [spare]


def _holder(port: int) -> dict | None:
    """Le nœud AIODOS qui garde ce port sur cette machine (son « hello »), ou None."""
    try:
        return client.hello("127.0.0.1", port, timeout=2)
    except Exception:  # noqa: BLE001 — autre programme, ou rien de lisible
        return None


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
        self.spare_port = None            # port réglé pris par un autre programme : celui-ci
        self.discovery_on = options.get("discovery", True)
        self.guard = ReplayGuard()
        self.pairing = Pairing(self)
        self.group = Group(self)                           # appairage par groupe
        self.internet = Internet(self, options)            # --internet, --internet-local
        self.public = Public(self)                         # réseau public (phase 3.0)
        self.visible = {}
        self.beacons = BeaconChecks(self)                  # adresses annoncées, vérifiées
        self.error = None
        self._server = None
        self._server_task = None
        self._discovery = None
        self._bound = None                # adresse d'écoute du serveur entre machines
        self._refresh_task = None
        self._last_profile = {}
        self._loop = None

    # ------------------------------------------------------------- réglages
    def name(self) -> str:
        return settings.get("node_name")

    @property
    def local(self) -> bool:
        """Réseau local : écoute sur le réseau, découverte, chemin direct entre machines."""
        return self.force or bool(settings.get("network_enabled"))

    @property
    def enabled(self) -> bool:
        """Le réseau tourne si le réseau local OU Internet est activé : réseau local coupé,
        mes machines restent jointes par Internet (comme les pairs d'un client BitTorrent)."""
        return self.local or self.internet.enabled

    def _listen_host(self) -> str:
        """Réseau local coupé : le serveur entre machines n'écoute que sur cette machine,
        pour le compagnon Internet qui lui relaie les requêtes."""
        if self.local or self.host not in ("0.0.0.0", "::"):
            return self.host
        return "127.0.0.1"

    @property
    def p2p_port(self) -> int:
        return int(self.port_override or self.spare_port or settings.get("p2p_port"))

    @property
    def running(self) -> bool:
        return self._server_task is not None and not self._server_task.done()

    # --------------------------------------------------------- cycle de vie
    async def apply(self):
        """Démarre ou arrête le réseau selon les réglages (réseau local, Internet) ; le réseau
        local allumé ou coupé, le serveur repart avec la bonne écoute."""
        if self.enabled and self.running and self._bound != self._listen_host():
            await self._stop()
        if self.enabled and not self.running:
            await self._start()
        elif not self.enabled and self.running:
            await self._stop()
        elif self.running:
            await self._sync_local()
        self.changed()

    async def _sync_local(self):
        """Réseau local allumé ou coupé : chemin direct entre machines, et découverte."""
        route.set_lan(self.local)
        if self.local and self.discovery_on and self._discovery is None:
            discovery = Discovery(int(settings.get("discovery_port")), self._beacon,
                                  self._on_beacon, self._manual_targets)
            try:
                await discovery.start()
                self._discovery = discovery
                if not discovery.listening and not self.error:
                    self.error = ("Un autre programme garde le port des annonces : ce nœud "
                                  "s'annonce à tes machines, mais ne voit pas les leurs.")
            except OSError as exc:
                self.error = f"Découverte automatique indisponible ({exc}). Ajout par adresse possible."
        elif not self.local and self._discovery is not None:
            await self._discovery.stop()
            self._discovery = None
            self.visible.clear()

    async def _start(self):
        from aiodos.network.p2p_app import create_p2p_app
        self._loop = asyncio.get_running_loop()
        self.error = None
        self.spare_port = None               # chaque démarrage retente d'abord le port réglé
        app = create_p2p_app(self)
        host = self._bound = self._listen_host()
        await self._listen(app, host)
        if self._failed() and not self.port_override:
            await self._fall_back(app, host)
        if self._failed():
            self.error = (f"Le port {self.p2p_port} est indisponible : le réseau n'a pas pu "
                          "démarrer.")
            log.warning(self.error)
            self._server_task = None
            return
        await self._sync_local()
        self.internet.launch()                            # si activé ; prêt un peu plus tard
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="aiodos-peers")
        if self.worker:
            self.worker.start()
        address = lan_address() if host == "0.0.0.0" else None
        if not self.local:
            log.info("Réseau local coupé : mes machines jointes par Internet seulement")
        elif address:
            log.info("Réseau local actif sur %s:%s (pour ajouter cette machine depuis une "
                     "autre : %s:%s)", self.host, self.p2p_port, address, self.p2p_port)
        else:
            log.info("Réseau local actif sur %s:%s", self.host, self.p2p_port)

    def _failed(self) -> bool:
        """Le serveur entre nœuds s'est arrêté sans démarrer (port pris)."""
        return self._server_task is None or (self._server_task.done()
                                             and not self._server.started)

    async def _listen(self, app, host: str, attempts: int = PORT_ATTEMPTS):
        """Le serveur entre nœuds sur p2p_port ; quelques essais (port encore tenu par
        l'instance qui vient de s'arrêter, lors d'une mise à jour). Un serveur qui démarre
        lentement est attendu (LISTEN_TIMEOUT), puis laissé démarrer : seul un serveur
        arrêté compte comme un port pris."""
        for attempt in range(attempts):
            # Délais d'en-têtes et plafonds par adresse (connections.py) plutôt qu'un
            # plafond global de connexions : 64 connexions muettes d'un appareil inconnu
            # suffisaient à refuser le serveur à tout le monde (relecture du 21/10/2026)
            config = uvicorn.Config(app, host=host, port=self.p2p_port, http=GuardedH11,
                                    log_config=None, log_level="warning", access_log=False)
            self._server = uvicorn.Server(config)
            self._server_task = asyncio.create_task(_serve(self._server), name="aiodos-p2p")
            deadline = self._loop.time() + LISTEN_TIMEOUT
            while (not (self._server.started or self._server_task.done())
                   and self._loop.time() < deadline):
                await asyncio.sleep(0.05)
            if not self._failed():
                if not self._server.started:
                    log.warning("Serveur entre nœuds lent à démarrer sur le port %s",
                                self.p2p_port)
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(PORT_RETRY_DELAY)

    async def _fall_back(self, app, host: str):
        """Port réglé pris par un autre programme, par exemple un ancien nœud AIODOS sous
        Termux sur le même téléphone : un port de secours, que les annonces et l'appairage
        donnent aux autres machines, et un avertissement qui dit quoi faire."""
        wanted = self.p2p_port
        other = await asyncio.to_thread(_holder, wanted)
        for port in _spare_ports(host, wanted):
            self.spare_port = port
            await self._listen(app, host, attempts=1)
            if not self._failed():
                break
        else:
            self.spare_port = None
            return
        if other:
            self.error = (f"Un autre nœud AIODOS (« {other.get('name') or '?'} ») tourne sur "
                          f"cet appareil et garde le port {wanted} : arrête-le (l'ancien nœud "
                          f"de Termux sur un téléphone : redémarrer le téléphone suffit). En "
                          f"attendant, ce nœud passe par le port {port}.")
        else:
            self.error = (f"Le port {wanted} est pris par un autre programme : ce nœud passe "
                          f"par le port {port}, qu'il annonce à tes machines.")
        log.warning(self.error)

    async def _stop(self):
        if self.worker:
            await self.worker.stop()
        from aiodos.cluster import group, helper
        await asyncio.to_thread(group.stop_all)            # cartes empruntées rendues
        await asyncio.to_thread(helper.stop_all, self)     # cartes prêtées reprises
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
        """Balise reçue : un indice, que n'importe quel appareil du réseau local peut
        envoyer. Une machine appairée n'est suivie à une autre adresse qu'après une réponse
        scellée avec la clé de la paire, qui dit où la requête lui est arrivée
        (network/beacons.py) ; son nom n'en vient jamais. Le plafond de MAX_VISIBLE ne vise
        que les machines inconnues : un flot d'identifiants inventés ne cache ni mes
        machines, ni leurs balises."""
        node_id, port = message.get("id"), message.get("port")
        if not peers.valid_id(node_id) or node_id == self.node_id:
            return
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            return
        now = time.time()
        known = peers.transport(node_id)
        is_new = node_id not in self.visible
        if known is None and is_new and self._strangers() >= MAX_VISIBLE:
            self._forget_unseen(now)
            if self._strangers() >= MAX_VISIBLE:
                return                    # un flot d'identifiants inventés
        name = profile.name(message.get("name")) or "?"
        self.visible[node_id] = {"node_id": node_id, "name": name, "address": address,
                                 "port": port, "last_seen": now, "paired": known is not None}
        if known is not None:
            self.beacons.hint(node_id, address, port, known)
        if is_new:
            self.changed()

    def _strangers(self) -> int:
        """Machines non appairées dans la liste des machines visibles."""
        return sum(1 for item in self.visible.values() if not item.get("paired"))

    def _forget_unseen(self, now: float):
        """Les machines non annoncées depuis VISIBLE_TTL disparaissent de la liste."""
        for node_id, item in list(self.visible.items()):
            if not item.get("manual") and now - item["last_seen"] > VISIBLE_TTL:
                del self.visible[node_id]

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
        # Chaque machine à part : une machine injoignable (jusqu'à 30 s par Internet) ne
        # retarde pas les nouvelles des autres, qui passeraient sinon pour hors ligne. Une
        # balise ne les marque plus « en ligne » (network/beacons.py) : sans cela, un tour
        # qui attendrait la plus lente dépasserait peers.ONLINE_WINDOW
        running = {}                      # machine -> ses nouvelles, en cours
        try:
            while True:
                known = await asyncio.to_thread(peers.list_peers)
                for peer in known:
                    task = running.get(peer["node_id"])
                    if task is None or task.done():
                        running[peer["node_id"]] = asyncio.create_task(
                            self._refresh_logged(peer), name=f"aiodos-nouvelles-{peer['name']}")
                for node_id in [n for n, t in running.items() if t.done()]:
                    del running[node_id]
                await self.internet.sync()
                await self.internet.refresh_status()
                await self.public.sync()
                await asyncio.sleep(REFRESH_EVERY)
        finally:
            for task in running.values():
                task.cancel()

    async def _refresh_logged(self, peer: dict):
        try:
            await self._refresh_peer(peer)
        except Exception as exc:  # noqa: BLE001 — une machine ne fait pas tomber la boucle
            log.warning("Nouvelles de « %s » : %s", peer["name"], exc)

    async def _refresh_peer(self, peer: dict):
        full = await asyncio.to_thread(peers.get, peer["node_id"])
        if not full or not (full.get("address") or full.get("net_id")):
            return
        want_profile = time.time() - self._last_profile.get(peer["node_id"], 0) > PROFILE_EVERY
        started = time.monotonic()
        try:
            reply = await asyncio.to_thread(
                client.call, self.node_id, full, "profile" if want_profile else "ping", {})
        except Exception:
            return              # hors ligne : « vue il y a … » le dira
        # Temps de réponse : celui d'un ping (un profil se calcule, nvidia-smi compris)
        elapsed_ms = None if want_profile else 1000 * (time.monotonic() - started)
        # Typé et borné avant d'être gardé, affiché ou utilisé (network/profile.py) : un
        # ami peut sceller n'importe quel JSON (audit du 20/10/2026 : HTML injecté dans la
        # vue Réseau, NaN qui faisait tomber GET /api/network)
        reply = profile.clean(reply) or {}
        await asyncio.to_thread(peers.touch, peer["node_id"], None, None, reply.get("name"))
        placement.note_load(peer["node_id"], reply.get("load"))   # si elle prête ses modèles
        activity.note(peer["node_id"], reply, elapsed_ms)         # son partage, en direct
        if want_profile:
            self._last_profile[peer["node_id"]] = time.time()
            await asyncio.to_thread(peers.set_profile, peer["node_id"], reply)
        await self.internet.exchange(full)                # clés Internet (compagnon)
        if full.get("trust") == sharing.MINE:              # retrait ou mise de côté manqués
            await asyncio.to_thread(self.group.flush, peer["node_id"])
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

    def remove_now(self, node_id: str):
        """Retire une machine appairée, depuis n'importe quel thread (« group/remove ») :
        elle est prévenue, oubliée ici, et retirée de la liste blanche du compagnon."""
        peer = peers.get(node_id)
        if peer and (peer.get("address") or peer.get("net_id")):
            try:
                client.call(self.node_id, peer, "unpair", {}, None, 4)
            except Exception:
                pass
        peers.remove(node_id)
        self.internet.forget(node_id)
        activity.forget(node_id)
        self.net_changed(node_id)
        self.changed()

    def present_later(self, friend_id: str, only=None, delay: float = 0):
        """Appairage par groupe (décision de l'auteur) : un ami choisi ici est présenté
        d'office à mes autres machines ; une nouvelle machine à moi, aux amis. Hors du fil
        appelant : il faut joindre trois machines (après `delay` secondes)."""
        def run():
            if delay:
                time.sleep(delay)
            try:
                self.group.present(friend_id, only)
            except Exception as exc:
                log.info("Présentation de « %s » impossible : %s", friend_id, exc)
        threading.Thread(target=run, daemon=True, name="aiodos-presentation").start()

    def present_new_machine(self, node_id: str):
        """Une nouvelle machine à moi : présentée aux amis qui l'acceptent."""
        for listed in peers.list_peers():
            if listed["trust"] == sharing.FRIEND and not listed.get("set_aside_at"):
                self.present_later(listed["node_id"], only=[node_id])

    async def unpair(self, node_id: str):
        peer = await asyncio.to_thread(peers.get, node_id)
        # Jointe sur le réseau local ou par Internet (appairée sur invitation : clé seule)
        if peer and (peer.get("address") or peer.get("net_id")):
            try:                # on prévient l'autre machine, sans l'exiger
                await asyncio.to_thread(client.call, self.node_id, peer, "unpair", {}, None, 4)
            except Exception:
                pass
        await asyncio.to_thread(peers.remove, node_id)
        self.internet.forget(node_id)
        activity.forget(node_id)
        await self.internet.sync()                        # retirée de la liste blanche
        self.changed()

    # ----------------------------------------------------------- interface
    def snapshot(self) -> dict:
        self._forget_unseen(time.time())
        paired = peers.list_peers()
        paired_ids = {p["node_id"] for p in paired}
        helping = {}                      # machine -> unités qu'elle calcule pour moi
        for counts in queue.remote_counts().values():
            for node_id, n in counts.items():
                helping[node_id] = helping.get(node_id, 0) + n
        shared = sharing.sent_summary()
        consulted = consult.summary()
        for peer in paired:
            peer["working_for_me"] = helping.get(peer["node_id"], 0)
            peer["shared"] = shared.get(peer["node_id"])         # parti chez cet ami
            peer["consulted"] = consulted.get(peer["node_id"])   # ses recherches chez moi
        self.internet.annotate(paired)
        working = self.worker.snapshot() if self.worker and self.running else None
        activity.annotate(paired, (working or {}).get("working"))   # en direct (activity.py)
        return {
            "enabled": self.enabled, "local": self.local, "running": self.running,
            "error": self.error,
            "node_id": self.node_id, "name": self.name(), "port": self.p2p_port,
            "discovery": self._discovery is not None,
            "peers": paired,
            "visible": [v for v in self.visible.values() if v["node_id"] not in paired_ids],
            "pairing": self.pairing.snapshot(),
            "introductions": self.group.offers(),          # présentations à accepter
            "invitations": invitations.snapshot(),
            "internet": self.internet.snapshot(),
            "public": self.public.snapshot(),              # réseau public (phase 3.0)
            "offload": bool(settings.get("offload_enabled")),
            "sharing": working,
        }

    def publish(self, event: dict):
        self.bus.publish(event)

    def changed(self):
        self.bus.publish({"type": "network"})
