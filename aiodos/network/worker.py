"""Prêter sa puissance : côté machine qui calcule pour les autres.

Quand le partage est activé et que les conditions sont réunies (PC sur secteur ;
téléphone en charge, assez chargé et pas trop chaud, cf. node/power.py), ce nœud
demande du travail à ses machines appairées, en commençant par celles qui ont le
plus calculé pour lui (réciprocité). Son propre travail passe toujours avant : il
ne prête que les ressources dont il n'a pas besoin. Une conversation sur ce PC
arrête et rend les unités qui occupent le GPU.

Pendant que l'utilisateur se sert du PC, le prêt continue en priorité basse
(décision du 17/10/2026, docs/arriere-plan.md § 5 bis) : jamais sur batterie, et
la carte graphique prêtée la moitié du temps au plus (IN_USE_GPU_SHARE). Le réglage
« Seulement quand je n'utilise pas ce PC » (share_when_idle) revient à l'ancienne
règle : dès que l'utilisateur revient, les unités en cours s'arrêtent et sont
rendues, comme l'économiseur d'écran de SETI@home.

Les fichiers reçus ne servent qu'au calcul : ils sont effacés aussitôt après,
et rien n'est enregistré dans les bibliothèques de cette machine. Leur taille vient de
l'autre machine : elle est bornée ici (work.MAX_BLOB, et de la place laissée libre sur le
disque), chaque morceau aussi (audit du 20/10/2026). Les réponses du propriétaire, qui peut
être un ami, passent par client.call : lues en flux et bornées avant d'être analysées (ni une
réponse énorme ni une bombe gzip n'épuisent la mémoire de cette machine), reconnues à leur
forme stricte, et rendues avant l'échéance de l'appel, même distillées octet par octet ; un
contenu illisible ou non fini y devient un refus (PeerError), qui ne fige pas la boucle du
prêt (relecture finale du 21/10/2026 : reply_limit.py, son prédécesseur, ne tenait pas
l'échéance et laissait passer SealError). Le calcul se fait
dans un thread de fond, à priorité abaissée, et suit la cadence du travail de fond
(node/priority.py, jobs/pacing.py : sur batterie, si le partage y est permis).

Ce qui est demandé, et ce qui est accepté (relecture du 21/10/2026). À une machine qui n'est
pas à moi, jamais la ressource « net » : je ne lis pas le Web pour un ami, et une version
d'avant me confierait encore ses pages, que je lui rendrais aussitôt, en boucle. D'un ami,
pas non plus d'image, de son ni de vidéo à indexer (_media_to_index) : un propriétaire à
jour ne les confie plus à un ami, et mes décodeurs ne tournent pas sur ses fichiers. Une unité
pour une ressource que je n'ai pas proposée est rendue sans être calculée. Une unité que je
dois rendre et qu'on me redonnerait aussitôt (pas de place pour son fichier, tâche que je
ne calcule pas pour cette machine, modèle Whisper absent) met cette machine, ou cette
ressource, au repos un moment (_rest). Un modèle Whisper absent se télécharge pour une de
mes machines, jamais pour un ami (services/media.py : décidé ici, d'après le niveau de
confiance, jamais d'après l'ordre reçu) ; sinon l'unité est rendue, sans compter d'échec.
Un résultat plus gros que ce que le propriétaire acceptera (« result_max » de l'ordre) n'est
pas envoyé : l'unité est rendue en échec, sans reproche (« too_big »). Enfin, un ami
n'apprend rien de cette machine par un échec : ni ses chemins, ni le nom de son compte, ni
son disque plein (_reason).
"""
import asyncio
import contextvars
import hashlib
import importlib.util
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiodos import __version__, i18n, paths, settings
from aiodos.engines import lending as engine_lending
from aiodos.i18n import t
from aiodos.imaging import engine as images_engine
from aiodos.jobs import leases, pacing, registry
from aiodos.jobs.executor import ENGINE_RETRY_DELAY, engine_down
from aiodos.jobs.kinds import engine as engine_kind
from aiodos.jobs.models import DEFAULT_SLOTS, GPU_BOUND
from aiodos.knowledge import embed_model
from aiodos.network import client, crypto, lent_history, peers, route, sharing, wire, work
from aiodos.network.client import PeerError
from aiodos.network.envelope import b64d
from aiodos.node import android, identity, keep_awake, places, power, priority
from aiodos.services import extract, media

log = logging.getLogger("aiodos.worker")


class _NotForFriends(Exception):
    """Un ordre d'une tâche réservée à mes machines, reçu d'un ami sans mon accord."""


class _NotNow(Exception):
    """Un téléphone débranché ou sorti du Wi-Fi ne calcule plus pour un ami."""


class _NoRoom(Exception):
    """Pas assez de place sur cette machine pour le fichier de l'unité : elle est rendue."""


class _BadOrder(RuntimeError):
    """L'ordre reçu, ou son fichier, ne tient pas (tâche inconnue, taille annoncée, morceau
    trop long, empreinte) : refusé avant tout calcul. Son message ne dit rien de cette
    machine : il part tel quel chez le propriétaire, ami compris (_reason), avec sa clé pour
    qu'il le lise dans sa langue (lazy, cf. _told)."""

    def __init__(self, key: str, **params):
        super().__init__(t(key, **params))
        self.lazy = i18n.lazy(key, **params)


