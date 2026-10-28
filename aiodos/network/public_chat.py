"""Faire calculer mes conversations par des inconnus : le côté demandeur du réseau public qui
calcule (docs/conception/reseau-public-calcul.md ; décisions de l'auteur du 27/10/2026). Le
côté prêteur : network/public_lending.py ; le choix de la machine : network/placement.py ;
les accords et la note de la première fois : chat/public_consent.py.

Une machine du réseau public qui prête (« lends », network/public.py) répond à une
conversation de cette machine sans savoir qui la demande : le compagnon (aiodos-net) la lui
confie par une clé jetable, en relais seulement (compagnon/src/public_ask.rs), sur sa route
locale POST /net/v1/public/ask/<clé du prêteur>/public/v1/chat. L'en-tête
X-Aiodos-Conversation y nomme la conversation : un identifiant tiré au hasard ici (tag),
jamais celui de la base, gardé en mémoire seulement ; une nouvelle conversation en a un
nouveau, donc une nouvelle clé jetable, et le prêteur ne peut pas relier deux conversations.
Le compagnon ne le lui transmet pas.

Identifiant concret d'une machine publique dans le choix des machines (placement.py) :
« public:<clé du prêteur>:<modèle> ».

Ce qui part (chat_stream) : le modèle, les messages en texte (rôle et contenu seulement,
prompt système compris) et les options (température, longueur, graine). Jamais d'image ni de
fichier : un message qui en porte est refusé ici, avant tout envoi (une conversation qui a
des pièces jointes reste chez moi, chat/public_consent.py). Une demande qui dépasse les
bornes du prêteur (contexte de public_lending.CONTEXT_TOKENS jetons, estimés de la même
façon ; corps de MAX_BODY octets) ne part pas : il la refuserait.

Ce qui revient : le NDJSON du prêteur (public_lending.py) : meta, delta…, puis done, ou error.
Tant qu'aucun mot n'est arrivé, tout refus (prêteur : 503 busy ou not_lending, 404 no_model,
413 too_big ; compagnon : 502 unreachable, 503 busy ; premier mot pas arrivé en FIRST_WORD_S)
lève PublicError, qui n'a pas de statut HTTP : placement.run passe alors à la machine
suivante. Après le premier mot, une erreur remonte (la réponse est gardée jusque-là, comme
pour mes machines). Le journal ne dit que la clé du prêteur (abrégée) et le code d'un refus,
jamais le texte de la conversation.
"""
import json
import logging
import math
import re
import secrets
import threading
import time
from collections import OrderedDict

import requests

from aiodos.i18n import t
from aiodos.network import client, public_lending, route
from aiodos.network.client import PeerError
from aiodos.network.route import NO_PROXY

log = logging.getLogger("aiodos.net")

PREFIX = "public:"
FEATURE = "public-ask"           # ce que le compagnon annonce savoir faire (compagnon/src/lib.rs)
ROUTE = "/net/v1/public/ask/"
LENDER_PATH = "/public/v1/chat"
CONVERSATION_HEADER = "X-Aiodos-Conversation"
MAX_BODY = 256 * 1024            # corps relayé au plus (compagnon/src/public_serve.rs)
CONNECT_S = 5                    # le compagnon est sur 127.0.0.1
# Premier mot attendu au plus : la recherche et la connexion du compagnon (10 s au plus), une
# clé jetable neuve au premier message d'une conversation (1 à 3 s), le prêteur qui charge
# peut-être son modèle
FIRST_WORD_S = 60
WATCH_S = 0.25
REFUSAL_BYTES = 4096             # refus lu au plus
MAX_TAGS = 2000                  # identifiants de conversation gardés en mémoire
_KEY = re.compile(r"[0-9A-Za-z]{16,128}")
# Codes des refus, chacun avec son message (public_chat.error_<code>)
CODES = {"busy", "not_lending", "no_model", "too_big", "bad_request", "unreachable", "engine",
         "slow", "cut", "unreadable", "documents", "attachments", "companion", "refused"}
# Codes des compagnons (public_ask.rs chez moi, public_serve.rs chez le prêteur, relayés
# tels quels) qui disent la même chose qu'un des nôtres. Le compagnon ne refait jamais
# d'essai (le prêteur ne calcule pas deux fois) : sur « interrupted » ou « timeout », c'est
# placement.run qui passe à la machine suivante, si aucun mot n'est arrivé
ALIASES = {"too_large": "too_big", "timeout": "slow", "too_slow": "slow",
           "interrupted": "cut", "unavailable": "unreachable", "conversation": "bad_request",
           "bad_key": "bad_request", "not_found": "bad_request", "method": "bad_request"}
BY_STATUS = {400: "bad_request", 401: "companion", 404: "no_model", 408: "slow",
             413: "too_big", 502: "unreachable", 503: "busy", 504: "slow"}
