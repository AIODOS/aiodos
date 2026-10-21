"""API compatible Ollama (/api/…) : Open WebUI, AnythingLLM, Continue, les bibliothèques
ollama pour Python et JavaScript, curl.

Formats vérifiés le 17/10/2026 dans la documentation d'Ollama (docs.ollama.com), son code
(v0.40.5) et celui des outils : /api/tags (« name » et « model » toujours), /api/chat et
/api/generate en flux NDJSON (application/x-ndjson, chaque ligne avec message.role et
message.content, la dernière avec done, done_reason et les compteurs), /api/embed et
l'ancienne /api/embeddings, /api/show (« model » ou l'ancien « name », que Continue
envoie encore ; model_info toujours présent), /api/version, /api/ps et « / » (« Ollama is
running »). Erreurs : {"error": "…"}.

Sur cette machine, la demande est relayée telle quelle à Ollama (answer.local_raw) ; quand
le moteur intégré tient lieu d'Ollama (engine.py), une conversation en texte passe par la
façade, et images, outils et format imposé par son routeur. Une machine appairée reçoit la
conversation en texte. Ces réponses-là sont remises ici au format d'Ollama (Lines). Pas de
gestion des modèles (pull, create, delete…) par la passerelle.
"""
import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from aiodos.gateway import answer, catalog, engine, errors, openai_api, scopes
from aiodos.gateway.answer import GatewayError
from aiodos.gateway.errors import ApiError
from aiodos.network import placement, remote_models
from aiodos.services import ollama

router = APIRouter()
NDJSON = "application/x-ndjson"
FORMATS = "0.40.5"          # version d'Ollama dont la passerelle suit les formats (plus haut)


def _error(exc, lang: str = "fr") -> Response:
    """{"error": "…", "code": "…", "hint": "…"} : les clients d'Ollama ne lisent que
    « error », les applis le code."""
    if isinstance(exc, answer.Upstream):
        return Response(exc.body, status_code=exc.status, media_type=exc.content_type)
    return errors.response("/api/", openai_api.api_error(exc), lang)


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


# ------------------------------------------------------------------ état
@router.api_route("/", methods=["GET", "HEAD"])
def root():
    return PlainTextResponse("Ollama is running")    # le texte qu'attendent les outils


@router.api_route("/api/version", methods=["GET", "HEAD"])
async def version():
    # Un numéro « 0.40.5 » : Open WebUI le lit nombre par nombre. Le moteur intégré, lui,
    # donnerait celui de llama.cpp : à sa place, celui des formats que suit la passerelle
    if await asyncio.to_thread(ollama.use_integrated):
        return {"version": FORMATS}
    return {"version": await asyncio.to_thread(ollama.version) or "0.0.0"}


def _tag(entry: dict) -> dict:
    return {"name": entry["id"], "model": entry["id"], "modified_at": entry["modified_at"],
            "size": entry["size"], "digest": entry["digest"], "details": entry["details"],
            "capabilities": entry["capabilities"]}


@router.api_route("/api/tags", methods=["GET", "HEAD"])
async def tags(request: Request):
    try:
        entries = openai_api.visible(request, await asyncio.to_thread(catalog.entries))
    except ApiError as exc:
        return _error(exc, errors.lang_of(request.headers))
    return {"models": [_tag(e) for e in entries]}


@router.get("/api/ps")
async def running():
    if await asyncio.to_thread(ollama.use_integrated):
        return await asyncio.to_thread(engine.running)
    try:
        status, content, kind = await asyncio.to_thread(answer.local_get, "/api/ps")
    except GatewayError:
        return {"models": []}
    return Response(content, status_code=status, media_type=kind)


@router.post("/api/show")
async def show(request: Request):
    try:
        body = await _body(request)
        model_id = body.get("model") or body.get("name")
        model = catalog.internal(model_id)
    except (GatewayError, catalog.UnknownModel) as exc:
        return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status", 404))
    name = placement.parse(model) or model
    if not remote_models.parse(model):
        tags = await asyncio.to_thread(_local_names)
        name = next((t for t in tags if placement.same_model(t, name)), name)
        if name in tags and await asyncio.to_thread(ollama.use_integrated):
            shown = await asyncio.to_thread(engine.show, name)
            if shown is not None:
                return shown
        elif name in tags:
            try:
                status, content, kind = await asyncio.to_thread(
                    answer.local_post, "/api/show", {**body, "model": name, "name": None})
            except GatewayError as exc:
                return _error(exc)
            return Response(content, status_code=status, media_type=kind)
    entry = await asyncio.to_thread(catalog.find, model_id)
    if entry is None:
        return JSONResponse({"error": f"model '{model_id}' not found"}, status_code=404)
    family = entry["details"].get("family") or ""
    return {"license": "", "modelfile": "", "parameters": "", "template": "",
            "details": entry["details"],
            "model_info": {"general.architecture": family} if family else {},
            "capabilities": entry["capabilities"], "modified_at": entry["modified_at"]}


