"""La passerelle servie par le moteur intégré (llama.cpp, aiodos/llm/) quand il tient lieu
d'Ollama sur cette machine (services/ollama.use_integrated) : les outils gardent les mêmes
API, sans Ollama.

- Catalogue : les modèles de la façade (ollama.list_models, capabilities), décrits comme le
  ferait Ollama (catalog.local_tags ; ici /api/show et /api/ps).
- Conversation en texte de l'API d'Ollama : la façade (ollama.chat_stream), remise au format
  d'Ollama par ollama_api comme la réponse d'une machine appairée (converse).
- Le reste va au routeur de llama.cpp, dont l'API est déjà celle d'OpenAI
  (integrated.forward) : /v1/chat/completions tel quel, outils, images, format imposé,
  logprobs et plusieurs réponses compris, le nom du modèle remis dans la réponse
  (openai_chat) ; images, outils et format imposé de l'API d'Ollama traduits pour lui, et
  sa réponse lue en retour (ollama_chat) ; texte brut et complétion de code (complete).
- Embeddings : la façade (ollama.embed).

Ce que le routeur ne prend pas est refusé clairement (400), pas donné pour une panne (502) :
images pour un modèle qui ne les voit pas, complétion de code sans ses jetons, texte trop
long. Relevé le 20/10/2026 avec llama.cpp b11707 : outils (qwen3.5 ; en flux, les arguments
arrivent par morceaux), images (qwen3.5 et son projecteur ; ailleurs, erreur 500), json_object
et json_schema, n = 2, logprobs, /infill (qwen2.5-coder ; 501 pour mistral, qui n'a pas les
jetons de complétion) ; un modèle d'embedding refuse un texte de plus de 2048 jetons (ses
lots, llm/engine.py), et le gabarit de mistral deux messages de suite du même rôle.

Fonctions bloquantes : dans le thread d'answer.Stream ou par asyncio.to_thread.
"""
import json
import logging
import math
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests

from aiodos import i18n
from aiodos.gateway import catalog
from aiodos.gateway.answer import GatewayError
from aiodos.i18n import t
from aiodos.llm import integrated, models as registry
from aiodos.llm import context as llm_context
from aiodos.llm.engine import EMBED_CONTEXT, EngineUnavailable
from aiodos.llm.messages import SAMPLING, for_template
from aiodos.services import ollama

log = logging.getLogger("aiodos.gateway")

TIMEOUT = (10, 900)
# Ce que le routeur refuse en connaissance de cause, même quand il répond 500
REFUSED = ("not supported", "too large", "exceeds the available context")
# Refus dont le message de llama-server, un extrait du gabarit, ne dirait rien à l'outil :
# son explication (clé de texte)
EXPLAINED = {"roles must alternate": "gateway_engine.roles_alternate"}
ROLES = {"system", "user", "assistant", "tool"}
IMAGE_TYPES = {"iVBOR": "image/png", "/9j/": "image/jpeg", "R0lG": "image/gif",
               "UklG": "image/webp"}
_TOO_LARGE = re.compile(r"input \((\d+) tokens\).*batch size: (\d+)")


# ------------------------------------------------------------------- erreurs
def failure(exc) -> GatewayError:
    """Une erreur du moteur intégré, au statut que comprend l'outil : refus clair (400) de
    ce qu'il ne prend pas, modèle écarté (404), panne (502, 503 s'il ne démarre pas)."""
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, integrated.Incompatible):
        return GatewayError(404, str(exc))
    if isinstance(exc, EngineUnavailable):
        return GatewayError(503, str(exc))
    if isinstance(exc, requests.RequestException):
        return GatewayError(502, t("gateway_engine.down", error=exc.__class__.__name__))
    return _refusal(500, _engine_text(str(exc)))


def _engine_text(message: str) -> str:
    """Le message de llama-server sans le « Moteur intégré : » de llm/integrated.py (dans
    l'une ou l'autre langue)."""
    for lang in i18n.LANGUAGES:
        with i18n.using(lang):
            message = message.removeprefix(t("llm.engine_error", error=""))
    return message


