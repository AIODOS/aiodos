"""Prêter ma puissance aux conversations d'inconnus : le côté prêteur du réseau public qui
calcule (docs/conception/reseau-public-calcul.md ; décisions de l'auteur du 27/10/2026).

Un inconnu du réseau public fait calculer sa conversation par cette machine, sans dire qui
il est (une clé jetable par conversation). Le compagnon (aiodos-net) la reçoit par son ALPN
public, la borne (une à la fois et 30 par heure par clé, deux à la fois et 120 par minute
pour tout le public, 256 Kio) et la relaie ici, depuis cette machine, sur POST
/public/v1/chat, avec l'en-tête X-Aiodos-Public (la clé jetable de l'appelant), jamais
X-Aiodos-Peer. Cet en-tête n'est cru que d'une connexion venue de cette machine
(connections.public_key) : sans lui, toute route /public/… répond 404, comme une route
inconnue ; avec lui, aucune route /p2p/… n'est atteinte (PublicOnly), une demande d'un
inconnu ne passe jamais pour celle d'une machine appairée.

Activé par défaut (réglage lend_public, « Prêter ma puissance aux conversations
d'inconnus »), après mes machines et mes amis, jamais sur batterie ; un téléphone seulement
en charge et en Wi-Fi, comme pour un ami (worker.friends_welcome). Vérifications, dans
l'ordre, chacune avec sa réponse, avant tout calcul (admit) :

  1. le prêt aux inconnus permis, le partage actif (worker.sharing_state), sur secteur :
     sinon 503 {"error": "not_lending"} ;
  2. une place : au plus une conversation publique à la fois, et seulement si personne
     d'autre ne veut cette machine (_taken : conversation de l'utilisateur ici,
     executor.interactive ; conversation de mes machines ou de mes amis, servie ou en
     attente, lend_queue ; calcul de fond sur la carte graphique, le mien ou celui que je
     fais pour eux, executor.busy), dans la limite de la puissance prêtée (après t s de
     calcul, pause de t × (100 − X) / X, comme worker._rest_gpu ; cf. _Clock : la lecture
     de la demande compte, le chargement du modèle non, 48 s sur 52,5 s lors de l'essai
     réel du 28/10/2026) et de la mémoire vive (places.memory_room), et au plus
     PER_MINUTE demandes par minute pour tout le public (seau de gateway/limits.py) : sinon
     503 {"error": "busy"} ;
  3. le modèle de conversation installé ici, au nom exact : sinon 404 {"error": "no_model"} ;
  4. la demande dans les bornes : du texte seulement, un contexte de CONTEXT_TOKENS jetons au
     plus (estimés prudemment, sans charger le modèle), une réponse ramenée à MAX_TOKENS
     jetons au plus : sinon 413 {"error": "too_big"}. Une demande mal formée : 400
     {"error": "bad_request"}.

La réponse, en flux application/x-ndjson, une ligne JSON par événement : {"type": "meta",
"model", "version"}, puis {"type": "delta", "text"} à chaque morceau, enfin {"type": "done",
"tokens", "seconds"} ; ou, en route, {"type": "error", "error": code} et la fin. Avant le
premier morceau, toutes les WAIT_S secondes, un signe de vie {"type": "wait", "stage":
"loading" (le moteur charge le modèle) ou "reading" (il lit la demande)} : le demandeur
attend alors plus longtemps son premier mot (public_chat.py) ; une version d'avant ignore ce
type (essai réel du 28/10/2026 : 45 à 53 s de chargement, pour 60 s d'attente). Codes :
« busy » (l'utilisateur, mes machines, mes amis ou le travail de fond reprennent cette
machine : ils passent devant et arrêtent le prêt, _watch), « not_lending » (prêt coupé,
machine débranchée…), « engine » (le moteur a failli). Sans réflexion (think faux) : seul le
texte de la réponse part, et la machine ne calcule pas ce que personne ne lira.

Rien n'est gardé : ni la conversation, ni la clé de l'appelant (lue pour savoir que la
demande vient du réseau public, puis oubliée) ; rien en base, et le journal ne dit que le
modèle, le nombre de jetons et la durée. Pendant le calcul, en mémoire seulement, le modèle
et l'heure du début, pour « Machines connectées » (serving, activity.public_live).

La lecture de textes pour le contrôle d'un demandeur (POST /public/v1/read,
network/lend_reading.py) passe par la même admission (admit) et la même tenue de compte du
calcul (begin, watching, end : en cours, veille, pause, place, profil).

Le profil public (« free ») est redit au compagnon, en fond, au début et à la fin de chaque
conversation publique, et à la fin de la pause qui suit (_announce, Public.push) : les
demandeurs voient en quelques secondes cette machine occupée, puis libre, au lieu
d'attendre la synchronisation toutes les 15 s.
"""
import asyncio
import itertools
import json
import logging
import math
import re
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aiodos import __version__, settings
from aiodos.gateway import limits
from aiodos.gateway.errors import ApiError
from aiodos.jobs.models import GPU_BOUND
from aiodos.network import connections, lend_queue
from aiodos.node import android, places, power, speed
from aiodos.services import ollama

