"""Réponses de la passerelle : quelle machine, par quel chemin, et le pont entre le
calcul (bloquant, dans un thread) et la réponse HTTP (asynchrone).

Sur cette machine, la demande d'un outil est relayée telle quelle à l'Ollama local
(local_raw) : outils (tool calling), images, format imposé, tout y passe, et la réponse
revient octet pour octet. Quand le moteur intégré tient lieu d'Ollama (llama.cpp,
services/ollama.use_integrated), engine.py la sert de même : par son routeur, dont l'API est
celle d'OpenAI, ou par la façade pour une conversation en texte. Une machine appairée, elle,
ne reçoit qu'une conversation en texte, par le protocole de prêt (network/lending.py) : l'API
qui a reçu la demande remet la réponse au format de l'outil. D'où, selon la demande :
  - local_only : elle n'a de sens qu'ici (images, format imposé, complétion de code) ;
  - prefer_local : cette machine d'abord (outils), une autre machine répondant sans eux.
"""
import asyncio
import json
import logging
import threading
import time

import requests

from aiodos import i18n
from aiodos.cluster import group as cluster
from aiodos.gateway import catalog
from aiodos.i18n import t
from aiodos.network import placement, public_chat, remote_models
from aiodos.network.client import PeerError
from aiodos.services import ollama

log = logging.getLogger("aiodos.gateway")

QUIET = placement.QUIET | {"head"}
UPSTREAM_TIMEOUT = (10, 900)
WATCH_EVERY = 1.0             # outil encore là ? (réponse attendue sans flux)


