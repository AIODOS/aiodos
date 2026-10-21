"""Gros modèles répartis (aiodos/cluster) : fichiers d'Ollama lus tels quels, découpage qui
garde la fin du modèle ici, prêt de la carte graphique réservé à mes machines, et flux de
llama-server traduit comme celui d'Ollama. Sans llama.cpp ni compagnon : leurs programmes
sont remplacés par des doublures."""
import json
import struct
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiodos import settings
from aiodos.chat import pipeline
from aiodos.cluster import engine, group, helper, plan, weights
from aiodos.network import peers, route
from aiodos.network.client import PeerError

KEY = bytes(32)
ME, OTHER = "a" * 32, "b" * 32


# ------------------------------------------------------------------ fichiers d'Ollama
def _string(text):
    data = text.encode()
    return struct.pack("<Q", len(data)) + data


def _gguf(path, arch="llama", layers=32, embedding=4096, tokens=("a", "b", "c"), size=0):
    meta = [("general.architecture", 8, _string(arch)),
            (f"{arch}.block_count", 4, struct.pack("<I", layers)),
            (f"{arch}.embedding_length", 4, struct.pack("<I", embedding)),
            ("tokenizer.ggml.scores", 9, struct.pack("<IQ", 6, 3) + struct.pack("<3f", 0, 1, 2)),
            ("tokenizer.ggml.tokens", 9,
             struct.pack("<IQ", 8, len(tokens)) + b"".join(_string(t) for t in tokens))]
    body = b"GGUF" + struct.pack("<IQQ", 3, 0, len(meta))
    for key, kind, value in meta:
        body += _string(key) + struct.pack("<I", kind) + value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + bytes(size))
    return path


def _ollama(root, name="qwen3.5", tag="9b", **gguf):
    digest = "0" * 63 + "1"
    _gguf(root / "blobs" / f"sha256-{digest}", **gguf)
    manifest = root / "manifests" / "registry.ollama.ai" / "library" / name / tag
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.projector", "digest": "sha256:" + "2" * 64},
        {"mediaType": weights.MODEL_LAYER, "digest": f"sha256:{digest}"}]}), encoding="utf-8")


def test_ollama_files_are_found_and_read(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "modeles"))
    _ollama(tmp_path / "modeles", layers=41, embedding=2048, size=1000)
    path = weights.gguf_path("qwen3.5:9b")
    assert path.name == "sha256-" + "0" * 63 + "1"
    head = weights.header(path)
    assert (head["architecture"], head["layers"], head["embedding"], head["vocabulary"]) == \
        ("llama", 41, 2048, 3)
    assert head["size"] == path.stat().st_size
    root = tmp_path / "x"
    assert weights.manifest_path("user/modele", root).parts[-4:] == \
        ("registry.ollama.ai", "user", "modele", "latest")
    assert weights.manifest_path("hf.co/org/depot:Q4_K_M", root).parts[-4:] == \
        ("hf.co", "org", "depot", "Q4_K_M")
    for bad in ("../../secret", ""):
        with pytest.raises(weights.WeightsError):
            weights.manifest_path(bad, root)
    with pytest.raises(weights.WeightsError, match="introuvable"):
        weights.gguf_path("absent")
    (tmp_path / "pas-gguf").write_bytes(b"ZZZZ")
    with pytest.raises(weights.WeightsError, match="GGUF"):
        weights.header(tmp_path / "pas-gguf")


# ------------------------------------------------------------------ moteur et découpage
def test_engine_output_is_parsed():
    assert engine.parse_version("version: 0.7.0-dev (build 11707, commit 1268edac5)\n"
                                "built with Clang") == "11707-1268edac5"
    assert engine.parse_version("rien") is None
    devices = engine.parse_devices(
        "Available devices:\n"
        "  Vulkan0: Intel(R) RaptorLake-S Mobile Graphics Controller (16236 MiB, 15468 MiB free)\n"
        "  Vulkan1: NVIDIA GeForce RTX 4070 Laptop GPU (7948 MiB, 7180 MiB free)\n")
    assert [d["name"] for d in devices] == ["Vulkan0", "Vulkan1"]
    # La carte intégrée au processeur partage la mémoire vive : jamais choisie s'il y en a une autre
    assert engine.best_device(devices)["name"] == "Vulkan1"
    assert engine.best_device(devices[:1])["name"] == "Vulkan0"
    assert engine.best_device([]) is None


