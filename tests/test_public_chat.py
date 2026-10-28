"""Faire calculer mes conversations par des inconnus : le côté demandeur du réseau public
(network/public_chat.py, network/placement.py, chat/public_consent.py ; décisions de
l'auteur du 27/10/2026).

Le compagnon est simulé par un vrai serveur HTTP local qui tient le contrat de sa route
POST /net/v1/public/ask/<clé>/public/v1/chat (compagnon/src/public_ask.rs) et répond pour
chaque prêteur ce que l'essai lui dicte (le NDJSON de public_lending.py, ou un refus) ;
l'annuaire public est rempli comme le ferait network/public.py. Cette machine est simulée
comme dans test_placement.py (son profil, son modèle)."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.chat import conversations, pipeline, public_consent
from aiodos.gateway import answer as gateway_answer
from aiodos.knowledge import chunking, store
from aiodos.network import placement, public, public_chat, route
from aiodos.node import speed
from aiodos.services import ollama

SMALL, BIG = "qwen3.5:0.8b", "qwen3.5:9b"
AUTO = placement.auto_id(SMALL)
MY_KEY = "00" * 32
PC, OTHER, THIRD = "a1" * 32, "b2" * 32, "c3" * 32
TOKEN = "jeton"
AUTH = {"X-Aiodos-Token": TOKEN}
NET_TOKEN = "jeton-du-compagnon"
TAG = re.compile(r"[A-Za-z0-9_-]{16,64}")


class Executor:
    def __init__(self, interactive=0):
        self.interactive = interactive
        self.busy = {"gpu": 0, "embed": 0}

    def interactive_begin(self):
        self.interactive += 1

    def interactive_end(self):
        self.interactive -= 1


# ------------------------------------------------------------- le compagnon simulé
class Companion:
    """Ce que public_chat lit du compagnon (running, listen, headers, supports), et sa route
    locale, servie pour de bon. script[clé] : ("answer", [morceaux]), ("refuse", statut,
    code), ("error_after", morceau, code) ou ("silent", secondes) ; par défaut, « Bonjour »."""

    def __init__(self):
        self.requests = []
        self.script = {}
        self.features = {"public-ask"}
        self.running = True
        self.headers = {"X-Aiodos-Net-Token": NET_TOKEN}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.listen = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def supports(self, feature):
        return feature in self.features

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        companion = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                prefix = "/net/v1/public/ask/"
                # Le corps lu d'abord : un refus envoyé avant, la connexion fermée sur un corps
                # pas lu, Windows la coupe (« unreachable » au lieu du refus)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("X-Aiodos-Net-Token") != NET_TOKEN:
                    return self._json(401, {"detail": "jeton"})
                if not self.path.startswith(prefix):
                    return self._json(404, {"error": "not_found"})
                key, _, tail = self.path[len(prefix):].partition("/")
                tag = self.headers.get("X-Aiodos-Conversation") or ""
                if not TAG.fullmatch(tag):
                    return self._json(400, {"error": "conversation"})
                body = json.loads(raw)
                companion.requests.append({"key": key, "tail": tail, "tag": tag, "body": body})
                act = companion.script.get(key, ("answer", ["Bonjour"]))
                if act[0] == "refuse":
                    return self._json(act[1], {"error": act[2], "detail": "refus"})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()

                def line(event):
                    self.wfile.write((json.dumps(event) + "\n").encode())
                    self.wfile.flush()
                line({"type": "meta", "model": body["model"], "version": "1.0.0"})
                if act[0] == "silent":
                    time.sleep(act[1])
                    return
                if act[0] == "error_after":
                    line({"type": "delta", "text": act[1]})
                    line({"type": "error", "error": act[2]})
                    return
                for piece in act[1]:
                    line({"type": "delta", "text": piece})
                line({"type": "done", "tokens": len(act[1]), "seconds": 0.1})
        return Handler


@pytest.fixture
def companion():
    fake = Companion()
    route.attach(fake)
    placement.forget()
    yield fake
    route.detach(fake)
    placement.forget()
    fake.close()


@pytest.fixture
def opened(monkeypatch):
    """Le réseau public rejoint, mes conversations confiables aux inconnus (activés par défaut,
    éteints dans les essais), sur un PC dont la vitesse n'est pas encore connue."""
    settings.set_many({"public_network": True, "use_public": True})
    monkeypatch.setattr(placement.android, "is_android", lambda: False)


