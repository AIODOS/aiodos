"""Faire calculer mes conversations par des inconnus : le côté demandeur du réseau public
(network/public_chat.py, network/placement.py, chat/public_consent.py ; décisions de
l'auteur du 27/10/2026).

Le compagnon est simulé par un vrai serveur HTTP local qui tient le contrat de sa route
POST /net/v1/public/ask/<clé>/public/v1/chat (compagnon/src/public_ask.rs) et répond pour
chaque prêteur ce que l'essai lui dicte (le NDJSON de public_lending.py, ou un refus) ;
l'annuaire public est rempli comme le ferait network/public.py. Cette machine est simulée
comme dans test_placement.py (son profil, son modèle)."""
import json
import logging
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
from aiodos.network import placement, public, public_chat, public_refusals, reputation, route
from aiodos.node import speed
from aiodos.services import ollama
from tests.test_collusion import establish

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
    code), ("error_after", morceau, code), ("silent", secondes) ou ("raw", [(attente,
    événement)…]) : après « meta », chaque événement tel quel, après son attente en
    secondes ; par défaut, « Bonjour »."""

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
                if act[0] == "raw":
                    try:
                        for pause, event in act[1]:
                            time.sleep(pause)
                            line(event)
                    except OSError:                  # le demandeur a coupé : c'est permis
                        pass
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


def checkable(*models, key="9f" * 32):
    """Une autre machine du réseau public, établie ici, qui prête ces modèles, sans place
    libre : elle n'est jamais choisie, mais ils deviennent contrôlables (placement._controllable :
    une machine établie les a ; deux machines vues ensemble ne suffisent plus, 29/10/2026)."""
    establish(key)
    return node(key, *models, free=0)


def measured(key, tps, established=True):
    """La vitesse de cette machine publique mesurée ici (une réponse servie de 10 s) ; et, sauf
    established=False, la machine établie ici (reputation.established) : seule une machine
    établie et mesurée passe devant cette machine (placement, 29/10/2026)."""
    reputation.note_served(key, int(tps * 10), 10.0)
    if established:
        establish(key)


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
    elles sont deux fois plus rapides, mesurées ici (inconnue sur un téléphone : devant)."""
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL, BIG, tps=50), node(OTHER, SMALL, lends=False),
              node(THIRD, SMALL, free=0), node("d4" * 32, BIG), node("e5" * 32, SMALL, seen=400),
              node(MY_KEY, SMALL))
    ex = Executor()
    assert placement.plan(AUTO, ex) == [SMALL]                       # sans permission : non
    assert placement.plan(AUTO, ex, public=True) == [SMALL, at(PC, SMALL)]   # PC, vitesse inconnue
    speed.note(100, 10)                                              # 10 jetons/s ici
    assert placement.plan(AUTO, ex, public=True) == [SMALL, at(PC, SMALL)]   # 50 annoncés
    measured(PC, 50)                                                 # 50 mesurés : devant
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
    measured(PC, 200)
    speed.note(100, 10)
    fixe = remote_models.model_id("fixe", SMALL)
    assert placement.plan(AUTO, Executor(), public=True) == [fixe, at(PC, SMALL), SMALL]
    assert placement.plan(AUTO, Executor(interactive=1), public=True) == [
        fixe, at(PC, SMALL), SMALL]
    local_models(monkeypatch)
    db.execute("DELETE FROM peers")
    assert placement.plan(AUTO, Executor(), public=True) == [at(PC, SMALL)]


