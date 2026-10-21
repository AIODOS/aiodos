# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""La conversation (`POST /v1/chat/completions`, toujours en flux), les embeddings
(`POST /v1/embeddings`) et l'ouverture d'une page d'AIODOS (`POST /aiodos/v1/open`).

chat() accepte les messages au format d'OpenAI et les blocs d'Anthropic (text, image en
base64, tool_use, tool_result ; la réflexion est retirée), et les images en octets, en
data-URL ou en base64 (jamais par adresse : rien n'est téléchargé). Ce qu'AIODOS y ajoute
(docs/api-applications.md § 5.5) :
- `think=False` envoie `reasoning_effort: "none"` (coupe la réflexion) ;
- `where` envoie `X-Aiodos-Where`, `batch=True` `X-Aiodos-Priority: background`,
  `require_tools=False` `X-Aiodos-Tools: optional` ;
- `: aiodos loading N` (le modèle se charge) appelle on_loading(N) et remet à zéro le délai
  d'inactivité ; avant le premier morceau, `load_timeout` vaut si le modèle n'est pas chargé ;
- le dernier morceau porte `aiodos` : modèle, machine, lieu, `stopped_by` ;
- une erreur en cours de route (`data: {"error": …}`) lève la bonne exception.
"""

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from urllib.parse import unquote

from . import _discovery, _transport, errors, texts
from .access import normalize_where

log = logging.getLogger("aiodos_client")

PAGES = ("applications", "models", "jobs", "gallery")
_TEXT = ("text", "input_text", "output_text")
_THINKING = ("thinking", "redacted_thinking")
_IMAGE = ("image", "image_url")
_STOPPED_BY = {"stop": "stop", "length": "max_tokens", "tool_calls": "tool_calls"}
_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"BM", "image/bmp"))


@dataclass
class ToolCall:
    """Un appel d'outil demandé par le modèle : `arguments` analysés (dict, vide si le texte
    n'est pas du JSON valide), `raw` le texte tel qu'il est arrivé."""
    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    raw: str = ""


@dataclass
class ChatResult:
    """La réponse entière d'une conversation."""
    text: str = ""
    thinking: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str | None = None
    stopped_by: str | None = None       # stop, max_tokens, context, tool_calls
    usage: dict | None = None
    model: str | None = None            # l'identifiant concret qui a répondu
    machine: str | None = None
    place: str | None = None            # here, mine, friend
    truncated: bool = False             # coupée par max_tokens ou par la fin du contexte
    context_length: int | None = None


# --- Messages -------------------------------------------------------------------------
def _invalid(lang, name, **fields):
    return errors.make("invalid_request", lang, message=texts.error(name, lang, **fields))


def _sniff(raw: bytes) -> str:
    for signature, media_type in _SIGNATURES:
        if raw.startswith(signature):
            return media_type
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def image_url(value, media_type=None, lang="fr") -> str:
    """Une image (octets, data-URL ou base64) en data-URL. Une adresse (http…, file…) est
    refusée : rien n'est téléchargé."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if not raw:
            raise _invalid(lang, "bad_image")
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{media_type or _sniff(raw)};base64,{encoded}"
    if isinstance(value, str):
        text = value.strip()
        if text[:5].lower() == "data:":
            return text
        if "://" in text[:16] or text[:5].lower() == "file:":
            raise _invalid(lang, "image_by_url")
        compact = "".join(text.split())
        try:
            raw = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            raise _invalid(lang, "bad_image") from None
        if not raw:
            raise _invalid(lang, "bad_image")
        return f"data:{media_type or _sniff(raw)};base64,{compact}"
    raise _invalid(lang, "bad_image")


def _blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, dict):
        return [content]
    if isinstance(content, (list, tuple)):
        return [{"type": "text", "text": item} if isinstance(item, str) else item
                for item in content]
    raise TypeError("content : un texte, un bloc, ou une liste de blocs")


def _kind(block):
    if not isinstance(block, dict):
        raise TypeError("chaque bloc : un dict avec « type »")
    return block.get("type")


def _text_of(content):
    parts = []
    for block in _blocks(content):
        kind = _kind(block)
        if kind in _TEXT:
            parts.append(str(block.get("text") or ""))
        elif kind not in _THINKING:
            raise ValueError(f"bloc {kind!r} inattendu dans un texte")
    return "\n".join(parts)


def _image_part(block, lang):
    if block.get("type") == "image_url":
        target = block.get("image_url")
        url = target.get("url") if isinstance(target, dict) else target
        part = {"type": "image_url", "image_url": {"url": image_url(url, lang=lang)}}
        if isinstance(target, dict) and target.get("detail"):
            part["image_url"]["detail"] = target["detail"]
        return part
    source = block.get("source")                        # Anthropic
    if isinstance(source, dict):
        if source.get("type") != "base64":
            raise _invalid(lang, "image_by_url")
        return {"type": "image_url",
                "image_url": {"url": image_url(source.get("data"), source.get("media_type"),
                                               lang)}}
    data = block.get("data", block.get("image"))
    return {"type": "image_url",
            "image_url": {"url": image_url(data, block.get("media_type"), lang)}}


def _tool_result(block, lang):
    pieces, images = [], []
    for item in _blocks(block.get("content")):
        kind = _kind(item)
        if kind in _TEXT:
            pieces.append(str(item.get("text") or ""))
        elif kind in _IMAGE:
            images.append(_image_part(item, lang))
        elif kind not in _THINKING:
            raise ValueError(f"bloc {kind!r} inattendu dans un tool_result")
    text = "\n".join(pieces)
    if block.get("is_error"):
        text = f"Error: {text}" if text else "Error"
    return text, images


def _collapse(parts):
    if not parts:
        return ""
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def _user(message, lang):
    tool_messages, parts = [], []
    for block in _blocks(message.get("content")):
        kind = _kind(block)
        if kind in _TEXT:
            parts.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind in _IMAGE:
            parts.append(_image_part(block, lang))
        elif kind == "tool_result":
            text, images = _tool_result(block, lang)
            tool_messages.append({"role": "tool", "tool_call_id": block.get("tool_use_id"),
                                  "content": text})
            parts.extend(images)
        elif kind not in _THINKING:
            raise ValueError(f"bloc inconnu dans un message de l'utilisateur : {kind!r}")
    for image in message.get("images") or ():
        parts.append({"type": "image_url", "image_url": {"url": image_url(image, lang=lang)}})
    result = list(tool_messages)
    if parts or not tool_messages:
        result.append({"role": "user", "content": _collapse(parts)})
    return result


def _openai_call(call):
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return {"id": call.get("id"), "type": "function",
            "function": {"name": function.get("name"), "arguments": arguments}}


def _assistant(message):
    pieces = []
    calls = [_openai_call(call) for call in message.get("tool_calls") or ()
             if isinstance(call, dict)]
    for block in _blocks(message.get("content")):
        kind = _kind(block)
        if kind in _TEXT:
            pieces.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            calls.append({"id": block.get("id"), "type": "function",
                          "function": {"name": block.get("name"),
                                       "arguments": json.dumps(block.get("input") or {},
                                                               ensure_ascii=False)}})
        elif kind not in _THINKING:
            raise ValueError(f"bloc inconnu dans un message de l'assistant : {kind!r}")
    result = {"role": "assistant",
              "content": "".join(pieces) if pieces else (None if calls else "")}
    if calls:
        result["tool_calls"] = calls
    return result


def to_openai(messages, system=None, lang="fr") -> list:
    """Les messages (format d'OpenAI ou blocs d'Anthropic) au format d'OpenAI, la réflexion
    retirée, les images en data-URL. `system` (texte ou blocs) passe en tête."""
    if isinstance(messages, (str, dict)):
        raise TypeError("messages : une liste de messages")
    result = []
    if system:
        result.append({"role": "system",
                       "content": system if isinstance(system, str) else _text_of(system)})
    for message in messages:
        if not isinstance(message, dict):
            raise TypeError("chaque message : un dict avec « role »")
        role = message.get("role")
        content = message.get("content")
        if role in ("system", "developer"):
            result.append({"role": "system",
                           "content": content if isinstance(content, str) else _text_of(content)})
        elif role == "tool":
            result.append({"role": "tool", "tool_call_id": message.get("tool_call_id"),
                           "content": content if isinstance(content, str) else _text_of(content)})
        elif role == "user":
            result.extend(_user(message, lang))
        elif role == "assistant":
            result.append(_assistant(message))
        else:
            raise ValueError(f"rôle inconnu : {role!r}")
    return result


def to_openai_tools(tools) -> list:
    """Les outils au format d'OpenAI (ceux d'Anthropic, {name, description, input_schema},
    sont convertis)."""
    result = []
    for tool in tools or ():
        if not isinstance(tool, dict):
            raise TypeError("chaque outil : un dict")
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            result.append(tool)
        elif isinstance(tool.get("name"), str):
            result.append({"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or tool.get("parameters")
                or {"type": "object", "properties": {}}}})
        else:
            raise ValueError("outil inconnu : ni format d'OpenAI, ni format d'Anthropic")
    return result


def _has_images(messages):
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(part, dict)
                                             and part.get("type") == "image_url"
                                             for part in content):
            return True
    return False


# --- Lecture de la réponse ------------------------------------------------------------
def _loading(comment):
    """Les secondes d'un commentaire « aiodos loading N », ou None pour un autre."""
    words = comment.split()
    if len(words) < 2 or words[0] != "aiodos" or words[1] != "loading":
        return None
    try:
        seconds = float(words[2]) if len(words) > 2 else 0.0
    except ValueError:
        return 0
    return int(seconds) if seconds.is_integer() else seconds


