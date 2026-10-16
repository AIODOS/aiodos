"""Prêter sa puissance : côté machine qui calcule pour les autres.

Quand le partage est activé et que les conditions sont réunies (PC inactif
depuis N minutes, sur secteur ; téléphone en charge, assez chargé et pas trop
chaud, cf. node/power.py), ce nœud demande du travail à ses machines
appairées, en commençant par celles qui ont le plus calculé pour lui
(réciprocité). Son propre travail passe toujours avant : il ne prête que les
ressources dont il n'a pas besoin. Dès que l'utilisateur revient, les unités en
cours s'arrêtent et sont rendues, comme l'économiseur d'écran de SETI@home ;
une conversation sur ce PC fait de même pour celles qui occupent le GPU.

Les fichiers reçus ne servent qu'au calcul : ils sont effacés aussitôt après,
et rien n'est enregistré dans les bibliothèques de cette machine.
"""
import asyncio
import hashlib
import importlib.util
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from coeur import __version__, paths, settings
from coeur.jobs import leases, registry
from coeur.jobs.executor import ENGINE_RETRY_DELAY, engine_down
from coeur.jobs.models import GPU_BOUND
from coeur.network import client, peers, wire, work
from coeur.network.client import PeerError
from coeur.network.envelope import b64d
from coeur.node import identity, power

log = logging.getLogger("coeur.worker")

CHECK_EVERY = 2          # surveillance des conditions de partage (secondes)
ASK_AGAIN = 10           # machine injoignable ou muette : on lui redemande plus tard
CAPS_TTL = 30
HEAT_MARGIN = 3          # °C : un téléphone trop chaud reprend 3 °C sous sa limite
BATTERY_MARGIN = 5       # % : un téléphone qui calcule s'arrête 5 % sous la charge minimale
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def sharing_state(previous=None) -> dict:
    """Cette machine peut-elle calculer pour les autres en ce moment ? Avec
    l'explication. `previous` : état précédent (écarts de reprise d'un téléphone)."""
    if not settings.get("share_enabled"):
        return {"active": False, "state": "off", "detail": "Partage désactivé."}
    supply = power.read()
    device = "ce téléphone" if supply["mobile"] else "ce PC"
    if settings.get("share_on_ac_only"):
        if supply["plugged"] is False:
            return {"active": False, "state": "battery", "detail": f"En pause : {device} est sur batterie."}
        if supply["plugged"] is None and supply["mobile"]:
            return {"active": False, "state": "unknown",
                    "detail": "En pause : impossible de savoir si ce téléphone est en charge "
                              "(Termux:API manquant ? Voir docs/android.md)."}
    if supply["mobile"]:
        paused = _phone_pause(supply, previous)
        if paused:
            return paused
    if settings.get("share_when_idle"):
        idle = identity.idle_seconds()
        wait = int(settings.get("idle_minutes") or 10) * 60
        if idle is not None and idle < wait:
            left = max(1, round((wait - idle) / 60))
            return {"active": False, "state": "busy",
                    "detail": f"En pause : {device} est utilisé (reprise après {left} min sans activité)."}
    return {"active": True, "state": "ready", "detail": "Prêt à calculer pour tes autres machines."}


def _phone_pause(supply, previous) -> dict | None:
    """Téléphone : batterie assez chargée et pas trop chaude, comme BOINC sous
    Android (90 % et 40 °C par défaut, puis 5 min d'attente après une pause).
    Ici, des écarts plutôt qu'une attente, pour ne pas s'arrêter et repartir sans
    cesse : un téléphone qui calcule ne s'arrête que 5 % sous la charge minimale
    (sur le port USB d'un PC, il peut se décharger en calculant), et ne repart
    qu'après avoir perdu 3 °C. Son inactivité n'est pas mesurable sans droits
    root : c'est la charge qui dit qu'il n'est pas dans une poche."""
    was = (previous or {}).get("state")
    level, minimum = supply["level"], int(settings.get("share_min_battery") or 0)
    floor = minimum - BATTERY_MARGIN if was == "ready" else minimum
    if level is not None and level < floor:
        return {"active": False, "state": "low_battery",
                "detail": f"En pause : batterie à {level} % (reprise à {minimum} %)."}
    heat, limit = supply["temperature"], float(settings.get("share_max_temp") or 0)
    if heat is None or not limit:
        return None
    resume = limit - HEAT_MARGIN
    if heat > (resume if was == "hot" else limit):
        return {"active": False, "state": "hot",
                "detail": f"En pause : batterie à {_celsius(heat)} (reprise à {_celsius(resume)})."}
    return None


def _celsius(value) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " °C"