def _local_names() -> set:
    try:
        return {t.get("name") or t.get("model") for t in catalog.local_tags()}
    except Exception:
        return set()


# --------------------------------------------------------------- embeddings
async def _embed(request: Request, path: str):
    try:
        where = scopes.for_request(request, "embed")
        if where is not None and "here" not in where:
            raise ApiError("where_denied", "Les embeddings ne se calculent que sur ce PC, que "
                           "cette requête exclut.", detail={"allowed": ["here"]})
    except ApiError as exc:
        return _error(exc, errors.lang_of(request.headers))
    try:
        body = await _body(request)
        name = catalog.local_name(body.get("model"))
        if await asyncio.to_thread(ollama.use_integrated):
            return await asyncio.to_thread(_embedded, path, body, name)
        status, content, kind = await asyncio.to_thread(answer.local_post, path,
                                                        {**body, "model": name})
    except catalog.UnknownModel as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except GatewayError as exc:
        return _error(exc)
    return Response(content, status_code=status, media_type=kind)


def _embedded(path: str, body: dict, name: str) -> dict:
    """Embeddings du moteur intégré, au format de l'une ou l'autre route d'Ollama. Le nombre
    de jetons lus n'est pas connu ici (0)."""
    started = time.perf_counter()
    if path == "/api/embeddings":                 # l'ancienne route : un texte, « prompt »
        prompt = body.get("prompt") or ""
        if not isinstance(prompt, str):
            raise GatewayError(400, "« prompt » doit être du texte.")
        vectors = engine.embed(name, [prompt] if prompt else [])
        return {"embedding": vectors[0] if vectors else []}
    texts = body.get("input")
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise GatewayError(400, "« input » doit être un texte ou une liste de textes.")
    vectors = engine.embed(name, texts, body.get("dimensions"))
    return {"model": body.get("model"), "embeddings": vectors,
            "total_duration": int((time.perf_counter() - started) * 1e9), "load_duration": 0,
            "prompt_eval_count": 0}


@router.post("/api/embed")
async def embed(request: Request):
    return await _embed(request, "/api/embed")


@router.post("/api/embeddings")
async def embeddings(request: Request):             # ancienne route, encore servie
    return await _embed(request, "/api/embeddings")


# ------------------------------------------------------------ conversation
def plain_messages(messages) -> list:
    """La conversation telle qu'une machine appairée la reçoit : rôles system, user et
    assistant, texte seul ; un résultat d'outil devient un message de l'utilisateur."""
    if not isinstance(messages, list):
        raise GatewayError(400, "« messages » doit être une liste.")
    out = []
    for m in messages:
        if not isinstance(m, dict):
            raise GatewayError(400, "Message invalide.")
        role = str(m.get("role") or "user").lower()
        content = m.get("content") or ""
        if not isinstance(content, str):
            raise GatewayError(400, "Le contenu d'un message doit être du texte.")
        if role == "tool":
            role, content = "user", f"[Résultat d'un outil] {content}"
        elif role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": content})
    return out


