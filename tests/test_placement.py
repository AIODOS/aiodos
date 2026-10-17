"""Choisir seul la machine qui répond (network/placement.py).

Cette machine si elle a le modèle et qu'elle est libre ; sinon la moins occupée de mes
machines qui le prêtent ; relève tant qu'aucun mot n'est arrivé ; charge annoncée par les
machines qui prêtent ; extraits de bibliothèques selon la machine qui répond. Sans réseau
ni Ollama : les machines appairées et le profil de cette machine sont simulés."""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.chat import pipeline
from aiodos.knowledge import chunking, store
from aiodos.network import client, peers, placement, remote_models
from aiodos.network.client import PeerError

KEY = b"k" * 32
MODEL = "qwen3.5:9b"
AUTO = placement.auto_id(MODEL)
HERE = MODEL


class Executor:
    """Ce que placement lit de l'exécuteur : conversations et unités en cours."""

    def __init__(self, interactive=0, busy=None):
        self.interactive = interactive
        self.busy = busy or {"gpu": 0, "embed": 0}
        self.history = []

    def interactive_begin(self):
        self.interactive += 1
        self.history.append("begin")

    def interactive_end(self):
        self.interactive -= 1
        self.history.append("end")


@pytest.fixture(autouse=True)
def clean():
    placement.forget()
    yield
    placement.forget()


def local_models(monkeypatch, *models, fits=True, loaded=False):
    """Profil de cette machine : ces modèles, qui tiennent (ou non) en mémoire vidéo."""
    profile = {"name": "Portable", "gpus": [{"utilization": 0}], "ollama": {"models": [
        {"name": m, "caps": ["completion"], "fits_vram": fits, "loaded": loaded}
        for m in models]}}
    monkeypatch.setattr(placement, "_local_profile", lambda: profile)


def lender(node_id, name, *models, trust="mine", fits=True, loaded=False, lends=True,
           online=True, aside=False, gpu=0):
    peers.save(node_id, name, KEY, "127.0.0.1", 1, trust=trust)
    peers.set_profile(node_id, {"lends_models": lends, "gpus": [{"utilization": gpu}],
                                "ollama": {"models": [
                                    {"name": m, "caps": ["completion"], "fits_vram": fits,
                                     "loaded": loaded} for m in models]}})
    if not online:
        db.execute("UPDATE peers SET last_seen=0 WHERE node_id=?", (node_id,))
    if aside:
        peers.set_aside(node_id, "contrôle échoué")
    return remote_models.model_id(node_id, MODEL)


# ------------------------------------------------------------------ le choix
def test_this_machine_answers_when_it_is_free(monkeypatch):
    local_models(monkeypatch, MODEL)
    fixe = lender("fixe", "PC fixe", MODEL, loaded=True)
    assert placement.plan(AUTO, Executor(busy={"gpu": 1, "embed": 2})) == [HERE, fixe]


def test_a_busy_machine_hands_over_to_a_free_one(monkeypatch):
    local_models(monkeypatch, MODEL)
    fixe = lender("fixe", "PC fixe", MODEL)
    assert placement.plan(AUTO, Executor(interactive=1)) == [fixe, HERE]


def test_the_least_busy_lender_first(monkeypatch):
    local_models(monkeypatch, MODEL)
    a = lender("a", "PC A", MODEL)
    b = lender("b", "PC B", MODEL)
    c = lender("c", "PC C", MODEL, loaded=True)
    placement.note_load("a", {"chats": 0, "units": 3})
    placement.note_load("b", {"chats": 1, "units": 0})
    placement.note_load("c", {"chats": 0, "units": 3})
    # libres d'abord (a, c), le modèle déjà chargé à égalité d'unités (c), puis b qui
    # répond déjà à une conversation, et cette machine, occupée par deux
    assert placement.plan(AUTO, Executor(interactive=2)) == [c, a, b, HERE]


def test_a_model_too_big_for_this_machine_goes_where_it_fits(monkeypatch):
    """qwen3.6 (22 Go) sur une carte de 8 Go : une machine libre où il tient répond."""
    local_models(monkeypatch, MODEL, fits=False)
    fixe = lender("fixe", "PC fixe", MODEL, fits=True)
    assert placement.plan(AUTO, Executor()) == [fixe, HERE]
    lender("fixe", "PC fixe", MODEL, fits=False)                   # nulle part : ici
    assert placement.plan(AUTO, Executor()) == [HERE, fixe]