def test_stop_interrupts_a_long_loading(tmp_path):
    """Bouton Stop pendant le premier chargement (les poids traversent le réseau) : le
    programme est arrêté aussitôt, sans attendre la fin du délai."""
    import sys
    import time
    stop = threading.Event()
    slow = engine.Process("essai-lent", Path(sys.executable),
                          ["-c", "import time; time.sleep(60)"], engine.free_port())
    threading.Timer(0.5, stop.set).start()
    began = time.monotonic()
    with pytest.raises(engine.EngineError, match="interrompu"):
        slow.start(timeout=60, stop=stop)
    assert time.monotonic() - began < 10 and not slow.alive


def test_the_setup_of_a_distributed_model_is_shown(monkeypatch):
    """Premier message : la mise en place (cartes demandées, poids envoyés) dit où elle en
    est, puis la réponse arrive ; une mise en place qui échoue le dit après ses étapes."""
    class Ready:
        def chat_stream(self, messages, think, stop, options, stats):
            yield "content", "Bonjour"

    def setup(name, stop=None, progress=None):
        progress("Demande de la carte graphique de « PC fixe »…")
        progress("Chargement du modèle réparti… 5 s")
        return Ready()
    monkeypatch.setattr(group, "_group", setup)
    assert list(group.chat_stream("reparti:gros:70b", [])) == [
        ("status", "Demande de la carte graphique de « PC fixe »…"),
        ("status", "Chargement du modèle réparti… 5 s"), ("content", "Bonjour")]

    def broken(name, stop=None, progress=None):
        progress("Demande de la carte graphique de « PC fixe »…")
        raise RuntimeError("« PC fixe » ne prête pas sa carte graphique.")
    monkeypatch.setattr(group, "_group", broken)
    stream = group.chat_stream("reparti:gros:70b", [])
    assert next(stream) == ("status", "Demande de la carte graphique de « PC fixe »…")
    with pytest.raises(RuntimeError, match="ne prête pas"):
        next(stream)


def test_a_setup_step_does_not_commit_the_answer():
    """Mode automatique : tant qu'il n'y a que des étapes de mise en place, une machine qui
    échoue laisse la place à la suivante (placement.run)."""
    from aiodos.network import placement

    def attempt(model):
        if model == "reparti:gros:70b":
            yield "status", "Chargement…"
            raise RuntimeError("machine partie")
        yield "content", "réponse d'ailleurs"
    items = list(placement.run(["reparti:gros:70b", "gros:70b"], attempt))
    assert ("content", "réponse d'ailleurs") in items and ("status", "Chargement…") in items


def test_the_end_of_the_model_stays_here():
    gib = 2 ** 30
    card = {"name": "Vulkan1", "free_mib": 8000}                        # 6 464 Mio utiles
    lenders = [{"node_id": "c", "free_mib": 8000}, {"node_id": "b", "free_mib": 16000}]
    assert plan.make(5 * gib, 32, card, lenders) is None               # tient ici
    assert plan.make(15 * gib, 32, card, []) is None                    # personne ne prête
    cut = plan.make(15 * gib, 32, card, lenders)
    # La plus grande carte d'abord, une seule suffit ; la carte d'ici garde la fin
    assert [h["node_id"] for h in cut["helpers"]] == ["b"]
    assert cut["devices"] == "RPC0/Vulkan1" and cut["split"] == "14464/6464"
    assert cut["complete"] and cut["layers"] == 33                     # dernier étage compris
    cut = plan.make(25 * gib, 32, card, lenders)
    assert cut["devices"] == "RPC0/RPC1/Vulkan1" and cut["complete"]
    # Pas de carte ici : le dernier étage reste sur le processeur d'ici
    cut = plan.make(10 * gib, 32, None, lenders)
    assert cut["devices"] == "RPC0" and cut["layers"] == 32
    # Trop gros pour toutes les cartes : les premières couches restent en mémoire vive ici
    cut = plan.make(60 * gib, 32, card, lenders)
    assert not cut["complete"] and 0 < cut["layers"] < 32
    # Une carte du réseau local passe avant une plus grande jointe par Internet
    lenders = [{"node_id": "loin", "free_mib": 24000}, {"node_id": "pres", "free_mib": 12000,
                                                         "near": True}]
    assert [h["node_id"] for h in plan.make(15 * gib, 32, card, lenders)["helpers"]] == ["pres"]
    assert [h["node_id"] for h in plan.make(25 * gib, 32, card, lenders)["helpers"]] == \
        ["pres", "loin"]


# ------------------------------------------------------------------ prêt de la carte
class FakeProcess:
    def __init__(self, port=40123):
        self.port, self.alive, self.started = port, False, False

    def start(self, timeout):
        self.alive = self.started = True
        return self

    def stop(self):
        self.alive = False