class Lines:
    """Réponse calculée ailleurs que par l'Ollama d'ici (machine appairée, moteur intégré)
    remise au format d'Ollama (chat ou generate)."""

    def __init__(self, model_id: str, chat: bool):
        self.model_id = model_id
        self.chat = chat
        self.started = time.perf_counter()
        self.stats = {}
        self.content = []
        self.thinking = []
        self.calls = []

    def _line(self, content: str, thinking: str = "", done=False, calls=None) -> dict:
        line = {"model": self.model_id, "created_at": catalog.now_iso()}
        if self.chat:
            message = {"role": "assistant", "content": content}
            if thinking:
                message["thinking"] = thinking
            if calls:
                message["tool_calls"] = calls
            line["message"] = message
        else:
            line["response"] = content
            if thinking:
                line["thinking"] = thinking
        line["done"] = done
        return line

    def take(self, kind: str, value) -> dict | None:
        """Ligne de flux pour un couple (genre, valeur), ou None."""
        if kind == "stats" and isinstance(value, dict):
            self.stats = value
        elif kind == "content":
            self.content.append(value)
            return self._line(value)
        elif kind == "thinking":
            self.thinking.append(value)
            return self._line("", value)
        elif kind == "tool_calls" and value:          # moteur intégré : appels d'outils
            self.calls.extend(value)
            return self._line("", calls=value)
        return None

    def final(self, whole=False) -> dict:
        """Dernière ligne (whole : avec toute la réponse, pour stream: false)."""
        elapsed = int((time.perf_counter() - self.started) * 1e9)
        line = self._line("".join(self.content) if whole else "",
                          "".join(self.thinking) if whole else "", done=True,
                          calls=self.calls if whole else None)
        line.update({"done_reason": self.stats.get("done_reason") or "stop",
                     "total_duration": elapsed, "load_duration": 0,
                     "prompt_eval_count": int(self.stats.get("prompt_tokens") or 0),
                     "prompt_eval_duration": 0,
                     "eval_count": int(self.stats.get("completion_tokens") or 0),
                     "eval_duration": elapsed})
        return line


