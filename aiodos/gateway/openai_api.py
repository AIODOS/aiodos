"""API compatible OpenAI (/v1/…) : le client openai pour Python ou JavaScript, Open WebUI,
AnythingLLM (« Generic OpenAI »), Continue, et les éditeurs qui parlent à « un serveur
compatible OpenAI ».

Formats vérifiés le 17/10/2026 dans la référence d'OpenAI (developers.openai.com, spécification
openai-openapi 2.3.0) et dans le code des outils :
- /v1/models : {"object": "list", "data": [{id, object, created, owned_by}]} ; « name » en
  plus, qu'Open WebUI affiche ;
- /v1/chat/completions, en flux SSE (« data: … » suivi d'une ligne vide, morceaux
  chat.completion.chunk, puis « data: [DONE] ») ou non ; usage en dernier morceau si
  stream_options.include_usage ; la réflexion du modèle dans reasoning_content (et
  reasoning, comme Ollama) : aucun champ officiel, ce sont ceux que lisent Open WebUI,
  Continue et AnythingLLM ;
- /v1/completions : texte brut, sans gabarit de conversation (l'autocomplétion de Continue y
  envoie un texte déjà entouré des jetons de complétion de code) ;
- /v1/embeddings : en base64 quand on le demande, ce que font par défaut les clients openai
  pour Python et JavaScript (float32, petit-boutiste) ;
- erreurs : {"error": {"message", "type", "param", "code"}}.

Sur cette machine, une conversation est relayée telle quelle à l'API compatible OpenAI
d'Ollama, ou à celle du routeur du moteur intégré quand il tient lieu d'Ollama (engine.py) :
outils, images, format imposé compris. Une machine appairée reçoit la conversation en texte,
et la réponse est remise ici au format d'OpenAI (Chunks), comme la complétion de texte brut.
"""
import asyncio
import base64
import json
import secrets
import time
from urllib.parse import quote

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from aiodos.gateway import answer, catalog, engine, errors, model_card, scopes
from aiodos.gateway.answer import GatewayError
from aiodos.gateway.errors import ApiError
from aiodos.i18n import t
from aiodos.llm import context as llm_context
from aiodos.network.client import PeerError
from aiodos.services import ollama

router = APIRouter(prefix="/v1")
TEXT_PARTS = {"text", "input_text", "refusal"}
# Paramètres d'OpenAI qui changeraient la réponse et qu'AIODOS ne traite pas : refusés plutôt
# qu'ignorés (une appli doit savoir qu'elle n'a pas eu ce qu'elle demandait)
REFUSED = ("audio", "prediction", "web_search_options")
LOADING_AFTER = 2.0         # s sans premier morceau (modèle à charger) : la réponse part…
LOADING_EVERY = 5.0         # … avec « : aiodos loading <s> » toutes les 5 s


def error(status: int, message: str, code=None, param=None, lang: str | None = None):
    return errors.response("/v1/", errors.from_status(status, message, code=code, param=param),
                           lang)


def api_error(exc) -> ApiError:
    """L'erreur à code stable d'une exception du calcul."""
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, GatewayError):
        return errors.from_status(exc.status, str(exc), code=exc.code, param=exc.param,
                                  detail=exc.detail)
    if isinstance(exc, PeerError):
        return errors.from_status(502, str(exc), code="peer_failed")
    return errors.from_status(answer.status_of(exc), str(exc))


def _failure(exc, lang: str | None = None):
    if isinstance(exc, answer.Upstream):        # Ollama répond déjà au format d'OpenAI
        return Response(exc.body, status_code=exc.status, media_type=exc.content_type)
    return errors.response("/v1/", api_error(exc), lang)


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise GatewayError(400, t("gateway_api.json_unreadable")) from exc
    if not isinstance(body, dict):
        raise GatewayError(400, t("gateway_api.json_not_object"))
    return body


def _executor(request: Request):
    return request.app.state.node.executor