log = logging.getLogger("aiodos.net")

BASE = "/public/"
ROLES = {"system", "user", "assistant"}
MAX_MESSAGES = 400
MAX_MODEL_NAME = 200
CONTEXT_TOKENS = 8192          # contexte d'une demande, au plus (estimé)
MAX_TOKENS = 2048              # réponse, au plus : « max_tokens » y est ramené
MESSAGE_TOKENS = 4             # balises d'un message dans le modèle de conversation
LETTERS_PER_TOKEN = 3          # lettres et espaces ASCII par jeton, au moins (estimation)
MAX_TEMPERATURE = 2.0
SEED_RANGE = (-1, 2 ** 32 - 1)  # -1 : au hasard (llama.cpp)
PER_MINUTE = limits.PER_MINUTE  # demandes par minute pour tout le public (120)
BUCKET = "réseau public"       # i18n: tel quel (seau de débit de tout le public, jamais montré)
WATCH_S = 0.25                 # s entre deux regards : quelqu'un d'autre veut-il la machine ?
CHECK_S = 5                    # s entre deux lectures du partage (alimentation, Wi-Fi…)
WAIT_S = 5                     # s entre deux signes de vie, avant le premier morceau
STAGE_TIMEOUT = 2              # s pour savoir si le modèle est chargé (sinon : « loading »)
_ASCII_TEXT = re.compile(r"[A-Za-z \t\r\n]+")

_lock = threading.Lock()
_rest = {"until": 0.0}         # pas de nouvelle conversation publique avant (monotonic)
_serving = {}                  # conversation servie en ce moment -> (modèle, début), rien d'autre
_numbers = itertools.count(1)


def serving() -> list:
    """Les conversations servies en ce moment au réseau public (« Machines connectées »,
    activity.public_live) : [{"model", "since"}], le modèle et l'heure du début (time.time),
    jamais le texte ni la clé de l'appelant, que ce module ne garde pas."""
    with _lock:
        return [{"model": model, "since": since}
                for model, since in sorted(_serving.values(), key=lambda s: s[1])]