def test_only_my_machines_answer_on_their_own(monkeypatch):
    local_models(monkeypatch)                                     # pas ici
    lender("ami", "PC de Léa", MODEL, trust="friend")
    lender("off", "Éteint", MODEL, online=False)
    lender("sec", "Ne prête pas", MODEL, lends=False)
    lender("aside", "Mis de côté", MODEL, aside=True)
    with pytest.raises(placement.NoMachine, match="Aucune machine"):
        placement.plan(AUTO, Executor())
    settings.set_value("auto_route_friends", True)                 # accepté dans les Réglages
    assert placement.plan(AUTO, Executor()) == [remote_models.model_id("ami", MODEL)]


def test_a_precise_choice_passes_as_it_is(monkeypatch):
    local_models(monkeypatch)
    for model in (MODEL, "peer:x:qwen3.5:9b", "cloud:mistral"):
        assert placement.plan(model, Executor()) == [model]


def test_conversations_sent_count_until_the_machine_says_more(monkeypatch):
    """Deux conversations envoyées coup sur coup ne vont pas toutes deux à la même machine,
    même si ses dernières nouvelles ne les comptent pas encore."""
    local_models(monkeypatch)
    a = lender("a", "PC A", MODEL)
    b = lender("b", "PC B", MODEL)
    placement.note_load("a", {"chats": 0, "units": 0})
    placement.note_load("b", {"chats": 0, "units": 1})
    started = threading.Event()
    release = threading.Event()

    def attempt(model):
        started.set()
        release.wait(5)
        yield "content", "ok"
    first = threading.Thread(target=lambda: list(placement.run([a], attempt)))
    first.start()
    started.wait(5)
    assert placement.plan(AUTO, Executor()) == [b, a]
    release.set()
    first.join(5)
    assert placement.plan(AUTO, Executor()) == [a, b]


def test_reported_loads_are_checked_and_forgotten(monkeypatch):
    local_models(monkeypatch)
    a = lender("a", "PC A", MODEL)
    b = lender("b", "PC B", MODEL)
    placement.note_load("a", {"chats": "beaucoup", "units": -5})   # rien d'utilisable
    placement.note_load("b", {"chats": 10 ** 9})
    assert placement._reported("a") == {"chats": 0, "units": 0}
    assert placement._reported("b")["chats"] == placement.LOAD_MAX
    assert placement.plan(AUTO, Executor()) == [a, b]
    placement.note_load("b", None)                                 # ne dit plus rien
    assert placement._reported("b") is None


def test_a_machine_tells_its_load_only_when_it_lends_and_only_to_my_machines(monkeypatch):
    """Un ami ne voit pas l'activité de cette machine (sharing.FRIEND_PROFILE)."""
    class Manager:
        executor = Executor(interactive=1, busy={"gpu": 1, "embed": 2})
    peers.save("mine", "PC fixe", KEY, "127.0.0.1", 1)
    peers.save("ami", "PC de Léa", KEY, "127.0.0.1", 1, trust="friend")
    assert placement.load_field(Manager(), "mine") == {}
    settings.set_value("lend_models", True)
    assert placement.load_field(Manager(), "mine") == {"load": {"chats": 1, "units": 3}}
    assert placement.load_field(Manager(), "ami") == {}
    assert placement.load_field(Manager(), "inconnue") == {}


def test_pings_and_profiles_carry_the_load(monkeypatch):
    from aiodos.network import p2p_app

    class Manager:
        executor = Executor(interactive=1)

        def name(self):
            return "PC fixe"
    peers.save("a", "PC portable", KEY, "127.0.0.1", 1)
    peers.save("ami", "PC de Léa", KEY, "127.0.0.1", 1, trust="friend")
    settings.set_value("lend_models", True)
    monkeypatch.setattr(p2p_app.identity, "profile", lambda: {"name": "PC fixe"})
    assert p2p_app._ping(Manager(), "a", {})["load"] == {"chats": 1, "units": 0}
    assert p2p_app._profile(Manager(), "a", {})["load"] == {"chats": 1, "units": 0}
    assert "load" not in p2p_app._ping(Manager(), "ami", {})
    assert "load" not in p2p_app._profile(Manager(), "ami", {})


def test_untagged_names_and_reserved_prefixes(monkeypatch):
    """« llama3 » trouve « llama3:latest », comme pour Ollama ; un modèle d'Ollama nommé
    comme un identifiant d'AIODOS (« cloud:x ») n'est jamais choisi d'office."""
    local_models(monkeypatch, "llama3:latest", "cloud:mistral")
    lender("fixe", "PC fixe", "llama3:latest")
    assert placement.plan("auto:llama3", Executor()) == [
        "llama3:latest", remote_models.model_id("fixe", "llama3:latest")]
    with pytest.raises(placement.NoMachine):
        placement.plan("auto:cloud:mistral", Executor())
    assert placement.same_model("qwen3.5:9b", "qwen3.5:9b")
    assert not placement.same_model("qwen3.5:9b", "qwen3.5")
    assert placement.same_model("moi/modele:latest", "moi/modele")