class FakeCompanion:
    """Compagnon qui garde ses tunnels, tient ses services et dit leurs flux (comme
    l'actuel)."""
    running = True

    def __init__(self, features=("tunnel-owner", "service-usage", "service-hold")):
        self.features = frozenset(features)
        self.exposed = []
        self.tunnels = []
        self.usage = {}

    def supports(self, feature):
        return feature in self.features

    def tunnel_guard_problem(self):
        return None if "tunnel-owner" in self.features else "compagnon trop ancien"

    def expose(self, services):
        """Comme le vrai : un service tenu l'est dès qu'il est exposé (l'état le dit)."""
        self.exposed.append(services)
        self.usage = {s["name"]: self.usage.get(s["name"]) or {
            "active": 0, "total": 0, "idle_s": None, "held": bool(s.get("hold")),
            "holding": 2 if s.get("hold") else None, "foreign": 0, "blocked": None}
            for s in services}

    def open_tunnel(self, net_id, name, guarded=True, max_connections=None):
        self.tunnels.append((net_id, name))
        return 51000 + len(self.tunnels)

    def set_tunnel_owner(self, port, pid):
        self.tunnels.append(("réservé", port, pid))

    def close_tunnel(self, port):
        self.tunnels.append(("fermé", port))

    def service_usage(self):
        return self.usage if "service-usage" in self.features else None


class FakeExecutor:
    def __init__(self):
        self.interactive = 0

    def interactive_begin(self):
        self.interactive += 1

    def interactive_end(self):
        self.interactive -= 1


class FakeManager:
    def __init__(self):
        self.executor = FakeExecutor()

    def name(self):
        return "PC fixe"


@pytest.fixture
def lending(monkeypatch, tmp_path):
    """Cette machine prête sa carte : llama.cpp et compagnon remplacés par des doublures."""
    companion = FakeCompanion()
    route.attach(companion)
    process = FakeProcess()
    monkeypatch.setattr(engine, "info", lambda max_age=0: {
        "folder": str(tmp_path), "version": "11707-abc",
        "devices": [{"name": "Vulkan1", "label": "RTX", "total_mib": 8000, "free_mib": 7000}]})
    monkeypatch.setattr(engine, "rpc_server", lambda where, device, cache: process)
    peers.save(OTHER, "PC portable", KEY, "127.0.0.1", 1)
    peers.set_net(OTHER, "c" * 64, {})
    settings.set_value("lend_gpu", True)
    yield companion, process
    helper.stop_all()
    route.detach()


def test_lending_is_refused_politely(lending):
    manager = FakeManager()
    ask = {"version": "11707-abc", "tunnel": "guarded"}
    settings.set_value("lend_gpu", False)
    with pytest.raises(PeerError, match="ne prête pas sa carte"):
        helper.lend(manager, OTHER, ask)
    settings.set_value("lend_gpu", True)
    with pytest.raises(PeerError, match="Versions de llama.cpp différentes"):
        helper.lend(manager, OTHER, dict(ask, version="9999-zzz"))
    peers.set_trust(OTHER, "friend")
    with pytest.raises(PeerError, match="Seules mes machines"):
        helper.lend(manager, OTHER, ask)
    peers.save(ME, "Inconnue", KEY, "127.0.0.1", 1)          # appairée, sans clé Internet
    with pytest.raises(PeerError, match="Clé Internet"):
        helper.lend(manager, ME, ask)
    assert lending[0].exposed == [] and not lending[1].started


def test_lending_opens_the_card_to_that_machine_only(lending):
    companion, process = lending
    manager = FakeManager()
    ask = {"version": "11707-abc", "tunnel": "guarded"}
    reply = helper.lend(manager, OTHER, ask)
    assert reply["device"] == "Vulkan1" and process.alive
    assert companion.exposed[-1] == [{"name": reply["service"], "port": process.port,
                                      "peers": ["c" * 64], "hold": True}]
    assert manager.executor.interactive == 1                 # la carte va à la conversation
    assert helper.keep(manager, OTHER, {"service": reply["service"]}) == {"ok": True}
    assert helper.keep(manager, ME, {"service": reply["service"]}) == {"ok": False}
    assert helper.release(manager, ME, {"service": reply["service"]}) == {"ok": False}
    assert helper.release(manager, OTHER, {"service": reply["service"]}) == {"ok": True}
    assert not process.alive and companion.exposed[-1] == []
    assert manager.executor.interactive == 0
    assert helper.keep(manager, OTHER, {"service": reply["service"]}) == {"ok": False}
    # Réglage coupé pendant un prêt : la carte est reprise au prochain signe de vie
    reply = helper.lend(manager, OTHER, ask)
    settings.set_value("lend_gpu", False)
    assert helper.keep(manager, OTHER, {"service": reply["service"]}) == {"ok": False}
    assert not process.alive and companion.exposed[-1] == []
    assert manager.executor.interactive == 0