def _route_headers(head=None, model=None) -> dict:
    """X-Aiodos-Model, X-Aiodos-Machine et X-Aiodos-Place : qui répond."""
    model = model or (answer.answered_by(head) if head else None)
    if not model:
        return {}
    info = answer.route_info(model)
    return {"X-Aiodos-Model": quote(model, safe=":@"),
            "X-Aiodos-Machine": quote(info["machine"] or "", safe=""),
            "X-Aiodos-Place": info["place"]}


def _meta(model) -> dict:
    """L'objet « aiodos » du dernier morceau, sans stopped_by."""
    info = answer.route_info(model)
    context = (llm_context.chat_context(model) if info["place"] == "here"
               and ollama.use_integrated() else None)
    return {**info, "context_length": context}


def _lang(request: Request) -> str:
    return errors.lang_of(request.headers)


# ------------------------------------------------------------------ modèles
def _model(entry: dict) -> dict:
    name = f"{entry['name']} · {entry['machine']}" if entry["machine"] else entry["name"]
    return {"id": entry["id"], "object": "model", "created": catalog.epoch(entry["modified_at"]),
            "owned_by": entry["machine"] or "aiodos", "name": name}


def visible(request: Request, entries: list) -> list:
    """Les modèles qu'une appli peut voir : ses droits (chat, embed) et son lieu ; la clé
    générale les voit tous, comme avant."""
    context = request.state.aiodos_app
    if context is None or (context.kind == "general" and not request.headers.get("x-aiodos-where")):
        return entries
    out = []
    for capability in ("chat", "embed"):
        if capability not in context.scopes:
            continue
        where = scopes.for_request(request, capability)
        out += [e for e in model_card.allowed(entries, where) if model_card.kind(e) == capability]
    return [e for e in entries if e in out]


@router.get("/models")
async def models(request: Request):
    try:
        entries = visible(request, await asyncio.to_thread(catalog.entries))
    except ApiError as exc:
        return errors.response("/v1/", exc, _lang(request))
    loaded = await asyncio.to_thread(model_card._loaded)
    return {"object": "list", "data": [model_card.openai_entry(e, loaded) for e in entries]}


@router.get("/models/{model_id:path}")
async def model(request: Request, model_id: str):
    entry = await asyncio.to_thread(catalog.find, model_id)
    try:
        if entry is not None and not visible(request, [entry]):
            entry = None
    except ApiError as exc:
        return errors.response("/v1/", exc, _lang(request))
    if entry is None:
        return error(404, t("gateway_api.model_unknown", model=model_id),
                     code="model_not_found",
                     param="model", lang=_lang(request))
    loaded = await asyncio.to_thread(model_card._loaded)
    return model_card.openai_entry(entry, loaded)


# ------------------------------------------------------------ conversation
def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or p.get("refusal") or "") for p in content
                         if isinstance(p, dict) and p.get("type") in TEXT_PARTS)
    raise GatewayError(400, t("gateway_api.content_unreadable"))


def _parts(messages) -> set:
    """Genres des morceaux de contenu des messages (text, image_url, input_audio…)."""
    return {p.get("type") for m in messages
            if isinstance(m, dict) and isinstance(m.get("content"), list)
            for p in m["content"] if isinstance(p, dict)}


def plain_messages(messages) -> list:
    """La conversation telle qu'une machine appairée la reçoit (cf. lending.py) : rôles
    system, user, assistant, texte seul ; « developer » devient system, un résultat
    d'outil devient un message de l'utilisateur."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            raise GatewayError(400, t("gateway_api.message_invalid"))
        role = str(m.get("role") or "user")
        content = _text(m.get("content"))
        if role == "developer":
            role = "system"
        elif role in ("tool", "function"):
            role, content = "user", t("gateway_prompts.tool_result", content=content)
        elif role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": content})
    return out


def sampling(body: dict) -> dict | None:
    """Réglages d'échantillonnage d'OpenAI → options d'Ollama (cf. lending.SAMPLING)."""
    options = {}
    limit = body.get("max_completion_tokens") or body.get("max_tokens")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        options["num_predict"] = limit
    for key in ("temperature", "top_p", "seed", "presence_penalty", "frequency_penalty"):
        value = body.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            options[key] = value
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        options["stop"] = [stop]
    elif isinstance(stop, list):
        stops = [s for s in stop if isinstance(s, str) and s]
        if stops:
            options["stop"] = stops
    return options or None