# Milliards de paramètres dans un nom de modèle : « :9b », « -0.8b », « 270m » ; pas « a3b »
# (paramètres actifs d'un mélange d'experts) ni « 8x7b »
_PARAMS = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)([bm])(?![a-z0-9])")
# Ce qui ne converse pas : modèles d'embedding, de reclassement, de transcription
_NOT_CHAT = re.compile(r"embed|bge|rerank|minilm|whisper|(?<![a-z])(e5|gte)-")

_lock = threading.Lock()
_tags = OrderedDict()            # conversation -> identifiant tiré au hasard


class PublicError(PeerError):
    """Une machine du réseau public n'a pas répondu, ou a refusé : code (CODES) et message
    lisible. Sans statut HTTP : avant le premier mot, la suivante prend le relais."""

    def __init__(self, code: str):
        code = ALIASES.get(code, code)
        self.code = code if code in CODES else "refused"
        super().__init__(t(f"public_chat.error_{self.code}"))


# ------------------------------------------------------------------ identifiants
def model_id(key: str, name: str) -> str:
    return f"{PREFIX}{key}:{name}"


def parse(model) -> tuple | None:
    """« public:<clé>:<modèle> » → (clé, modèle) ; None pour un autre identifiant. Le nom du
    modèle peut lui-même contenir « : » (qwen3.5:9b)."""
    model = str(model or "")
    if not model.startswith(PREFIX):
        return None
    key, _, name = model[len(PREFIX):].partition(":")
    return (key, name) if _KEY.fullmatch(key) and name else None


def valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY.fullmatch(key))


def params(name) -> float | None:
    """Milliards de paramètres lus dans le nom (« qwen3.5:9b » → 9, « qwen3.5:0.8b » → 0.8,
    « gemma3:270m » → 0.27) ; None s'il n'en dit rien. Le plus grand nombre lu compte."""
    found = [float(n) / (1000 if unit == "m" else 1)
             for n, unit in _PARAMS.findall(str(name or "").lower())]
    found = [n for n in found if math.isfinite(n) and n > 0]
    return max(found) if found else None


def converses(name) -> bool:
    """Un modèle de conversation, d'après son nom (le profil public ne dit que les noms)."""
    return bool(name) and not _NOT_CHAT.search(str(name).lower())


# --------------------------------------------------- l'identifiant de la conversation
def tag(conversation=None) -> str:
    """L'identifiant opaque de cette conversation pour le compagnon (16 à 64 caractères
    [A-Za-z0-9_-]) : tiré au hasard la première fois, le même ensuite, en mémoire seulement.
    Sans conversation : un nouveau à chaque appel."""
    if conversation is None:
        return secrets.token_urlsafe(24)
    with _lock:
        found = _tags.get(conversation)
        if found is None:
            found = _tags[conversation] = secrets.token_urlsafe(24)    # 32 caractères
            while len(_tags) > MAX_TAGS:
                _tags.popitem(last=False)
        else:
            _tags.move_to_end(conversation)
        return found


def forget_conversation(conversation):
    """La conversation est supprimée : son identifiant est oublié."""
    with _lock:
        _tags.pop(conversation, None)


def forget():
    """Tout oublier (essais)."""
    with _lock:
        _tags.clear()


# ------------------------------------------------------------------- le compagnon
def ready():
    """Le compagnon actif, s'il sait confier une conversation au réseau public ; sinon None."""
    companion = route.companion()
    if companion is None or not getattr(companion, "running", False):
        return None
    supports = getattr(companion, "supports", None)
    return companion if supports is not None and supports(FEATURE) else None


# ------------------------------------------------------------------- la demande
def _text_messages(messages) -> list:
    """Les messages en texte seulement (rôle et contenu, rien d'autre) ; une image : refusé
    ici, rien ne part."""
    out = []
    for message in messages or []:
        if not isinstance(message, dict):
            raise PublicError("bad_request")
        if message.get("images"):
            raise PublicError("attachments")
        role, content = message.get("role"), message.get("content")
        if role not in public_lending.ROLES or not isinstance(content, str):
            raise PublicError("bad_request")
        out.append({"role": role, "content": content})
    if not out:
        raise PublicError("bad_request")
    tokens = sum(public_lending.MESSAGE_TOKENS + public_lending.estimate_tokens(m["content"])
                 for m in out)
    if len(out) > public_lending.MAX_MESSAGES or tokens > public_lending.CONTEXT_TOKENS:
        raise PublicError("too_big")
    return out


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _options(options) -> dict:
    """Les options d'Ollama (temperature, num_predict, seed) dans la forme du prêteur."""
    options = options if isinstance(options, dict) else {}
    out = {}
    if _number(options.get("temperature")):
        out["temperature"] = float(options["temperature"])
    wanted = options.get("num_predict", options.get("max_tokens"))
    if _number(wanted) and wanted > 0:
        out["max_tokens"] = int(wanted)
    seed = options.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool):
        out["seed"] = seed
    return out