# ------------------------------------------------------------------ côté conversation
def test_distributed_models_are_offered_with_my_lending_machines(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "info", lambda max_age=0: {
        "folder": str(tmp_path), "version": "11707-abc",
        "devices": [{"name": "Vulkan1", "label": "RTX", "total_mib": 8000, "free_mib": 7000}]})
    monkeypatch.setattr(group.ollama, "list_models",
                        lambda: [{"name": "gros:70b", "size": 1}, {"name": "petit:1b", "size": 1},
                                 {"name": "plongement", "size": 1}])
    monkeypatch.setattr(group.ollama, "capabilities_many", lambda names: {
        "gros:70b": ["completion"], "petit:1b": ["completion"], "plongement": ["embedding"]})
    sizes = {"gros:70b": 40 * 2 ** 30, "petit:1b": 2 ** 30, "plongement": 40 * 2 ** 30}
    monkeypatch.setattr(group.weights, "gguf_path", lambda name: tmp_path / name)
    monkeypatch.setattr(group, "_header", lambda path: {"size": sizes[path.name], "layers": 80})
    monkeypatch.setattr(group.route, "companion", lambda: FakeCompanion())  # tunnels gardés
    assert group.available() == []                           # aucune machine ne prête
    peers.save(OTHER, "PC fixe", KEY, "127.0.0.1", 1)
    peers.set_net(OTHER, "c" * 64, {})
    profile = {"cluster": {"lends": True, "version": "11707-abc", "free_mib": 48000}}
    peers.set_profile(OTHER, profile)
    offers = group.available()
    assert [o["id"] for o in offers] == ["reparti:gros:70b"]
    assert offers[0]["machines"] == ["PC fixe"] and offers[0]["complete"]
    peers.set_profile(OTHER, {"cluster": dict(profile["cluster"], version="1-autre")})
    assert group.available() == []                           # autre version de llama.cpp
    peers.set_profile(OTHER, profile)
    peers.set_trust(OTHER, "friend")
    assert group.available() == []                           # un ami ne prête pas sa carte


def test_pipeline_sends_distributed_models_to_the_group(monkeypatch):
    seen = []

    def fake(model, messages, think=True, stop=None):
        seen.append(model)
        yield "content", "réponse"
    monkeypatch.setattr(pipeline.cluster, "chat_stream", fake)
    assert list(pipeline.generate("reparti:gros:70b", [])) == [("content", "réponse")]
    assert seen == ["reparti:gros:70b"]
    assert not pipeline.runs_here("reparti:gros:70b") and pipeline.runs_here("qwen3.5:9b")
    assert group.parse("reparti:gros:70b") == "gros:70b" and group.parse("gros:70b") is None


def test_messages_suit_strict_chat_templates():
    """Mistral exige l'alternance utilisateur / assistant : les consignes du milieu
    rejoignent la question qui les suit, celles du début restent une seule consigne."""
    def m(role, content):
        return {"role": role, "content": content}
    reshaped = group.for_template([
        m("system", "Tu es AIODOS."), m("system", "Sommaire."), m("user", "Q1"),
        m("assistant", "R1"), m("system", "Résultats web."), m("system", "RAPPEL."),
        m("user", "Q2")])
    assert reshaped == [m("system", "Tu es AIODOS.\n\nSommaire."), m("user", "Q1"),
                        m("assistant", "R1"), m("user", "Résultats web.\n\nRAPPEL.\n\nQ2")]
    assert group.for_template([m("system", "A"), m("system", "B"), m("user", "Q")]) == \
        [m("system", "A\n\nB"), m("user", "Q")]
    assert group.for_template([m("user", "Q"), m("user", "suite"), m("system", "fin")]) == \
        [m("user", "Q\n\nsuite\n\nfin")]