def safe_name(name) -> str:
    """Nom de fichier sûr pour un fichier reçu. Le nom d'origine compte :
    l'extension choisit l'extracteur, et il figure dans les transcriptions."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(str(name or "")).name).strip(" .")
    if base.split(".")[0].upper() in _RESERVED:
        base = "_" + base
    return base[:150] or "fichier"


def label_of(order) -> str:
    payload = order.get("payload") or {}
    return str(payload.get("source") or payload.get("url")
               or (order.get("blob") or {}).get("name") or order.get("task"))


@dataclass
class Assignment:
    """Une unité qu'une autre machine nous a confiée."""
    peer_id: str
    peer_name: str
    order: dict
    stop: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.peer_id}:{self.order['id']}"

    @property
    def resource(self) -> str:
        return self.order["resource"]

    def info(self) -> dict:
        return {"peer": self.peer_id, "peer_name": self.peer_name, "unit": self.order["id"],
                "task": self.order["task"], "resource": self.resource,
                "label": label_of(self.order), "started": self.started}


class Worker:
    def __init__(self, manager):
        self.m = manager
        self.executor = manager.executor
        self.assignments = {}            # clé -> Assignment
        self.state = {"active": False, "state": "off", "detail": "Partage désactivé."}
        self._task = None
        self._wake = None
        # machine -> (instant où lui redemander, ressources déjà proposées) : une
        # machine qui n'avait rien pour le GPU peut en avoir pour les embeddings
        self._next_ask = {}
        self._gpu_rest_until = 0.0       # pause imposée par « GPU prêté au maximum »
        self._caps = (0.0, None)

    # ------------------------------------------------------------ cycle de vie
    def start(self):
        shutil.rmtree(paths.work_dir(), ignore_errors=True)    # restes d'un arrêt brutal
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="coeur-worker")

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        self._interrupt(lambda a: True)

    def snapshot(self) -> dict:
        return {**self.state, "working": [a.info() for a in self.assignments.values()]}

    # ----------------------------------------------------------------- boucle
    async def _loop(self):
        while True:
            delay = CHECK_EVERY
            try:
                delay = await self._tick()
            except Exception:
                log.exception("Erreur du partage de puissance")
            try:                       # réveil anticipé quand une unité se termine
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _tick(self) -> float:
        state = await asyncio.to_thread(sharing_state, self.state)
        if state != self.state:
            self.state = state
            self.m.changed()
        if not state["active"]:
            self._interrupt(lambda a: True)
            return CHECK_EVERY
        if self.executor.interactive:              # l'interactif d'abord
            self._interrupt(lambda a: a.resource in GPU_BOUND)
        offered = await asyncio.to_thread(self._offered)
        if not offered:
            return CHECK_EVERY
        for peer in await asyncio.to_thread(self._candidates):
            until, asked = self._next_ask.get(peer["node_id"], (0, set()))
            if until > time.monotonic() and set(offered) <= asked:
                continue                           # rien de nouveau à lui proposer
            reply = await asyncio.to_thread(self._ask, peer, offered)
            order = (reply or {}).get("unit")
            if order:
                self._next_ask.pop(peer["node_id"], None)
                self._launch(peer, order)
                return 0.05                        # d'autres ressources sont peut-être libres
            delay = float((reply or {}).get("retry_after") or ASK_AGAIN)
            self._next_ask[peer["node_id"]] = (time.monotonic() + delay, set(offered))
        return CHECK_EVERY

    def _interrupt(self, which):
        for a in self.assignments.values():
            if which(a):
                a.stop.set()

    def _offered(self) -> list:
        """Ressources prêtables : libres ici, sans travail local en attente."""
        free = set(self.executor.free_resources())
        if time.monotonic() < self._gpu_rest_until:
            free -= GPU_BOUND
        free -= leases.local_demand(free)
        return sorted(free)

    def _candidates(self) -> list:
        """Machines en ligne, celles qui ont le plus calculé pour moi d'abord."""
        online = [p for p in peers.list_peers() if p["online"] and p.get("address")]
        online.sort(key=lambda p: p["received_s"] - p["given_s"], reverse=True)
        return online

    def capabilities(self) -> dict:
        at, caps = self._caps
        if caps is None or time.monotonic() - at > CAPS_TTL:
            caps = {"work_api": work.WORK_API, "version": __version__,
                    "models": work.local_models(),
                    "whisper": importlib.util.find_spec("faster_whisper") is not None}
            self._caps = (time.monotonic(), caps)
        return caps

    def _ask(self, peer, offered):
        full = peers.get(peer["node_id"])
        if not full:
            return None
        try:
            return client.call(self.m.node_id, full, "work/lease",
                               {"resources": offered, "caps": self.capabilities()}, timeout=8)
        except PeerError as exc:
            log.debug("Demande de travail à %s impossible : %s", peer["name"], exc)
            return None

    # ------------------------------------------------------------ une unité
    def _launch(self, peer, order):
        a = Assignment(peer["node_id"], peer["name"], order)
        self.assignments[a.key] = a
        self.executor.begin_remote(a.key, a.info())
        asyncio.create_task(self._run(a), name=f"coeur-work-{a.key}")

    async def _run(self, a: Assignment):
        renewer = asyncio.create_task(self._renew(a))
        try:
            outcome = await asyncio.to_thread(self._process, a)
            log.info("Unité %s de « %s » (%s) : %s", a.order["id"], a.peer_name,
                     label_of(a.order), outcome)
        finally:
            renewer.cancel()
            self.assignments.pop(a.key, None)
            self.executor.end_remote(a.key)
            if self._wake:
                self._wake.set()

    async def _renew(self, a: Assignment):
        every = max(5.0, float(a.order.get("lease_s") or leases.LEASE_S) / 3)
        while True:
            await asyncio.sleep(every)
            try:
                reply = await asyncio.to_thread(self._call, a, "work/renew",
                                                {"unit": a.order["id"]}, 8)
            except PeerError:
                continue
            if not reply.get("ok"):
                a.stop.set()       # reprise ou annulée chez le propriétaire : inutile de continuer

    def _call(self, a: Assignment, action, payload, timeout=30) -> dict:
        peer = peers.get(a.peer_id)
        if not peer:
            raise PeerError("Machine désappairée entre-temps.")
        return client.call(self.m.node_id, peer, action, payload, timeout=timeout)

    def _quiet(self, a: Assignment, action, **extra):
        try:
            self._call(a, action, {"unit": a.order["id"], **extra}, timeout=8)
        except PeerError:
            pass                   # sans réponse, le bail expirera chez le propriétaire

    def _process(self, a: Assignment) -> str:
        started = time.perf_counter()
        folder = paths.work_dir() / f"{a.peer_id[:8]}-{int(a.order['id'])}"
        try:
            spec = registry.TASKS.get(a.order["task"])
            if spec is None or not spec.portable:
                raise RuntimeError(f"tâche « {a.order['task']} » inconnue sur cette machine")
            file = self._download(a, folder) if a.order.get("blob") else None
            output = spec.compute(a.order["payload"], file, a.order.get("options") or {}, a.stop)
            compute_s = time.perf_counter() - started
            reply = self._send_result(a, output, compute_s)
            if not reply.get("accepted"):
                return f"résultat refusé ({reply.get('reason')})"
            peers.account(a.peer_id, given_s=compute_s, given_units=1)
            self._rest_gpu(a, compute_s)
            return f"faite en {compute_s:.1f} s"
        except registry.Interrupted:
            self._quiet(a, "work/release")
            return "interrompue et rendue"
        except PeerError as exc:
            return f"propriétaire injoignable ({exc})"
        except Exception as exc:
            if engine_down(exc):
                self.executor.cooldown[a.resource] = time.monotonic() + ENGINE_RETRY_DELAY
                self._quiet(a, "work/release")
                return "Ollama injoignable ici : unité rendue"
            self._quiet(a, "work/fail", error=str(exc)[:400],
                        compute_s=time.perf_counter() - started)
            return f"échec : {exc}"
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _download(self, a: Assignment, folder: Path) -> str:
        blob = a.order["blob"]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_name(blob.get("name"))
        size, received, digest = int(blob["size"]), 0, hashlib.sha256()
        with open(target, "wb") as f:
            while received < size:
                if a.stop.is_set():
                    raise registry.Interrupted()
                reply = self._call(a, "work/blob", {"unit": a.order["id"], "offset": received,
                                                    "size": work.BLOB_CHUNK}, timeout=60)
                data = b64d(reply.get("data") or "")
                if not data:
                    break
                f.write(data)
                digest.update(data)
                received += len(data)
        if received != size or digest.hexdigest() != blob.get("sha256"):
            raise RuntimeError("fichier reçu incomplet ou modifié pendant le transfert")
        return str(target)

    def _send_result(self, a: Assignment, output, compute_s) -> dict:
        body = {"unit": a.order["id"], "output": wire.pack(output), "compute_s": compute_s}
        for attempt in range(3):
            try:
                return self._call(a, "work/result", body, timeout=60)
            except PeerError:
                if attempt == 2:
                    raise
                time.sleep(3)
        return {}

    def _rest_gpu(self, a: Assignment, compute_s):
        """« GPU prêté au maximum : X % » : après une unité GPU de t secondes,
        pause de t × (100 − X) / X avant d'en reprendre une (comme BOINC)."""
        share = int(settings.get("share_max_gpu") or 100)
        if a.resource in GPU_BOUND and 0 < share < 100:
            rest = compute_s * (100 - share) / share
            self._gpu_rest_until = max(self._gpu_rest_until, time.monotonic() + rest)