def local_models(monkeypatch, *models):
    profile = {"name": "Portable", "gpus": [{"utilization": 0}], "ollama": {"models": [
        {"name": m, "caps": ["completion"], "fits_vram": True, "loaded": True} for m in models]}}
    monkeypatch.setattr(placement, "_local_profile", lambda: profile)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": m} for m in models])

    def here(model, messages, think=None, stop=None, **kw):
        yield "content", f"Ici ({model})."
    monkeypatch.setattr(ollama, "chat_stream", here)


def node(key, *models, lends=True, free=1, tps=40.0, seen=5):
    raw = {"key": key, "version": "1.0.0", "models": list(models), "lends": lends,
           "free": free, "seen_s": seen}
    if tps is not None:
        raw["tps"] = tps
    return raw


def directory(*nodes):
    """L'annuaire public tel que network/public.py le garde après l'avoir lu au compagnon."""
    public._remember(public._typed({"enabled": True, "key": MY_KEY, "nodes": list(nodes)}))


def at(key, model):
    return public_chat.model_id(key, model)


def _events(response):
    return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def ask(ui, text, model=AUTO, **extra):
    """Un message envoyé comme le fait l'interface ; sans bibliothèques ni mémoire, sauf si
    l'essai les demande."""
    extra = {"libraries": False, "conversations": False, **extra}
    with ui.stream("POST", "/api/chat", headers=AUTH,
                   json={"text": text, "model": model, **extra}) as response:
        assert response.status_code == 200, response.read()
        return _events(response)


def routes(events):
    return [e["model"] for e in events if e["type"] == "route"]


def notices(events):
    return [e["kind"] for e in events if e["type"] == "notice"]


# ----------------------------------------------------------------- le choix
def test_public_machines_that_can_answer(monkeypatch, companion, opened):
    """Seules celles qui prêtent, ont une place et le modèle (nom exact), vues récemment, et
    pas cette machine elle-même ; en dernier à vitesse comparable, devant cette machine si
    elles sont deux fois plus rapides (inconnue sur un téléphone : devant)."""
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL, BIG, tps=50), node(OTHER, SMALL, lends=False),
              node(THIRD, SMALL, free=0), node("d4" * 32, BIG), node("e5" * 32, SMALL, seen=400),
              node(MY_KEY, SMALL))
    ex = Executor()
    assert placement.plan(AUTO, ex) == [SMALL]                       # sans permission : non
    assert placement.plan(AUTO, ex, public=True) == [SMALL, at(PC, SMALL)]   # PC, vitesse inconnue
    speed.note(100, 10)                                              # 10 jetons/s ici
    assert placement.plan(AUTO, ex, public=True) == [at(PC, SMALL), SMALL]
    speed.forget()
    speed.note(300, 10)                                              # 30 jetons/s : 50 < 60
    assert placement.plan(AUTO, ex, public=True) == [SMALL, at(PC, SMALL)]
    speed.forget()
    monkeypatch.setattr(placement.android, "is_android", lambda: True)    # téléphone, inconnue
    assert placement.plan(AUTO, ex, public=True) == [at(PC, SMALL), SMALL]
    # Un modèle précis passe tel quel ; la passerelle ne demande jamais le réseau public
    assert placement.plan(SMALL, ex, public=True) == [SMALL]
    assert at(PC, SMALL) not in gateway_answer.plan(SMALL, ex)
    # Sa place prise par une conversation que je viens de lui envoyer : plus candidate
    with placement._occupy(at(PC, SMALL), ex):
        assert placement.plan(AUTO, ex, public=True) == [SMALL]
    # Un compagnon d'une version d'avant (sans « public-ask ») : aucune machine publique
    companion.features = set()
    assert placement.plan(AUTO, ex, public=True) == [SMALL]


def test_my_free_machines_stay_ahead_of_a_faster_stranger(monkeypatch, companion, opened):
    """Mes machines d'abord : une machine publique plus rapide passe devant celle-ci, jamais
    devant une de mes machines libres ; aucune machine à moi : la machine publique seule."""
    from aiodos.network import peers, remote_models
    local_models(monkeypatch, SMALL)
    peers.save("fixe", "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.set_profile("fixe", {"lends_models": True, "ollama": {"models": [
        {"name": SMALL, "caps": ["completion"], "fits_vram": True, "loaded": False}]}})
    directory(node(PC, SMALL, tps=200))
    speed.note(100, 10)
    fixe = remote_models.model_id("fixe", SMALL)
    assert placement.plan(AUTO, Executor(), public=True) == [fixe, at(PC, SMALL), SMALL]
    assert placement.plan(AUTO, Executor(interactive=1), public=True) == [
        fixe, at(PC, SMALL), SMALL]
    local_models(monkeypatch)
    db.execute("DELETE FROM peers")
    assert placement.plan(AUTO, Executor(), public=True) == [at(PC, SMALL)]


