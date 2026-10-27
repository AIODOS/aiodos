"""Moteur intégré (aiodos/llm/) : les modèles d'Ollama lus sans Ollama, ceux d'AIODOS en
premier, les incompatibles écartés ; le routeur de llama.cpp joué par un faux serveur
(flux de réflexion et de réponse, embeddings normés, modèle qui ne se charge pas) ; et
services/ollama.py qui aiguille vers l'un ou l'autre moteur."""
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiodos import settings
from aiodos.llm import engine, integrated, models
from aiodos.services import ollama


# ----------------------------------------------------------------- faux fichiers
def _gguf(path, arch="qwen3", pooling=None, template=""):
    """Un en-tête GGUF minimal (sans tenseurs), lisible par cluster/weights.header."""
    def string(text):
        data = text.encode("utf-8")
        return struct.pack("<Q", len(data)) + data
    kv = [("general.architecture", 8, string(arch)), (f"{arch}.block_count", 4, struct.pack("<I", 2)),
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
    return path


def _ollama_model(root, name, tag, **gguf):
    projector = gguf.pop("projector", False)
    blobs = root / "blobs"
    digest = f"sha256-{abs(hash((name, tag))):064x}"[:71]
    _gguf(blobs / digest, **gguf)
    layers = [{"mediaType": "application/vnd.ollama.image.model", "digest": digest.replace("-", ":", 1)}]
    if projector:
        pdigest = f"sha256-{abs(hash((name, tag, 'p'))):064x}"[:71]
        _gguf(blobs / pdigest, arch="clip")
        layers.append({"mediaType": models.PROJECTOR_LAYER, "digest": pdigest.replace("-", ":", 1)})
    manifest = root / "manifests" / "registry.ollama.ai" / "library" / name / tag
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"layers": layers}), encoding="utf-8")


@pytest.fixture
def ollama_dir(tmp_path, monkeypatch):
    root = tmp_path / "ollama"
    monkeypatch.setenv("OLLAMA_MODELS", str(root))
    _ollama_model(root, "qwen3.5", "9b", template="{% if enable_thinking %}<think>{% endif %} tools",
                  projector=True)
    _ollama_model(root, "nomic-embed-text", "latest", arch="nomic-bert", pooling=1)
    models.forget_cache()
    yield root
    models.forget_cache()


def test_ollama_models_are_read_without_ollama(ollama_dir):
    found = {m.name: m for m in models.available()}
    assert set(found) == {"qwen3.5:9b", "nomic-embed-text:latest"}
    chat, embed = found["qwen3.5:9b"], found["nomic-embed-text:latest"]
    assert chat.mmproj and not chat.embedding and chat.id == "qwen3.5-9b"
    assert embed.embedding and not embed.mmproj and models.find("nomic-embed-text") == embed
    assert integrated.capabilities("qwen3.5:9b") == ["completion", "vision", "thinking", "tools"]
    assert integrated.capabilities("nomic-embed-text") == ["embedding"]
    ini = engine.presets(models.available())
    assert "[qwen3.5-9b]" in ini and "mmproj = " in ini and "embeddings = true" in ini


def test_an_aiodos_model_comes_first_and_an_incompatible_one_is_set_aside(ollama_dir):
    folder = models.models_dir() / "qwen3.5-9b"
    _gguf(folder / "qwen3.5-9b.gguf", template="<think>")
    (folder / "model.json").write_text(json.dumps({
        "name": "qwen3.5:9b", "file": "qwen3.5-9b.gguf", "license": "Apache-2.0"}), encoding="utf-8")
    models.forget_cache()
    chosen = models.find("qwen3.5:9b")
    assert chosen.source == "aiodos" and chosen.license == "Apache-2.0"
    models.mark_incompatible(chosen, "wrong number of tensors")
    assert models.find("qwen3.5:9b").source == "ollama"     # le fichier écarté, pas le nom


def test_the_router_gives_its_key_to_each_model_server(ollama_dir, data_dir, monkeypatch):
    """La clé par l'environnement : le routeur retire « api-key » des arguments de ses
    llama-server, mais leur donne son environnement (vérifié avec llama.cpp b11707). Un
    modèle d'embedding lit chaque texte d'un seul tenant (lots à la taille du contexte)."""
    launched = []

    class Launched:
        def __init__(self, name, program, args, port, env=None):
            self.args, self.env, self.port, self.alive = args, env, port, False
            launched.append(self)

        def start(self, timeout, ready=None):
            self.alive = True
            return self

        def stop(self):
            self.alive = False

    monkeypatch.setattr(engine.programs, "folder", lambda: data_dir)
    monkeypatch.setattr(engine.programs, "Process", Launched)
    router = engine.Router()
    router.url()
    assert launched[0].env == {"LLAMA_API_KEY": router.key}
    assert router.key not in " ".join(launched[0].args)
    ini = (data_dir / "llm" / "modeles.ini").read_text(encoding="utf-8")
    sections = {s.split("]")[0].strip("["): s for s in ini.split("\n\n")}
    embedding = sections[models.find("nomic-embed-text").id]
    assert f"batch-size = {engine.EMBED_CONTEXT}" in embedding
    assert f"ubatch-size = {engine.EMBED_CONTEXT}" in embedding
    assert "batch-size" not in sections["qwen3.5-9b"]


