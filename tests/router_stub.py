"""Faux moteur intégré pour les essais : le routeur de llama.cpp (llama-server en mode
routeur) joué par un vrai serveur HTTP sur 127.0.0.1, aux formats de llama.cpp b11707
relevés le 20/10/2026 (octets réels, cf. aiodos/gateway/engine.py), avec des réponses
connues d'avance ; et les fichiers de faux modèles qu'il sert, posés comme ceux qu'AIODOS
télécharge (data/models/<dossier>/model.json, en-tête GGUF lisible par llm/models.py).

    install(["petit:1b", ...], vision=[...], embedding=[...], thinking=[...])
    with RouterStub(integrated._router.key, vision={"voyant-4b"}, fim={"code-7b"}) as router:
        router.url        # ce que integrated._router.url() doit rendre
        router.requests   # (chemin, corps) de chaque demande reçue
"""
import asyncio
import json
import struct
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aiodos.llm import models
from tests.conftest import _bag_of_words
from tests.nodes import free_port

ANSWER = "Bonjour du moteur intégré"
THOUGHT = "Je réfléchis."
AFTER_TOOL = "Il fait beau à Lyon."
SEEN = "Je vois une image."
JSON_ANSWER = '{"ville": "Lyon"}'
COMPLETION = " return 1"
INFILL = "    a, b = 0, 1"
CALL = ("meteo", ['{"', 'ville": "', 'Lyon"}'])          # appel d'outil, arguments par bouts
MAX_WORDS = 50                     # au-delà, un texte est « trop long » pour l'embedding
FINGERPRINT = "b11707-1268edac5"
NO_IMAGES = ("image input is not supported - hint: if this is unexpected, you may need to "
             "provide the mmproj")
NO_INFILL = ("Infill is not supported by this model: prefix token is missing. suffix token is "
             "missing. middle token is missing. ")
NOT_ALTERNATING = ("Unable to generate parser for this template. Automatic parser generation "
                   "failed: \n------------\nError: Jinja Exception: Conversation roles must "
                   "alternate user/assistant/user/assistant/...")


# ------------------------------------------------------------------ faux modèles
def _gguf(path, arch="llama", pooling=None, template=""):
    """En-tête GGUF minimal (sans tenseurs), lisible par cluster/weights.header."""
    def string(text):
        data = text.encode("utf-8")
        return struct.pack("<Q", len(data)) + data
    kv = [("general.architecture", 8, string(arch)),
          (f"{arch}.block_count", 4, struct.pack("<I", 2)),
          (f"{arch}.embedding_length", 4, struct.pack("<I", 8))]
    if pooling is not None:
        kv.append((f"{arch}.pooling_type", 4, struct.pack("<I", pooling)))
    if template:
        kv.append(("tokenizer.chat_template", 8, string(template)))
    body = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv))
    for key, kind, value in kv:
        body += string(key) + struct.pack("<I", kind) + value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + b"\0" * 64)


def install(names, vision=(), embedding=(), thinking=(), tools=()):
    """Faux modèles d'AIODOS dans data/models : leurs capacités viennent de leur fichier
    (projecteur d'images, mise en commun d'un modèle d'embedding, gabarit qui parle de
    réflexion ou d'outils), comme pour les vrais (llm/integrated.capabilities)."""
    for name in names:
        folder = models.models_dir() / models.router_id(name)
        template = " ".join(["{{ messages }}", *(["enable_thinking"] if name in thinking else []),
                             *(["tools"] if name in tools else [])])
        is_embedding = name in embedding
        _gguf(folder / "poids.gguf", arch="bert" if is_embedding else "llama",
              pooling=1 if is_embedding else None, template="" if is_embedding else template)
        meta = {"name": name, "file": "poids.gguf", "license": "Apache-2.0",
                "kind": "embedding" if is_embedding else "chat", "family": "llama",
                "parameters": "1B", "quantization": "Q4_K_M", "sha256": f"empreinte-{name}"}
        if name in vision:
            _gguf(folder / "projecteur.gguf", arch="clip")
            meta["mmproj"] = "projecteur.gguf"
        (folder / "model.json").write_text(json.dumps(meta), encoding="utf-8")
    models.forget_cache()


def _pieces(text: str) -> list:
    words = text.split(" ")
    return [w if i == 0 else " " + w for i, w in enumerate(words)]