def test_parameters_read_in_a_model_name():
    assert public_chat.params("qwen3.5:9b") == 9 and public_chat.params("qwen3.5:0.8b") == 0.8
    assert public_chat.params("qwen3.6:35b-a3b") == 35 and public_chat.params("gemma3:270m") == 0.27
    assert public_chat.params("mistral:latest") is None
    assert not public_chat.converses("embeddinggemma:latest")
    assert not public_chat.converses("qwen3-embedding:0.6b") and public_chat.converses(BIG)
    assert public_chat.parse(at(PC, BIG)) == (PC, BIG)
    assert public_chat.parse("public:pas une clé:x") is None and public_chat.parse(BIG) is None
    assert placement.reserved(at(PC, BIG))         # jamais pris pour un modèle installé


# --------------------------------------------- un modèle plus gros, et son accord
def test_a_bigger_model_is_offered_once_then_used_or_not(monkeypatch, companion, opened):
    """Le S23 a qwen3.5:0.8b, un PC du réseau public qwen3.5:9b. Pas encore demandé : la carte
    d'accord, et la réponse part tout de suite sur ce téléphone ; oui : le 9b répond, marqué
    « réseau public » ; non : seulement le même modèle, sans carte."""
    local_models(monkeypatch, SMALL)
    directory(node(PC, BIG, "embeddinggemma:latest", tps=60))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")
        assert notices(events) == ["public_bigger"]
        card = next(e for e in events if e["type"] == "notice")
        assert card["model"] == BIG
        assert routes(events) == [SMALL] and companion.requests == []
        assert settings.get("public_bigger_model") is None       # la carte ne décide rien

        settings.set_value("public_bigger_model", True)          # « Oui » (carte ou Réglages)
        events = ask(ui, "Bonjour", conversation_id=events[0]["conversation_id"])
        assert routes(events) == [at(PC, BIG)] and "public_bigger" not in notices(events)
        route_event = next(e for e in events if e["type"] == "route")
        assert route_event["machine"] == "une machine du réseau public"
        assert events[-1]["error"] is None and "Bonjour" in events[-1]["html"]
        sent = companion.requests[-1]
        assert sent["key"] == PC and sent["tail"] == "public/v1/chat"
        assert sent["body"]["v"] == 1 and sent["body"]["model"] == BIG
        assert all(set(m) == {"role", "content"} for m in sent["body"]["messages"])
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
        assert conv["model"] == AUTO                       # la conversation reste automatique
        assert conv["messages"][-1]["model"] == at(PC, BIG)   # la réponse dit qui l'a calculée

        settings.set_value("public_bigger_model", False)         # « Non » : le même modèle
        events = ask(ui, "Encore")
        assert routes(events) == [SMALL] and notices(events) == []
    assert gateway_answer.circle_of(at(PC, BIG)) == "public"
    assert gateway_answer.route_info(at(PC, BIG))["place"] == "public"


