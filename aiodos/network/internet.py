"""Internet (phase 2) vu du nœud : le compagnon aiodos-net, et ce qu'il faut lui dire.

Le gestionnaire du réseau (manager.py) délègue ici : lancer le compagnon quand le
réseau démarre et que l'utilisateur a activé Internet ; lui donner sa liste blanche
(les machines appairées dont on connaît la clé Internet) ; échanger ces clés avec les
machines appairées (action scellée « net/info », cf. netinfo.py) ; décrire l'état pour
l'interface (chemin direct ou relayé, latence, pour chaque machine).
"""
import asyncio
import functools
import json
import logging
import time

from aiodos import settings
from aiodos.network import client, invitations, netinfo, peers, route
from aiodos.network.client import PeerError
from aiodos.network.companion import Companion, CompanionError, shown

log = logging.getLogger("aiodos.net")


def _guarded(method):
    """Une erreur imprévue côté Internet n'arrête jamais la surveillance des machines
    appairées (boucle de manager.py) : elle est écrite au journal, c'est tout."""
    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except Exception:
            log.exception("Internet : erreur imprévue (%s)", method.__name__)
    return wrapper


class Internet:
    def __init__(self, manager, options: dict):
        self.m = manager
        self.force = bool(options.get("internet"))            # --internet
        self.local = bool(options.get("internet_local"))      # --internet-local (tests)
        self.relay_only = options.get("internet_relay_only")  # --internet-relay-only URL (tests)
        self.companion = None
        self.launches = 0          # +1 à chaque nouveau compagnon (réseau ou Internet relancés)
        self._exchanged = {}       # machine -> (instant, infos qu'on lui a données)
        self._sent = None          # (lancement du compagnon, liste blanche envoyée)
        self._status = None        # dernier état du compagnon (pour l'interface)
        self._tasks = set()        # tâches de fond en cours (gardées jusqu'à leur fin)

    @property
    def enabled(self) -> bool:
        return self.force or bool(settings.get("internet_enabled"))

    def options(self) -> dict:
        if self.local:             # deux nœuds sur une machine : ni relais, ni annuaire, ni box
            return {"relays": ["none"], "directory": "none", "bind": "127.0.0.1:0",
                    "portmap": False}
        if self.relay_only:        # essai d'un relais : lui seul, jamais en direct, ni annuaire
            return {"relays": [str(self.relay_only)], "directory": "none", "portmap": False,
                    "relay_only": True}
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
    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def launch(self):
        """Lance le compagnon en tâche de fond : le réseau local n'attend pas qu'il soit
        prêt (premier lancement d'un téléphone, programme qui ne répond pas…)."""
        if not self.enabled or self.companion is not None:
            return
        companion = Companion(self.forward(), self.options())
        self.companion = companion
        self.launches += 1
        route.attach(companion)        # utilisé seulement quand il tourne
        self._sent = None
        self._spawn(self._ready(companion))

    @_guarded
    async def _ready(self, companion):
        if await asyncio.to_thread(companion.start) and self.companion is companion:
            await self.sync()
            await self.refresh_status()
            self.m.changed()

    async def stop(self):
        companion, self.companion = self.companion, None
        self._status = None
        if companion is not None:
            route.detach(companion)
            await asyncio.to_thread(companion.stop)

    async def apply(self):
        """Réglage « Internet » changé : compagnon lancé ou arrêté (réseau local requis)."""
        if self.m.running and self.enabled:
            self.launch()
        else:
            await self.stop()

    @_guarded
    async def relaunch(self):
        """Compagnon arrêté puis relancé : sur le programme préféré, arrivé depuis son
        lancement (le compagnon livré avec la version, cf. updates/service.py)."""
        if self.companion is None:
            return
        await self.stop()
        if self.m.running:
            self.launch()

    def relaunch_from_thread(self):
        loop = self.m._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: self._spawn(self.relaunch()))

    # ---------------------------------------------------------- liste blanche
    @_guarded
    async def sync(self):
        """Liste blanche du compagnon = machines appairées dont on connaît la clé ;
        envoyée quand elle change, et après chaque relance du compagnon."""
        companion = self.companion
        if companion is None or not companion.running:
            return
        rows = await asyncio.to_thread(lambda: peers.net_list() + invitations.allowlist_rows())
        entries = netinfo.allowlist(rows)
        state = (companion.generation, json.dumps(entries, sort_keys=True))
        if state == self._sent:
            return
        try:
            reply = await asyncio.to_thread(companion.set_peers, entries)
        except CompanionError as exc:
            log.warning("Liste blanche du compagnon non mise à jour : %s", exc)
            return
        skipped = (reply or {}).get("skipped") or []
        if skipped:            # clé illisible : écartée seule, les autres machines restent
            log.warning("Liste blanche : %d entrée(s) écartée(s) : %s", len(skipped),
                        " ; ".join(map(str, skipped))[:500])
        self._sent = state

    def changed_from_thread(self, forget=None):
        """Clé Internet d'une machine reçue, ou machine désappairée par l'autre côté
        (handler réseau, dans un thread) : liste blanche à revoir."""
        if forget is not None:
            self.forget(forget)
        loop = self.m._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: self._spawn(self.sync()))

    @_guarded
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
    @_guarded
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
        running = bool(companion and companion.running)
        return {
            "enabled": self.enabled,
            "running": running,
            "error": companion.error if companion else None,
            "id": companion.id if companion else None,
            "program": companion.origin if running else None,    # d'où vient le programme
            # ... à montrer dans la langue de l'interface (tt(program_t, program))
            "program_t": shown(companion.origin) if running else None,
            "route": route.mode(),
            "relays": [r.get("url") for r in status.get("home_relays") or []
                       if r.get("connected")],
            # Relais qui refusent cette machine : sa clé manque à leur liste blanche
            "denied_by": [r.get("url") for r in status.get("home_relays") or []
                          if r.get("denied")],
            "relay_only": bool(status.get("relay_only")),
        }

    def annotate(self, paired: list):
        """Ajoute à chaque machine appairée l'état de sa connexion Internet, s'il y en a."""
        by_key = {p.get("id"): p for p in (self._status or {}).get("peers") or []}
        for peer in paired:
            seen = by_key.get(peer.get("net_id"))
            peer["internet"] = None if seen is None else {
                "connected": seen.get("connected"), "path": seen.get("path"),
                "rtt_ms": seen.get("rtt_ms"), "direction": seen.get("direction")}
