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
d'Ollama (outils, images, format imposé compris) ; une machine appairée reçoit la
conversation en texte, et la réponse est remise ici au format d'OpenAI.
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

from aiodos.gateway import answer, catalog
from aiodos.gateway.answer import GatewayError

router = APIRouter(prefix="/v1")
TEXT_PARTS = {"text", "input_text", "refusal"}


def error(status: int, message: str, code=None, param=None):
    kind = "invalid_request_error" if status < 500 else "api_error"
    if status == 404 and code is None:
        code = "model_not_found"
    return JSONResponse({"error": {"message": message, "type": kind, "param": param,
                                   "code": code}}, status_code=status)


def _failure(exc):
    if isinstance(exc, answer.Upstream):        # Ollama répond déjà au format d'OpenAI
        return Response(exc.body, status_code=exc.status, media_type=exc.content_type)
    return error(answer.status_of(exc), str(exc))


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise GatewayError(400, "Corps JSON illisible.") from exc
    if not isinstance(body, dict):
        raise GatewayError(400, "Corps JSON attendu : un objet.")
    return body


def _executor(request: Request):
    return request.app.state.node.executor


def _route_headers(head) -> dict:
    model = answer.answered_by(head)
    return {"X-Aiodos-Model": quote(model, safe=":@")} if model else {}


# ------------------------------------------------------------------ modèles
def _model(entry: dict) -> dict:
    name = f"{entry['name']} · {entry['machine']}" if entry["machine"] else entry["name"]
    return {"id": entry["id"], "object": "model", "created": catalog.epoch(entry["modified_at"]),
            "owned_by": entry["machine"] or "aiodos", "name": name}


@router.get("/models")
async def models():
    entries = await asyncio.to_thread(catalog.entries)
    return {"object": "list", "data": [_model(e) for e in entries]}


@router.get("/models/{model_id:path}")
async def model(model_id: str):
    entry = await asyncio.to_thread(catalog.find, model_id)
    if entry is None:
        return error(404, f"Le modèle « {model_id} » n'existe pas dans le cercle AIODOS.",
                     param="model")
    return _model(entry)


# ------------------------------------------------------------ conversation
def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or p.get("refusal") or "") for p in content
                         if isinstance(p, dict) and p.get("type") in TEXT_PARTS)
    raise GatewayError(400, "Contenu de message illisible.")


def _has_media(messages) -> bool:
    return any(isinstance(m, dict) and isinstance(m.get("content"), list) and any(
        isinstance(p, dict) and p.get("type") not in TEXT_PARTS for p in m["content"])
        for m in messages)


def plain_messages(messages) -> list:
    """La conversation telle qu'une machine appairée la reçoit (cf. lending.py) : rôles
    system, user, assistant, texte seul ; « developer » devient system, un résultat
    d'outil devient un message de l'utilisateur."""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            raise GatewayError(400, "Message invalide.")
        role = str(m.get("role") or "user")
        content = _text(m.get("content"))
        if role == "developer":
            role = "system"
        elif role in ("tool", "function"):
            role, content = "user", f"[Résultat d'un outil] {content}"
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
    """Réponse d'une machine appairée remise au format d'OpenAI."""

    def __init__(self, model_id: str, kind: str, include_usage=False):
        self.id = ("chatcmpl-" if kind == "chat" else "cmpl-") + secrets.token_hex(12)
        self.created = int(time.time())
        self.model_id = model_id
        self.kind = kind                  # « chat » ou « text » (complétion brute)
        self.include_usage = include_usage
        self.stats = {}
        self.content = []
        self.thinking = []

    @property
    def finish(self) -> str:
        return "length" if self.stats.get("done_reason") == "length" else "stop"

    @property
    def usage(self) -> dict:
        prompt = int(self.stats.get("prompt_tokens") or 0)
        completion = int(self.stats.get("completion_tokens") or 0)
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion}

    def _chunk(self, choice: dict | None) -> str:
        obj = {"id": self.id, "created": self.created, "model": self.model_id,
               "object": "chat.completion.chunk" if self.kind == "chat" else "text_completion",
               "choices": [choice] if choice is not None else []}
        if self.include_usage:
            obj["usage"] = self.usage if choice is None else None
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
        if kind == "stats" and isinstance(value, dict):
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
        events = [self._chunk(self._choice({}, "", self.finish))]
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
        return {"id": self.id, "object": "chat.completion" if self.kind == "chat"
                else "text_completion", "created": self.created, "model": self.model_id,
                "choices": [choice], "usage": self.usage}


def _sse_error(exc) -> str:
    return "data: " + json.dumps({"error": {"message": str(exc), "type": "api_error",
                                            "param": None, "code": None}},
                                 ensure_ascii=False) + "\n\n"


async def _formatted(stream: answer.Stream, head: list, chunks: Chunks, streaming: bool):
    headers = _route_headers(head)
    if not streaming:
        try:
            for item in head:
                chunks.take(*item)
            while (item := await stream.next()) is not None:
                chunks.take(*item)
        except Exception as exc:
            return _failure(exc)
        finally:
            stream.close()
        return JSONResponse(chunks.whole(), headers=headers)

    async def events():
        try:
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
        except Exception as exc:
            yield _sse_error(exc)
        finally:
            stream.close()
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