def _thinks(body: dict) -> bool:
    return body.get("reasoning_effort") != "none" and body.get("think") is not False


class Chunks:
    """Réponse remise au format d'OpenAI : celle d'une machine appairée, ou une complétion de
    texte brut par cette machine."""

    def __init__(self, model_id: str, kind: str, include_usage=False, limit=None):
        self.id = ("chatcmpl-" if kind == "chat" else "cmpl-") + secrets.token_hex(12)
        self.created = int(time.time())
        self.model_id = model_id
        self.kind = kind                  # « chat » ou « text » (complétion brute)
        self.include_usage = include_usage
        self.limit = limit                # max_tokens demandé, s'il l'est
        self.meta = None                  # objet « aiodos » du dernier morceau (_meta)
        self.stats = {}
        self.content = []
        self.thinking = []

    def _aiodos(self) -> dict | None:
        if self.meta is None:
            return None
        return {**self.meta, "stopped_by": engine.stopped_by(self.finish, self.limit,
                                                             self.usage)}

    @property
    def finish(self) -> str:
        return "length" if self.stats.get("done_reason") == "length" else "stop"

    @property
    def usage(self) -> dict:
        prompt = int(self.stats.get("prompt_tokens") or 0)
        completion = int(self.stats.get("completion_tokens") or 0)
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion}

    def _chunk(self, choice: dict | None, last=False) -> str:
        obj = {"id": self.id, "created": self.created, "model": self.model_id,
               # i18n: tel quel (type d'objet de l'API OpenAI)
               "object": "chat.completion.chunk" if self.kind == "chat" else "text_completion",
               "choices": [choice] if choice is not None else []}
        if self.include_usage:
            obj["usage"] = self.usage if choice is None else None
        if last and self.meta is not None:
            obj["aiodos"] = self._aiodos()
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def _choice(self, delta=None, text="", finish=None) -> dict:
        if self.kind == "chat":
            return {"index": 0, "delta": delta or {}, "logprobs": None, "finish_reason": finish}
        return {"index": 0, "text": text, "logprobs": None, "finish_reason": finish}

    def start(self) -> str | None:
        if self.kind != "chat":
            return None
        return self._chunk(self._choice({"role": "assistant", "content": ""}))

    def take(self, kind: str, value) -> str | None:
        if kind == "route":
            self.meta = _meta(value)
        elif kind == "stats" and isinstance(value, dict):
            self.stats = value
        elif kind == "content":
            self.content.append(value)
            return self._chunk(self._choice({"content": value}, value))
        elif kind == "thinking":
            self.thinking.append(value)
            if self.kind == "chat":
                return self._chunk(self._choice({"reasoning_content": value,
                                                 "reasoning": value}))
        return None

    def end(self) -> list:
        events = [self._chunk(self._choice({}, "", self.finish), last=True)]
        if self.include_usage:
            events.append(self._chunk(None))
        return events + ["data: [DONE]\n\n"]

    def whole(self) -> dict:
        text = "".join(self.content)
        if self.kind == "chat":
            message = {"role": "assistant", "content": text, "refusal": None}
            if self.thinking:
                message["reasoning_content"] = "".join(self.thinking)
            choice = {"index": 0, "message": message, "logprobs": None,
                      "finish_reason": self.finish}
        else:
            choice = {"index": 0, "text": text, "logprobs": None, "finish_reason": self.finish}
        # i18n: tel quel (type d'objet de l'API OpenAI)
        whole = {"id": self.id, "object": "chat.completion" if self.kind == "chat"
                 else "text_completion", "created": self.created, "model": self.model_id,
                 "choices": [choice], "usage": self.usage}
        if self.meta is not None:
            whole["aiodos"] = self._aiodos()
        return whole


