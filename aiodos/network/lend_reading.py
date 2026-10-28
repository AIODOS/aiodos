"""Lire des textes pour le contrôle d'une autre machine : le côté prêteur de la vraisemblance
(docs/conception/reseau-public-calcul.md § 8 ; network/spot_check.py chez le demandeur ;
llm/reading.py pour la lecture elle-même).

Une machine qui vient de répondre à une demande de contrôle (sans savoir que c'en est une)
reçoit ensuite des textes à lire sous le même modèle, après les mêmes messages : la réponse
contrôlée et la sienne, dans un ordre tiré au hasard. Elle rend pour chacun un résumé fait de
nombres seulement ({"n", "mean", "window", "eos"}, llm/reading.summarize), ou None pour un
texte qu'elle ne peut pas lire. Deux chemins :

- **mes machines** : l'action en flux « read » du serveur entre machines (p2p_app.py),
  réservée à mes machines (sharing.MINE : jamais un ami), comme une conversation prêtée
  (lending.chat) : le réglage « Prêter mes modèles », une place dans la file
  (lend_queue : signes de vie en attendant), les unités de fond suspendues pendant la
  lecture ; morceaux {"queued": rang}, puis {"readings": […]} ;
- **une machine du réseau public** : POST /public/v1/read, relayée par le compagnon comme
  /public/v1/chat (X-Aiodos-Public : 404 sans elle, PublicOnly), avec la même admission
  (public_lending.admit : prêt aux inconnus permis, partage actif, sur secteur, une place,
  la pause de la puissance prêtée, le seau de tout le public, le modèle installé au nom
  exact, les messages dans leurs bornes) et les mêmes codes. La réponse, en NDJSON comme une
  conversation : {"type": "meta", "model", "version"}, des signes de vie {"type": "wait",
  "stage"} toutes les public_lending.WAIT_S secondes (le compagnon n'attend le début d'une
  réponse que 120 s), puis {"type": "reading", "readings": […]} ou {"type": "error",
  "error": code}. Le temps de lecture compte pour la pause, comme une conversation.

Bornes : MAX_TEXTS textes au plus (A, B, et la réponse d'une première machine de contrôle
quand une seconde tranche), chacun de TEXT_TOKENS jetons au plus (estimés comme le contexte,
public_lending.estimate_tokens) : sinon 413 « too_big ». Seul le moteur intégré sait lire :
sur Ollama, 404 « no_reading » (ou un refus lisible entre mes machines) ; une version
d'avant n'a pas la route ni l'action (404) : le demandeur compare alors les textes seuls.

Rien n'est gardé : ni les messages, ni les textes, ni la clé de l'appelant ; le journal ne
dit que le modèle, le nombre de textes et de jetons lus, et la durée.
"""
import asyncio
import logging
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aiodos import __version__, settings
from aiodos.i18n import t
from aiodos.llm import reading
from aiodos.network import activity, connections, lend_queue, lending, public_lending, sharing
from aiodos.network.client import PeerError
from aiodos.network.public_lending import Refused
from aiodos.services import ollama

log = logging.getLogger("aiodos.net")

PATH = "/public/v1/read"
MAX_TEXTS = 3
TEXT_TOKENS = 2 * public_lending.MAX_TOKENS     # estimés : l'estimation est prudente
KEEPALIVE_S = lending.KEEPALIVE_S


def reads() -> bool:
    """Cette machine sait-elle lire ? (le moteur intégré, pas Ollama)"""
    try:
        return ollama.use_integrated()
    except Exception:  # noqa: BLE001 — réglage illisible : non
        return False