class GatewayError(Exception):
    """Demande refusée, avec le statut HTTP à rendre, un message lisible et, pour les applis,
    un code stable (gateway/errors.py ; sans lui, celui du statut)."""

    def __init__(self, status: int, message: str, code: str | None = None, detail=None,
                 param=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail
        self.param = param


class Disconnected(GatewayError):
    """L'outil est parti avant la réponse : le calcul est arrêté."""

    def __init__(self):
        super().__init__(499, t("gateway_api.disconnected"))


class Upstream(Exception):
    """L'Ollama de cette machine a répondu par une erreur : rendue telle quelle."""

    def __init__(self, status: int, body: bytes, content_type: str):
        super().__init__(body[:300].decode("utf-8", "replace"))
        self.status = status
        self.body = body
        self.content_type = content_type


def is_local(model) -> bool:
    """Un modèle de l'Ollama de cette machine (ni prêté, ni réparti)."""
    return not (remote_models.parse(model) or cluster.parse(model))


def circle_of(model) -> str:
    """Le cercle de la machine qui répondrait avec cet identifiant concret : here, mine
    (une de mes machines, ou un gros modèle réparti entre elles), friends ou public (une
    machine du réseau public : jamais dans le plan de la passerelle aujourd'hui, cf.
    scopes.SERVED)."""
    if public_chat.parse(model):
        return "public"
    parsed = remote_models.parse(model)
    if parsed:
        return "friends" if placement.destination(model) == "friend" else "mine"
    return "mine" if cluster.parse(model) else "here"


def route_info(model) -> dict:
    """Qui répond avec cet identifiant concret : {"model", "machine", "place"} (en-têtes
    X-Aiodos-Model, X-Aiodos-Machine, X-Aiodos-Place)."""
    from aiodos.gateway import model_card
    place = {"here": "here", "mine": "mine", "friends": "friend",
             "public": "public"}[circle_of(model)]
    machine = placement.machine_of(model) if place != "here" else model_card._machine_name()
    return {"model": model, "machine": machine, "place": place}


def plan(model_id, executor, *, local_only=False, prefer_local=False, where=None,
         tools=False, require_tools=False) -> list:
    """Identifiants concrets à essayer, dans l'ordre (cf. placement.plan).

    where : les cercles permis à l'appli (None : pas de filtre, clé générale). Un modèle
    désigné précisément hors de ces cercles : 403 where_denied ; le choix automatique, lui,
    ne garde que les machines permises. tools et require_tools : seule cette machine reçoit
    les outils ; sans elle, 400 tools_unavailable plutôt qu'une réponse sans outils."""
    try:
        internal = catalog.internal(model_id)
        models = placement.plan(internal, executor)
    except catalog.UnknownModel as exc:
        raise GatewayError(404, str(exc), code="model_not_found", param="model") from exc
    except placement.NoMachine as exc:
        raise GatewayError(404, str(exc), code="model_not_found", param="model") from exc
    if where is not None:
        allowed = [m for m in models if circle_of(m) in where]
        if not allowed:
            if placement.parse(internal) is None:          # une machine désignée
                raise GatewayError(403, t("gateway_api.where_refused_model", model=model_id),
                                   code="where_denied", detail={"allowed": sorted(where)})
            raise GatewayError(404, t("gateway_api.no_allowed_machine", model=model_id),
                               code="model_not_found", param="model")
        models = allowed
    if local_only:
        models = [m for m in models if is_local(m)]
        if not models:
            raise GatewayError(400, t("gateway_api.local_only", model=model_id),
                               code="local_only", param="model")
    elif tools and require_tools:
        models = [m for m in models if is_local(m)]
        if not models:
            raise GatewayError(400, t("gateway_api.tools_elsewhere", model=model_id),
                               code="tools_unavailable", param="tools")
    elif prefer_local:
        models.sort(key=lambda m: not is_local(m))
    return models


def status_of(exc) -> int:
    if isinstance(exc, GatewayError):
        return exc.status
    if isinstance(exc, PeerError):
        return 502                       # la machine appairée n'a pas pu répondre
    return 500


def local_raw(path: str, body: dict, stop):
    """POST vers l'Ollama de cette machine, réponse relayée telle quelle : ("head", type
    de contenu), puis ("raw", octets) au fil de l'eau."""
    try:
        response = requests.post(ollama.base() + path, json=body, stream=True,
                                 timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        raise GatewayError(502, t("gateway_api.ollama_down",
                                  error=exc.__class__.__name__)) from exc
    with response:
        kind = response.headers.get("content-type", "application/json")
        if response.status_code >= 400:
            raise Upstream(response.status_code, response.content, kind)
        yield "head", kind
        for chunk in response.iter_content(chunk_size=None):
            if stop.is_set():
                return
            if chunk:
                yield "raw", chunk


def local_generate(body: dict, stop):
    """/api/generate de cette machine lu ligne à ligne (complétion de texte brut, cf.
    openai_api) : ("thinking" | "content", texte), puis ("stats", …)."""
    try:
        response = requests.post(ollama.base() + "/api/generate", json={**body, "stream": True},
                                 stream=True, timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        raise GatewayError(502, t("gateway_api.ollama_down",
                                  error=exc.__class__.__name__)) from exc
    with response:
        if response.status_code >= 400:
            raise GatewayError(response.status_code,
                               error_text(response.content, response.status_code))
        for line in response.iter_lines():
            if stop.is_set():
                return
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                raise RuntimeError(obj["error"])
            if obj.get("thinking"):
                yield "thinking", obj["thinking"]
            if obj.get("response"):
                yield "content", obj["response"]
            if obj.get("done"):
                yield "stats", {"prompt_tokens": obj.get("prompt_eval_count") or 0,
                                "completion_tokens": obj.get("eval_count") or 0,
                                "done_reason": obj.get("done_reason") or "stop"}
                return


def local_get(path: str):
    """GET vers l'Ollama de cette machine : (statut, octets, type de contenu)."""
    return _local("GET", path, None, 15)


def local_post(path: str, body: dict, timeout=180):
    """POST sans flux vers l'Ollama de cette machine : (statut, octets, type de contenu)."""
    return _local("POST", path, {k: v for k, v in body.items() if v is not None}, timeout)


def _local(method, path, body, timeout):
    try:
        response = requests.request(method, ollama.base() + path, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise GatewayError(502, t("gateway_api.ollama_down",
                                  error=exc.__class__.__name__)) from exc
    return (response.status_code, response.content,
            response.headers.get("content-type", "application/json"))


def error_text(content: bytes, status: int) -> str:
    """Message d'erreur d'Ollama ({"error": "…"}) ou, à défaut, le statut."""
    try:
        return json.loads(content).get("error") or f"HTTP {status}"
    except (ValueError, AttributeError):
        return f"HTTP {status}"


def remote(model, messages, think, options, stop):
    """Conversation en texte calculée ailleurs que par l'Ollama d'ici : une machine
    appairée, qui donne ensuite ("stats", …) si elle sait le dire, ou un gros modèle
    réparti entre mes machines."""
    if cluster.parse(model):
        yield from cluster.chat_stream(model, messages, think, stop, options=options,
                                       stats=True)
    else:
        yield from remote_models.chat_stream(model, messages, think, stop, options=options,
                                             stats=True)


def run(models: list, attempt, executor, interactive: bool = True):
    """produce(stop) pour Stream : les machines du plan essayées tour à tour. interactive :
    faux pour une appli en priorité basse (rien n'est suspendu ici pour elle)."""
    def produce(stop):
        for kind, value in placement.run(models, lambda m: attempt(m, stop), executor, stop,
                                         interactive=interactive):
            if kind == "route":
                log.info("Passerelle : réponse par « %s »", value)
            yield kind, value
    return produce


def for_app(request) -> dict:
    """Ce que la requête d'une appli impose au calcul : {"where", "interactive", "cut"}
    (cut : le signal d'arrêt de l'appli, levé si on la retire)."""
    from aiodos.gateway import apps, scopes
    context = getattr(request.state, "aiodos_app", None)
    cut = apps.stop_event(context.id) if context is not None and context.kind != "general" \
        else None
    return {"interactive": scopes.priority_of(request) == "interactive", "cut": cut}


class Stream:
    """Calcul bloquant (dans un thread) lu en asynchrone ; close() l'arrête. cut : un
    signal qui, levé (appli retirée), l'arrête aussi."""

    def __init__(self, produce, cut: threading.Event | None = None):
        self.stop = threading.Event()
        self.cut = cut
        self._inbox = asyncio.Queue()
        self._watched = time.monotonic()
        loop = asyncio.get_running_loop()
        lang = i18n.current()

        def pump():
            def put(item):
                loop.call_soon_threadsafe(self._inbox.put_nowait, item)
            try:
                for item in produce(self.stop):
                    if self.stop.is_set():
                        break
                    if cut is not None and cut.is_set():
                        self.stop.set()
                        put(("error", GatewayError(401, t("gateway_errors.key_revoked"),
                                                   code="key_revoked")))
                        return
                    put(("item", item))
                put(("end", None))
            except Exception as exc:
                try:
                    put(("error", exc))
                except RuntimeError:          # boucle déjà fermée (arrêt d'AIODOS)
                    pass

        def in_language():                    # les messages du calcul : ceux de la requête
            with i18n.using(lang):
                pump()
        threading.Thread(target=in_language, daemon=True, name="aiodos-passerelle").start()

    async def next(self, request=None):
        """Élément suivant ; None à la fin. L'erreur du calcul remonte ici. request : la
        demande d'un outil qui attend sans flux (rien ne lui est encore parti) ; s'il se
        déconnecte, le calcul s'arrête (Disconnected). Une réponse en flux, elle, est
        coupée par Starlette, qui ferme alors le générateur. Un relais vers l'Ollama d'ici
        sans flux ne s'arrête pas en route : Ollama n'envoie rien avant la fin."""
        watched = request is not None or self.cut is not None
        while True:
            if self.cut is not None and self.cut.is_set():
                self.close()                          # appli retirée : tout de suite
                raise GatewayError(401, t("gateway_errors.key_revoked"), code="key_revoked")
            if request is not None and time.monotonic() - self._watched >= WATCH_EVERY:
                self._watched = time.monotonic()      # même quand les mots arrivent vite
                if await request.is_disconnected():
                    self.close()
                    raise Disconnected()
            try:
                kind, value = await asyncio.wait_for(
                    self._inbox.get(), WATCH_EVERY if watched else None)
                break
            except TimeoutError:
                continue
        if kind == "error":
            raise value
        return value if kind == "item" else None

    async def head(self, request=None) -> list:
        """Éléments jusqu'au premier qui engage la réponse (inclus) : tant qu'il n'est pas
        là, rien n'est parti vers l'outil, et une erreur garde son vrai statut HTTP."""
        items = []
        while True:
            item = await self.next(request)
            if item is None:
                return items
            items.append(item)
            if item[0] not in QUIET:
                return items

    def close(self):
        self.stop.set()


def answered_by(items) -> str | None:
    """Dernier identifiant concret annoncé (« route ») dans ces éléments."""
    model = None
    for kind, value in items:
        if kind == "route":
            model = value
    return model