def test_without_router_mode_one_llama_server_per_model(ollama_dir, data_dir, monkeypatch):
    """Le llama.cpp d'Android refuse le mode routeur (« subprocess is not enabled on this
    build », vu sur le S23 le 27/10/2026) : un llama-server par modèle (llm/direct.py), mêmes
    réglages, même clé par l'environnement, au plus MODELS_MAX en marche."""
    launched = []

    class Launched:
        def __init__(self, name, program, args, port, env=None):
            self.args, self.env, self.port, self.alive = args, env, port, False
            launched.append(self)

        def start(self, timeout, ready=None):
            if "--models-preset" in self.args:
                raise engine.programs.EngineError(
                    "llama-server s'est arrêté : E srv  llama_server: failed to initialize "
                    "router models: subprocess is not enabled on this build")
            self.alive = True
            return self

        def stop(self):
            self.alive = False

    monkeypatch.setattr(engine.programs, "folder", lambda: data_dir)
    monkeypatch.setattr(engine.programs, "Process", Launched)
    router = engine.Router()
    chat, embed = models.find("qwen3.5:9b"), models.find("nomic-embed-text")
    first = router.url(chat.id)
    assert len(launched) == 2 and first == f"http://127.0.0.1:{launched[1].port}"
    args = launched[1].args
    assert args[args.index("--alias") + 1] == chat.id and "--mmproj" in args
    assert "--embeddings" not in args and args[args.index("--fit") + 1] == "on"
    assert launched[1].env == {"LLAMA_API_KEY": router.key} and router.key not in " ".join(args)
    assert router.url(chat.id) == first and len(launched) == 2        # déjà prêt
    router.url(embed.id)
    embedding = launched[2].args
    assert "--embeddings" in embedding
    assert embedding[embedding.index("--ubatch-size") + 1] == str(engine.EMBED_CONTEXT)
    assert router.status() == {chat.id: "loaded", embed.id: "loaded"}
    assert router.unload(chat.id) and router.status() == {embed.id: "loaded"}
    router._direct.limit = 1           # place pour un seul : le moins récemment servi s'arrête
    router.url(chat.id)
    assert router.status() == {chat.id: "loaded"} and not launched[2].alive
    with pytest.raises(engine.EngineUnavailable):
        router.url("absent")
    router.stop()
    assert router.status() == {} and not any(p.alive for p in launched)