class _TooBig(Exception):
    """Le résultat dépasse ce que son propriétaire acceptera (« result_max » de l'ordre,
    network/body_limit.py chez lui) : l'envoyer ne servirait à rien. `size` et `limit` :
    les deux tailles, écrites (_mib)."""

    def __init__(self, size: str, limit: str):
        super().__init__(t("worker.size_limit", size=size, limit=limit))
        self.sizes = {"size": size, "limit": limit}


CHECK_EVERY = 2          # surveillance des conditions de partage (secondes)
ASK_AGAIN = 10           # machine injoignable ou muette : on lui redemande plus tard
# Délai d'une demande de travail. Sur le réseau local, une réponse vient en moins d'une
# seconde ; à travers Internet (réseau mobile faible, relais), il a fallu jusqu'à 10 s
# le 16/10/2026 : avec 8 s, un téléphone en 4G n'obtenait jamais rien.
LEASE_TIMEOUT = 30
CHUNK_RETRY_DELAYS = (2, 5, 10, 20, 30, 30)   # coupure en plein transfert : ~1 min 40 de patience
RESULT_RETRY_DELAYS = (3, 10, 20)              # résultat non remis : nouveaux essais après…
CAPS_TTL = 30
HEAT_MARGIN = 3          # °C : un téléphone trop chaud reprend 3 °C sous sa limite
BATTERY_MARGIN = 5       # % : un téléphone qui calcule s'arrête 5 % sous la charge minimale
IN_USE_GPU_SHARE = 50    # % : carte graphique prêtée au plus, pendant que l'utilisateur se sert du PC
DISK_MARGIN = 1024 ** 3  # place laissée libre sur le disque une fois un fichier reçu
LEASE_REPLY = 4 * 1024 ** 2      # un ordre de calcul (réponse à work/lease), au plus
SMALL_REPLY = 256 * 1024         # réponse à work/renew, work/result, work/fail, work/release
NO_ROOM_RETRY = 600      # s : faute de place, cette machine ne redemande pas de travail avant
SLOW_RETRY = 300         # s : propriétaire qui a laissé une demande aller jusqu'à son échéance
# s : après une unité que je ne calcule pas pour elle (réservée à mes machines, ressource
# que je n'ai pas proposée), cette machine n'est plus redemandée avant
REFUSED_RETRY = 600
# s : modèle Whisper absent ici, non téléchargé pour elle : plus de son ni de vidéo de cette
# machine avant (chaque essai recommencerait par la copie du fichier)
WHISPER_RETRY = 3600
# Jamais proposées à une machine qui n'est pas à moi : « net », lire le Web depuis cette
# machine (index_url, réservée à mes machines dans les deux sens : jobs/kinds/index.py)
MINE_ONLY_RESOURCES = frozenset({"net"})
ENVELOPE_MARGIN = 4096   # octets : « from », « to », « ts » autour du résultat scellé
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def _state(active: bool, state: str, key: str, **params) -> dict:
    """Un état du partage, et son explication : rendue dans la langue du nœud (detail, que
    lisent aussi mes machines d'une version d'avant) et à traduire chez qui la lit (detail_t,
    i18n.lazy : l'interface d'ici, et mes machines, cf. activity.field)."""
    return {"active": active, "state": state, "detail": t(key, **params),
            "detail_t": i18n.lazy(key, **params)}


def sharing_state(previous=None) -> dict:
    """Cette machine peut-elle calculer pour les autres en ce moment ? Avec
    l'explication. `previous` : état précédent (écarts de reprise d'un téléphone)."""
    if not settings.get("share_enabled"):
        return _state(False, "off", "worker.sharing_off")
    supply = power.read()
    if settings.get("share_on_ac_only"):
        if supply["plugged"] is False:
            return _state(False, "battery", "worker.paused_battery_phone" if supply["mobile"]
                          else "worker.paused_battery_pc")
        if supply["plugged"] is None and supply["mobile"]:
            return _state(False, "unknown", "worker.paused_charge_unknown")
    if supply["mobile"]:
        paused = _phone_pause(supply, previous)
        if paused:
            return paused
        return _state(True, "ready", "worker.ready")
    # Plus bas, ce n'est plus un téléphone : « ce PC »
    idle = identity.idle_seconds()
    wait = int(settings.get("idle_minutes") or 10) * 60
    in_use = idle is None or idle < wait             # inactivité inconnue : on suppose que si
    if settings.get("share_when_idle"):
        if idle is not None and idle < wait:
            left = max(1, round((wait - idle) / 60))
            return _state(False, "busy", "worker.paused_in_use", minutes=left)
    elif in_use and supply["plugged"] is False:
        # Prêt pendant l'utilisation : jamais sur batterie, même si le partage y est permis
        return _state(False, "battery", "worker.paused_in_use_battery")
    elif in_use:
        # Activité inconnue (Linux, nœud sans écran) : les mêmes égards que pendant une
        # utilisation. « Seulement quand je n'utilise pas ce PC » prête alors sans retenue,
        # faute de pouvoir mesurer une utilisation (règle d'avant).
        return _state(True, "in_use", "worker.ready_low_priority" if idle is not None
                      else "worker.ready_low_priority_unmeasured")
    return _state(True, "ready", "worker.ready")