class _Collector:
    """Rassemble les morceaux d'une réponse (texte, réflexion, appels d'outils par index)."""

    def __init__(self, on_text, on_thinking, lang):
        self.on_text = on_text
        self.on_thinking = on_thinking
        self.lang = lang
        self.text = []
        self.thinking = []
        self.calls = {}
        self.finish_reason = None
        self.stopped_by = None
        self.usage = None
        self.model = None
        self.machine = None
        self.place = None
        self.context_length = None

    def headers(self, headers):
        """Qui répond, d'après les en-têtes (le dernier morceau le redit) ; le modèle et la
        machine y sont encodés pour cent, en UTF-8."""
        model = headers.get("X-Aiodos-Model")
        self.model = unquote(model, encoding="utf-8", errors="replace") if model else None
        machine = headers.get("X-Aiodos-Machine")
        self.machine = unquote(machine, encoding="utf-8", errors="replace") if machine else None
        self.place = headers.get("X-Aiodos-Place") or None

    def _text(self, piece):
        if isinstance(piece, str) and piece:
            self.text.append(piece)
            if self.on_text is not None:
                self.on_text(piece)

    def _thought(self, piece):
        if isinstance(piece, str) and piece:
            self.thinking.append(piece)
            if self.on_thinking is not None:
                self.on_thinking(piece)

    def _extension(self, ext):
        if not isinstance(ext, dict):
            return
        for name in ("model", "machine", "place", "stopped_by"):
            if isinstance(ext.get(name), str) and ext[name]:
                setattr(self, name, ext[name])
        length = ext.get("context_length")
        if isinstance(length, int) and not isinstance(length, bool):
            self.context_length = length

    def chunk(self, chunk):
        if chunk.get("error"):
            raise errors.from_response(None, None, {"error": chunk["error"]}, self.lang)
        for choice in chunk.get("choices") or ():
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                self._text(delta.get("content"))
                thought = delta.get("reasoning_content")
                self._thought(thought if thought is not None else delta.get("reasoning"))
                for call in delta.get("tool_calls") or ():
                    if isinstance(call, dict):
                        self._call_piece(call)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        self._extension(chunk.get("aiodos"))
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        if not self.model and isinstance(chunk.get("model"), str):
            self.model = chunk["model"]

    def _call_piece(self, call):
        index = call.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = 0
        entry = self.calls.setdefault(index, {"id": None, "name": None, "arguments": []})
        if call.get("id"):
            entry["id"] = call["id"]
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if function.get("name") and not entry["name"]:
            entry["name"] = function["name"]
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            entry["arguments"].append(arguments)
        elif isinstance(arguments, dict):
            entry["arguments"].append(json.dumps(arguments, ensure_ascii=False))

    def whole(self, data):
        """Une réponse sans flux (chat.completion)."""
        if not isinstance(data, dict):
            raise errors.make("protocol", self.lang)
        if data.get("error"):
            raise errors.from_response(None, None, {"error": data["error"]}, self.lang)
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        self._text(message.get("content"))
        thought = message.get("reasoning_content")
        self._thought(thought if thought is not None else message.get("reasoning"))
        for index, call in enumerate(message.get("tool_calls") or ()):
            if isinstance(call, dict):
                converted = _openai_call(call)
                self.calls[index] = {"id": converted["id"],
                                     "name": converted["function"]["name"],
                                     "arguments": [converted["function"]["arguments"]]}
        self.finish_reason = first.get("finish_reason") or self.finish_reason
        if isinstance(data.get("usage"), dict):
            self.usage = data["usage"]
        if not self.model and isinstance(data.get("model"), str):
            self.model = data["model"]
        self._extension(data.get("aiodos"))

    def result(self) -> ChatResult:
        calls = []
        for index in sorted(self.calls):
            entry = self.calls[index]
            raw = "".join(entry["arguments"])
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                log.debug("arguments d'outil illisibles pour %s : gardés dans raw", entry["name"])
                parsed = {}
            calls.append(ToolCall(id=entry["id"] or f"call_{index}", name=entry["name"] or "",
                                  arguments=parsed, raw=raw))
        stopped_by = self.stopped_by or _STOPPED_BY.get(self.finish_reason)
        return ChatResult(text="".join(self.text), thinking="".join(self.thinking),
                          tool_calls=calls, finish_reason=self.finish_reason,
                          stopped_by=stopped_by, usage=self.usage, model=self.model,
                          machine=self.machine, place=self.place,
                          truncated=self.finish_reason == "length"
                          or stopped_by in ("max_tokens", "context"),
                          context_length=self.context_length)