# ------------------------------------------------------------------ la relève
def test_a_machine_that_fails_before_answering_hands_over():
    tried = []

    def attempt(model):
        tried.append(model)
        yield "sources", []
        if model == "peer:a:m":
            raise PeerError("« PC A » ne prête plus ses modèles.")
        yield "content", f"réponse de {model}"
    out = list(placement.run(["peer:a:m", "m"], attempt, Executor()))
    assert tried == ["peer:a:m", "m"]
    assert out == [("route", "peer:a:m"), ("sources", []), ("route", "m"), ("sources", []),
                   ("content", "réponse de m")]


def test_no_relay_once_the_answer_has_started_or_was_stopped():
    def attempt(model):
        yield "content", "Bon"
        raise PeerError("Connexion coupée.")
    with pytest.raises(PeerError):
        list(placement.run(["peer:a:m", "m"], attempt))
    stop = threading.Event()

    def stopped(model):
        stop.set()
        raise PeerError("arrêt")
        yield
    with pytest.raises(PeerError):
        list(placement.run(["peer:a:m", "m"], stopped, stop=stop))


def test_background_gpu_work_waits_only_while_this_machine_answers():
    executor = Executor()

    def attempt(model):
        yield "content", str(executor.interactive)
    assert list(placement.run(["m"], attempt, executor))[-1] == ("content", "1")
    assert list(placement.run(["peer:a:m"], attempt, executor))[-1] == ("content", "0")
    assert executor.history == ["begin", "end"] and executor.interactive == 0


# ---------------------------------------------- ce qui est proposé à l'interface
def test_each_model_is_offered_once_in_automatic_mode(monkeypatch):
    from aiodos.services import ollama
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": MODEL, "size": 6}, {"name": "embeddinggemma", "size": 1}])
    monkeypatch.setattr(ollama, "capabilities_many", lambda names: {
        MODEL: ["completion"], "embeddinggemma": ["embedding"]})
    lender("fixe", "PC fixe", MODEL, "qwen3.6")
    lender("ami", "PC de Léa", "mistral:7b", trust="friend")
    models = pipeline.available_models()
    auto = {m["label"]: m["machines"] for m in models if m["group"] == "auto"}
    assert auto == {MODEL: ["cette machine", "PC fixe"], "qwen3.6": ["PC fixe"]}
    friend = next(m for m in models if m["label"] == "mistral:7b")
    assert friend["group"] == "remote" and not friend["auto"] and friend["trust"] == "friend"


# --------------------------------------- extraits selon la machine qui répond
def _library(name, text, embed, scope=None):
    collection_id = store.create_collection(name)
    if scope:
        store.set_scope(collection_id, scope)
    pieces = chunking.chunk_text(text)
    store.upsert_entry(collection_id, f"{name}.txt", "file", None,
                       list(zip(pieces, embed(pieces))))


def test_extracts_follow_the_machine_that_answers(fake_embed):
    """Gardée sur ce PC : jamais ailleurs ; mémoire des conversations : jamais ailleurs ;
    chez un ami : seulement ce qui est partagé avec mes amis."""
    store.ensure_defaults()
    settings.set_value("rag_min_score", 0.1)
    _library("secret", "fusée lunaire plan secret", fake_embed, scope="local")
    _library("maison", "fusée lunaire notes maison", fake_embed)
    _library("partage", "fusée lunaire notes partagées", fake_embed, scope="friends")
    memory = store.conversation_collection_id()
    store.upsert_entry(memory, "Conv · #9", "conversation", "9",
                       [("fusée lunaire souvenir", fake_embed(["fusée lunaire souvenir"])[0])])

    def seen(destination):
        messages, sources = pipeline.build_messages(
            [], "fusée lunaire", libraries=True, conversations=True, web_search=False,
            destination=destination)
        text = " ".join(m["content"] for m in messages)
        return {s["label"] for s in sources}, text

    here, _ = seen(None)
    assert here == {"secret.txt", "maison.txt", "partage.txt", "Conv · #9"}
    mine, text = seen("mine")
    assert mine == {"maison.txt", "partage.txt"} and "secret" not in text
    friend, text = seen("friend")
    assert friend == {"partage.txt"} and "maison" not in text


# ----------------------------------------------- la conversation de l'interface
TOKEN = "jeton"
AUTH = {"X-Aiodos-Token": TOKEN}