def _refusal(status: int, message: str, error=None) -> GatewayError:
    """error : l'objet d'erreur de llama-server, s'il en a donné un (n_prompt_tokens, n_ctx
    pour un texte trop long)."""
    lowered = message.lower()
    error = error if isinstance(error, dict) else {}
    if error.get("type") == "exceed_context_size_error" or "exceeds the available context" \
            in lowered or "context size" in lowered and "exceed" in lowered:
        needed, limit = error.get("n_prompt_tokens"), error.get("n_ctx")
        detail = ({"needed": needed, "limit": limit}
                  if isinstance(needed, int) and isinstance(limit, int) else {})
        text = (t("gateway_engine.context_exceeded", needed=needed, limit=limit) if detail
                else t("gateway_errors.context_length_exceeded"))
        return GatewayError(400, text, code="context_length_exceeded", detail=detail,
                            param="messages")
    if status in (400, 413, 422, 501) or any(r in lowered for r in REFUSED):
        message = next((t(text) for key, text in EXPLAINED.items() if key in lowered), message)
        return GatewayError(400, t("gateway_engine.refused", error=message))
    return GatewayError(502, t("gateway_engine.failed", error=message), code="engine_failed")


@contextmanager
def _guard():
    try:
        yield
    except (RuntimeError, requests.RequestException) as exc:
        raise failure(exc) from exc


def _forward(model: str, path: str, body: dict, stream=False, timeout=TIMEOUT):
    """Demande au routeur pour ce modèle ; ses refus deviennent des GatewayError."""
    with _guard():
        response = integrated.forward(model, path, body, stream=stream, timeout=timeout)
    if response.status_code >= 400:
        message, error = _router_error(response)
        response.close()
        raise _refusal(response.status_code, message, error)
    return response


def _router_error(response) -> tuple:
    """{"error": {"code", "message", "type", …}} : le message de llama-server, et l'objet."""
    try:
        error = response.json().get("error")
        message = error.get("message") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        error = message = None
    return str(message or f"HTTP {response.status_code}"), error


# ---------------------------------------------------------------- catalogue
def _caps(model: str) -> list:
    """Capacités d'un modèle de cette machine ; 404 s'il n'y est pas."""
    caps = ollama.capabilities(model)
    if not caps:
        raise GatewayError(404, t("gateway_engine.model_missing", model=model))
    return caps


def _conversing(model: str, images=False):
    """Un modèle d'ici qui converse, et qui voit les images s'il en reçoit."""
    caps = _caps(model)
    if "completion" not in caps:
        raise GatewayError(400, t("gateway_engine.embedding_only", model=model))
    if images and "vision" not in caps:
        raise GatewayError(400, t("gateway_engine.no_vision", model=model),
                           code="vision_unsupported", param="messages")


def _context(found: registry.Model) -> int:
    """Le contexte que le routeur donne à ce modèle (llm/engine.py)."""
    return EMBED_CONTEXT if found.embedding else llm_context.chat_context(found.name)


def show(name: str) -> dict | None:
    """/api/show d'un modèle d'ici, None s'il n'y est pas. num_ctx et
    <architecture>.context_length : le contexte du routeur, sur lequel Continue règle la
    taille de ses demandes."""
    found = registry.find(name)
    if found is None:
        return None
    context, family = _context(found), found.family
    return {"license": found.license, "modelfile": "", "parameters": f"num_ctx {context}",
            "template": found.template,
            "details": catalog.details({"family": family, "parameters": found.parameters,
                                        "quantization": found.quantization}),
            "model_info": ({"general.architecture": family, f"{family}.context_length": context}
                           if family else {}),
            "capabilities": ollama.capabilities(found.name), "modified_at": catalog.now_iso()}


def running() -> dict:
    """/api/ps : les modèles que le routeur a chargés ; il les décharge après IDLE_UNLOAD s
    sans usage."""
    tags = {t["name"]: t for t in catalog.local_tags()}
    until = (datetime.now(timezone.utc) + timedelta(seconds=integrated.IDLE_UNLOAD)) \
        .isoformat().replace("+00:00", "Z")
    out = []
    for m in ollama.loaded_models():
        tag, found = tags.get(m["name"]) or {}, registry.find(m["name"])
        out.append({"name": m["name"], "model": m["name"], "size": m["size"],
                    "digest": tag.get("digest", ""), "details": tag.get("details") or {},
                    "expires_at": until, "size_vram": m["size_vram"],
                    "context_length": _context(found) if found
                    else llm_context.chat_context(m["name"])})
    return {"models": out}


