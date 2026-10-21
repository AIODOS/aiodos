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

from aiodos.cluster import group as cluster
from aiodos.gateway import catalog
from aiodos.network import placement, remote_models
from aiodos.network.client import PeerError
from aiodos.services import cloud, ollama

log = logging.getLogger("aiodos.gateway")

QUIET = placement.QUIET | {"head"}
UPSTREAM_TIMEOUT = (10, 900)
WATCH_EVERY = 1.0             # outil encore là ? (réponse attendue sans flux)


class GatewayError(Exception):
    """Demande refusée, avec le statut HTTP à rendre et un message lisible."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Disconnected(GatewayError):
    """L'outil est parti avant la réponse : le calcul est arrêté."""

    def __init__(self):
        super().__init__(499, "Outil déconnecté avant la réponse.")


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


def plan(model_id, executor, *, local_only=False, prefer_local=False) -> list:
    """Identifiants concrets à essayer, dans l'ordre (cf. placement.plan)."""
    try:
        models = placement.plan(catalog.internal(model_id), executor)
    except catalog.UnknownModel as exc:
        raise GatewayError(404, str(exc)) from exc
    except placement.NoMachine as exc:
        raise GatewayError(404, str(exc)) from exc
    # Rien vers le cloud par ici, quel que soit le chemin : son envoi se valide dans
    # l'interface, message par message
    models = [m for m in models if not cloud.provider_of(m)]
    if not models:
        raise GatewayError(404, f"« {model_id} » n'est pas un modèle du cercle AIODOS.")
    if local_only:
        models = [m for m in models if is_local(m)]
        if not models:
            raise GatewayError(400, f"Cette demande (images, format imposé, complétion de "
                               f"code…) ne peut être calculée que par cette machine, qui n'a "
                               f"pas « {model_id} ».")
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
        raise GatewayError(502, f"Ollama ne répond pas sur cette machine "
                           f"({exc.__class__.__name__}).") from exc
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
        raise GatewayError(502, f"Ollama ne répond pas sur cette machine "
                           f"({exc.__class__.__name__}).") from exc
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
        raise GatewayError(502, f"Ollama ne répond pas sur cette machine "
                           f"({exc.__class__.__name__}).") from exc
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


def run(models: list, attempt, executor):
    """produce(stop) pour Stream : les machines du plan essayées tour à tour."""
    def produce(stop):
        for kind, value in placement.run(models, lambda m: attempt(m, stop), executor, stop):
            if kind == "route":
                log.info("Passerelle : réponse par « %s »", value)
            yield kind, value
    return produce


class Stream:
    """Calcul bloquant (dans un thread) lu en asynchrone ; close() l'arrête."""

    def __init__(self, produce):
        self.stop = threading.Event()
        self._inbox = asyncio.Queue()
        self._watched = time.monotonic()
        loop = asyncio.get_running_loop()

        def pump():
            def put(item):
                loop.call_soon_threadsafe(self._inbox.put_nowait, item)
            try:
                for item in produce(self.stop):
                    if self.stop.is_set():
                        break
                    put(("item", item))
                put(("end", None))
            except Exception as exc:
                try:
                    put(("error", exc))
                except RuntimeError:          # boucle déjà fermée (arrêt d'AIODOS)
                    pass
        threading.Thread(target=pump, daemon=True, name="aiodos-passerelle").start()

    async def next(self, request=None):
        """Élément suivant ; None à la fin. L'erreur du calcul remonte ici. request : la
        demande d'un outil qui attend sans flux (rien ne lui est encore parti) ; s'il se
        déconnecte, le calcul s'arrête (Disconnected). Une réponse en flux, elle, est
        coupée par Starlette, qui ferme alors le générateur. Un relais vers l'Ollama d'ici
        sans flux ne s'arrête pas en route : Ollama n'envoie rien avant la fin."""
        while True:
            if request is not None and time.monotonic() - self._watched >= WATCH_EVERY:
                self._watched = time.monotonic()      # même quand les mots arrivent vite
                if await request.is_disconnected():
                    self.close()
                    raise Disconnected()
            try:
                kind, value = await asyncio.wait_for(
                    self._inbox.get(), None if request is None else WATCH_EVERY)
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