def test_the_first_time_a_note_then_never_again(monkeypatch, companion, opened):
    local_models(monkeypatch)                                     # le modèle n'est pas ici
    directory(node(PC, SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        first = ask(ui, "Bonjour")
        second = ask(ui, "Encore")
    assert routes(first) == [at(PC, SMALL)] and notices(first) == ["public_first"]
    assert notices(second) == [] and settings.get("public_warned") is True


# ----------------------------------------------------------- mes documents
def _library(name, text, embed):
    collection_id = store.create_collection(name)
    pieces = chunking.chunk_text(text)
    store.upsert_entry(collection_id, f"{name}.txt", "file", None,
                       list(zip(pieces, embed(pieces))))


def test_my_documents_go_to_a_stranger_only_with_consent(monkeypatch, companion, opened,
                                                         fake_embed):
    settings.set_value("rag_min_score", 0.1)
    local_models(monkeypatch, SMALL)
    monkeypatch.setattr(placement.android, "is_android", lambda: True)   # le public devant
    directory(node(PC, SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        # Après le démarrage : sinon, le modèle d'embedding des vecteurs n'étant pas noté, le
        # nœud les retire et les refait en fond (reindex.schedule), pendant la question
        store.ensure_defaults()
        _library("maison", "fusée lunaire notes maison", fake_embed)
        events = ask(ui, "fusée lunaire", libraries=True)       # pas encore demandé
        assert notices(events) == ["public_documents"]
        assert routes(events) == [at(PC, SMALL), SMALL]          # passée à cette machine
        assert companion.requests == [] and events[-1]["error"] is None
        assert settings.get("public_documents") is None

        settings.set_value("public_documents", False)            # non : reste chez moi, sans carte
        events = ask(ui, "fusée lunaire", libraries=True)
        assert routes(events) == [at(PC, SMALL), SMALL] and notices(events) == []
        assert companion.requests == []

        events = ask(ui, "fusée lunaire", libraries=False)       # sans mes documents : part
        assert routes(events) == [at(PC, SMALL)]
        sent = " ".join(m["content"] for m in companion.requests[-1]["body"]["messages"])
        assert "maison" not in sent                              # ni passage, ni sommaire

        settings.set_value("public_documents", True)             # oui : les passages utiles partent
        events = ask(ui, "fusée lunaire", libraries=True)
        assert routes(events) == [at(PC, SMALL)]
        assert "notes maison" in " ".join(
            m["content"] for m in companion.requests[-1]["body"]["messages"])

        settings.set_value("public_documents", None)             # nulle part ailleurs : refus lisible
        local_models(monkeypatch)
        events = ask(ui, "fusée lunaire", libraries=True)
        assert "tes documents" in events[-1]["error"]


# ------------------------------------------- « Sur mes machines seulement », pièces jointes
def test_on_my_machines_only_keeps_a_conversation_home(monkeypatch, companion, opened):
    local_models(monkeypatch, SMALL)
    monkeypatch.setattr(placement.android, "is_android", lambda: True)
    directory(node(PC, SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour", mine_only=True)              # nouvelle conversation
        conv_id = events[0]["conversation_id"]
        assert routes(events) == [SMALL] and companion.requests == []
        assert ui.get(f"/api/conversations/{conv_id}", headers=AUTH).json()["mine_only"] is True
        events = ask(ui, "Encore", conversation_id=conv_id)      # gardé avec la conversation
        assert routes(events) == [SMALL]
        assert ui.patch(f"/api/conversations/{conv_id}", headers=AUTH,
                        json={"mine_only": False}).status_code == 200
        events = ask(ui, "Et maintenant", conversation_id=conv_id)
        assert routes(events) == [at(PC, SMALL)]
        settings.set_value("use_public", False)                  # coupé dans les Réglages
        assert routes(ask(ui, "Bonjour")) == [SMALL]
    assert conversations.get(conv_id)["mine_only"] is False


def test_attachments_never_go_to_a_stranger(monkeypatch, companion, opened):
    """Une conversation qui porte une pièce jointe reste chez moi ; et rien d'une image ne
    peut partir, même si on le demandait (défense en profondeur)."""
    conv_id = conversations.create()
    message = conversations.add_message(conv_id, "user", "Regarde")
    assert public_consent.allowed(conv_id, False)
    db.execute("INSERT INTO attachments(id, message_id, name, kind, type, size, sha256, created) "
               "VALUES('p1', ?, 'photo.png', 'image', 'image/png', 3, 'x', 0)", (message,))
    assert not public_consent.allowed(conv_id, False)
    assert not public_consent.allowed(None, False, joined=[{"id": "p2"}])
    with pytest.raises(public_chat.PublicError) as refused:
        public_chat.body(at(PC, SMALL), [{"role": "user", "content": "x", "images": ["AAAA"]}])
    assert refused.value.code == "attachments"
    long = [{"role": "user", "content": "mot " * 30000}]
    with pytest.raises(public_chat.PublicError, match="trop longue"):
        public_chat.body(at(PC, SMALL), long)                    # au-delà des bornes du prêteur


# ------------------------------------------------- l'identifiant de la conversation
def test_a_random_identifier_per_conversation(monkeypatch, companion, opened):
    local_models(monkeypatch)
    directory(node(PC, SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        first = ask(ui, "Bonjour")
        conv_id = first[0]["conversation_id"]
        ask(ui, "Encore", conversation_id=conv_id)
        ask(ui, "Autre conversation")
    tags = [r["tag"] for r in companion.requests]
    assert len(tags) == 3 and tags[0] == tags[1] != tags[2]
    assert all(TAG.fullmatch(tag) and str(conv_id) != tag for tag in tags)
    assert public_chat.tag(conv_id) == tags[0]                  # en mémoire seulement
    public_chat.forget_conversation(conv_id)
    assert public_chat.tag(conv_id) != tags[0]


# ------------------------------------------------- refus : la machine suivante répond
def test_a_refusal_before_the_first_word_hands_over(monkeypatch, companion, opened):
    local_models(monkeypatch, SMALL)
    monkeypatch.setattr(placement.android, "is_android", lambda: True)
    directory(node(PC, SMALL, tps=90), node(OTHER, SMALL, tps=80), node(THIRD, SMALL, tps=70))
    companion.script = {PC: ("refuse", 503, "busy"), OTHER: ("refuse", 502, "unreachable"),
                        THIRD: ("refuse", 404, "no_model")}
    plan = placement.plan(AUTO, Executor(), public=True)
    assert plan == [at(PC, SMALL), at(OTHER, SMALL), at(THIRD, SMALL), SMALL]

    def attempt(model):
        yield from pipeline.generate(model, [{"role": "user", "content": "Salut"}],
                                     conversation=public_chat.tag(1))
    out = list(placement.run(plan, attempt))
    assert [v for k, v in out if k == "route"] == plan
    assert out[-1] == ("content", f"Ici ({SMALL}).")
    # Le compagnon coupé en route ou en retard : la suivante aussi (il ne refait jamais d'essai)
    companion.script = {PC: ("refuse", 502, "interrupted"), OTHER: ("refuse", 504, "timeout"),
                        THIRD: ("refuse", 408, "too_slow")}
    assert list(placement.run(plan, attempt))[-1] == ("content", f"Ici ({SMALL}).")


def test_no_first_word_in_time_hands_over(monkeypatch, companion, opened):
    local_models(monkeypatch, SMALL)
    monkeypatch.setattr(public_chat, "FIRST_WORD_S", 1)
    companion.script = {PC: ("silent", 4)}
    plan = [at(PC, SMALL), SMALL]
    started = time.monotonic()
    out = list(placement.run(plan, lambda m: pipeline.generate(m, [
        {"role": "user", "content": "Salut"}])))
    assert out[-1] == ("content", f"Ici ({SMALL}).") and time.monotonic() - started < 4


def test_an_error_after_the_first_word_is_not_handed_over(monkeypatch, companion, opened):
    """La réponse a commencé : elle n'est pas refaite ailleurs ; le début est gardé."""
    local_models(monkeypatch, SMALL)
    monkeypatch.setattr(placement.android, "is_android", lambda: True)
    directory(node(PC, SMALL))
    companion.script = {PC: ("error_after", "Bon", "busy")}
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")
        assert routes(events) == [at(PC, SMALL)]
        assert events[-1]["error"] == "La machine du réseau public est occupée."
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
    assert conv["messages"][-1]["content"] == "Bon"


def test_the_stream_reads_the_lender_contract(companion, opened):
    """Le NDJSON du prêteur : les morceaux, puis le compte des jetons ; un autre modèle que
    celui demandé, ou un refus sans corps lisible, n'est pas pris pour une réponse."""
    companion.script = {PC: ("answer", ["Bon", "jour"])}
    out = list(public_chat.chat_stream(at(PC, SMALL), [{"role": "user", "content": "Salut"}],
                                       options={"temperature": 0, "num_predict": 64, "seed": 7},
                                       stats=True))
    assert out[:2] == [("content", "Bon"), ("content", "jour")]
    assert out[2][0] == "stats" and out[2][1]["completion_tokens"] == 2
    assert companion.requests[-1]["body"]["options"] == {"temperature": 0.0, "max_tokens": 64,
                                                         "seed": 7}
    companion.headers = {"X-Aiodos-Net-Token": "faux"}            # jeton refusé (401)
    with pytest.raises(public_chat.PublicError) as refused:
        list(public_chat.chat_stream(at(PC, SMALL), [{"role": "user", "content": "Salut"}]))
    assert refused.value.code == "companion"


# ------------------------------------------------------------------- migration
def test_migration_27_adds_on_my_machines_only():
    conn = db.connect()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    db.execute("ALTER TABLE conversations DROP COLUMN mine_only")
    db.execute("INSERT INTO conversations(title, model, created, updated) "
               "VALUES('Avant', 'auto:x', 0, 0)")
    db.execute("PRAGMA user_version=26")
    db.close()
    db._initialized.discard(path)
    assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION >= 27
    assert db.query_one("SELECT mine_only FROM conversations WHERE title='Avant'")[0] == 0