def load(model: str, unload: bool):
    """Chargement demandé sans rien à calculer (/api/generate ou /api/chat sans texte),
    ou déchargement (keep_alive 0)."""
    _caps(model)
    if unload:
        integrated.unload(model)
        return
    with _guard():
        response = integrated.forward(model, "/models/load", {}, timeout=(10, 120))
    response.close()          # 400 « model is already running » : déjà chargé, très bien


# ------------------------------------------------------------- conversation
def converse(model: str, messages: list, think: bool, options, stop):
    """Conversation en texte par la façade (ollama.chat_stream) : couples ("thinking" |
    "content", texte), puis ("stats", …), comme ceux d'une machine appairée."""
    _conversing(model)
    with _guard():
        yield from ollama.chat_stream(model, messages, think=None if think else False,
                                      stop=stop, options=options, stats=True)


def stopped_by(finish, limit=None, usage=None) -> str:
    """Pourquoi la réponse s'est arrêtée : stop, tool_calls, max_tokens (la limite demandée)
    ou context (la fin du contexte du modèle, sans limite demandée ou avant elle)."""
    if finish == "tool_calls":
        return "tool_calls"
    if finish != "length":
        return "stop"
    produced = (usage or {}).get("completion_tokens") if isinstance(usage, dict) else None
    if limit and (not produced or produced >= limit):
        return "max_tokens"
    return "context"


def openai_chat(model: str, model_id: str, body: dict, stop, *, plain=None, images=False,
                think=True, extra=None):
    """/v1/chat/completions d'un outil, tel quel par le routeur : ("head", type de contenu),
    puis ("raw", octets). model : le modèle d'ici qui répond ; model_id : celui qu'a demandé
    l'outil, remis dans la réponse à la place de l'identifiant du routeur. plain : la
    conversation en texte, quand elle n'est que cela, mise en forme pour les gabarits stricts
    (for_template). think : faux pour une réponse sans réflexion (reasoning_effort « none »).
    extra : l'objet « aiodos » (qui a répondu, contexte) ajouté au morceau qui porte
    finish_reason, avec stopped_by, ou à la réponse entière sans flux."""
    _conversing(model, images=images)
    payload = dict(body)
    if plain is not None:
        payload["messages"] = for_template(plain)
    if not think:
        kwargs = body.get("chat_template_kwargs")
        payload["chat_template_kwargs"] = {**(kwargs if isinstance(kwargs, dict) else {}),
                                           "enable_thinking": False}
    limit = body.get("max_completion_tokens") or body.get("max_tokens")
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else None
    streaming = bool(body.get("stream"))
    with _forward(model, "/v1/chat/completions", payload, stream=streaming) as response:
        if not streaming:
            with _guard():
                reply = response.json()
            out = {**reply, "model": model_id}
            if extra is not None:
                choice = (reply.get("choices") or [None])[0] if isinstance(reply, dict) else None
                finish = choice.get("finish_reason") if isinstance(choice, dict) else None
                out["aiodos"] = {**extra, "stopped_by": stopped_by(finish, limit,
                                                                   reply.get("usage"))}
            yield "head", "application/json"
            yield "raw", _dumps(out)
            return
        yield "head", "text/event-stream"
        for event in _events(response, stop):
            out = {**event, "model": model_id}
            choice = (event.get("choices") or [None])[0]
            if extra is not None and isinstance(choice, dict) and choice.get("finish_reason"):
                out["aiodos"] = {**extra, "stopped_by": stopped_by(
                    choice["finish_reason"], limit, event.get("usage"))}
            yield "raw", b"data: " + _dumps(out) + b"\n\n"
        if not stop.is_set():
            yield "raw", b"data: [DONE]\n\n"


def ollama_chat(model: str, body: dict, messages: list, stop):
    """/api/chat (ou /api/generate) d'un outil qui porte des images, des outils ou un format
    imposé, traduit pour le routeur : couples ("thinking" | "content", texte),
    ("tool_calls", appels au format d'Ollama), puis ("stats", …)."""
    _conversing(model, images=any(isinstance(m, dict) and m.get("images") for m in messages))
    payload = {"messages": _openai_messages(messages), "stream": True,
               "stream_options": {"include_usage": True}, **_sampling(body.get("options"))}
    if body.get("tools"):
        payload["tools"] = body["tools"]
    response_format = _response_format(body.get("format"))
    if response_format:
        payload["response_format"] = response_format
    if body.get("think") is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    with _forward(model, "/v1/chat/completions", payload, stream=True) as response:
        yield from _answer(response, stop)