# ------------------------------------------------------------------ faux routeur
class FakeRouter(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/models":
            self._json(200, {"data": [{"id": "qwen3.5-9b", "status": {"value": "loaded"}}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeRouter.seen.append((self.path, body, self.headers.get("Authorization")))
        if body["model"] == "casse":
            return self._json(500, {"error": {"message": "model name=casse failed to load"}})
        if self.path == "/v1/embeddings":
            return self._json(200, {"data": [{"index": i, "embedding": [3.0, 4.0]}
                                             for i, _ in enumerate(body["input"])]})
        if self.path == "/models/unload":
            return self._json(200, {"success": True})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in ({"choices": [{"delta": {"reasoning_content": "Je réfléchis."}}]},
                      {"choices": [{"delta": {"content": "51"}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}):
            # De l'UTF-8 brut, sans encodage déclaré : comme llama-server
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def router(ollama_dir, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(integrated._router, "url", lambda model_id=None: base)
    monkeypatch.setattr(integrated._router, "status", lambda: {"qwen3.5-9b": "loaded"})
    FakeRouter.seen = []
    yield base
    server.shutdown()


def test_a_conversation_streams_thinking_then_answer(router):
    pieces = list(integrated.chat_stream("qwen3.5:9b", [{"role": "user", "content": "17 × 3 ?"}],
                                         think=False, options={"temperature": 0.2,
                                                              "num_predict": 64}, stats=True))
    assert ("thinking", "Je réfléchis.") in pieces and ("content", "51") in pieces   # UTF-8
    assert pieces[-1] == ("stats", {"prompt_tokens": 7, "completion_tokens": 2,
                                    "done_reason": "stop"})
    path, body, auth = FakeRouter.seen[0]
    assert path == "/v1/chat/completions" and body["model"] == "qwen3.5-9b"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["temperature"] == 0.2 and body["max_tokens"] == 64
    assert auth == f"Bearer {integrated._router.key}"


def test_embeddings_come_back_normed_and_in_batches(router):
    vectors = integrated.embed([f"texte {i}" for i in range(20)], "nomic-embed-text")
    assert len(vectors) == 20 and vectors[0] == pytest.approx([0.6, 0.8])
    assert [len(b["input"]) for p, b, _ in FakeRouter.seen if p == "/v1/embeddings"] == [16, 4]
    with pytest.raises(RuntimeError, match="pas un modèle d'embedding"):
        integrated.embed(["x"], "qwen3.5:9b")


def test_a_model_that_does_not_load_is_set_aside_with_a_clear_message(router, monkeypatch):
    broken = models.find("qwen3.5:9b")
    monkeypatch.setattr(integrated, "_model", lambda name: broken.__class__(
        **{**broken.__dict__, "id": "casse"}))
    with pytest.raises(RuntimeError, match="format propre à Ollama"):
        list(integrated.chat_stream("qwen3.5:9b", [{"role": "user", "content": "?"}]))
    assert models.find("qwen3.5:9b") is None           # plus proposé
    assert integrated.list_models() == [m for m in integrated.list_models()
                                        if m["name"] != "qwen3.5:9b"]


def test_loaded_models_and_unloading_chat_models(router, monkeypatch):
    unloaded = []
    monkeypatch.setattr(integrated._router, "unload", lambda model_id: unloaded.append(model_id) or True)
    assert [m["name"] for m in integrated.loaded_models()] == ["qwen3.5:9b"]
    assert integrated.unload_chat_models() == ["qwen3.5:9b"] and unloaded == ["qwen3.5-9b"]


# ------------------------------------------------------------------- aiguillage
def test_the_service_switches_between_engines(monkeypatch):
    monkeypatch.setattr(integrated, "list_models", lambda max_age=0: [{"name": "intégré"}])
    monkeypatch.setattr(integrated, "available", lambda: True)
    settings.set_value("llm_engine", "auto")
    assert ollama.use_integrated() and ollama.list_models(0) == [{"name": "intégré"}]
    settings.set_value("llm_engine", "ollama")
    assert not ollama.use_integrated()
    monkeypatch.setattr(integrated, "available", lambda: False)
    settings.set_value("llm_engine", "auto")
    assert not ollama.use_integrated()                  # pas de llama.cpp : Ollama


# ------------------------------------------------------------------ téléchargement
class FakeHub(BaseHTTPRequestHandler):
    files = {}
    cut = {}                       # fichier -> octets envoyés avant une coupure (une fois)

    def log_message(self, *args):
        pass

    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        data = FakeHub.files[name]
        start = int(self.headers.get("Range", "bytes=0-")[6:].split("-")[0])
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        limit = FakeHub.cut.pop(name, None)
        self.wfile.write(data[start:start + limit] if limit else data[start:])


def test_a_catalog_model_downloads_resumes_and_is_checked(ollama_dir, monkeypatch):
    import hashlib
    from aiodos.llm import catalog, download
    weights_data = open(_gguf(ollama_dir / "tmp.gguf", arch="gemma-embedding", pooling=1), "rb").read()
    FakeHub.files = {"emb.gguf": weights_data}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    spec = {"repo": "x/y", "revision": "abc", "license": "Gemma", "kind": "embedding",
            "family": "gemma-embedding", "parameters": "300M", "quantization": "Q8_0",
            "files": {"model": ("emb.gguf", hashlib.sha256(weights_data).hexdigest(),
                                len(weights_data))}}
    monkeypatch.setitem(catalog.MODELS, "petit:latest", spec)
    monkeypatch.setattr(catalog, "HF", f"http://127.0.0.1:{server.server_address[1]}")
    FakeHub.cut = {"emb.gguf": 40}                  # la connexion coupe en route
    with pytest.raises(download.DownloadError, match="reprendra"):
        download.pull("petit", progress=lambda *a: None)
    seen = []
    assert download.pull("petit", progress=lambda got, total, step: seen.append((got, total)))
    assert seen[-1] == (len(weights_data), len(weights_data))
    installed = models.find("petit")
    assert installed.source == "aiodos" and installed.embedding and installed.license == "Gemma"
    # Un fichier abîmé est refusé, et supprimé
    FakeHub.files = {"emb.gguf": weights_data[:-1] + b"!"}
    monkeypatch.setitem(catalog.MODELS, "abime:latest", {**spec})
    with pytest.raises(download.DownloadError, match="empreinte"):
        download.pull("abime")
    assert not list((models.models_dir() / "abime-latest").glob("*.part"))
    server.shutdown()


def test_the_model_that_reads_images(ollama_dir):
    assert integrated.vision_model("qwen2.5vl:7b").name == "qwen3.5:9b"   # absent : remplacé
    assert integrated.vision_model("qwen3.5:9b").name == "qwen3.5:9b"
    with pytest.raises(RuntimeError, match="Aucun modèle"):
        models.mark_incompatible(models.find("qwen3.5:9b"), "essai")
        integrated.vision_model("qwen2.5vl:7b")