def read_chat(exchange, on_text=None, on_thinking=None, on_loading=None, lang="fr"):
    """La réponse d'une conversation, lue sur un échange ouvert (flux SSE, ou JSON entier)."""
    response = exchange.response
    collected = _Collector(on_text, on_thinking, lang)
    collected.headers(response.headers)
    if _transport.content_type(response.headers) == "application/json":
        body = exchange.read_body()
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise errors.make("protocol", lang) from None
        collected.whole(data)
        return collected.result()
    done = False
    for kind, value in exchange.events():
        if kind == "comment":
            seconds = _loading(value)
            if seconds is not None and on_loading is not None:
                on_loading(seconds)
            continue
        if value.strip() == "[DONE]":
            done = True
            break
        try:
            chunk = json.loads(value)
        except ValueError:
            raise errors.make("protocol", lang) from None
        if not isinstance(chunk, dict):
            raise errors.make("protocol", lang)
        collected.chunk(chunk)
    if not done and collected.finish_reason is None:
        raise errors.make("protocol", lang, message=texts.error("cut", lang))
    return collected.result()


# --- Les trois fonctions ----------------------------------------------------------------
def chat(client, messages, model=None, system=None, tools=None, think=None, max_tokens=None,
         temperature=None, top_p=None, stop=None, seed=None, response_format=None, where=None,
         batch=False, require_tools=True, on_text=None, on_thinking=None, on_loading=None,
         cancel=None, timeout=None) -> ChatResult:
    """Une conversation, toujours en flux.

    `model` : un identifiant du catalogue (ou un Model) ; absent, le meilleur modèle permis
    (outils et vision selon la demande). `think` : False coupe la réflexion, un texte
    (« low »…) la règle. `on_text(morceau)`, `on_thinking(morceau)`, `on_loading(secondes)`
    sont appelés dans le fil de l'appelant ; `cancel` (fonction ou threading.Event) arrête
    vraiment le calcul (Cancelled, message « Arrêté ») ; `timeout` : le délai d'inactivité.
    """
    lang = client.lang
    converted = to_openai(messages, system, lang)
    tool_list = to_openai_tools(tools) if tools else None
    circles = normalize_where(where)
    if model is None:
        needs = []
        if tool_list and require_tools:
            needs.append("tools")
        if _has_images(converted):
            needs.append("vision")
        chosen = client.models("chat").best(needs=needs)
        if chosen is None:
            raise errors.make("model_not_found", lang,
                              message=texts.error("no_model", lang,
                                                  needs=", ".join(needs) or "chat"),
                              hint=texts.hint("no_model", lang), detail={"needs": needs})
        model_id = chosen.id
    else:
        model_id = str(getattr(model, "id", model))
    body = {"model": model_id, "messages": converted, "stream": True,
            "stream_options": {"include_usage": True}}
    if tool_list:
        body["tools"] = tool_list
    if think is False:
        body["reasoning_effort"] = "none"
    elif isinstance(think, str) and think:
        body["reasoning_effort"] = think
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    if temperature is not None:
        body["temperature"] = float(temperature)
    if top_p is not None:
        body["top_p"] = float(top_p)
    if stop is not None:
        body["stop"] = stop
    if seed is not None:
        body["seed"] = int(seed)
    if response_format is not None:
        body["response_format"] = response_format
    headers = {}
    if circles:
        headers["X-Aiodos-Where"] = ",".join(circles)
    if batch:
        headers["X-Aiodos-Priority"] = "background"
    if not require_tools:
        headers["X-Aiodos-Tools"] = "optional"
    idle = float(timeout or client.timeout)
    known = client._known_model(model_id)
    first = idle if known is not None and known.loaded else max(idle, client.load_timeout)
    exchange = client._stream("/v1/chat/completions", body, headers=headers,
                              first_timeout=first, timeout=idle, cancel=cancel, feature="chat")
    with exchange:
        return read_chat(exchange, on_text, on_thinking, on_loading, lang)