def complete(model: str, prompt: str, suffix, options, stop):
    """Texte brut, sans gabarit de conversation ; avec suffix, complétion de code au milieu
    d'un texte (/infill : les jetons de complétion du modèle, s'il en a). Couples
    ("content", texte), puis ("stats", …)."""
    _conversing(model)
    sampling = _sampling(options)
    if not suffix:
        payload = {"prompt": prompt, "stream": True, "stream_options": {"include_usage": True},
                   **sampling}
        with _forward(model, "/v1/completions", payload, stream=True) as response:
            yield from _answer(response, stop)
        return
    payload = {"input_prefix": prompt, "input_suffix": suffix, "stream": True, **sampling}
    try:
        response = _forward(model, "/infill", payload, stream=True)
    except GatewayError as exc:
        if exc.status == 400 and "not supported" in str(exc):
            raise GatewayError(400, t("gateway_engine.no_infill", model=model)) from exc
        raise
    with response:
        yield from _infilled(response, stop)


# ------------------------------------------------------------------ lecture
def _dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _events(response, stop):
    """Objets JSON d'un flux de llama-server (« data: … »), jusqu'à [DONE] ; une erreur en
    route remonte en GatewayError."""
    response.encoding = "utf-8"          # llama-server ne dit pas l'encodage de son flux
    with _guard():
        for line in response.iter_lines(decode_unicode=True):
            if stop.is_set():
                return
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("error"):
                error = event["error"] if isinstance(event["error"], dict) else {
                    "message": event["error"]}
                code = error.get("code")
                raise _refusal(code if isinstance(code, int) else 500,
                               str(error.get("message") or t("gateway_engine.unknown_error")),
                               error)
            yield event


def _answer(response, stop):
    """Flux d'OpenAI du routeur (conversation, ou texte brut) en couples : ("thinking" |
    "content", texte), ("tool_calls", […]) à la fin s'il y en a, puis ("stats", …)."""
    calls, finish, usage = {}, "stop", {}
    for event in _events(response, stop):
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        choice = (event.get("choices") or [None])[0]
        if not isinstance(choice, dict):
            continue
        finish = choice.get("finish_reason") or finish
        delta = choice.get("delta") or {}
        if delta.get("reasoning_content"):
            yield "thinking", delta["reasoning_content"]
        text = delta.get("content") or choice.get("text")
        if text:
            yield "content", text
        for call in delta.get("tool_calls") or []:
            _gather(calls, call)
    if stop.is_set():
        return
    if calls:
        yield "tool_calls", [_ollama_call(i, calls[i]) for i in sorted(calls)]
    yield "stats", {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "done_reason": "length" if finish == "length" else "stop"}


def _infilled(response, stop):
    """Flux de /infill, au format propre à llama-server : content, puis stop et compteurs."""
    for event in _events(response, stop):
        if event.get("content"):
            yield "content", event["content"]
        if event.get("stop"):
            yield "stats", {"prompt_tokens": int(event.get("tokens_evaluated") or 0),
                            "completion_tokens": int(event.get("tokens_predicted") or 0),
                            "done_reason": "length" if event.get("stop_type") == "limit"
                            else "stop"}
            return


def _gather(calls: dict, call):
    """Un morceau d'appel d'outil en flux : identifiant et nom au premier, arguments par
    bouts de texte."""
    if not isinstance(call, dict):
        return
    slot = calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
    function = call.get("function") or {}
    slot["id"] = call.get("id") or slot["id"]
    slot["name"] = function.get("name") or slot["name"]
    slot["arguments"] += function.get("arguments") or ""


def _ollama_call(index: int, slot: dict) -> dict:
    """Appel d'outil au format d'Ollama : les arguments en objet, pas en texte."""
    try:
        arguments = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
    except ValueError:
        log.warning("Passerelle : arguments illisibles pour l'outil « %s »", slot["name"])
        arguments = {}
    call = {"function": {"index": index, "name": slot["name"],
                         "arguments": arguments if isinstance(arguments, dict) else {}}}
    if slot["id"]:
        call["id"] = slot["id"]
    return call