def body(model, messages, options=None) -> bytes:
    """Le corps envoyé au prêteur ({"v": 1, "model", "messages", "options"}) ; PublicError
    s'il ne peut pas partir (image, fichier, trop long)."""
    parsed = parse(model)
    if parsed is None:
        raise PublicError("bad_request")
    data = json.dumps({"v": 1, "model": parsed[1], "messages": _text_messages(messages),
                       "options": _options(options)}, ensure_ascii=False).encode()
    if len(data) > MAX_BODY:
        raise PublicError("too_big")
    return data


def _refusal(response) -> str:
    """Le code d'un refus ({"error": code} du prêteur ou du compagnon), lu borné ; à défaut,
    celui que dit le statut."""
    code = None
    try:
        raw = response.raw.read(REFUSAL_BYTES, decode_content=False)
        found = json.loads(raw)
        code = found.get("error") if isinstance(found, dict) else None
    except Exception:  # noqa: BLE001 — un refus illisible se lit à son statut
        pass
    if isinstance(code, str):
        code = ALIASES.get(code, code)
        if code in CODES:
            return code
    return BY_STATUS.get(response.status_code, "refused")


def _watch(response, stop, first, done, why: list, deadline: float):
    """Coupe la réponse si la conversation est arrêtée (Stop, client parti) ou si aucun mot
    n'est arrivé à l'échéance : le prêteur arrête alors son calcul (compagnon)."""
    while not done.wait(WATCH_S):
        if stop is not None and stop.is_set():
            why.append("stop")
        elif not first.is_set() and time.monotonic() > deadline:
            why.append("slow")
        else:
            continue
        client._cut(response)
        return


def _event(line: bytes) -> dict:
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        raise PublicError("unreadable") from None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise PublicError("unreadable")
    return event


def chat_stream(model, messages, think=True, stop=None, options=None, stats=False,
                conversation=None):
    """Couples (genre, texte), comme remote_models.chat_stream, calculés par une machine du
    réseau public (« public:<clé>:<modèle> ») : ("content", texte)…, puis ("stats", {…}) si
    stats. Sans réflexion (le prêteur ne la calcule pas). conversation : l'identifiant opaque
    de la conversation (tag) ; sans lui, une clé jetable pour cette seule demande."""
    data = body(model, messages, options)
    key, name = parse(model)
    companion = ready()
    if companion is None:
        raise PublicError("companion")
    if stop is not None and stop.is_set():
        return
    headers = {**companion.headers, CONVERSATION_HEADER: conversation or tag(None),
               "Content-Type": "application/json", "Accept": "application/x-ndjson"}
    started = time.monotonic()
    try:
        response = requests.post(f"http://{companion.listen}{ROUTE}{key}{LENDER_PATH}",
                                 data=data, headers=headers, stream=True,
                                 timeout=(CONNECT_S, FIRST_WORD_S), proxies=NO_PROXY)
    except requests.Timeout as exc:
        raise PublicError("slow") from exc
    except requests.RequestException as exc:
        raise PublicError("unreachable") from exc
    with response:
        if response.status_code >= 400:
            code = _refusal(response)
            log.info("Réseau public : %s… refuse (%s)", key[:10], code)
            raise PublicError(code)
        client._plain(response)
        first, done, why = threading.Event(), threading.Event(), []
        threading.Thread(target=_watch, daemon=True, name="aiodos-public-demande",
                         args=(response, stop, first, done, why,
                               started + FIRST_WORD_S)).start()
        try:
            for line in client._lines(response):
                if stop is not None and stop.is_set():
                    return
                if not line.strip():
                    continue
                event = _event(line)
                kind = event["type"]
                if kind == "meta" and event.get("model") != name:
                    raise PublicError("unreadable")          # un autre modèle que celui demandé
                if kind == "delta":
                    text = event.get("text")
                    if isinstance(text, str) and text:
                        first.set()
                        yield "content", text
                elif kind == "done":
                    if stats:
                        tokens, seconds = event.get("tokens"), event.get("seconds")
                        yield "stats", {
                            "completion_tokens": int(tokens) if _number(tokens) else 0,
                            "seconds": float(seconds) if _number(seconds) else 0.0}
                    return
                elif kind == "error":
                    code = event.get("error")
                    log.info("Réseau public : %s… s'arrête (%s)", key[:10], code)
                    raise PublicError(code if isinstance(code, str) else "refused")
            if stop is not None and stop.is_set():
                return
            raise PublicError("slow" if "slow" in why else "cut")    # flux fini sans « done »
        except client._CUT as exc:
            if stop is not None and stop.is_set():
                return
            raise PublicError("slow" if "slow" in why else "cut") from exc
        finally:
            done.set()
