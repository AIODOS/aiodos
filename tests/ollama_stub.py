"""Faux Ollama pour les essais : un vrai serveur HTTP sur 127.0.0.1, aux formats d'Ollama
0.40.5 relevés le 17/10/2026 (docs.ollama.com et octets réels), avec des réponses connues
d'avance. Chaque machine d'un essai peut avoir le sien, avec ses propres modèles.

    with OllamaStub(["petit:1b"]) as ollama:
        ollama.url        # à mettre dans le réglage ollama_url d'un nœud
        ollama.requests   # (chemin, corps) de chaque demande reçue
"""
import asyncio
import json
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from tests.conftest import _bag_of_words
from tests.nodes import free_port

EMBEDDING = "plongement:latest"
STAMP = "2026-10-01T10:00:00.1234567+02:00"


def answer_of(model: str) -> str:
    """Réponse que donne toujours ce faux modèle."""
    return f"Bonjour de {model}"


def _tag(name: str) -> dict:
    caps = ["embedding"] if name == EMBEDDING else ["completion"]
    return {"name": name, "model": name, "modified_at": STAMP, "size": 1_000_000,
            "digest": f"digest-{name}",
            "details": {"parent_model": "", "format": "gguf", "family": "llama",
                        "families": ["llama"], "parameter_size": "1B",
                        "quantization_level": "Q4_K_M"},
            "capabilities": caps}


def _pieces(text: str) -> list:
    words = text.split(" ")
    return [w if i == 0 else " " + w for i, w in enumerate(words)]


class OllamaStub:
    def __init__(self, models=("petit:1b",), delay: float = 0.0, name: str = ""):
        self.models = list(models) + [EMBEDDING]
        self.delay = delay                # secondes entre deux mots (réponse en cours)
        self.name = name                  # dit dans chaque réponse : quelle machine a répondu
        self.requests = []
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = None
        self._thread = None

    # ------------------------------------------------------------- serveur
    def __enter__(self):
        app = self._app()
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                     log_level="warning", log_config=None))
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

    def answer(self, model: str) -> str:
        return f"{answer_of(model)} ({self.name})" if self.name else answer_of(model)

    # -------------------------------------------------------------- routes
    def _app(self) -> FastAPI:
        app = FastAPI()
        stub = self

        def unknown(model):
            return JSONResponse({"error": f"model '{model}' not found"}, status_code=404)

        async def body_of(request: Request) -> dict:
            body = await request.json()
            stub.requests.append((request.url.path, body))
            return body

        @app.get("/")
        def root():
            return PlainTextResponse("Ollama is running")

        @app.get("/api/version")
        def version():
            return {"version": "0.40.5"}

        @app.get("/api/tags")
        def tags():
            return {"models": [_tag(m) for m in stub.models]}

        @app.get("/api/ps")
        def ps():
            return {"models": []}

        @app.post("/api/show")
        async def show(request: Request):
            body = await body_of(request)
            model = body.get("model") or body.get("name")
            if model not in stub.models:
                return unknown(model)
            tag = _tag(model)
            return {"license": "", "modelfile": "", "parameters": "num_ctx 4096",
                    "template": "{{ .Prompt }}", "details": tag["details"],
                    "model_info": {"general.architecture": "llama",
                                   "llama.context_length": 4096},
                    "capabilities": tag["capabilities"], "modified_at": STAMP}

        @app.post("/api/embed")
        async def embed(request: Request):
            body = await body_of(request)
            if body.get("model") not in stub.models:
                return unknown(body.get("model"))
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return {"model": body["model"], "embeddings": _bag_of_words(texts),
                    "total_duration": 1, "load_duration": 0, "prompt_eval_count": len(texts)}

        @app.post("/api/embeddings")
        async def embeddings(request: Request):
            body = await body_of(request)
            if body.get("model") not in stub.models:
                return unknown(body.get("model"))
            return {"embedding": _bag_of_words([body["prompt"]])[0]}

        async def slowly(model):
            """Le temps d'écrire toute la réponse, quand elle ne vient pas en flux."""
            if stub.delay:
                await asyncio.sleep(stub.delay * len(_pieces(stub.answer(model))))

        def lines(model, field, done_extra):
            async def produce():
                for piece in _pieces(stub.answer(model)):
                    if stub.delay:
                        await asyncio.sleep(stub.delay)
                    yield json.dumps({"model": model, "created_at": STAMP, **field(piece),
                                      "done": False}) + "\n"
                yield json.dumps({"model": model, "created_at": STAMP, **field(""),
                                  "done": True, "done_reason": "stop", "total_duration": 10,
                                  "load_duration": 1, "prompt_eval_count": 3,
                                  "prompt_eval_duration": 1, "eval_count": 4,
                                  "eval_duration": 5, **done_extra}) + "\n"
            return produce()

        @app.post("/api/chat")
        async def chat(request: Request):
            body = await body_of(request)
            model = body.get("model")
            if model not in stub.models:
                return unknown(model)
            field = lambda text: {"message": {"role": "assistant", "content": text}}
            if body.get("stream") is False:
                await slowly(model)
                return {"model": model, "created_at": STAMP,
                        "message": {"role": "assistant", "content": stub.answer(model)},
                        "done": True, "done_reason": "stop", "prompt_eval_count": 3,
                        "eval_count": 4}
            return StreamingResponse(lines(model, field, {}), media_type="application/x-ndjson")

        @app.post("/api/generate")
        async def generate(request: Request):
            body = await body_of(request)
            model = body.get("model")
            if model not in stub.models:
                return unknown(model)
            if not body.get("prompt"):
                return {"model": model, "created_at": STAMP, "response": "", "done": True,
                        "done_reason": "load"}
            field = lambda text: {"response": text}
            if body.get("stream") is False:
                await slowly(model)
                return {"model": model, "created_at": STAMP, "response": stub.answer(model),
                        "done": True, "done_reason": "stop"}
            return StreamingResponse(lines(model, field, {"context": [1, 2, 3]}),
                                     media_type="application/x-ndjson")

        @app.post("/v1/chat/completions")
        async def openai_chat(request: Request):
            body = await body_of(request)
            model = body.get("model")
            if model not in stub.models:
                return JSONResponse({"error": {"message": f"model '{model}' not found",
                                               "type": "api_error", "param": None,
                                               "code": None}}, status_code=404)
            base = {"id": "chatcmpl-1", "created": 1, "model": model,
                    "system_fingerprint": "fp_ollama"}
            if not body.get("stream"):
                await slowly(model)
                return {**base, "object": "chat.completion", "choices": [{
                    "index": 0, "message": {"role": "assistant", "content": stub.answer(model)},
                    "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}

            async def events():
                for piece in _pieces(stub.answer(model)):
                    chunk = {**base, "object": "chat.completion.chunk", "choices": [{
                        "index": 0, "delta": {"role": "assistant", "content": piece},
                        "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                end = {**base, "object": "chat.completion.chunk", "choices": [{
                    "index": 0, "delta": {"role": "assistant", "content": ""},
                    "finish_reason": "stop"}]}
                yield f"data: {json.dumps(end)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(events(), media_type="text/event-stream")

        return app