# ------------------------------------------------------- de l'API d'Ollama
def _openai_messages(messages: list) -> list:
    """Messages de l'API d'Ollama → ceux d'OpenAI : images en adresses data:, arguments des
    appels d'outils en texte, chaque résultat d'outil relié à son appel (identifiants de 9
    caractères quand l'outil n'en donne pas : le gabarit de mistral l'exige)."""
    out, waiting, made = [], [], 0
    for m in messages:
        if not isinstance(m, dict):
            raise GatewayError(400, t("gateway_api.message_invalid"))
        role = str(m.get("role") or "user").lower()
        role = role if role in ROLES else "user"
        content = m.get("content") or ""
        if not isinstance(content, str):
            raise GatewayError(400, t("gateway_api.content_not_text"))
        item = {"role": role, "content": content}
        images = m.get("images") or []
        if images:
            if not isinstance(images, list) or not all(isinstance(i, str) and i for i in images):
                raise GatewayError(400, t("gateway_engine.images_invalid"))
            item["content"] = ([{"type": "text", "text": content}] if content else []) + [
                {"type": "image_url", "image_url": {"url": _data_url(i)}} for i in images]
        calls = m.get("tool_calls") if role == "assistant" else None
        if isinstance(calls, list) and calls:
            item["tool_calls"], waiting = [], []
            for call in calls:
                function = (call.get("function") if isinstance(call, dict) else None) or {}
                arguments = function.get("arguments")
                made += 1
                call_id = str(call.get("id") or f"call{made:05d}")
                item["tool_calls"].append({"id": call_id, "type": "function", "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": arguments if isinstance(arguments, str)
                    else json.dumps(arguments or {}, ensure_ascii=False)}})
                waiting.append(call_id)
        elif role == "tool":
            call_id = m.get("tool_call_id") or (waiting[0] if waiting else None)
            if call_id:
                item["tool_call_id"] = str(call_id)
                if call_id in waiting:
                    waiting.remove(call_id)
        out.append(item)
    return out


def _data_url(image: str) -> str:
    if image.startswith("data:"):
        return image
    kind = next((t for prefix, t in IMAGE_TYPES.items() if image.startswith(prefix)),
                "image/png")
    return f"data:{kind};base64,{image}"


def _sampling(options) -> dict:
    """Réglages d'échantillonnage d'Ollama → champs de llama-server ; les autres (num_ctx…)
    restent aux valeurs du routeur."""
    if not isinstance(options, dict):
        return {}
    return {SAMPLING[k]: v for k, v in options.items() if k in SAMPLING}


def _response_format(wanted):
    """« format » d'Ollama (« json » ou un schéma JSON) → response_format d'OpenAI."""
    if not wanted:
        return None
    if wanted == "json":
        return {"type": "json_object"}
    if isinstance(wanted, dict):
        return {"type": "json_schema", "json_schema": {"name": "format", "schema": wanted}}
    raise GatewayError(400, t("gateway_engine.format_invalid"))


# --------------------------------------------------------------- embeddings
def embed(model: str, texts: list, dimensions=None) -> list:
    """Vecteurs normés de ces textes (ollama.embed) ; dimensions : les premières seulement,
    normées de nouveau, comme Ollama (modèles « Matryoshka »)."""
    if "embedding" not in _caps(model):
        raise GatewayError(400, t("llm.not_embedding", name=model))
    if not texts:
        return []
    try:
        vectors = ollama.embed(list(texts), model)
    except (RuntimeError, requests.RequestException) as exc:
        too_large = _TOO_LARGE.search(str(exc))
        if too_large:
            raise GatewayError(400, t("gateway_engine.text_too_long", model=model,
                                      tokens=too_large.group(1),
                                      limit=too_large.group(2))) from exc
        raise failure(exc) from exc
    if (isinstance(dimensions, int) and not isinstance(dimensions, bool)
            and 0 < dimensions < len(vectors[0])):
        vectors = [_unit(v[:dimensions]) for v in vectors]
    return vectors


def _unit(vector) -> list:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]