def test_an_announced_speed_never_puts_a_stranger_ahead(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : un « tps » annoncé jusqu'à 100 000 faisait passer un menteur
    devant cette machine, qui récoltait les conversations. Seule la vitesse mesurée ici (une
    réponse servie au moins) fait passer une machine publique devant cette machine ;
    l'annoncée ne départage que les machines publiques entre elles. Jamais mesurée, une
    machine publique sert quand cette machine est occupée ou n'a pas le modèle. Ici la règle
    stricte : une machine établie prête ce modèle (la règle des petits réseaux, et l'amorce d'un
    téléphone qui ne se fie pas davantage à l'annonce : test_public_small.py)."""
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL, tps=99_999), node(OTHER, SMALL, tps=10), checkable(SMALL))
    plan = lambda busy=0: placement.plan(AUTO, Executor(busy), public=True)  # noqa: E731
    speed.note(100, 10)                                   # 10 jetons/s ici
    assert plan() == [SMALL, at(PC, SMALL), at(OTHER, SMALL)]
    speed.forget()
    monkeypatch.setattr(placement.android, "is_android", lambda: True)   # un téléphone non plus
    assert plan() == [SMALL, at(PC, SMALL), at(OTHER, SMALL)]
    assert placement.candidates(SMALL, Executor(), public=True)[1].tps == 1000   # borné
    measured(OTHER, 30, established=False)                # mesurée ici, pas établie : derrière
    assert plan() == [SMALL, at(PC, SMALL), at(OTHER, SMALL)]
    measured(OTHER, 30)                                   # établie, mesurée : devant le téléphone
    assert plan() == [at(OTHER, SMALL), SMALL, at(PC, SMALL)]
    # Cette machine occupée : les machines publiques libres d'abord, mesurées ou non ; à
    # modèle égal, l'établie d'abord, même moins rapide que ce que l'autre annonce
    assert plan(busy=1) == [at(OTHER, SMALL), at(PC, SMALL), SMALL]
    local_models(monkeypatch)                             # le modèle n'est pas ici
    assert plan() == [at(OTHER, SMALL), at(PC, SMALL)]


def test_parameters_read_in_a_model_name():
    assert public_chat.params("qwen3.5:9b") == 9 and public_chat.params("qwen3.5:0.8b") == 0.8
    assert public_chat.params("qwen3.6:35b-a3b") == 35 and public_chat.params("gemma3:270m") == 0.27
    assert public_chat.params("mistral:latest") is None
    # Une taille invraisemblable est ignorée (public_chat.MAX_PARAMS)
    assert public_chat.params("qwen3.5:999b") == 999 and public_chat.params("x:1000b") == 1000
    assert public_chat.params("qwen3.5:2000b") is None
    assert public_chat.params("qwen3.5:9b-5000b") == 9
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
    directory(node(PC, BIG, "embeddinggemma:latest", tps=60), checkable(BIG))
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