def welcomes(peer_id: str) -> bool:
    """Cette machine travaille-t-elle pour celle-ci en ce moment ? Toujours pour mes
    machines ; pour un ami, un téléphone seulement en charge et en Wi-Fi."""
    if not android.is_android() or sharing.level(peer_id) != sharing.FRIEND:
        return True
    return friends_welcome()


def friends_welcome() -> bool:
    """Un téléphone ne calcule pour un ami qu'en charge et en Wi-Fi (décision de l'auteur,
    appairage par groupe) : ni sa batterie, ni ses données mobiles. Dans le doute, non."""
    return bool(power.read().get("plugged")) and android.on_wifi() is True


def _phone_pause(supply, previous) -> dict | None:
    """Téléphone : batterie assez chargée et pas trop chaude, comme BOINC sous
    Android (90 % et 40 °C par défaut, puis 5 min d'attente après une pause).
    Ici, des écarts plutôt qu'une attente, pour ne pas s'arrêter et repartir sans
    cesse : un téléphone qui calcule ne s'arrête que 5 % sous la charge minimale
    (sur le port USB d'un PC, il peut se décharger en calculant), et ne repart
    qu'après avoir perdu 3 °C. Son inactivité (écran allumé) n'est pas mesurable
    sans adb : c'est la charge qui dit qu'il n'est pas dans une poche."""
    was = (previous or {}).get("state")
    level, minimum = supply["level"], int(settings.get("share_min_battery") or 0)
    floor = minimum - BATTERY_MARGIN if was == "ready" else minimum
    if level is not None and level < floor:
        return _state(False, "low_battery", "worker.paused_low_battery", level=level,
                      minimum=minimum)
    heat, limit = supply["temperature"], float(settings.get("share_max_temp") or 0)
    if heat is None or not limit:
        return None
    resume = limit - HEAT_MARGIN
    if heat > (resume if was == "hot" else limit):
        # Températures écrites ici (décimale de la langue du nœud), telles quelles chez qui lit
        return _state(False, "hot", "worker.paused_hot", heat=_celsius(heat),
                      resume=_celsius(resume))
    return None


def _celsius(value) -> str:
    """« 40,4 °C », « 37 °C » : une décimale au plus, à la façon de la langue."""
    return t("format.celsius", n=i18n.number(value, 1).rstrip("0").rstrip(",."))