def _sse_error(exc) -> str:
    return "data: " + json.dumps(errors.body("/v1/", api_error(exc)),
                                 ensure_ascii=False) + "\n\n"


async def _formatted_events(stream: answer.Stream, head: list, chunks: Chunks):
    """Les morceaux SSE d'une réponse remise au format d'OpenAI."""
    if (first := chunks.start()) is not None:
        yield first
    for item in head:
        if (event := chunks.take(*item)) is not None:
            yield event
    while (item := await stream.next()) is not None:
        if (event := chunks.take(*item)) is not None:
            yield event
    for event in chunks.end():
        yield event


async def _relay_events(stream: answer.Stream, head: list):
    """Les octets d'une réponse relayée telle quelle (moteur de ce PC)."""
    for kind, value in head:
        if kind == "raw":
            yield value
    while (item := await stream.next()) is not None:
        if item[0] == "raw":
            yield item[1]


async def _formatted(stream: answer.Stream, head: list, chunks: Chunks, streaming: bool,
                     request: Request):
    headers = _route_headers(head)
    if not streaming:
        try:
            for item in head:
                chunks.take(*item)
            while (item := await stream.next(request)) is not None:
                chunks.take(*item)
        except Exception as exc:
            return _failure(exc, _lang(request))
        finally:
            stream.close()
        return JSONResponse(chunks.whole(), headers=headers)

    async def events():
        try:
            async for event in _formatted_events(stream, head, chunks):
                yield event
        except Exception as exc:
            yield _sse_error(exc)
        finally:
            stream.close()
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


def _relay(stream: answer.Stream, head: list, content_type: str):
    async def body():
        try:
            async for chunk in _relay_events(stream, head):
                yield chunk
        except Exception as exc:
            yield _sse_error(exc).encode("utf-8")
        finally:
            stream.close()
    return StreamingResponse(body(), media_type=content_type, headers=_route_headers(head))


def _early(stream: answer.Stream, head_task, model: str, chunks: Chunks):
    """Une seule machine peut répondre, et son modèle se charge : la réponse part tout de
    suite, et « : aiodos loading <s> » (un commentaire SSE, que les SDK ignorent) toutes les
    LOADING_EVERY s jusqu'au premier morceau ; le délai d'inactivité des clients repart à
    chaque fois. Une erreur arrive alors dans le flux (data: {"error": …})."""
    async def events():
        started = time.monotonic()
        try:
            while not head_task.done():
                done, _ = await asyncio.wait({head_task}, timeout=LOADING_EVERY)
                if not done:
                    waited = int(time.monotonic() - started + LOADING_AFTER)
                    yield f": aiodos loading {waited}\n\n"
            head = head_task.result()
            if any(kind == "head" for kind, _ in head):
                async for chunk in _relay_events(stream, head):
                    yield chunk
            else:
                async for event in _formatted_events(stream, head, chunks):
                    yield event
        except Exception as exc:
            yield _sse_error(exc)
        finally:
            stream.close()
            if not head_task.done():
                head_task.cancel()
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers=_route_headers(model=model))


