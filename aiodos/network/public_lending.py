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
     fais pour eux, executor.busy), dans la limite de la puissance prêtée (après une
     conversation de t s, pause de t × (100 − X) / X, comme worker._rest_gpu) et de la
     mémoire vive (places.memory_room), et au plus PER_MINUTE demandes par minute pour tout
     le public (seau de gateway/limits.py) : sinon 503 {"error": "busy"} ;
  3. le modèle de conversation installé ici, au nom exact : sinon 404 {"error": "no_model"} ;
  4. la demande dans les bornes : du texte seulement, un contexte de CONTEXT_TOKENS jetons au
     plus (estimés prudemment, sans charger le modèle), une réponse ramenée à MAX_TOKENS
     jetons au plus : sinon 413 {"error": "too_big"}. Une demande mal formée : 400
     {"error": "bad_request"}.

La réponse, en flux application/x-ndjson, une ligne JSON par événement : {"type": "meta",
"model", "version"}, puis {"type": "delta", "text"} à chaque morceau, enfin {"type": "done",
"tokens", "seconds"} ; ou, en route, {"type": "error", "error": code} et la fin. Codes :
« busy » (l'utilisateur, mes machines, mes amis ou le travail de fond reprennent cette
machine : ils passent devant et arrêtent le prêt, _watch), « not_lending » (prêt coupé,
machine débranchée…), « engine » (le moteur a failli). Sans réflexion (think faux) : seul le
texte de la réponse part, et la machine ne calcule pas ce que personne ne lira.

Rien n'est gardé : ni la conversation, ni la clé de l'appelant (lue pour savoir que la
demande vient du réseau public, puis oubliée) ; rien en base, et le journal ne dit que le
modèle, le nombre de jetons et la durée.
"""
import asyncio
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
_ASCII_TEXT = re.compile(r"[A-Za-z \t\r\n]+")

_lock = threading.Lock()
_rest = {"until": 0.0}         # pas de nouvelle conversation publique avant (monotonic)


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
    with _lock:
        return time.monotonic() < _rest["until"]


def _rest_after(manager, seconds: float):
    """« Puissance prêtée : X % » (node/places.py) : après une conversation publique de t s,
    pause de t × (100 − X) / X avant d'en prendre une autre (comme worker._rest_gpu) ;
    pendant que l'utilisateur se sert du PC, X vaut au plus worker.IN_USE_GPU_SHARE."""
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
    place = _take_place(manager)
    try:
        body = _parse(raw)
        model = _model(body)
        return {"model": model, "messages": _messages(body.get("messages")),
                "options": _options(body.get("options")), "place": place}
    except BaseException:
        place.release()
        raise


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


def answer(manager, asked: dict, stop):
    """Les événements de la réponse (meta, delta…, puis done ou error). `stop` est levé si
    l'appelant part, ou par _watch ; la place est rendue à la fin du calcul."""
    model, place = asked["model"], asked["place"]
    started = time.monotonic()
    done, why = threading.Event(), []
    try:
        yield {"type": "meta", "model": model, "version": __version__}
        if stop.is_set():
            return
        threading.Thread(target=_watch, args=(manager, stop, done, why), daemon=True,
                         name="aiodos-public-veille").start()
        tokens, counted, failed = 0, None, False
        try:
            for kind, value in ollama.chat_stream(model, asked["messages"], think=False,
                                                  stop=stop, options=asked["options"],
                                                  stats=True):
                if stop.is_set():
                    break
                if kind in ("content", "thinking"):
                    tokens += 1
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
            # i18n: tel quel (journal)
            log.info("Conversation pour le réseau public : %s, %d jetons, %s s", model,
                     produced, seconds)
            yield {"type": "done", "tokens": produced, "seconds": seconds}
    finally:
        done.set()
        _rest_after(manager, time.monotonic() - started)
        place.release()


class _Stream(StreamingResponse):
    """La réponse en NDJSON ; le calcul (bloquant) tourne dans un thread. Quoi qu'il arrive à
    la réponse (fin, appelant parti, erreur d'envoi), le calcul s'arrête (stop), et la place
    est rendue : par le calcul quand il finit, ou ici s'il n'a jamais commencé."""

    def __init__(self, events, place: _Place, stop):
        self._events, self._place, self._stop = events, place, stop
        super().__init__(self._lines(), media_type="application/x-ndjson")

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
        try:
            while (event := await inbox.get()) is not None:
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode()
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
        stop = threading.Event()
        return _Stream(answer(manager, asked, stop), asked["place"], stop)


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