def _relay(stream: answer.Stream, head: list, content_type: str):
    async def body():
        try:
            for kind, value in head:
                if kind == "raw":
                    yield value
            while (item := await stream.next()) is not None:
                if item[0] == "raw":
                    yield item[1]
        except Exception as exc:
            yield _sse_error(exc).encode("utf-8")
        finally:
            stream.close()
    return StreamingResponse(body(), media_type=content_type, headers=_route_headers(head))


@router.post("/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await _body(request)
    except GatewayError as exc:
        return _failure(exc)
    model_id = str(body.get("model") or "")
    messages = body.get("messages")
    if not model_id:
        return error(400, "Il manque le modèle (« model »).", param="model")
    if not isinstance(messages, list) or not messages:
        return error(400, "Il manque les messages (« messages »).", param="messages")
    streaming = bool(body.get("stream"))
    include_usage = streaming and bool((body.get("stream_options") or {}).get("include_usage"))
    response_format = (body.get("response_format") or {}).get("type") \
        if isinstance(body.get("response_format"), dict) else None
    local_only = (_has_media(messages) or response_format in ("json_object", "json_schema")
                  or bool(body.get("logprobs")) or (body.get("n") or 1) > 1)
    tools = bool(body.get("tools") or body.get("functions")) or any(
        isinstance(m, dict) and (m.get("tool_calls") or m.get("role") in ("tool", "function"))
        for m in messages)
    executor = _executor(request)
    try:
        models = await asyncio.to_thread(answer.plan, model_id, executor,
                                         local_only=local_only, prefer_local=tools)
        plain = plain_messages(messages)
    except GatewayError as exc:
        return _failure(exc)
    think, options = _thinks(body), sampling(body)

    def attempt(model, stop):
        if answer.is_local(model):
            yield from answer.local_raw("/v1/chat/completions", {**body, "model": model}, stop)
        else:
            yield from answer.remote(model, plain, think, options, stop)

    stream = answer.Stream(answer.run(models, attempt, executor))
    try:
        head = await stream.head()
    except Exception as exc:
        stream.close()
        return _failure(exc)
    raw = next((value for kind, value in head if kind == "head"), None)
    if raw is not None:
        return _relay(stream, head, raw)
    return await _formatted(stream, head, Chunks(model_id, "chat", include_usage), streaming)


@router.post("/completions")
async def completions(request: Request):
    """Complétion de texte brut, sur cette machine seulement : la conversation d'une
    machine appairée passe toujours par le gabarit de son modèle."""
    try:
        body = await _body(request)
    except GatewayError as exc:
        return _failure(exc)
    model_id = str(body.get("model") or "")
    prompt = body.get("prompt")
    if isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], str):
        prompt = prompt[0]
    if not model_id:
        return error(400, "Il manque le modèle (« model »).", param="model")
    if not isinstance(prompt, str):
        return error(400, "« prompt » doit être un texte (une seule complétion à la fois).",
                     param="prompt")
    streaming = bool(body.get("stream"))
    include_usage = streaming and bool((body.get("stream_options") or {}).get("include_usage"))
    executor = _executor(request)
    try:
        models = await asyncio.to_thread(answer.plan, model_id, executor, local_only=True)
    except GatewayError as exc:
        return _failure(exc)
    suffix = body.get("suffix")
    generate = {"prompt": prompt, "options": sampling(body) or {}}
    if isinstance(suffix, str) and suffix:
        generate["suffix"] = suffix        # complétion de code par le gabarit du modèle
    else:
        generate["raw"] = True

    def attempt(model, stop):
        yield from answer.local_generate({**generate, "model": model}, stop)

    stream = answer.Stream(answer.run(models, attempt, executor))
    try:
        head = await stream.head()
    except Exception as exc:
        stream.close()
        return _failure(exc)
    return await _formatted(stream, head, Chunks(model_id, "text", include_usage), streaming)


# --------------------------------------------------------------- embeddings
def _vector(values, as_base64: bool):
    if as_base64:
        return base64.b64encode(np.asarray(values, dtype="<f4").tobytes()).decode("ascii")
    return values


@router.post("/embeddings")
async def embeddings(request: Request):
    """Sur cette machine seulement (le modèle d'embedding d'Ollama, /api/embed)."""
    try:
        body = await _body(request)
        name = catalog.local_name(body.get("model"))
    except catalog.UnknownModel as exc:
        return error(400, str(exc), param="model")
    except GatewayError as exc:
        return _failure(exc)
    inputs = body.get("input")
    if isinstance(inputs, str):
        inputs = [inputs]
    if not isinstance(inputs, list) or not inputs or not all(isinstance(t, str) for t in inputs):
        return error(400, "« input » doit être un texte ou une liste de textes.", param="input")
    payload = {"model": name, "input": inputs, "truncate": True}
    if isinstance(body.get("dimensions"), int):
        payload["dimensions"] = body["dimensions"]
    try:
        reply = await asyncio.to_thread(_embed, payload)
    except GatewayError as exc:
        return _failure(exc)
    as_base64 = body.get("encoding_format") == "base64"
    vectors = reply.get("embeddings") or []
    tokens = int(reply.get("prompt_eval_count") or 0)
    return {"object": "list", "model": body.get("model"),
            "data": [{"object": "embedding", "index": i, "embedding": _vector(v, as_base64)}
                     for i, v in enumerate(vectors)],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}


def _embed(payload: dict) -> dict:
    status, content, _ = answer.local_post("/api/embed", payload)
    if status >= 400:
        raise GatewayError(status, answer.error_text(content, status))
    try:
        return json.loads(content)
    except ValueError as exc:
        raise GatewayError(502, "Réponse illisible d'Ollama.") from exc