@router.post("/chat/completions")
async def chat_completions(request: Request):
    lang = _lang(request)
    try:
        body = await _body(request)
    except GatewayError as exc:
        return _failure(exc, lang)
    model_id = str(body.get("model") or "")
    messages = body.get("messages")
    if not model_id:
        return error(400, t("gateway_api.model_required"), param="model", lang=lang)
    if not isinstance(messages, list) or not messages:
        return error(400, t("gateway_api.messages_required"), param="messages", lang=lang)
    refused = next((p for p in REFUSED if body.get(p) is not None), None)
    modalities = body.get("modalities")
    if refused is None and isinstance(modalities, list) and "audio" in modalities:
        refused = "modalities"
    if refused:
        return error(400, t("gateway_api.param_unsupported", param=refused),
                     code="unsupported_parameter", param=refused, lang=lang)
    streaming = bool(body.get("stream"))
    include_usage = streaming and bool((body.get("stream_options") or {}).get("include_usage"))
    response_format = (body.get("response_format") or {}).get("type") \
        if isinstance(body.get("response_format"), dict) else None
    several = isinstance(body.get("n"), int) and body["n"] > 1
    media = bool(_parts(messages) - TEXT_PARTS)
    local_only = (media or response_format in ("json_object", "json_schema")
                  or bool(body.get("logprobs")) or several)
    tools = bool(body.get("tools") or body.get("functions")) or any(
        isinstance(m, dict) and (m.get("tool_calls") or m.get("role") in ("tool", "function"))
        for m in messages)
    executor = _executor(request)
    context = request.state.aiodos_app
    # Une appli (pas la clé générale, comme avant) n'a jamais d'outils retirés en silence
    require_tools = (context is not None and context.kind != "general"
                     and request.headers.get("x-aiodos-tools", "").strip().lower() != "optional")
    app = answer.for_app(request)
    try:
        where = scopes.for_request(request, "chat")
        models = await asyncio.to_thread(answer.plan, model_id, executor,
                                         local_only=local_only, prefer_local=tools,
                                         where=where, tools=tools, require_tools=require_tools)
    except (GatewayError, ApiError) as exc:
        return _failure(exc, lang)
    think, options = _thinks(body), sampling(body)
    limit = body.get("max_completion_tokens") or body.get("max_tokens")
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else None

    def attempt(model, stop):
        if not answer.is_local(model):    # en texte, seulement pour une autre machine
            yield from answer.remote(model, plain_messages(messages), think, options, stop)
        elif ollama.use_integrated():     # le routeur, la conversation en texte mise en forme
            plain = None if tools or media else plain_messages(messages)
            yield from engine.openai_chat(model, model_id, body, stop, plain=plain,
                                          images="image_url" in _parts(messages), think=think,
                                          extra=_meta(model))
        else:
            yield from answer.local_raw("/v1/chat/completions", {**body, "model": model}, stop)

    stream = answer.Stream(answer.run(models, attempt, executor,
                                      interactive=app["interactive"]), cut=app["cut"])
    chunks = Chunks(model_id, "chat", include_usage, limit=limit)
    head_task = asyncio.ensure_future(stream.head(request))    # l'outil parti : arrêté
    if streaming and len(models) == 1:
        done, _ = await asyncio.wait({head_task}, timeout=LOADING_AFTER)
        if not done:
            return _early(stream, head_task, models[0], chunks)
    try:
        head = await head_task
    except Exception as exc:
        stream.close()
        return _failure(exc, lang)
    raw = next((value for kind, value in head if kind == "head"), None)
    if raw is not None:
        return _relay(stream, head, raw)
    response = await _formatted(stream, head, chunks, streaming, request)
    if tools and not require_tools and not answer.is_local(answer.answered_by(head) or ""):
        response.headers["X-Aiodos-Warning"] = "tools-dropped"     # clé générale : comme avant
    return response