class _SSE(BaseHTTPRequestHandler):
    """Faux llama-server : réflexion, puis réponse, en événements « data: »."""
    seen = {}

    def do_POST(self):
        _SSE.seen = {"auth": self.headers.get("Authorization"),
                     "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta in ({"reasoning_content": "Je réfléchis."}, {"content": "Bon"}, {"content": "jour"}):
            chunk = {"choices": [{"delta": delta}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):
        pass


def test_the_server_stream_reads_like_ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSE)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        g = group.Group("gros:70b")
        g.server = FakeProcess(port=server.server_address[1])
        parts = list(g.chat_stream([{"role": "user", "content": "Bonjour ?"}], think=False))
        assert parts == [("thinking", "Je réfléchis."), ("content", "Bon"), ("content", "jour")]
        assert _SSE.seen["auth"] == f"Bearer {g.api_key}"
        assert _SSE.seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    finally:
        server.shutdown()


def test_sampling_options_and_token_count_reach_llama_server(monkeypatch):
    """Passerelle : max_tokens, stop… vont à llama-server, qui donne le nombre de jetons."""
    import json as _json

    from aiodos.cluster import group as cluster_group
    sent = {}

    class Reply:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_lines(self, decode_unicode=False):
            chunks = [{"choices": [{"delta": {"content": "Bon"}, "finish_reason": None}]},
                      {"choices": [{"delta": {}, "finish_reason": "length"}]},
                      {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 1}}]
            return iter([f"data: {_json.dumps(c)}" for c in chunks] + ["data: [DONE]"])

    def fake_post(url, json=None, **kw):
        sent.update(json)
        return Reply()
    monkeypatch.setattr(cluster_group.requests, "post", fake_post)
    group = cluster_group.Group.__new__(cluster_group.Group)
    group.api_key, group.last_used = "clé", 0
    group.server = type("Server", (), {"port": 1})()
    out = list(group.chat_stream([{"role": "user", "content": "x"}], think=False,
                                 options={"num_predict": 5, "stop": ["\n"], "num_ctx": 9},
                                 stats=True))
    assert out == [("content", "Bon"), ("stats", {"prompt_tokens": 7, "completion_tokens": 1,
                                                 "done_reason": "length"})]
    assert sent["max_tokens"] == 5 and sent["stop"] == ["\n"] and "num_ctx" not in sent
    assert sent["stream_options"] == {"include_usage": True}


def test_a_distributed_model_is_prepared_while_typing(monkeypatch):
    """Choisi dans la conversation, le modèle se met en place d'avance ; une question qui
    arrive avant la fin attend cette mise en place, en relayant ses nouvelles, sans en
    lancer une deuxième."""
    import threading
    started, go_on = [], threading.Event()

    class Server:
        alive = True

    def start(self, stop=None, progress=None):
        started.append(self.name)
        self.status = "Demande de la carte graphique de « PC fixe »…"
        progress(self.status)
        self.server = Server()            # lancé, mais pas encore chargé
        go_on.wait(10)
        self.ready = True
    monkeypatch.setattr(group.Group, "start", start)
    monkeypatch.setattr(group, "_groups", {})
    monkeypatch.setattr(group, "_prepared", {})
    changes = []
    model = "reparti:grand:70b"
    assert group.prepare(model, changed=lambda: changes.append(1))["state"] == "preparing"
    deadline = time.time() + 5
    while not group.preparation(model)["status"] and time.time() < deadline:
        time.sleep(0.05)
    assert group.preparation(model)["status"].startswith("Demande de la carte")
    assert group.prepare(model)["state"] == "preparing"            # pas une deuxième fois
    assert group.preparation(model)["state"] == "preparing"        # lancé n'est pas chargé
    relayed, found = [], {}
    asking = threading.Thread(target=lambda: found.update(
        group=group._group("grand:70b", progress=relayed.append)))
    asking.start()
    time.sleep(1.5)
    assert relayed == ["Demande de la carte graphique de « PC fixe »…"]   # la question attend
    go_on.set()
    asking.join(10)
    assert started == ["grand:70b"] and found["group"].server.alive
    assert group.preparation(model)["state"] == "ready" and changes
    assert group.prepare(model)["state"] == "ready"                # déjà prêt : rien à faire


def test_a_failed_preparation_says_why(monkeypatch):
    def start(self, stop=None, progress=None):
        raise RuntimeError("Aucune de mes machines ne prête sa carte graphique en ce moment.")
    monkeypatch.setattr(group.Group, "start", start)
    monkeypatch.setattr(group, "_groups", {})
    monkeypatch.setattr(group, "_prepared", {})
    group.prepare("reparti:grand:70b")
    deadline = time.time() + 5
    while group.preparation("reparti:grand:70b")["state"] == "preparing" and time.time() < deadline:
        time.sleep(0.05)
    state = group.preparation("reparti:grand:70b")
    assert state["state"] == "error" and "prête sa carte" in state["error"]
    with pytest.raises(RuntimeError):
        group.prepare("grand:70b")                                 # pas un modèle réparti