def _events(response):
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def test_an_automatic_conversation_says_which_machine_answered(monkeypatch, fake_embed):
    store.ensure_defaults()
    local_models(monkeypatch)                                     # le modèle n'est pas ici
    fixe = lender("fixe", "PC fixe", MODEL)
    sent = {}

    def fake_stream(my_id, peer, action, payload, stop=None, **kw):
        sent.update(peer=peer["node_id"], payload=payload)
        yield {"content": "Bonjour"}
    monkeypatch.setattr(client, "stream", fake_stream)
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        assert any(m["id"] == AUTO for m in ui.get("/api/models", headers=AUTH).json())
        with ui.stream("POST", "/api/chat", headers=AUTH,
                       json={"text": "Salut", "model": AUTO, "libraries": False}) as response:
            events = _events(response)
        route = next(e for e in events if e["type"] == "route")
        assert route == {"type": "route", "model": fixe, "machine": "PC fixe"}
        assert events[-1]["type"] == "done" and events[-1]["error"] is None
        assert sent["peer"] == "fixe" and sent["payload"]["model"] == MODEL
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
        assert conv["model"] == AUTO                       # la conversation reste automatique
        assert conv["messages"][-1]["model"] == fixe       # la réponse dit qui l'a calculée

        lender("fixe", "PC fixe", MODEL, lends=False)      # plus personne : refus lisible
        refused = ui.post("/api/chat", headers=AUTH, json={"text": "Encore", "model": AUTO})
        assert refused.status_code == 400 and "Aucune machine" in refused.json()["detail"]


def test_a_large_model_split_between_my_machines_passes_as_it_is(monkeypatch):
    """« reparti:<modèle> » (cluster/group.py) : choisi tel quel, jamais en mode automatique,
    et sans pause du travail de fond (il ne tourne pas seulement ici, cf. runs_here)."""
    local_models(monkeypatch)
    executor = Executor()
    assert placement.plan("reparti:qwen3.6", executor) == ["reparti:qwen3.6"]
    assert not pipeline.runs_here("reparti:qwen3.6")
    assert placement.destination("reparti:qwen3.6") is None     # le texte reste ici

    def attempt(model):
        yield "content", str(executor.interactive)
    assert list(placement.run(["reparti:qwen3.6"], attempt, executor))[-1] == ("content", "0")
    assert placement.machine_of("reparti:qwen3.6") == "réparti entre mes machines"


def test_a_request_refused_by_its_content_is_not_tried_elsewhere():
    """404 (modèle parti), 5xx, machine injoignable : la suivante essaie ; 400 (la demande
    elle-même) : ailleurs, elle serait refusée aussi."""
    from aiodos.gateway.answer import GatewayError
    tried = []

    def failing(status):
        def attempt(model):
            tried.append(model)
            raise GatewayError(status, "refus")
            yield
        return attempt
    with pytest.raises(GatewayError):
        list(placement.run(["m", "peer:a:m"], failing(400)))
    assert tried == ["m"]
    tried.clear()
    with pytest.raises(GatewayError):
        list(placement.run(["m", "peer:a:m"], failing(404)))
    assert tried == ["m", "peer:a:m"]


def test_passages_from_other_machines_follow_the_machine_that_answers(monkeypatch, fake_embed):
    """Chercher chez les autres en mode automatique : une machine autorisée passe d'abord
    (ici, celle-ci, bien que plus lente), et la machine d'un ami qui prend le relais ne
    reçoit ni leurs passages, ni les réponses qui s'en servaient (pipeline.peers_allowed)."""
    from aiodos.network import remote_search
    from aiodos.services import ollama
    store.ensure_defaults()
    settings.set_value("auto_route_friends", True)
    local_models(monkeypatch, MODEL, fits=False)                  # lent ici
    friend = lender("ami", "PC de Léa", MODEL, trust="friend")    # rapide chez l'ami
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": MODEL}])
    assert placement.plan(AUTO, Executor()) == [friend, HERE]     # sans recherche ailleurs
    monkeypatch.setattr(remote_search, "search", lambda question, accepted: ([{
        "node_id": "fixe", "library": "Notes", "document": "plan.txt", "text": "passage ouvert",
        "rank": 0, "score": 0.9, "peer": "PC fixe"}], []))
    seen = []

    def fake_generate(model, messages, think=True, stop=None):
        seen.append((model, any("passage ouvert" in m["content"] for m in messages)))
        if model == HERE:
            raise RuntimeError("Ollama ne répond pas")            # relève par l'ami
        yield "content", "Bonjour"
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        with ui.stream("POST", "/api/chat", headers=AUTH, json={
                "text": "Salut", "model": AUTO, "libraries": False,
                "peer_libraries": ["fixe"]}) as response:
            events = _events(response)
    assert seen == [(HERE, True), (friend, False)]
    assert events[-1]["error"] is None