def embed(client, texts_, model=None) -> list:
    """Les vecteurs des textes (un texte ou une liste), dans l'ordre : list[list[float]]."""
    lang = client.lang
    items = [texts_] if isinstance(texts_, str) else list(texts_)
    if not all(isinstance(item, str) for item in items):
        raise TypeError("embed : des textes")
    if not items:
        return []
    if model is None:
        chosen = client.models("embed").best()
        if chosen is None:
            raise errors.make("model_not_found", lang,
                              message=texts.error("no_model", lang, needs="embed"),
                              hint=texts.hint("no_model", lang), detail={"needs": ["embed"]})
        model_id = chosen.id
    else:
        model_id = str(getattr(model, "id", model))
    data = client._call("POST", "/v1/embeddings", body={"model": model_id, "input": items},
                        feature="embed")
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != len(items):
        raise errors.make("protocol", lang)
    try:
        ordered = sorted(rows, key=lambda row: row["index"])
        vectors = [[float(x) for x in row["embedding"]] for row in ordered]
    except (KeyError, TypeError, ValueError):
        raise errors.make("protocol", lang) from None
    return vectors


def open_in_aiodos(client, page, model=None) -> bool:
    """Ramène la fenêtre d'AIODOS sur une page ; vrai si une fenêtre l'a montrée."""
    if page not in PAGES:
        raise ValueError(f"page : parmi {', '.join(PAGES)}")
    _, disc, applies = client._resolve()
    if applies and disc.running:
        _discovery.allow_foreground(disc.pid)       # la fenêtre d'AIODOS peut passer devant
    data = client._call("POST", "/aiodos/v1/open", body={"page": page, "model": model},
                        feature="open")
    return isinstance(data, dict) and data.get("opened") is True