def texts(raw) -> list:
    """Les textes à lire : une liste de 1 à MAX_TEXTS chaînes, chacune de TEXT_TOKENS
    jetons estimés au plus. ValueError (mal formée) ou OverflowError (trop longue)."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_TEXTS \
            or not all(isinstance(text, str) for text in raw):
        raise ValueError("texts")                           # i18n: tel quel (jamais montré)
    if any(len(text) > reading.MAX_CHARS or public_lending.estimate_tokens(text) > TEXT_TOKENS
           for text in raw):
        raise OverflowError("texts")                        # i18n: tel quel (jamais montré)
    return list(raw)


def _logged(model, found, began) -> str:
    # i18n: tel quel (journal) ; des nombres seulement
    return "%s, %d texte(s), %d jetons lus, %.1f s" % (
        model, len(found), sum(r["n"] for r in found if r), time.monotonic() - began)


# ---------------------------------------------------------------- mes machines
def read(manager, sender, payload, stop):
    """L'action en flux « read » (p2p_app.STREAM_HANDLERS) : vérifie la demande tout de
    suite (PeerError), puis rend le générateur des morceaux."""
    if sharing.level(sender) != sharing.MINE:
        raise PeerError(t("lending.read_mine_only"))
    if not settings.get("lend_models"):
        raise PeerError(t("lending.not_lending", name=manager.name()))
    if not reads():
        raise PeerError(t("lending.read_unavailable", name=manager.name()))
    payload = payload if isinstance(payload, dict) else {}
    model = str(payload.get("model") or "")
    messages = lending._clean(payload.get("messages"))
    try:
        wanted = texts(payload.get("texts"))
    except (ValueError, OverflowError):
        raise PeerError(t("lending.read_invalid")) from None
    try:
        installed = {m["name"] for m in ollama.list_models()}
    except Exception as exc:
        raise PeerError(t("lending.ollama_down", name=manager.name())) from exc
    if model not in installed:
        raise PeerError(t("lending.model_gone", model=model, name=manager.name()))

    def answer():
        ticket = lend_queue.enter(sender)            # une à la fois, comme une conversation
        try:
            while not lend_queue.wait(ticket, stop, tick=KEEPALIVE_S):
                yield {"queued": lend_queue.position(ticket)}
            began = time.monotonic()
            manager.executor.interactive_begin()
            try:
                with activity.chat("for", sender, stop):
                    found = reading.read(model, messages, wanted, stop=stop)
            except reading.ReadingError as exc:
                if stop.is_set():
                    return
                raise PeerError(t("lending.read_failed", name=manager.name())) from exc
            except reading.Unavailable as exc:
                raise PeerError(t("lending.read_unavailable", name=manager.name())) from exc
            finally:
                manager.executor.interactive_end()
            log.info("Lecture pour une de mes machines : " + _logged(model, found, began))
            yield {"readings": found}
        except lend_queue.Busy:
            raise PeerError(t("lending.busy", name=manager.name())) from None
        except lend_queue.Cancelled:
            return                                       # elle est partie avant son tour
        finally:
            lend_queue.leave(ticket)
    return answer()


# ------------------------------------------------------------- réseau public
def admit(manager, raw: bytes) -> dict:
    """public_lending.admit, puis les textes : Refused au premier refus, avant tout calcul
    (la place rendue). → {"model", "messages", "options", "texts", "place"}."""
    if public_lending.lending_now(manager) and not reads():
        raise Refused(404, "no_reading")                 # avant de prendre une place
    asked = public_lending.admit(manager, raw)
    try:
        body = public_lending._parse(raw)
        try:
            asked["texts"] = texts(body.get("texts"))
        except ValueError:
            raise public_lending._bad() from None
        except OverflowError:
            raise Refused(413, "too_big") from None
        return asked
    except BaseException:
        asked["place"].release()
        raise


def answer(manager, asked: dict, stop, clock=None):
    """Les événements de la lecture (meta, reading ou error ; les signes de vie,
    public_lending._Stream), comme public_lending.answer : veille, place rendue, pause
    comptée, profil public redit."""
    model, place = asked["model"], asked["place"]
    clock = clock if clock is not None else public_lending._Clock()
    began = time.monotonic()
    done, why = threading.Event(), []
    number = public_lending.begin(manager, model)
    try:
        yield {"type": "meta", "model": model, "version": __version__}
        if stop.is_set():
            return
        public_lending.watching(manager, stop, done, why)
        found, failed = None, False
        try:
            found = reading.read(model, asked["messages"], asked["texts"], stop=stop)
        except Exception as exc:  # noqa: BLE001 — l'appelant reçoit un code, sans détail
            failed = not stop.is_set()
            if failed:
                # Le type seulement : le message pourrait citer la demande
                # i18n: tel quel (journal)
                log.warning("Lecture pour le réseau public : %s, en échec (%s)", model,
                            type(exc).__name__)
        if why or failed:
            # i18n: tel quel (journal)
            log.info("Lecture pour le réseau public : %s, arrêtée (%s)", model,
                     why[0] if why else "engine")
            yield {"type": "error", "error": why[0] if why else "engine"}
        elif not stop.is_set() and found is not None:
            log.info("Lecture pour le réseau public : " + _logged(model, found, began))
            yield {"type": "reading", "readings": found}
    finally:
        public_lending.end(manager, number, done, clock, place)


def mount(app, manager):
    """La route publique de lecture, dans l'application du serveur entre machines
    (p2p_app.create_p2p_app), déclarée avant ses routes génériques."""

    @app.post(PATH)
    async def public_read(request: Request):
        if not connections.public_key(request.scope):
            raise HTTPException(404)          # comme une route inconnue (cf. PublicOnly)
        raw = await request.body()
        try:
            asked = await asyncio.to_thread(admit, manager, raw)
        except Refused as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status)
        stop, clock = threading.Event(), public_lending._Clock()
        return public_lending._Stream(answer(manager, asked, stop, clock), asked["place"], stop,
                                      asked["model"], clock)