class Refused(Exception):
    """Une demande refusée avant tout calcul : son statut HTTP et son code."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


def forget():
    """Plus de pause en cours (essais)."""
    with _lock:
        _rest["until"] = 0.0


# ------------------------------------------------------------ prêter, ou non
def lending_now(manager) -> bool:
    """Cette machine prête-t-elle aux inconnus en ce moment ? Le réglage, le partage actif
    (worker.sharing_state), jamais sur batterie ; un téléphone en charge et en Wi-Fi."""
    if not settings.get("lend_public"):
        return False
    from aiodos.network import worker
    held = getattr(getattr(manager, "worker", None), "state", None)
    if not worker.sharing_state(held)["active"]:
        return False
    supply = power.read()
    # Alimentation inconnue : le secteur sur un PC (pacing.py), la batterie sur un téléphone
    if supply["plugged"] is False or (supply["plugged"] is None and supply["mobile"]):
        return False
    return not android.is_android() or worker.friends_welcome()


def _taken(manager) -> bool:
    """Quelqu'un d'autre veut-il cette machine ? L'utilisateur (une conversation ici), mes
    machines ou mes amis (une conversation servie ou en attente), le travail de fond sur la
    carte graphique (le mien, ou celui que je calcule pour eux)."""
    executor = getattr(manager, "executor", None)
    if executor is not None:
        if executor.interactive:
            return True
        busy = getattr(executor, "busy", None) or {}
        if any(busy.get(name, 0) for name in GPU_BOUND):
            return True
    return lend_queue.waiting() > 0


def _resting() -> bool:
    return _rest_left() > 0


def _rest_left() -> float:
    """Secondes de pause qui restent (0 : aucune)."""
    with _lock:
        return max(0.0, _rest["until"] - time.monotonic())


class _Clock:
    """Le temps de calcul d'une conversation publique, que la pause compte (_rest_after) :
    depuis que le modèle est chargé, lecture de la demande comprise ; le chargement du modèle
    n'en est pas (relecture du 28/10/2026 : compté du premier jeton au dernier, il laissait de
    côté la lecture, et un inconnu qui envoyait ~8 000 jetons avec max_tokens = 1 faisait
    calculer cette machine sans pause).

    since : le début de la demande, avancé à la fin de chaque lecture de l'étape du moteur qui
    le dit encore en train de charger le modèle (_Stream._now, une lecture toutes les WAIT_S s
    tant que rien n'est parti). Le chargement n'est donc pas compté, à WAIT_S s près, en trop
    plutôt qu'en moins : sans aucune lecture (premier mot en moins de WAIT_S s), toute la
    demande compte ; un appelant parti pendant le chargement (les lectures s'arrêtent avec la
    réponse) fait compter le reste du chargement."""

    def __init__(self):
        self.since = time.monotonic()

    def loading(self, at: float):
        """Une lecture finie à `at` (monotonic) disait le modèle encore en chargement."""
        self.since = max(self.since, at)

    def counted(self) -> float:
        return max(0.0, time.monotonic() - self.since)


def _rest_after(manager, seconds: float):
    """« Puissance prêtée : X % » (node/places.py) : après t s de calcul (depuis que le
    modèle est chargé, lecture de la demande comprise : _Clock ; le chargement n'en est pas),
    pause de t × (100 − X) / X avant une autre conversation publique (comme
    worker._rest_gpu) ; pendant que l'utilisateur se sert du PC, X vaut au plus
    worker.IN_USE_GPU_SHARE."""
    from aiodos.network import worker
    share = places.share()
    state = getattr(getattr(manager, "worker", None), "state", None) or {}
    if state.get("state") == "in_use":
        share = min(share, worker.IN_USE_GPU_SHARE)
    if 0 < share < 100 and seconds > 0:
        with _lock:
            _rest["until"] = max(_rest["until"],
                                 time.monotonic() + seconds * (100 - share) / share)


def _room(manager) -> bool:
    return not _resting() and not _taken(manager) and places.memory_room()


def free(manager) -> bool:
    """Une conversation publique peut-elle être prise maintenant ? (profil public)"""
    return lending_now(manager) and _room(manager) and lend_queue.idle()


def _announce(manager, delay: float = 0.0):
    """Le profil public redit au compagnon, en fond (Public.push), tout de suite ou dans
    `delay` secondes (la fin d'une pause) : jamais d'attente pour la réponse."""
    push = getattr(getattr(manager, "public", None), "push", None)
    if push is None:
        return
    if delay <= 0:
        push()
        return
    timer = threading.Timer(delay, push)
    timer.daemon = True
    timer.start()


def profile_fields(manager) -> dict:
    """Ce que le profil public dit du prêt (network/public.py) : « lends » (cette machine
    prête aux inconnus), « free » (1 : une conversation peut être prise maintenant, 0 sinon)
    et, si elle est connue, « tps » (jetons par seconde, node/speed.py)."""
    lends = lending_now(manager)
    fields = {"lends": lends, "free": int(lends and _room(manager) and lend_queue.idle())}
    tps = speed.median()
    if tps is not None:
        fields["tps"] = tps
    return fields


# ------------------------------------------------------------------ admission
class _Place:
    """La place prise dans la file des conversations prêtées (lend_queue), rendue une seule
    fois : par le calcul quand il finit, ou par la réponse s'il n'a jamais commencé."""

    def __init__(self, ticket):
        self._ticket = ticket
        self._lock = threading.Lock()
        self.started = False

    def release(self):
        with self._lock:
            ticket, self._ticket = self._ticket, None
        if ticket is not None:
            lend_queue.leave(ticket)


def _take_place(manager) -> _Place:
    """La place de la conversation publique, prise tout de suite, ou Refused « busy »."""
    try:
        limits.take(BUCKET)
    except ApiError:
        raise Refused(503, "busy") from None
    if not _room(manager):
        raise Refused(503, "busy")
    ticket = lend_queue.take_now(lend_queue.PUBLIC)
    if ticket is None:
        raise Refused(503, "busy")
    return _Place(ticket)


def _bad():
    return Refused(400, "bad_request")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _parse(raw: bytes) -> dict:
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise _bad() from None
    if not isinstance(body, dict) or body.get("v") != 1:
        raise _bad()
    return body


def _model(body: dict) -> str:
    """Le modèle demandé, s'il est installé ici sous ce nom exact et fait pour converser."""
    model = body.get("model")
    if not isinstance(model, str) or not model or len(model) > MAX_MODEL_NAME:
        raise _bad()
    try:
        installed = {m["name"] for m in ollama.list_models()}
    except Exception:
        raise Refused(503, "not_lending") from None      # moteur arrêté : rien à prêter
    if model not in installed:
        raise Refused(404, "no_model")
    caps = ollama.capabilities(model)
    if "embedding" in caps and "completion" not in caps:
        raise Refused(404, "no_model")
    return model


def estimate_tokens(text: str) -> int:
    """Jetons d'un texte, estimés prudemment sans le modèle (le compte exact demanderait de
    le charger) : un pour LETTERS_PER_TOKEN lettres ou espaces ASCII (un texte anglais en
    compte plutôt 4 par jeton, un texte français 3 à 4), un pour tout autre signe : chiffre,
    ponctuation, lettre accentuée, idéogramme (les modèles comptent souvent un jeton par
    chiffre, et un idéogramme en vaut à peu près un)."""
    plain = sum(len(run) for run in _ASCII_TEXT.findall(text))
    return -(-plain // LETTERS_PER_TOKEN) + len(text) - plain


def _messages(raw) -> list:
    """Du texte seulement (ni image ni fichier), un contexte de CONTEXT_TOKENS au plus."""
    if not isinstance(raw, list) or not raw:
        raise _bad()
    if len(raw) > MAX_MESSAGES:
        raise Refused(413, "too_big")
    cleaned, tokens = [], 0
    for message in raw:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str) \
                or message["role"] not in ROLES:
            raise _bad()
        if not isinstance(message.get("content"), str) or set(message) - {"role", "content"}:
            raise Refused(413, "too_big")
        cleaned.append({"role": message["role"], "content": message["content"]})
        tokens += MESSAGE_TOKENS + estimate_tokens(message["content"])
    if tokens > CONTEXT_TOKENS:
        raise Refused(413, "too_big")
    return cleaned


def _options(raw) -> dict:
    """temperature (0 à MAX_TEMPERATURE), max_tokens (ramené à MAX_TOKENS ; MAX_TOKENS
    sans lui), seed (entier, ou null) : en réglages d'échantillonnage (lending.SAMPLING)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _bad()
    options = {"num_predict": MAX_TOKENS}
    temperature = raw.get("temperature")
    if temperature is not None:
        if not _number(temperature):
            raise _bad()
        options["temperature"] = max(0.0, min(MAX_TEMPERATURE, float(temperature)))
    wanted = raw.get("max_tokens")
    if wanted is not None:
        if not _number(wanted):
            raise _bad()
        options["num_predict"] = max(1, min(MAX_TOKENS, int(wanted)))
    seed = raw.get("seed")
    if seed is not None:
        if not isinstance(seed, int) or isinstance(seed, bool) \
                or not SEED_RANGE[0] <= seed <= SEED_RANGE[1]:
            raise _bad()
        options["seed"] = seed
    return options


def admit(manager, raw: bytes) -> dict:
    """Les vérifications, dans l'ordre (cf. plus haut) ; Refused au premier refus, avant
    tout calcul. → {"model", "messages", "options", "place"} : la place est prise."""
    if not lending_now(manager):
        raise Refused(503, "not_lending")
    # La demande lue et bornée d'abord, sans rien prendre : une requête mal formée, trop
    # grosse ou pour un modèle absent ne consomme ni la place ni le seau des BUCKET par minute
    # (des refus rapides en série rendaient le prêteur « occupé » pour tous : relecture du
    # 28/10/2026, comme les deux seaux du compagnon, public_serve.rs)
    body = _parse(raw)
    asked = {"model": _model(body), "messages": _messages(body.get("messages")),
             "options": _options(body.get("options"))}
    asked["place"] = _take_place(manager)
    return asked


# ------------------------------------------------------------------- réponse
def _watch(manager, stop, done, why: list):
    """Pendant le calcul : quelqu'un d'autre veut cette machine (« busy »), ou elle ne prête
    plus (« not_lending ») ; `stop` est alors levé, et `why` dit pourquoi."""
    checked = time.monotonic()
    while not done.wait(WATCH_S):
        reason = None
        if _taken(manager):
            reason = "busy"
        elif time.monotonic() - checked >= CHECK_S:
            checked = time.monotonic()
            try:
                if not lending_now(manager):
                    reason = "not_lending"
            except Exception:  # noqa: BLE001 — une lecture ratée n'arrête pas le prêt
                log.debug("Partage illisible pendant une conversation publique", exc_info=True)
        if reason:
            why.append(reason)
            stop.set()
            return


def begin(manager, model: str) -> int:
    """Un calcul pour le réseau public commence (une conversation, ou une lecture :
    lend_reading.py) : noté en cours (serving : le modèle, l'heure), profil public redit
    (occupée). → son numéro, pour end."""
    number = next(_numbers)
    with _lock:
        _serving[number] = (model, time.time())     # en cours (serving) : le modèle, l'heure
    _announce(manager)                              # occupée : « free » 0 chez les demandeurs
    return number


def watching(manager, stop, done, why: list):
    """La veille du calcul (_watch), dans un fil à part, jusqu'à `done`."""
    threading.Thread(target=_watch, args=(manager, stop, done, why), daemon=True,
                     name="aiodos-public-veille").start()


def end(manager, number: int, done, clock: _Clock, place: _Place):
    """Le calcul `number` est fini : plus en cours, veille arrêtée, pause comptée, place
    rendue, profil public redit (libre, ou en pause, puis libre à la fin de la pause)."""
    with _lock:
        _serving.pop(number, None)
    done.set()
    _rest_after(manager, clock.counted())
    place.release()
    _announce(manager)                              # libre (ou en pause : « free » 0)
    left = _rest_left()
    if left > 0:                                    # et libre à la fin de la pause
        _announce(manager, left + 0.1)


def answer(manager, asked: dict, stop, clock: _Clock | None = None):
    """Les événements de la réponse (meta, delta…, puis done ou error ; les signes de vie,
    _Stream). `stop` est levé si l'appelant part, ou par _watch ; la place est rendue à la
    fin du calcul, et le profil public redit au début et à la fin (_announce). clock : le
    temps de calcul compté pour la pause (_Clock, tenu par _Stream)."""
    model, place = asked["model"], asked["place"]
    clock = clock if clock is not None else _Clock()
    started = time.monotonic()
    began = None                    # premier jeton : la génération commence (journal)
    done, why = threading.Event(), []
    number = begin(manager, model)
    try:
        yield {"type": "meta", "model": model, "version": __version__}
        if stop.is_set():
            return
        watching(manager, stop, done, why)
        tokens, counted, failed = 0, None, False
        try:
            for kind, value in ollama.chat_stream(model, asked["messages"], think=False,
                                                  stop=stop, options=asked["options"],
                                                  stats=True):
                if stop.is_set():
                    break
                if kind in ("content", "thinking"):
                    tokens += 1
                    if began is None:
                        began = time.monotonic()
                if kind == "content" and value:
                    yield {"type": "delta", "text": value}
                elif kind == "stats" and isinstance(value, dict):
                    counted = value.get("completion_tokens")
        except Exception as exc:  # noqa: BLE001 — l'appelant reçoit un code, sans détail
            failed = not stop.is_set()
            if failed:
                # Le type seulement : le message du moteur pourrait citer la demande
                # i18n: tel quel (journal)
                log.warning("Conversation pour le réseau public : %s, moteur en échec (%s)",
                            model, type(exc).__name__)
        if why or failed:
            # i18n: tel quel (journal)
            log.info("Conversation pour le réseau public : %s, arrêtée (%s)", model,
                     why[0] if why else "engine")
            yield {"type": "error", "error": why[0] if why else "engine"}
        elif not stop.is_set():
            seconds = round(time.monotonic() - started, 1)
            produced = counted if isinstance(counted, int) and counted > 0 else tokens
            generating = round(time.monotonic() - began, 1) if began is not None else 0.0
            # i18n: tel quel (journal)
            log.info("Conversation pour le réseau public : %s, %d jetons, %s s de génération, "
                     "%s s de calcul, sur %s s", model, produced, generating,
                     round(clock.counted(), 1), seconds)
            yield {"type": "done", "tokens": produced, "seconds": seconds}
    finally:
        end(manager, number, done, clock, place)


def _stage(model) -> str:
    """Où en est le moteur avant le premier jeton : « reading » (le modèle est chargé, il
    lit la demande) ou « loading » (il le charge, ou on ne sait pas)."""
    try:
        loaded = {m["name"] for m in ollama.loaded_models()}
    except Exception:  # noqa: BLE001 — illisible : on dit qu'il charge
        return "loading"
    return "reading" if model in loaded else "loading"


def _stage_seen(model) -> tuple:
    """(étape, instant monotonic où sa lecture a fini) : _Clock."""
    return _stage(model), time.monotonic()


def _line(event) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


class _Stream(StreamingResponse):
    """La réponse en NDJSON ; le calcul (bloquant) tourne dans un thread. Tant qu'aucun
    morceau n'est parti, un signe de vie toutes les WAIT_S secondes ({"type": "wait",
    "stage"} : cf. le début du module). Quoi qu'il arrive à la réponse (fin, appelant parti,
    erreur d'envoi), le calcul s'arrête (stop), et la place est rendue : par le calcul quand
    il finit, ou ici s'il n'a jamais commencé. Chaque lecture de l'étape qui dit le modèle en
    chargement avance le début du calcul compté pour la pause (_Clock)."""

    def __init__(self, events, place: _Place, stop, model: str, clock: _Clock | None = None):
        self._events, self._place, self._stop, self._model = events, place, stop, model
        self._clock = clock if clock is not None else _Clock()
        self._probe = None                 # lecture de l'étape en cours (_now), une à la fois
        super().__init__(self._lines(), media_type="application/x-ndjson")

    def _seen(self, probe) -> str | None:
        """L'étape lue par une lecture finie (None : en échec) ; « loading » avance _Clock."""
        if probe.cancelled() or probe.exception() is not None:
            return None
        found, at = probe.result()
        if found == "loading":
            self._clock.loading(at)
        return found

    async def _now(self, stage: str) -> str:
        """L'étape du moteur : « reading » une fois vue, on n'y revient pas ; sinon relue, une
        lecture à la fois (le moteur intégré peut tenir son verrou tout un chargement : elles
        s'empileraient dans les fils d'asyncio), STAGE_TIMEOUT s au plus ; plus lente,
        « loading », et elle continue pour le signe suivant."""
        if stage == "reading":
            return stage
        if self._probe is None:
            self._probe = asyncio.ensure_future(asyncio.to_thread(_stage_seen, self._model))
        try:
            await asyncio.wait_for(asyncio.shield(self._probe), STAGE_TIMEOUT)
        except Exception:  # noqa: BLE001 — lente (ou en échec) : on dit qu'il charge
            if self._probe.done():
                self._seen(self._probe)
                self._probe = None
            return "loading"
        probe, self._probe = self._probe, None
        return self._seen(probe) or "loading"

    async def _wait_line(self, stage: str) -> tuple:
        """(étape, ligne) d'un signe de vie."""
        stage = await self._now(stage)
        return stage, _line({"type": "wait", "stage": stage})

    async def _lines(self):
        loop = asyncio.get_running_loop()
        inbox = asyncio.Queue()
        events = self._events

        def pump():
            put = lambda item: loop.call_soon_threadsafe(inbox.put_nowait, item)  # noqa: E731
            try:
                for event in events:
                    put(event)
            except Exception:  # noqa: BLE001 — imprévu : l'appelant reçoit un code
                log.exception("Conversation pour le réseau public en échec")  # i18n: tel quel
                put({"type": "error", "error": "engine"})
            finally:
                put(None)

        self._place.started = True
        threading.Thread(target=pump, daemon=True, name="aiodos-public").start()
        first, stage = False, "loading"
        try:
            while True:
                try:
                    event = await (inbox.get() if first else
                                   asyncio.wait_for(inbox.get(), WAIT_S))
                except asyncio.TimeoutError:          # rien depuis WAIT_S s : un signe de vie
                    stage, line = await self._wait_line(stage)
                    yield line
                    continue
                if event is None:
                    break
                first = first or event.get("type") == "delta"
                yield _line(event)
        finally:
            self._stop.set()              # fin, ou appelant parti : le calcul s'arrête

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._stop.set()
            if not self._place.started:
                self._place.release()


def mount(app, manager):
    """La route publique, dans l'application du serveur entre machines
    (p2p_app.create_p2p_app), déclarée avant ses routes génériques."""

    @app.post("/public/v1/chat")
    async def public_chat(request: Request):
        if not connections.public_key(request.scope):
            raise HTTPException(404)          # comme une route inconnue (cf. PublicOnly)
        raw = await request.body()
        try:
            asked = await asyncio.to_thread(admit, manager, raw)
        except Refused as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status)
        stop, clock = threading.Event(), _Clock()
        return _Stream(answer(manager, asked, stop, clock), asked["place"], stop,
                       asked["model"], clock)


async def _not_found(send):
    data = b'{"detail":"Not Found"}'
    await send({"type": "http.response.start", "status": 404,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": data})


class PublicOnly:
    """Middleware ASGI du serveur entre machines : une requête d'un inconnu (clé jetable
    crue, connections.public_key) n'atteint que les routes /public/… ; une requête sans
    elle n'en atteint aucune. 404 dans les deux cas, comme une route inconnue, avant que
    son corps ne soit lu."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            public = (scope.get("path") or "").startswith(BASE)
            if public != bool(connections.public_key(scope)):
                return await _not_found(send)
        await self.app(scope, receive, send)