def safe_name(name) -> str:
    """Nom de fichier sûr pour un fichier reçu. Le nom d'origine compte :
    l'extension choisit l'extracteur, et il figure dans les transcriptions."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(str(name or "")).name).strip(" .")
    if base.split(".")[0].upper() in _RESERVED:
        base = "_" + base
    return base[:150] or "fichier"


def _reply_size(action: str, payload: dict) -> int:
    """Réponse attendue du propriétaire, au plus : un morceau de fichier (base64 dans la
    réponse, puis encore base64 dans l'enveloppe scellée) pour work/blob, peu sinon."""
    if action == "work/blob":
        try:
            size = max(1, min(int(payload.get("size") or work.BLOB_CHUNK), work.BLOB_CHUNK))
        except (TypeError, ValueError):
            size = work.BLOB_CHUNK
        return size * 16 // 9 + 64 * 1024
    return LEASE_REPLY if action == "work/lease" else SMALL_REPLY


def _sealed_size(payload: dict) -> int:
    """Taille, au plus, de la requête qui porte `payload` : JSON, scellé, puis en base64
    dans l'enveloppe (network/envelope.wrap), telle que la mesure network/body_limit.py
    chez le propriétaire. Le JSON est fait ici une fois de trop : moins coûteux qu'un envoi
    de dizaines de Mio refusé trois fois."""
    data = json.dumps(payload, ensure_ascii=False)
    size = len(data) if data.isascii() else len(data.encode("utf-8", "surrogatepass"))
    return 4 * ((size + crypto.NONCE + crypto.TAG + 2) // 3) + ENVELOPE_MARGIN


def _mib(size: int) -> str:
    return t("p2p.size_mib", n=i18n.number(size / 1024 ** 2, 1))


def _retry_after(reply) -> float:
    """Délai avant de redemander du travail à cette machine, tel qu'elle le dit ; ASK_AGAIN
    si elle n'en dit rien de lisible (ni nombre, ni NaN, ni durée négative)."""
    try:
        delay = float((reply or {}).get("retry_after") or ASK_AGAIN)
    except (TypeError, ValueError):
        return ASK_AGAIN
    return delay if 0 < delay < float("inf") else ASK_AGAIN


def _media_to_index(order) -> bool:
    """Une image, un son ou une vidéo à indexer ? Un propriétaire à jour ne les confie plus
    à un ami (son résultat ne se revérifierait pas chez lui : network/work.lease) ; seul un
    ami malveillant, ou resté sur une version d'avant, en envoie encore, et ma machine ferait
    tourner pour rien ses décodeurs (vision, ffmpeg) sur un fichier de lui (relecture
    finale du 21/10/2026). Refusée pour un ami, comme une tâche réservée à mes machines."""
    if not isinstance(order, dict) or order.get("task") != "index_file":
        return False
    blob = order.get("blob") if isinstance(order.get("blob"), dict) else {}
    return extract.classify(safe_name(blob.get("name"))) in ("image", "media")


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
        self.state = _state(False, "off", "worker.sharing_off")
        self._task = None
        self._wake = None
        # machine -> (instant où lui redemander, ressources déjà proposées) : une
        # machine qui n'avait rien pour le GPU peut en avoir pour les embeddings
        self._next_ask = {}
        # machine -> {ressource : instant} : ce qui ne lui est plus proposé d'ici là (_rest)
        self._resting = {}
        self._last_note = {}             # machine -> dernière raison de ne rien recevoir
        self._gpu_rest_until = 0.0       # pause imposée par « GPU prêté au maximum »
        self._caps = (0.0, None)

    # ------------------------------------------------------------ cycle de vie
    def start(self):
        shutil.rmtree(paths.work_dir(), ignore_errors=True)    # restes d'un arrêt brutal
        self._wake = asyncio.Event()
        # Dans un contexte neuf, et non celui de qui démarre le réseau (une requête de
        # l'interface, dans sa langue) : l'état du partage est dit dans la langue du nœud
        self._task = asyncio.create_task(self._loop(), name="aiodos-worker",
                                         context=contextvars.Context())

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
            offer = self._offer_for(peer, offered)
            if not offer:
                continue                           # rien à lui proposer pour l'instant
            until, asked = self._next_ask.get(peer["node_id"], (0, set()))
            if until > time.monotonic() and set(offer) <= asked:
                continue                           # rien de nouveau à lui proposer
            reply = await asyncio.to_thread(self._ask, peer, offer)
            order = (reply or {}).get("unit")
            if order:
                self._next_ask.pop(peer["node_id"], None)
                self._last_note.pop(peer["node_id"], None)
                if not isinstance(order, dict) or order.get("resource") not in offer:
                    await asyncio.to_thread(self._unexpected, peer, order)
                    continue
                self._launch(peer, order)
                return 0.05                        # d'autres ressources sont peut-être libres
            self._next_ask[peer["node_id"]] = (time.monotonic() + _retry_after(reply),
                                               set(offer))
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

    def _offer_for(self, peer, offered) -> list:
        """Ce que je propose à cette machine, parmi les ressources prêtables ici : à une
        machine qui n'est pas à moi, rien de MINE_ONLY_RESOURCES ; et rien de ce qui est au
        repos pour elle (_rest)."""
        now = time.monotonic()
        resting = self._resting.get(peer["node_id"]) or {}
        mine = peer.get("trust") == sharing.MINE
        return [r for r in offered
                if (mine or r not in MINE_ONLY_RESOURCES) and resting.get(r, 0) <= now]

    def _rest(self, peer_id, resources, seconds):
        """Ne plus proposer ces ressources à cette machine pendant `seconds` : elle me
        redonnerait aussitôt l'unité que je viens de lui rendre (ou la même sorte d'unité),
        et nous ferions des allers-retours sans fin (relecture du 21/10/2026 : des centaines
        par seconde avec une machine d'une version d'avant)."""
        until = time.monotonic() + seconds
        resting = self._resting.setdefault(peer_id, {})
        for resource in resources:
            resting[resource] = max(resting.get(resource, 0), until)

    def _unexpected(self, peer, order):
        """Une unité pour une ressource que je n'ai pas proposée : la machine ne suit pas le
        protocole (une version d'avant s'en tient aux ressources demandées,
        leases.claim_remote). Calculée, elle occuperait ici une ressource au-delà de ses
        places (executor.begin_remote), ou que je garde à mes machines : rendue sans être
        calculée, et cette machine au repos."""
        self._rest(peer["node_id"], DEFAULT_SLOTS, REFUSED_RETRY)
        # i18n: tel quel (journal, _note)
        self._note(peer, "unité reçue pour une ressource que je n'avais pas proposée : rendue")
        unit = order.get("id") if isinstance(order, dict) else None
        full = peers.get(peer["node_id"])
        if unit is None or not full:
            return
        try:
            client.call(self.m.node_id, full, "work/release", {"unit": unit}, timeout=8,
                        limit=SMALL_REPLY)
        except PeerError:
            pass                   # sans réponse, le bail expirera chez elle

    def _candidates(self) -> list:
        """Machines en ligne, joignables sur le réseau local ou par Internet (une machine
        appairée sur invitation n'a qu'une clé Internet) : mes machines d'abord, puis les
        amis, et parmi elles celles qui ont le plus calculé pour moi. Les demandes partent
        l'une après l'autre : un ami qui fait attendre la sienne jusqu'à son échéance ne
        retarde pas celles de mes machines (relecture finale du 21/10/2026)."""
        online = [p for p in peers.list_peers()
                  if p["online"] and (p.get("address") or p.get("net_id"))]
        if android.is_android() and not friends_welcome():
            online = [p for p in online if p.get("trust") != sharing.FRIEND]
        online.sort(key=lambda p: (p.get("trust") != sharing.MINE,
                                   -(p["received_s"] - p["given_s"])))
        return online

    def capabilities(self) -> dict:
        at, caps = self._caps
        if caps is None or time.monotonic() - at > CAPS_TTL:
            caps = {"work_api": work.WORK_API, "work_revision": work.WORK_REVISION,
                    "version": __version__, "models": work.local_models(),
                    "whisper": importlib.util.find_spec("faster_whisper") is not None,
                    "readers": extract.readable_documents(),
                    "images": images_engine.capabilities(),
                    # Le ComfyUI accordé de ce PC (travaux des applis de mes machines)
                    "engines": engine_lending.capabilities()}
            self._caps = (time.monotonic(), caps)
        # Ses places, et combien sont prises : relues à chaque demande (essaim, § 3)
        return {**caps, "places": self.executor.slots, "busy": dict(self.executor.busy)}

    def _ask(self, peer, offered):
        full = peers.get(peer["node_id"])
        if not full:
            return None
        caps = self.capabilities()
        if sharing.level(peer["node_id"]) != sharing.MINE and caps.get("images") \
                and not settings.get("lend_images_friends"):
            caps = {k: v for k, v in caps.items() if k != "images"}   # pas sans mon accord
        if sharing.level(peer["node_id"]) != sharing.MINE:
            # ComfyUI : à mes machines seulement (L2b). Ses nœuds ajoutés et ses modèles
            # diraient trop de cette machine à un ami (conception, § 3.5)
            caps = {k: v for k, v in caps.items() if k != "engines"}
        try:
            reply = client.call(self.m.node_id, full, "work/lease",
                                {"resources": offered, "caps": caps}, timeout=LEASE_TIMEOUT,
                                limit=LEASE_REPLY)
        except PeerError as exc:
            # i18n: tel quel (journal, _note)
            self._note(peer, f"demande de travail impossible ({exc})")
            # Elle m'a fait attendre jusqu'à l'échéance (le prêt attend chaque machine à son
            # tour) : pas avant un moment
            if isinstance(exc, client.TooSlow):
                return {"unit": None, "retry_after": SLOW_RETRY}
            return None
        if not isinstance(reply, dict):
            # i18n: tel quel (journal, _note)
            self._note(peer, "réponse illisible à une demande de travail")
            return None
        if reply.get("reason"):
            # Bornée : elle vient de l'autre machine (un ami peut-être) et va au journal
            self._note(peer, str(reply["reason"])[:500])
        return reply

    def _note(self, peer, message):
        """Journalise ce qui empêche de recevoir du travail : une fois par cause et par
        machine, pour qu'une machine injoignable ne remplisse pas le journal."""
        if self._last_note.get(peer["node_id"]) != message:
            self._last_note[peer["node_id"]] = message
            log.info("« %s » : %s", peer["name"], message)

    # ------------------------------------------------------------ une unité
    def _launch(self, peer, order):
        a = Assignment(peer["node_id"], peer["name"], order)
        self.assignments[a.key] = a
        self.executor.begin_remote(a.key, a.info())
        asyncio.create_task(self._run(a), name=f"aiodos-work-{a.key}")

    async def _run(self, a: Assignment):
        renewer = asyncio.create_task(self._renew(a))
        try:
            outcome = await priority.run(self._process, a)      # thread de fond
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
            asked = {"unit": a.order["id"]}
            payload = a.order.get("payload") or {}
            if payload.get("engine"):         # l'étape du calcul, pour l'appli qui attend
                stage = engine_kind.stage_of(payload.get("run_uuid"))
                if stage:
                    asked["progress"] = {"stage": stage}
            try:
                reply = await asyncio.to_thread(self._call, a, "work/renew", asked, 8)
            except PeerError:
                continue
            if not reply.get("ok"):
                # Chez le propriétaire : annulée, mise en pause « tout de suite », partie
                # ailleurs, ou faite ailleurs en fin de partie (state) : inutile de continuer ;
                # elle est rendue sans essai ni reproche
                if reply.get("state"):
                    log.info("Unité %s arrêtée : %s chez son propriétaire", a.order["id"],  # i18n: tel quel (journal)
                             {"paused": "pause", "cancelled": "annulée",
                              "done": "faite ailleurs"}.get(reply["state"], "reprise"))
                a.stop.set()
            elif not await asyncio.to_thread(welcomes, a.peer_id):
                a.stop.set()       # téléphone débranché ou sorti du Wi-Fi : l'unité est rendue

    def _call(self, a: Assignment, action, payload, timeout=30) -> dict:
        peer = peers.get(a.peer_id)
        if not peer:
            raise PeerError(t("worker.unpaired_meanwhile"))
        return client.call(self.m.node_id, peer, action, payload, timeout=timeout,
                           limit=_reply_size(action, payload))

    def _quiet(self, a: Assignment, action, **extra):
        try:
            self._call(a, action, {"unit": a.order["id"], **extra}, timeout=8)
        except PeerError:
            pass                   # sans réponse, le bail expirera chez le propriétaire

    def _process(self, a: Assignment) -> str:
        """Calcule l'unité. → ce qui en est advenu, qui ne va qu'au journal (_run) : en
        français, comme lui. L'historique de la vue Tâches (lent_history) en garde une ligne :
        le genre, pour qui, le modèle, la durée de calcul, la taille, l'issue (ended : son
        code et sa raison courte), jamais le fichier ni son nom."""
        started, waited = time.perf_counter(), pacing.waited()
        folder = paths.work_dir() / f"{a.peer_id[:8]}-{int(a.order['id'])}"

        def computed() -> float:
            # Les attentes (conversation, pauses sur batterie) ne sont pas du calcul
            return max(0.0, time.perf_counter() - started - (pacing.waited() - waited))
        awake = False
        lent, ended, output, compute_s = None, (lent_history.FAILED, "error"), None, None
        try:
            mine = sharing.level(a.peer_id) == sharing.MINE
            lent = lent_history.begin(circle=lent_history.MINE if mine else lent_history.FRIEND,
                                      peer_id=a.peer_id, peer=a.peer_name,
                                      **lent_history.unit_fields(a.order))
            spec = registry.TASKS.get(a.order["task"])
            if spec is None or not spec.portable:
                raise _BadOrder("worker.unknown_task", task=str(a.order["task"])[:60])
            if not mine and not registry.may_compute_for_friend(spec):
                raise _NotForFriends()
            if not mine and _media_to_index(a.order):
                raise _NotForFriends()
            if not welcomes(a.peer_id):
                raise _NotNow()
            file = self._download(a, folder) if a.order.get("blob") else None
            if a.order["payload"].get("engine"):
                # Un travail de moteur : pas de veille sur secteur pendant l'unité, et ses
                # entrées, par empreinte, avant le calcul
                # i18n: tel quel (raison de keep_awake : une clé interne, jamais affichée)
                keep_awake.hold("unité de moteur")
                awake = True
                if a.order["payload"].get("inputs"):
                    a.order["payload"]["files"] = self._download_inputs(a, folder)
            # Un modèle Whisper qui manque se télécharge pour une de mes machines, jamais pour
            # un ami (services/media.py). Le calcul se fait dans ce thread : le contexte y est vu
            with media.downloads(mine):
                output = spec.compute(a.order["payload"], file, a.order.get("options") or {},
                                      a.stop)
            compute_s = computed()
            reply = self._send_result(a, output, compute_s)
            if not reply.get("accepted"):
                ended = (lent_history.FAILED, "result_refused")
                return f"résultat refusé ({reply.get('reason')})"   # i18n: tel quel (journal)
            ended = (lent_history.DONE, None)
            peers.account(a.peer_id, given_s=compute_s, given_units=1)
            self._rest_gpu(a, compute_s)
            return f"faite en {compute_s:.1f} s"                    # i18n: tel quel (journal)
        except registry.Interrupted:
            ended = (lent_history.STOPPED, "interrupted")
            self._quiet(a, "work/release")
            return "interrompue et rendue"                          # i18n: tel quel (journal)
        except _NotForFriends:
            ended = (lent_history.REFUSED, "mine_only")
            self._quiet(a, "work/release")
            # Elle me la redonnerait aussitôt (une version d'avant me confie encore ses
            # pages) : plus rien ne lui est demandé pendant un moment
            self._rest(a.peer_id, DEFAULT_SLOTS, REFUSED_RETRY)
            return "réservée à mes machines : rendue"               # i18n: tel quel (journal)
        except _NotNow:
            ended = (lent_history.REFUSED, "not_now")
            self._quiet(a, "work/release")
            # i18n: tel quel (journal)
            return "téléphone hors charge ou hors Wi-Fi : rendue à l'ami"
        except _NoRoom:
            ended = (lent_history.REFUSED, "no_room")
            self._quiet(a, "work/release")
            # Rien d'autre de cette machine pendant un moment : elle redonnerait la même
            self._rest(a.peer_id, DEFAULT_SLOTS, NO_ROOM_RETRY)
            # i18n: tel quel (journal)
            return "pas assez de place ici pour son fichier : unité rendue"
        except media.WhisperMissing:
            ended = (lent_history.REFUSED, "no_model")
            # Ni l'unité ni ce fichier n'y sont pour rien : rendue sans compter d'échec (un
            # ami me mettrait de côté au bout de dix). Ses autres sons demanderaient le même
            # modèle : plus de cette ressource pour elle pendant un moment
            self._quiet(a, "work/release")
            self._rest(a.peer_id, {a.resource}, WHISPER_RETRY)
            model = str((a.order.get("options") or {}).get("whisper") or "?")[:40]
            # i18n: tel quel (journal)
            return (f"modèle Whisper « {model} » absent ici, non téléchargé pour cette "
                    "machine : unité rendue")
        except embed_model.Missing as exc:
            ended = (lent_history.REFUSED, "no_model")
            # De même pour son modèle d'embeddings (knowledge/embed_model.py) : ni l'unité ni
            # son fichier n'y sont pour rien ; ses autres unités le demanderaient aussi
            self._quiet(a, "work/release")
            self._rest(a.peer_id, {a.resource}, REFUSED_RETRY)
            # i18n: tel quel (journal)
            return f"modèle d'embeddings « {exc.model[:40]} » absent ici : unité rendue"
        except _TooBig as exc:
            ended = (lent_history.FAILED, "too_big")
            # Dit au propriétaire dans la langue d'ici (error) et dans la sienne (error_t)
            told = i18n.lazy("worker.result_too_big", **exc.sizes)
            self._quiet(a, "work/fail", error=i18n.render(told), error_t=told, too_big=True,
                        compute_s=computed())
            # i18n: tel quel (journal)
            return f"résultat trop volumineux pour son propriétaire ({exc}) : rendu en échec"
        except PeerError as exc:
            ended = (lent_history.FAILED, "unreachable")
            return f"propriétaire injoignable ({exc})"              # i18n: tel quel (journal)
        except Exception as exc:
            if engine_down(exc):
                ended = (lent_history.FAILED, "engine")
                self.executor.cooldown[a.resource] = time.monotonic() + ENGINE_RETRY_DELAY
                self._quiet(a, "work/release")
                return "Ollama injoignable ici : unité rendue"      # i18n: tel quel (journal)
            # Un ordre qui ne tient pas est refusé avant tout calcul ; le reste : un échec
            ended = (lent_history.REFUSED, "bad_order") if isinstance(exc, _BadOrder) \
                else (lent_history.FAILED, "error")
            self._quiet(a, "work/fail", error=self._reason(a, exc), error_t=self._told(a, exc),
                        compute_s=computed())
            return f"échec : {exc}"                                 # i18n: tel quel (journal)
        finally:
            if awake:
                keep_awake.release("unité de moteur")   # i18n: tel quel (clé interne)
            shutil.rmtree(folder, ignore_errors=True)
            if lent is not None:
                lent.end(*ended, compute_s if compute_s is not None else computed(),
                         **lent_history.unit_sizes(a.order, output))

    @staticmethod
    def _told(a: Assignment, exc) -> list | None:
        """Ce que le propriétaire apprend d'un échec (cf. _reason), à lire dans sa langue :
        [clé, {paramètres}] (i18n.lazy, envoyé en « error_t ») ; None pour le message d'une
        erreur tel quel, qui ne va qu'à une de mes machines."""
        if isinstance(exc, _BadOrder):
            return exc.lazy
        if sharing.level(a.peer_id) == sharing.MINE:
            return None
        if isinstance(exc, OSError):
            return i18n.lazy("worker.failed_io")
        return i18n.lazy("worker.failed_compute", kind=exc.__class__.__name__)

    @staticmethod
    def _reason(a: Assignment, exc) -> str:
        """Ce que le propriétaire apprend d'un échec. Une de mes machines : le message tel
        quel. Un ami : rien de cette machine. Seul un refus de son ordre (_BadOrder : tâche,
        taille, morceaux, empreinte) lui est dit tel quel. Une erreur de lecture ou
        d'écriture dirait que le disque d'ici est plein ; une erreur du calcul (lecteur de
        documents, transcription…) peut citer des chemins de ce PC, donc le nom de son
        compte (relecture du 21/10/2026) : il n'en apprend que la sorte."""
        told = Worker._told(a, exc)
        return str(exc)[:400] if told is None else i18n.render(told)[:400]

    def _download(self, a: Assignment, folder: Path) -> str:
        """Le fichier de l'unité, morceau par morceau. Sa taille et ses morceaux viennent de
        l'autre machine : bornés ici, avant d'écrire quoi que ce soit."""
        blob = a.order["blob"]
        size = blob.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _BadOrder("worker.size_unreadable")
        if size > work.MAX_BLOB:
            raise _BadOrder("worker.file_too_big", size=size // 1024 ** 2,
                            limit=work.MAX_BLOB // 1024 ** 2)
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free - size < DISK_MARGIN:
            raise _NoRoom()
        target = folder / safe_name(blob.get("name"))
        received, digest = 0, hashlib.sha256()
        owner = peers.get(a.peer_id)
        chunk = work.BLOB_CHUNK if owner and route.lan_first(owner) else work.INTERNET_CHUNK
        with open(target, "wb") as f:
            while received < size:
                if a.stop.is_set():
                    raise registry.Interrupted()
                want = min(chunk, size - received)
                reply = self._chunk(a, received, want)
                text = reply.get("data") or ""
                if not isinstance(text, str) or len(text) > 4 * ((want + 2) // 3):
                    raise _BadOrder("worker.chunk_too_long")
                data = b64d(text)
                if len(data) > want:
                    raise _BadOrder("worker.chunk_too_long")
                if not data:
                    break
                f.write(data)
                digest.update(data)
                received += len(data)
        if received != size or digest.hexdigest() != blob.get("sha256"):
            raise _BadOrder("worker.file_altered")
        return str(target)

    def _download_inputs(self, a: Assignment, folder: Path) -> dict:
        """Les entrées d'un travail de moteur ({slot: chemin}), chacune morceau par morceau
        et vérifiée par son empreinte. Tailles et empreintes viennent de l'autre machine :
        bornées ici avant d'écrire quoi que ce soit."""
        entries = a.order["payload"]["inputs"]
        if not isinstance(entries, list) or len(entries) > 64:
            raise _BadOrder("worker.inputs_unreadable")
        sizes = [e.get("size") if isinstance(e, dict) else None for e in entries]
        if any(isinstance(s, bool) or not isinstance(s, int) or s < 0 for s in sizes):
            raise _BadOrder("worker.input_size_unreadable")
        if sum(sizes) > work.MAX_BLOB:
            raise _BadOrder("worker.inputs_too_big", size=sum(sizes) // 1024 ** 2,
                            limit=work.MAX_BLOB // 1024 ** 2)
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free - sum(sizes) < DISK_MARGIN:
            raise _NoRoom()
        found = {}
        owner = peers.get(a.peer_id)
        chunk = work.BLOB_CHUNK if owner and route.lan_first(owner) else work.INTERNET_CHUNK
        for n, entry in enumerate(entries, 1):
            sha, size = str(entry.get("sha256") or ""), entry["size"]
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise _BadOrder("worker.input_hash_unreadable")
            target = folder / f"entree-{n}"
            received, digest = 0, hashlib.sha256()
            with open(target, "wb") as f:
                while received < size:
                    if a.stop.is_set():
                        raise registry.Interrupted()
                    want = min(chunk, size - received)
                    reply = self._chunk(a, received, want, sha256=sha)
                    text = reply.get("data") or ""
                    if not isinstance(text, str) or len(text) > 4 * ((want + 2) // 3):
                        raise _BadOrder("worker.input_chunk_too_long")
                    data = b64d(text)
                    if len(data) > want:
                        raise _BadOrder("worker.input_chunk_too_long")
                    if not data:
                        break
                    f.write(data)
                    digest.update(data)
                    received += len(data)
            if received != size or digest.hexdigest() != sha:
                raise _BadOrder("worker.input_altered")
            found[str(entry.get("slot"))] = str(target)
        return found

    def _chunk(self, a: Assignment, offset: int, size: int, sha256: str | None = None) -> dict:
        """Un morceau du fichier. Coupure (4G, relais, changement de réseau) : redemandé au
        même endroit après une pause qui s'allonge, tant que le bail tient (le
        renouvellement continue à côté) et que personne n'arrête l'unité."""
        asked = {"unit": a.order["id"], "offset": offset, "size": size}
        if sha256:                        # une entrée d'un travail de moteur
            asked["sha256"] = sha256
        for delay in (*CHUNK_RETRY_DELAYS, None):
            try:
                return self._call(a, "work/blob", asked, timeout=60)
            except PeerError as exc:
                if delay is None or a.stop.is_set():
                    raise
                log.info("Unité %s de « %s » : transfert coupé à %s octets (%s), reprise "
                         "dans %s s", a.order["id"], a.peer_name, offset, exc, delay)
                if a.stop.wait(delay):
                    raise registry.Interrupted() from exc
        raise PeerError(t("worker.transfer_failed"))

    def _send_result(self, a: Assignment, output, compute_s) -> dict:
        body = {"unit": a.order["id"], "output": wire.pack(output), "compute_s": compute_s}
        # Ce que le propriétaire acceptera, dit dans l'ordre (une version d'avant n'en dit
        # rien, et n'a pas de limite) : au-delà, il refuserait la requête après qu'elle a été
        # envoyée, trois fois, puis le bail expirerait (relecture du 21/10/2026)
        limit = a.order.get("result_max")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            size = _sealed_size(body)
            if size > limit:
                raise _TooBig(_mib(size), _mib(limit))
        # Un gros résultat n'est accepté que de l'adresse connue de cette machine chez le
        # propriétaire (network/body_limit.py) : après un changement d'adresse, son prochain
        # « ping » (toutes les 15 s) la lui apprend, d'où des essais sur une trentaine de s
        for delay in (*RESULT_RETRY_DELAYS, None):
            try:
                return self._call(a, "work/result", body, timeout=60)
            except PeerError:
                if delay is None:
                    raise
                time.sleep(delay)
        return {}

    def _rest_gpu(self, a: Assignment, compute_s):
        """« Puissance prêtée : X % » (node/places.py, 60 % par défaut) : après une unité GPU
        de t secondes, pause de t × (100 − X) / X avant d'en reprendre une (comme BOINC).
        Pendant que l'utilisateur se sert du PC, X vaut au plus IN_USE_GPU_SHARE."""
        share = places.share()
        if self.state.get("state") == "in_use":
            share = min(share, IN_USE_GPU_SHARE)
        if a.resource in GPU_BOUND and 0 < share < 100:
            rest = compute_s * (100 - share) / share
            self._gpu_rest_until = max(self._gpu_rest_until, time.monotonic() + rest)