# ------------------------------------------------------------------- faux routeur
class RouterStub:
    def __init__(self, key: str, vision=(), fim=(), embedding=(), strict=(), broken=(),
                 delay: float = 0.0):
        self.key = key
        self.vision, self.fim, self.embedding = set(vision), set(fim), set(embedding)
        self.strict = set(strict)          # gabarit qui exige l'alternance (mistral)
        self.broken = set(broken)          # ne se charge pas
        self.delay = delay                 # secondes entre deux morceaux d'une réponse en flux
        self.requests = []
        self.loaded = {}                   # identifiant -> « loaded »
        self.unloaded = []
        self.cut = threading.Event()       # un flux s'est arrêté avant la fin (outil parti)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = None
        self._thread = None

    def __enter__(self):
        self._server = uvicorn.Server(uvicorn.Config(self._app(), host="127.0.0.1",
                                                     port=self.port, log_level="warning",
                                                     log_config=None))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(5)

    def bodies(self, path: str) -> list:
        return [body for p, body in self.requests if p == path]

    # -------------------------------------------------------------- réponses
    def _chat(self, body: dict) -> dict:
        """Ce que répond le faux modèle : réflexion, texte ou appel d'outil, fin, usage."""
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        parts = [p for m in messages if isinstance(m.get("content"), list) for p in m["content"]]
        thinks = (body.get("chat_template_kwargs") or {}).get("enable_thinking") is not False
        if body.get("tools") and last.get("role") != "tool":
            return {"thinking": THOUGHT if thinks else "", "content": "", "call": CALL,
                    "finish": "tool_calls"}
        if last.get("role") == "tool":
            text = AFTER_TOOL
        elif any(p.get("type") == "image_url" for p in parts):
            text = SEEN
        elif body.get("response_format"):
            text = JSON_ANSWER
        else:
            text = ANSWER
        limit = body.get("max_tokens")
        finish = "length" if isinstance(limit, int) and limit < len(_pieces(text)) else "stop"
        return {"thinking": THOUGHT if thinks else "", "content": text, "call": None,
                "finish": finish}

    def _refused(self, model: str, path: str, body: dict):
        """Les refus de llama-server, aux mêmes codes et messages."""
        if model in self.broken:
            return 500, f"model name={model} failed to load", "server_error"
        messages = body.get("messages") or []
        parts = {p.get("type") for m in messages if isinstance(m.get("content"), list)
                 for p in m["content"]}
        if path == "/v1/chat/completions":
            if "image_url" in parts and model not in self.vision:
                return 500, NO_IMAGES, "server_error"
            if "input_audio" in parts:
                return 500, "audio input is not supported - hint: if this is unexpected, you "\
                            "may need to provide the mmproj", "server_error"
            roles = [m.get("role") for m in messages]
            if model in self.strict and (
                    "system" in roles[1:] or any(a == b for a, b in zip(roles, roles[1:]))):
                return 400, NOT_ALTERNATING, "invalid_request_error"
        if path == "/infill" and model not in self.fim:
            return 501, NO_INFILL, "not_supported_error"
        if path == "/v1/embeddings":
            if model not in self.embedding:
                return 501, ("This server does not support embeddings. Start it with "
                             "`--embeddings`"), "not_supported_error"
            for text in body.get("input") or []:
                if len(text.split()) > MAX_WORDS:
                    return 500, (f"input ({2 * len(text.split())} tokens) is too large to "
                                 "process. increase the physical batch size (current batch "
                                 "size: 512)"), "server_error"
        return None

    def _app(self) -> FastAPI:
        app = FastAPI()
        stub = self

        def error(code: int, message: str, kind: str):
            return JSONResponse({"error": {"code": code, "message": message, "type": kind}},
                                status_code=code)

        async def paced(chunks):
            """Un flux SSE ; noté coupé si l'outil (la passerelle) part avant la fin."""
            done = False
            try:
                for chunk in chunks:
                    if stub.delay:
                        await asyncio.sleep(stub.delay)
                    data = chunk if chunk == "[DONE]" else json.dumps(chunk, ensure_ascii=False)
                    yield f"data: {data}\n\n".encode("utf-8")
                done = True
            finally:
                if not done:
                    stub.cut.set()

        @app.get("/models")
        def listed():
            return {"data": [{"id": i, "status": {"value": v}} for i, v in stub.loaded.items()]}

        @app.post("/{path:path}")
        async def post(path: str, request: Request):
            if request.headers.get("authorization") != f"Bearer {stub.key}":
                return error(401, "Invalid API Key", "authentication_error")
            body = await request.json()
            path = "/" + path
            stub.requests.append((path, body))
            model = body.get("model")
            if path == "/models/load":
                if model in stub.loaded:
                    return error(400, "model is already running", "invalid_request_error")
                stub.loaded[model] = "loaded"
                return {"success": True}
            if path == "/models/unload":
                stub.loaded.pop(model, None)
                return {"success": True}
            refused = stub._refused(model, path, body)
            if refused:
                return error(*refused)
            stub.loaded[model] = "loaded"
            if path == "/v1/embeddings":
                vectors = _bag_of_words(body["input"])
                return {"model": model, "object": "list",
                        "usage": {"prompt_tokens": 7, "total_tokens": 7},
                        "data": [{"embedding": v, "index": i, "object": "embedding"}
                                 for i, v in enumerate(vectors)]}
            if path == "/infill":
                return StreamingResponse(paced(stub._infill(body)),
                                         media_type="text/event-stream")
            if path == "/v1/completions":
                return StreamingResponse(paced(stub._completion(model, body)),
                                         media_type="text/event-stream")
            if path != "/v1/chat/completions":
                return error(404, "File Not Found", "not_found_error")
            reply = stub._chat(body)
            if not body.get("stream"):
                return stub._whole(model, body, reply)
            return StreamingResponse(paced(stub._chunks(model, body, reply)),
                                     media_type="text/event-stream")
        return app

    def _usage(self, reply: dict) -> dict:
        completion = len(_pieces(reply["content"])) if reply["content"] else 3
        return {"completion_tokens": completion, "prompt_tokens": 7,
                "total_tokens": 7 + completion, "prompt_tokens_details": {"cached_tokens": 0}}

    def _whole(self, model: str, body: dict, reply: dict) -> dict:
        message = {"role": "assistant", "content": reply["content"]}
        if reply["thinking"]:
            message["reasoning_content"] = reply["thinking"]
        if reply["call"]:
            name, pieces = reply["call"]
            message["tool_calls"] = [{"type": "function", "id": "appel-du-routeur",
                                      "function": {"name": name, "arguments": "".join(pieces)}}]
        n = body.get("n") if isinstance(body.get("n"), int) else 1
        choices = []
        for i in range(max(1, n)):
            choice = {"finish_reason": reply["finish"], "index": i, "message": message}
            if body.get("logprobs"):
                choice["logprobs"] = {"content": [{"id": 1, "token": "Bon", "logprob": -0.1,
                                                   "bytes": [66, 111, 110]}]}
            choices.append(choice)
        return {"choices": choices, "created": 1792493245, "model": model,
                "system_fingerprint": FINGERPRINT, "object": "chat.completion",
                "usage": self._usage(reply), "id": "chatcmpl-routeur",
                "timings": {"prompt_n": 7, "predicted_n": 3}}

    def _chunks(self, model: str, body: dict, reply: dict) -> list:
        base = {"created": 1792493245, "id": "chatcmpl-routeur", "model": model,
                "system_fingerprint": FINGERPRINT, "object": "chat.completion.chunk"}

        def chunk(delta, finish=None):
            return {**base, "choices": [{"finish_reason": finish, "index": 0, "delta": delta}]}
        out = [chunk({"role": "assistant", "content": None})]
        if reply["thinking"]:
            out += [chunk({"reasoning_content": p}) for p in _pieces(reply["thinking"])]
        if reply["content"]:
            out += [chunk({"content": p}) for p in _pieces(reply["content"])]
        if reply["call"]:
            name, pieces = reply["call"]
            out.append(chunk({"tool_calls": [{"index": 0, "id": "appel-du-routeur",
                                              "type": "function", "function": {
                                                  "name": name, "arguments": pieces[0]}}]}))
            out += [chunk({"tool_calls": [{"index": 0, "function": {"arguments": p}}]})
                    for p in pieces[1:]]
        out.append(chunk({}, reply["finish"]))
        if (body.get("stream_options") or {}).get("include_usage"):
            out.append({**base, "choices": [], "usage": self._usage(reply),
                        "timings": {"prompt_n": 7, "predicted_n": 3}})
        return out + ["[DONE]"]

    def _completion(self, model: str, body: dict) -> list:
        base = {"created": 1792493269, "model": model, "system_fingerprint": FINGERPRINT,
                "object": "text_completion", "id": "chatcmpl-texte"}
        pieces = _pieces(COMPLETION.strip())
        out = [{**base, "choices": [{"text": " " + p.strip(), "index": 0, "logprobs": None,
                                     "finish_reason": None}]} for p in pieces]
        end = {**base, "choices": [{"text": "", "index": 0, "logprobs": None,
                                    "finish_reason": "stop"}]}
        if (body.get("stream_options") or {}).get("include_usage"):
            end["usage"] = {"completion_tokens": len(pieces), "prompt_tokens": 4,
                            "total_tokens": 4 + len(pieces)}
        return out + [end, "[DONE]"]

    def _infill(self, body: dict) -> list:
        limit = body.get("max_tokens") or body.get("n_predict")
        pieces = _pieces(INFILL)
        out = [{"index": 0, "content": p, "tokens": [1], "stop": False, "id_slot": -1,
                "tokens_predicted": i + 1, "tokens_evaluated": 19} for i, p in enumerate(pieces)]
        return out + [{"index": 0, "content": "", "tokens": [], "id_slot": 1, "stop": True,
                       "stop_type": "limit" if limit else "eos", "model": body.get("model"),
                       "tokens_predicted": len(pieces), "tokens_evaluated": 19}]