def _ndjson(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


async def _respond(stream: answer.Stream, head: list, model_id: str, chat: bool,
                   streaming: bool, request: Request):
    """Réponse HTTP une fois la machine engagée : relais tel quel (l'Ollama d'ici) ou mise
    au format d'Ollama (machine appairée, moteur intégré). Les en-têtes X-Aiodos-* disent qui
    a répondu."""
    response = await _respond_body(stream, head, model_id, chat, streaming, request)
    for name, value in openai_api._route_headers(head).items():
        response.headers[name] = value
    return response


async def _respond_body(stream: answer.Stream, head: list, model_id: str, chat: bool,
                        streaming: bool, request: Request):
    raw = next((value for kind, value in head if kind == "head"), None)
    if raw is not None:
        async def relay():
            try:
                for kind, value in head:
                    if kind == "raw":
                        yield value
                while (item := await stream.next()) is not None:
                    if item[0] == "raw":
                        yield item[1]
            except Exception as exc:
                yield _ndjson({"error": str(exc)})
            finally:
                stream.close()
        return StreamingResponse(relay(), media_type=raw)

    remote = Lines(model_id, chat)
    if not streaming:
        try:
            for item in head:
                remote.take(*item)
            while (item := await stream.next(request)) is not None:
                remote.take(*item)
        except Exception as exc:
            return _error(exc)
        finally:
            stream.close()
        return JSONResponse(remote.final(whole=True))

    async def lines():
        try:
            for item in head:
                if (line := remote.take(*item)) is not None:
                    yield _ndjson(line)
            while (item := await stream.next()) is not None:
                if (line := remote.take(*item)) is not None:
                    yield _ndjson(line)
            yield _ndjson(remote.final())
        except Exception as exc:
            yield _ndjson({"error": str(exc)})      # comme Ollama : le statut est parti
        finally:
            stream.close()
    return StreamingResponse(lines(), media_type=NDJSON)


def _think(body: dict) -> bool:
    return body.get("think") is not False


def _options(body: dict) -> dict | None:
    return body.get("options") if isinstance(body.get("options"), dict) else None


async def _answer(request: Request, path: str, body: dict, messages, *, chat: bool,
                  local_only: bool, prefer_local: bool, here):
    """here(modèle, stop) : la réponse du moteur intégré de cette machine, quand il tient
    lieu d'Ollama (engine.py)."""
    lang = errors.lang_of(request.headers)
    model_id = str(body.get("model") or "")
    if not model_id:
        return JSONResponse({"error": "model is required", "code": "invalid_request"},
                            status_code=400)
    streaming = body.get("stream") is not False          # en flux par défaut
    executor = _executor(request)
    context = request.state.aiodos_app
    app_key = context is not None and context.kind != "general"
    # Une appli ne charge ni ne décharge rien (invariant I5) : elle ne décide pas pour les
    # autres ; une requête sans rien à calculer lui répond aussitôt
    if app_key and messages is None:
        line = Lines(model_id, chat).final()
        line.update(done_reason="load", total_duration=0, eval_duration=0)
        return JSONResponse(line)
    require_tools = (app_key and prefer_local and request.headers.get(
        "x-aiodos-tools", "").strip().lower() != "optional")
    app = answer.for_app(request)
    try:
        where = scopes.for_request(request, "chat")
        models = await asyncio.to_thread(answer.plan, model_id, executor,
                                         local_only=local_only, prefer_local=prefer_local,
                                         where=where, tools=prefer_local,
                                         require_tools=require_tools)
    except (GatewayError, ApiError) as exc:
        return _error(exc, lang)
    think, options = _think(body), _options(body)
    unload = body.get("keep_alive") in (0, "0", "0s") and not app_key

    def attempt(model, stop):
        if not answer.is_local(model):
            if messages is None:          # chargement ou déchargement : rien à calculer là-bas
                yield "content", ""
            else:                         # en texte, seulement pour une autre machine
                yield from answer.remote(model, plain_messages(messages), think, options, stop)
        elif not ollama.use_integrated():
            yield from answer.local_raw(path, {**body, "model": model}, stop)
        elif messages is None:
            engine.load(model, unload)
            yield "content", ""
        else:
            yield from here(model, stop)

    stream = answer.Stream(answer.run(models, attempt, executor,
                                      interactive=app["interactive"]), cut=app["cut"])
    try:
        head = await stream.head(request)
    except Exception as exc:
        stream.close()
        return _error(exc, lang)
    if messages is None and raw_head(head) is None:
        stream.close()
        line = Lines(model_id, chat).final()
        line.update(done_reason="unload" if unload else "load", total_duration=0,
                    eval_duration=0)
        return JSONResponse(line)
    return await _respond(stream, head, model_id, chat, streaming, request)


def raw_head(head):
    return next((value for kind, value in head if kind == "head"), None)


@router.post("/api/chat")
async def chat(request: Request):
    try:
        body = await _body(request)
    except GatewayError as exc:
        return _error(exc)
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return JSONResponse({"error": "« messages » doit être une liste."}, status_code=400)
    images = any(isinstance(m, dict) and m.get("images") for m in messages)
    tools = bool(body.get("tools")) or any(
        isinstance(m, dict) and (m.get("tool_calls") or m.get("role") == "tool")
        for m in messages)
    special = images or tools or bool(body.get("format"))

    def here(model, stop):                # le texte par la façade, le reste par le routeur
        if special:
            return engine.ollama_chat(model, body, messages, stop)
        return engine.converse(model, plain_messages(messages), _think(body), _options(body),
                               stop)
    return await _answer(request, "/api/chat", body, messages or None, chat=True,
                         local_only=images or bool(body.get("format")), prefer_local=tools,
                         here=here)


@router.post("/api/generate")
async def generate(request: Request):
    try:
        body = await _body(request)
    except GatewayError as exc:
        return _error(exc)
    prompt = body.get("prompt") or ""
    if not isinstance(prompt, str):
        return JSONResponse({"error": "« prompt » doit être du texte."}, status_code=400)
    special = any(body.get(k) for k in ("images", "suffix", "raw", "format", "template",
                                        "context"))
    messages = None
    if prompt:
        system = body.get("system")
        messages = ([{"role": "system", "content": system}] if isinstance(system, str)
                    and system else []) + [{"role": "user", "content": prompt}]

    def here(model, stop):
        if body.get("template") or body.get("context"):
            raise GatewayError(400, "« template » et « context » (propres à Ollama) ne sont pas "
                               "pris par le moteur intégré : envoie la conversation entière par "
                               "/api/chat.")
        suffix = body.get("suffix")
        if suffix or body.get("raw"):     # complétion de code, texte brut
            return engine.complete(model, prompt, suffix if isinstance(suffix, str) else None,
                                   _options(body), stop)
        if body.get("images") or body.get("format"):
            question = {**messages[-1], "images": body.get("images") or []}
            return engine.ollama_chat(model, body, [*messages[:-1], question], stop)
        return engine.converse(model, messages, _think(body), _options(body), stop)
    return await _answer(request, "/api/generate", body, messages, chat=False,
                         local_only=special, prefer_local=False, here=here)


# ------------------------------------------------------------- le reste
@router.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
def unknown(rest: str):
    return JSONResponse({"error": f"« /api/{rest} » n'est pas servi par la passerelle "
                         "d'AIODOS : les modèles s'installent et se gèrent dans AIODOS "
                         "(« Installer un modèle ») ou avec Ollama.", "code": "not_supported",
                         "hint": "Installe le modèle dans AIODOS."}, status_code=404)