def test_only_a_model_another_machine_can_check_is_bigger(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : un nœud seul à annoncer « qwen3.5:999b » passait pour le plus
    gros modèle et n'avait jamais de machine de contrôle. Un modèle plus gros n'est pris que
    contrôlable : chez des machines publiques qui prêtent (place libre ou non) de trois groupes
    indépendants (29/10/2026 : deux machines vues ensemble ne suffisent plus), ou chez une de
    mes machines ; une taille invraisemblable ne compte pas."""
    from aiodos.network import peers
    from tests.test_collusion import seen
    liar, huge = "qwen3.5:999b", "qwen3.5:2000b"
    local_models(monkeypatch, SMALL)
    settings.set_value("public_bigger_model", True)
    plan = lambda: placement.plan(AUTO, Executor(), public=True, bigger=True)  # noqa: E731
    directory(node(PC, liar), node(OTHER, BIG))
    assert placement.bigger_public(SMALL) is None and plan() == [SMALL]
    directory(node(PC, liar, BIG), node(OTHER, BIG, free=0))     # deux machines vues ensemble
    assert placement.bigger_public(SMALL) is None and plan() == [SMALL]
    seen(OTHER, 7)                                                # trois groupes indépendants
    seen(THIRD, 14)
    directory(node(PC, liar, BIG), node(OTHER, BIG, free=0), node(THIRD, BIG, free=0))
    assert placement.bigger_public(SMALL) == BIG and plan() == [at(PC, BIG), SMALL]
    directory(node(PC, liar), node(OTHER, liar, lends=False))     # l'autre ne prête pas
    assert placement.bigger_public(SMALL) is None
    directory(node(PC, huge), node(OTHER, huge))                  # contrôlable, invraisemblable
    assert placement.bigger_public(SMALL) is None and plan() == [SMALL]
    directory(node(PC, BIG))                                      # chez une de mes machines
    assert placement.bigger_public(SMALL) is None
    peers.save("fixe", "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.set_profile("fixe", {"lends_models": True, "ollama": {"models": [
        {"name": BIG, "caps": ["completion"], "fits_vram": True, "loaded": False}]}})
    assert placement.bigger_public(SMALL) == BIG and plan() == [at(PC, BIG), SMALL]


def test_a_model_is_checkable_only_by_independent_groups(monkeypatch, companion, opened):
    """Contre la collusion (29/10/2026 au soir) : deux nœuds neufs d'un même menteur rendaient
    leur modèle « contrôlable » (deux machines distinctes suffisaient). Il faut désormais des
    machines qui prêtent (place libre ou non) de trois groupes indépendants (sightings.groups :
    celles apparues ensemble, de proche en proche, font un seul groupe), ou une machine établie
    qui le prête (de côté : plus rien), ou une de mes machines (cf. plus haut)."""
    from tests.test_collusion import seen
    fourth, fifth, mid = "d4" * 32, "e5" * 32, "qwen3.5:4b"
    checkable_now = lambda: placement._controllable(placement._lending())  # noqa: E731
    directory(node(PC, BIG), node(OTHER, BIG))                  # deux nœuds neufs, ensemble
    assert _tagged(BIG) not in checkable_now()
    directory(node(PC, BIG), node(OTHER, BIG), node(THIRD, BIG))
    assert _tagged(BIG) not in checkable_now()                  # trois, vus ensemble
    seen(PC, 0)
    seen(OTHER, 4)
    seen(THIRD, 8)                                              # 4 h, puis 4 h : un seul groupe
    assert _tagged(BIG) not in checkable_now()
    seen(THIRD, 12)                                             # 8 h avant OTHER : deux groupes
    assert _tagged(BIG) not in checkable_now()
    seen(fourth, 24)
    directory(node(PC, BIG), node(OTHER, BIG), node(THIRD, BIG), node(fourth, BIG, lends=False))
    assert _tagged(BIG) not in checkable_now()                  # ne prête pas : ne compte pas
    directory(node(PC, BIG), node(OTHER, BIG), node(THIRD, BIG), node(fourth, BIG, free=0))
    assert _tagged(BIG) in checkable_now()                      # trois groupes
    # Une seule machine établie qui le prête suffit ; mise de côté, plus rien
    directory(node(PC, mid), node(fifth, mid, free=0))
    assert _tagged(mid) not in checkable_now()
    establish(fifth)
    assert _tagged(mid) in checkable_now()
    reputation.set_aside(fifth)
    assert _tagged(mid) not in checkable_now()


def _tagged(name):
    return placement._tagged(name)


def test_established_machines_first_at_a_comparable_rank(monkeypatch, companion, opened):
    """À modèle égal, les machines publiques établies d'abord, même moins rapides ; jamais
    devant un modèle plus gros (« Automatique · réseau public » : le meilleur modèle d'abord)."""
    local_models(monkeypatch)                                   # le modèle n'est pas ici
    directory(node(PC, SMALL, tps=90), node(OTHER, SMALL, tps=10))
    plan = lambda: placement.plan(AUTO, Executor(), public=True)  # noqa: E731
    assert plan() == [at(PC, SMALL), at(OTHER, SMALL)]
    establish(OTHER)
    assert plan() == [at(OTHER, SMALL), at(PC, SMALL)]
    candidates = placement.candidates(SMALL, Executor(), public=True)
    assert [c.established for c in candidates] == [True, False]
    directory(node(PC, BIG, tps=90), node(OTHER, SMALL, tps=10), checkable(BIG, SMALL))
    assert [c.model for c in placement.best_public()] == [at(PC, BIG), at(OTHER, SMALL)]


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
    measured(PC, 40)
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
    measured(PC, 40)
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


def test_a_public_machine_is_never_chosen_by_hand(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : « public:<clé>:<modèle> » envoyé tel quel (une autre interface,
    un script avec le jeton) partait chez l'inconnu malgré use_public coupé et « Sur mes
    machines seulement ». Seul le choix automatique propose une machine publique."""
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL))
    settings.set_value("use_public", False)
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        for use_public in (False, True):
            settings.set_value("use_public", use_public)
            answer = ui.post("/api/chat", headers=AUTH, json={
                "text": "Mon secret", "model": at(PC, SMALL), "mine_only": True,
                "libraries": False, "conversations": False})
            assert answer.status_code == 400 and "à la main" in answer.json()["detail"]
    assert companion.requests == [] and conversations.list_tree()["conversations"] == []
    with pytest.raises(placement.NoMachine):
        placement.plan(at(PC, SMALL), Executor(), public=True)
    with pytest.raises(gateway_answer.GatewayError):            # les applis non plus
        gateway_answer.plan(at(PC, SMALL), Executor())


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
    for key, tps in ((PC, 90), (OTHER, 80), (THIRD, 70)):
        measured(key, tps)
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


# ----------------------------------------------------- les signes de vie du prêteur
def _loading(waits, every, stage="loading", words=("Bon", "jour")):
    """Le prêteur charge son modèle : `waits` signes de vie toutes les `every` s, puis la
    réponse."""
    return ("raw", [(every, {"type": "wait", "stage": stage})] * waits
            + [(0, {"type": "delta", "text": w}) for w in words]
            + [(0, {"type": "done", "tokens": len(words), "seconds": 9.0})])


def test_signs_of_life_extend_the_wait_for_the_first_word(monkeypatch, companion, opened):
    """Essai réel du 28/10 : 45 à 53 s de chargement chez le prêteur, pour 60 s d'attente.
    Ses signes de vie repoussent l'échéance du premier mot (jamais au-delà de LOADING_MAX_S)
    et l'interface dit qu'il charge le modèle, une fois par étape ; sans eux, l'échéance de
    toujours."""
    monkeypatch.setattr(public_chat, "FIRST_WORD_S", 0.5)
    monkeypatch.setattr(public_chat, "ALIVE_GRACE_S", 1.0)
    salut = [{"role": "user", "content": "Salut"}]
    companion.script = {PC: _loading(8, 0.2)}                # 1,6 s avant le premier mot
    out = list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert out == [("status", "La machine du réseau public charge le modèle…"),
                   ("content", "Bon"), ("content", "jour")]
    companion.script = {PC: ("raw", [(1.6, {"type": "delta", "text": "Trop tard"})])}
    with pytest.raises(public_chat.PublicError) as late:
        list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert late.value.code == "slow"
    monkeypatch.setattr(public_chat, "LOADING_MAX_S", 1.2)  # des signes de vie sans fin
    companion.script = {PC: _loading(25, 0.2)}
    started = time.monotonic()
    with pytest.raises(public_chat.PublicError) as endless:
        list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert endless.value.code == "slow" and time.monotonic() - started < 3.5
    # Le modèle déjà chargé, il lit la demande : dit autrement, une fois ; après le premier
    # mot, un signe de vie ne dit plus rien
    companion.script = {PC: ("raw", [(0, {"type": "wait", "stage": "reading"}),
                                     (0, {"type": "wait", "stage": "reading"}),
                                     (0, {"type": "delta", "text": "Oui"}),
                                     (0, {"type": "wait", "stage": "loading"}),
                                     (0, {"type": "done", "tokens": 1, "seconds": 0.1})])}
    assert list(public_chat.chat_stream(at(PC, SMALL), salut)) == [
        ("status", "La machine du réseau public lit la conversation…"), ("content", "Oui")]


def test_a_status_before_the_first_word_still_hands_over(monkeypatch, companion, opened):
    """« Charge le modèle… » n'engage pas la réponse : un refus ensuite, et la machine
    suivante répond (placement.QUIET)."""
    local_models(monkeypatch, SMALL)
    companion.script = {PC: ("raw", [(0, {"type": "wait", "stage": "loading"}),
                                     (0, {"type": "error", "error": "busy"})])}
    out = list(placement.run([at(PC, SMALL), SMALL], lambda m: pipeline.generate(m, [
        {"role": "user", "content": "Salut"}])))
    assert ("status", "La machine du réseau public charge le modèle…") in out
    assert out[-1] == ("content", f"Ici ({SMALL}).")


def test_an_unknown_event_is_ignored(companion, opened):
    """Ce qu'un prêteur plus récent pourrait envoyer, le demandeur l'ignore : c'est ainsi que
    les versions d'avant le 28/10 lisent « wait » (elles ne connaissent que meta, delta, done
    et error)."""
    companion.script = {PC: ("raw", [(0, {"type": "plus tard", "stage": 3}),
                                     (0, {"type": "delta", "text": "Bon"}),
                                     (0, {"type": "done", "tokens": 1, "seconds": 0.1})])}
    assert list(public_chat.chat_stream(at(PC, SMALL), [{"role": "user", "content": "x"}])) \
        == [("content", "Bon")]


def test_an_error_after_the_first_word_is_not_handed_over(monkeypatch, companion, opened):
    """La réponse a commencé : elle n'est pas refaite ailleurs ; le début est gardé."""
    local_models(monkeypatch, SMALL)
    monkeypatch.setattr(placement.android, "is_android", lambda: True)
    directory(node(PC, SMALL))
    measured(PC, 40)
    companion.script = {PC: ("error_after", "Bon", "busy")}
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")
        assert routes(events) == [at(PC, SMALL)]
        assert events[-1]["error"] == "La machine du réseau public est occupée."
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
    assert conv["messages"][-1]["content"] == "Bon"


def test_an_endless_answer_is_cut(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : le prêteur borne sa réponse (2 048 jetons) ; un menteur qui
    envoyait des Mio de texte les voyait tous gardés, rendus à chaque morceau sur la boucle du
    nœud et enregistrés. Coupée au-delà de MAX_TEXT caractères (le début reste affiché), ou de
    MAX_STREAM octets de flux, même sans texte."""
    salut = [{"role": "user", "content": "Salut"}]
    piece = "x" * 100_000
    # « raw » : le compagnon simulé tolère que le demandeur coupe en route
    companion.script = {PC: ("raw", [(0, {"type": "delta", "text": piece})] * 5)}
    out = []
    with pytest.raises(public_chat.PublicError) as cut:
        for kind, value in public_chat.chat_stream(at(PC, SMALL), salut):
            out.append(value)
    assert cut.value.code == "too_long" and "".join(out) == piece
    assert len(piece) <= public_chat.MAX_TEXT < 2 * len(piece)
    assert reputation.benched(PC)                       # écartée quelques minutes
    reputation.forget()
    # Dans la conversation : le début gardé, et pourquoi elle s'arrête
    local_models(monkeypatch)
    directory(node(PC, SMALL))
    companion.script = {PC: ("raw", [(0, {"type": "delta", "text": text})
                                     for text in ("Bon", piece, piece)])}
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")
        assert events[-1]["error"] == ("La réponse de la machine du réseau public dépassait "
                                       "la longueur ou la durée permise : elle a été coupée.")
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
    assert conv["messages"][-1]["content"] == "Bon" + piece
    # Des événements inconnus sans fin, sans un mot
    monkeypatch.setattr(public_chat, "MAX_STREAM", 8192)
    companion.script = {PC: ("raw", [(0, {"type": "plus tard", "pad": "y" * 200})] * 100)}
    with pytest.raises(public_chat.PublicError) as junk:
        list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert junk.value.code == "too_long"


def test_a_whole_answer_has_a_deadline(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : après le premier mot, plus aucune échéance ; un prêteur pouvait
    envoyer un octet toutes les 59 s sans fin. Toute la réponse : TOTAL_S secondes au plus
    depuis la demande ; au-delà, coupée (« too_long »), le début gardé, le prêteur écarté."""
    monkeypatch.setattr(public_chat, "TOTAL_S", 1.0)
    drops = ("raw", [(0.3, {"type": "delta", "text": "goutte "})] * 20)   # 6 s en tout
    companion.script = {PC: drops}
    out, started = [], time.monotonic()
    with pytest.raises(public_chat.PublicError) as late:
        for kind, value in public_chat.chat_stream(at(PC, SMALL),
                                                   [{"role": "user", "content": "Salut"}]):
            out.append(value)
    assert late.value.code == "too_long" and time.monotonic() - started < 3
    assert 1 <= len(out) < 20 and set(out) == {"goutte "}
    assert reputation.benched(PC)
    reputation.forget()
    # Dans la conversation : le début gardé, et pourquoi elle s'arrête
    local_models(monkeypatch)
    directory(node(PC, SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")
        assert events[-1]["error"] == ("La réponse de la machine du réseau public dépassait "
                                       "la longueur ou la durée permise : elle a été coupée.")
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
    assert conv["messages"][-1]["content"].startswith("goutte ")


def test_a_lender_that_fails_is_set_aside_a_few_minutes(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : des signes de vie pendant 180 s puis rien, une réponse lente,
    coupée, illisible ou trop longue, un moteur arrêté : le prêteur n'était jamais pénalisé ;
    il lisait la conversation sans y répondre, et restait candidat. Il est écarté BENCH_S
    secondes, en mémoire (ce n'est pas une preuve de triche) : ni candidat, ni machine de
    contrôle ; puis il revient. Un simple refus (occupée…) ne l'écarte pas."""
    local_models(monkeypatch)                                     # le modèle n'est pas ici
    monkeypatch.setattr(public_chat, "FIRST_WORD_S", 0.5)
    monkeypatch.setattr(public_chat, "ALIVE_GRACE_S", 0.5)
    monkeypatch.setattr(public_chat, "LOADING_MAX_S", 1.0)
    directory(node(PC, SMALL), node(OTHER, SMALL))
    plan = lambda: placement.plan(AUTO, Executor(), public=True)  # noqa: E731
    salut = [{"role": "user", "content": "Salut"}]
    assert plan() == [at(PC, SMALL), at(OTHER, SMALL)]
    words = [(0, {"type": "delta", "text": "Bon"})]
    for code, act in (("slow", _loading(20, 0.2)),               # des signes de vie, puis rien
                      ("slow", ("silent", 2)),
                      ("cut", ("raw", words)),                   # fini sans « done »
                      ("unreadable", ("raw", [(0, "pas un événement")])),
                      ("engine", ("raw", [(0, {"type": "error", "error": "engine"})]))):
        reputation.forget()
        companion.script = {PC: act}
        with pytest.raises(public_chat.PublicError) as failed:
            list(public_chat.chat_stream(at(PC, SMALL), salut))
        assert failed.value.code == code
        assert reputation.benched(PC) and reputation.is_set_aside(PC), code
        assert plan() == [at(OTHER, SMALL)], code
        assert placement.public_with(SMALL) == [(OTHER, SMALL)], code   # ni contrôle
    assert reputation.get(PC) is None                            # rien en base
    # Quelques minutes après : de retour
    reputation.forget()
    monkeypatch.setattr(reputation, "BENCH_S", 0.3)
    companion.script = {PC: ("raw", words)}
    with pytest.raises(public_chat.PublicError):
        list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert plan() == [at(OTHER, SMALL)]
    time.sleep(0.4)
    assert not reputation.benched(PC) and plan() == [at(PC, SMALL), at(OTHER, SMALL)]
    # Un refus avant tout calcul : pas écartée
    for status, code in ((503, "busy"), (503, "not_lending"), (404, "no_model")):
        companion.script = {PC: ("refuse", status, code)}
        with pytest.raises(public_chat.PublicError):
            list(public_chat.chat_stream(at(PC, SMALL), salut))
        assert not reputation.benched(PC), code


# ------------------------------- refus qui contredisent le profil (network/public_refusals.py)
def test_a_lender_that_keeps_refusing_after_announcing_a_place_is_set_aside(
        monkeypatch, companion, opened, caplog):
    """Relecture du 28/10/2026 : un prêteur reçoit la conversation avant de répondre ; un
    menteur la lisait puis refusait « avant calcul » (occupé…), sans jamais être écarté.
    Proposé par le choix des machines (une place, le modèle annoncés), il refuse trois fois en
    dix minutes : écarté comme après un échec. La conversation passe toujours à la suivante,
    comme avant ; le journal ne dit ni la conversation ni la clé entière."""
    local_models(monkeypatch)                                     # le modèle n'est pas ici
    directory(node(PC, SMALL), node(OTHER, SMALL, tps=10))
    companion.script = {PC: ("refuse", 503, "busy")}
    secret = "Mon secret bien gardé"
    caplog.set_level(logging.INFO, logger="aiodos.net")
    for n in (1, 2, 3):
        plan = placement.plan(AUTO, Executor(), public=True)
        assert plan == [at(PC, SMALL), at(OTHER, SMALL)], n
        out = list(placement.run(plan, lambda m: public_chat.chat_stream(
            m, [{"role": "user", "content": secret}], conversation=public_chat.tag(1))))
        assert [v for k, v in out if k == "route"] == plan        # la suivante répond
        assert out[-1] == ("content", "Bonjour"), n
        assert reputation.benched(PC) == (n == 3), n
    assert public_refusals.count(PC) == 0                         # écartée : tout recommence
    assert placement.plan(AUTO, Executor(), public=True) == [at(OTHER, SMALL)]
    assert placement.public_with(SMALL) == [(OTHER, SMALL)]       # ni machine de contrôle
    lines = [r.getMessage() for r in caplog.records if r.name == "aiodos.net"]
    said = [line for line in lines if "3 fois en 10 min" in line]
    assert len(said) == 1 and reputation.short(PC) in said[0] and "busy" in said[0]
    assert not any(PC in line or secret in line for line in lines)


def test_an_isolated_refusal_does_not_set_a_lender_aside(monkeypatch, companion, opened):
    """La course normale : la place vient d'être prise, et le profil ne le dit pas encore. Un
    refus, deux même, n'écartent pas ; ni un refus d'une machine qui n'a pas été proposée
    (pas de choix, choix trop ancien, un autre modèle), ni un refus qui ne contredit pas le
    profil (injoignable) ; une note de choix ne vaut que pour un essai."""
    local_models(monkeypatch)
    directory(node(PC, SMALL))
    salut = [{"role": "user", "content": "Salut"}]

    def refused(status, code):
        companion.script = {PC: ("refuse", status, code)}
        with pytest.raises(public_chat.PublicError) as failed:
            list(public_chat.chat_stream(at(PC, SMALL), salut))
        assert failed.value.code == code

    assert placement.plan(AUTO, Executor(), public=True) == [at(PC, SMALL)]
    refused(503, "busy")
    assert public_refusals.count(PC) == 1 and not reputation.benched(PC)
    refused(503, "busy")                                          # sans nouveau choix
    refused(503, "not_lending")
    assert public_refusals.count(PC) == 1
    public_refusals.offered(PC, BIG)                              # un autre modèle annoncé
    refused(404, "no_model")
    placement.plan(AUTO, Executor(), public=True)
    refused(502, "unreachable")                                   # ne contredit rien
    monkeypatch.setattr(public_refusals, "OFFER_S", 0.1)
    placement.plan(AUTO, Executor(), public=True)
    time.sleep(0.2)                                               # choix trop ancien
    refused(503, "busy")
    assert public_refusals.count(PC) == 1 and not reputation.benched(PC)
    # Une réponse servie entre deux n'efface rien, mais une seule de plus non plus n'écarte pas
    monkeypatch.setattr(public_refusals, "OFFER_S", 120)
    companion.script = {}
    placement.plan(AUTO, Executor(), public=True)
    assert list(public_chat.chat_stream(at(PC, SMALL), salut)) == [("content", "Bonjour")]
    placement.plan(AUTO, Executor(), public=True)
    refused(503, "busy")
    assert public_refusals.count(PC) == 2 and not reputation.benched(PC)
    assert placement.plan(AUTO, Executor(), public=True) == [at(PC, SMALL)]


def test_each_refusal_that_contradicts_the_profile_counts(monkeypatch, companion, opened):
    """no_model alors que le profil annonçait ce modèle ; too_big ou bad_request sur une
    demande que nos bornes acceptaient (plus strictes que celles du prêteur) ; un refus dans le
    flux avant le premier mot : chacun compte. Une machine de contrôle proposée (public_with)
    aussi."""
    local_models(monkeypatch)
    directory(node(PC, SMALL), node(OTHER, SMALL))
    salut = [{"role": "user", "content": "Salut"}]
    companion.script = {PC: ("refuse", 404, "no_model")}
    assert at(PC, SMALL) in placement.plan(AUTO, Executor(), public=True)
    with pytest.raises(public_chat.PublicError) as refused:
        list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert refused.value.code == "no_model" and public_refusals.count(PC) == 1
    companion.script = {PC: ("refuse", 413, "too_big")}
    assert (PC, SMALL) in placement.public_with(SMALL)           # machine de contrôle
    with pytest.raises(public_chat.PublicError):
        list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert public_refusals.count(PC) == 2 and not reputation.benched(PC)
    companion.script = {PC: ("raw", [(0, {"type": "wait", "stage": "loading"}),
                                     (0, {"type": "error", "error": "bad_request"})])}
    placement.plan(AUTO, Executor(), public=True)
    with pytest.raises(public_chat.PublicError) as streamed:
        list(public_chat.chat_stream(at(PC, SMALL), salut))
    assert streamed.value.code == "bad_request"
    assert reputation.benched(PC) and reputation.is_set_aside(PC)
    assert placement.plan(AUTO, Executor(), public=True) == [at(OTHER, SMALL)]
    assert reputation.get(PC) is None                            # rien en base


def test_the_first_note_says_the_system_prompt_goes_too(monkeypatch, companion, opened):
    """Les consignes générales données à l'IA (prompt système), personnalisées ou non, partent
    chez l'inconnu avec la conversation (c'est voulu) : la note de la première fois le dit."""
    from pathlib import Path
    root = Path(public_chat.__file__).resolve().parents[1] / "i18n"
    said = {lang: json.loads((root / lang / "chat.json").read_text(encoding="utf-8"))
            ["public_first_text"] for lang in ("fr", "en")}
    assert "y compris tes consignes générales données à l'IA (prompt système)" in said["fr"]
    assert "including your general instructions given to the AI (system prompt)" in said["en"]
    local_models(monkeypatch)
    directory(node(PC, SMALL))
    settings.set_value("system_prompt", "Appelle-moi Hippolyte.")
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        assert notices(ask(ui, "Bonjour")) == ["public_first"]
    assert companion.requests[-1]["body"]["messages"][0] == {
        "role": "system", "content": "Appelle-moi Hippolyte."}


def test_a_lone_surrogate_does_not_lose_the_answer(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : « \\ud800 » est du JSON valide ; reçu d'un prêteur, il faisait
    échouer l'envoi de la réponse à l'interface puis son enregistrement (réponse perdue)."""
    local_models(monkeypatch)
    directory(node(PC, SMALL))
    companion.script = {PC: ("answer", ["Bon\ud800", "jour"])}
    assert list(public_chat.chat_stream(at(PC, SMALL), [{"role": "user", "content": "x"}])) \
        == [("content", "Bon?"), ("content", "jour")]
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")
        assert events[-1]["error"] is None and "Bon?jour" in events[-1]["html"]
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
    assert conv["messages"][-1]["content"] == "Bon?jour"


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