@router.post("/completions")
async def completions(request: Request):
    """Complétion de texte brut, sur cette machine seulement : la conversation d'une
    machine appairée passe toujours par le gabarit de son modèle."""
    lang = _lang(request)
    try:
        body = await _body(request)
    except GatewayError as exc:
        return _failure(exc, lang)
    model_id = str(body.get("model") or "")
    prompt = body.get("prompt")
    if isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], str):
        prompt = prompt[0]
    if not model_id:
        return error(400, t("gateway_api.model_required"), param="model", lang=lang)
    if not isinstance(prompt, str):
        return error(400, t("gateway_api.prompt_single"), param="prompt", lang=lang)
    streaming = bool(body.get("stream"))
    include_usage = streaming and bool((body.get("stream_options") or {}).get("include_usage"))
    executor = _executor(request)
    app = answer.for_app(request)
    try:
        where = scopes.for_request(request, "chat")
        models = await asyncio.to_thread(answer.plan, model_id, executor, local_only=True,
                                         where=where)
    except (GatewayError, ApiError) as exc:
        return _failure(exc, lang)
    suffix = body.get("suffix")
    generate = {"prompt": prompt, "options": sampling(body) or {}}
    if isinstance(suffix, str) and suffix:
        generate["suffix"] = suffix        # complétion de code par le gabarit du modèle
    else:
        generate["raw"] = True

    def attempt(model, stop):
        if ollama.use_integrated():
            yield from engine.complete(model, prompt, generate.get("suffix"),
                                       generate["options"], stop)
        else:
            yield from answer.local_generate({**generate, "model": model}, stop)

    stream = answer.Stream(answer.run(models, attempt, executor,
                                      interactive=app["interactive"]), cut=app["cut"])
    try:
        head = await stream.head(request)
    except Exception as exc:
        stream.close()
        return _failure(exc, lang)
    limit = body.get("max_tokens") if isinstance(body.get("max_tokens"), int) else None
    return await _formatted(stream, head, Chunks(model_id, "text", include_usage, limit=limit),
                            streaming, request)


# --------------------------------------------------------------- embeddings
def _vector(values, as_base64: bool):
    if as_base64:
        return base64.b64encode(np.asarray(values, dtype="<f4").tobytes()).decode("ascii")
    return values


@router.post("/embeddings")
async def embeddings(request: Request):
    """Sur cette machine seulement (son modèle d'embedding : Ollama ou le moteur intégré)."""
    lang = _lang(request)
    try:
        where = scopes.for_request(request, "embed")
        if where is not None and "here" not in where:
            raise ApiError("where_denied", t("gateway_api.embed_where_denied"),
                           detail={"allowed": ["here"]})
        body = await _body(request)
        name = catalog.local_name(body.get("model"))
    except catalog.UnknownModel as exc:
        return error(400, str(exc), param="model", lang=lang)
    except (GatewayError, ApiError) as exc:
        return _failure(exc, lang)
    inputs = body.get("input")
    if isinstance(inputs, str):
        inputs = [inputs]
    if (not isinstance(inputs, list) or not inputs
            or not all(isinstance(text, str) for text in inputs)):
        return error(400, t("gateway_api.input_invalid"), param="input", lang=lang)
    payload = {"model": name, "input": inputs, "truncate": True}
    if isinstance(body.get("dimensions"), int):
        payload["dimensions"] = body["dimensions"]
    try:
        reply = await asyncio.to_thread(_embed, payload)
    except GatewayError as exc:
        return _failure(exc, lang)
    as_base64 = body.get("encoding_format") == "base64"
    vectors = reply.get("embeddings") or []
    tokens = int(reply.get("prompt_eval_count") or 0)
    return {"object": "list", "model": body.get("model"),
            "data": [{"object": "embedding", "index": i, "embedding": _vector(v, as_base64)}
                     for i, v in enumerate(vectors)],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}


def _embed(payload: dict) -> dict:
    """{"embeddings", "prompt_eval_count"} comme /api/embed d'Ollama ; le moteur intégré ne
    dit pas le nombre de jetons lus (0)."""
    if ollama.use_integrated():
        return {"embeddings": engine.embed(payload["model"], payload["input"],
                                           payload.get("dimensions")), "prompt_eval_count": 0}
    status, content, _ = answer.local_post("/api/embed", payload)
    if status >= 400:
        raise GatewayError(status, answer.error_text(content, status))
    try:
        return json.loads(content)
    except ValueError as exc:
        raise GatewayError(502, t("gateway_api.ollama_unreadable")) from exc
