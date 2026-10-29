"""Un téléphone neuf, sans aucun modèle, converse grâce au réseau public (« Automatique · réseau
public », network/placement.py PUBLIC_BEST, chat/public_consent.py entry ; 28/10/2026) : le
cas le plus important pour l'auteur, AIODOS reliant d'abord les machines d'inconnus.

Sans modèle de conversation ici ni sur mes machines, une machine du réseau public répond avec
son meilleur modèle (le plus grand nombre de paramètres lu dans le nom), puis la suivante si
elle refuse ; pas de carte d'un modèle plus gros (rien ici à quoi comparer), la carte des
documents et la note de la première fois comme ailleurs. Réglages coupés, « Sur mes machines
seulement », pièces jointes : un refus lisible, rien ne part. Le titre de la conversation se
fait sans modèle, et rien d'autre que la réponse ne part chez un inconnu.

Le compagnon simulé et l'annuaire public sont ceux de test_public_chat.py."""
import json
import logging
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from aiodos import db, paths, settings
from aiodos.api.server import create_app
from aiodos.chat import conversations, enrich, public_consent, retouches
from aiodos.network import peers, placement
from aiodos.services import ollama
from tests import i18n_js
from tests.test_public_chat import (AUTH, BIG, OTHER, PC, SMALL, THIRD, TOKEN, Executor,
                                    _library, ask, at, checkable, directory, local_models, node,
                                    notices, routes)
from tests.test_public_chat import companion, opened  # noqa: F401 (fixtures)

BEST = placement.PUBLIC_BEST
MID = "qwen3.5:4b"
FOURTH, FIFTH, SIXTH = "d4" * 32, "e5" * 32, "f6" * 32
NO_PUBLIC = ("Aucune machine du réseau public ne peut répondre pour l'instant ; installe un "
             "modèle ou réessaie plus tard.")
NODE = shutil.which("node")


def nothing_here(monkeypatch):
    """Aucun modèle ici (un téléphone neuf) ; un appel au moteur de cette machine se verrait."""
    local_models(monkeypatch)
    monkeypatch.setattr(ollama, "capabilities_many", lambda names: {n: [] for n in names})
    called = []

    def here(model, messages, **kw):
        called.append(model)
        yield "content", "ici"
    monkeypatch.setattr(ollama, "chat_stream", here)
    return called


def refused(ui, text="Bonjour", **extra):
    """Un envoi refusé avant tout (400) : son message."""
    response = ui.post("/api/chat", headers=AUTH, json={"text": text, "model": BEST,
                                                         "libraries": False, **extra})
    assert response.status_code == 400, response.text
    return response.json()["detail"]


def conversations_count():
    return db.query_one("SELECT COUNT(*) FROM conversations")[0]


# ------------------------------------------------------------- le sélecteur de modèles
def test_the_model_selector_offers_automatic_public(monkeypatch, companion, opened):
    """Rien ici ni sur mes machines : « Automatique · réseau public », prêt si une machine du
    réseau public peut répondre ; un modèle ici, ou chez une de mes machines : rien de plus
    qu'avant."""
    nothing_here(monkeypatch)
    directory(node(PC, SMALL), node(OTHER, BIG, "embeddinggemma:latest"), checkable(SMALL, BIG))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        assert ui.get("/api/models", headers=AUTH).json() == [
            {"id": BEST, "label": "Automatique · réseau public", "group": "public",
             "ready": True, "models": [BIG, SMALL], "searching": False}]
        directory(node(PC, SMALL, free=0), checkable(SMALL))     # plus de place nulle part
        assert ui.get("/api/models", headers=AUTH).json() == [
            {"id": BEST, "label": "Automatique · réseau public", "group": "public",
             "ready": False, "models": [], "searching": False}]
        directory(node(PC, SMALL), checkable(SMALL))
        assert ui.get("/api/models", headers=AUTH).json()[0]["ready"] is True
        companion.features = set()                               # compagnon d'avant : non plus
        assert ui.get("/api/models", headers=AUTH).json()[0]["ready"] is False
        companion.features = {"public-ask"}

        monkeypatch.setattr(ollama, "list_models",               # un modèle ici : comme avant
                            lambda max_age=10: [{"name": SMALL, "size": 6}])
        monkeypatch.setattr(ollama, "capabilities_many", lambda n: {m: ["completion"] for m in n})
        assert all(m["group"] != "public" for m in ui.get("/api/models", headers=AUTH).json())

        local_models(monkeypatch)                                # chez une de mes machines
        peers.save("fixe", "PC fixe", b"k" * 32, "127.0.0.1", 1)
        peers.set_profile("fixe", {"lends_models": True, "ollama": {"models": [
            {"name": MID, "caps": ["completion"], "fits_vram": True, "loaded": False}]}})
        assert all(m["group"] != "public" for m in ui.get("/api/models", headers=AUTH).json())


def test_without_public_or_on_my_machines_only_nothing_changes(monkeypatch, companion, opened):
    """Réglages coupés : rien de proposé, comme avant ; « Sur mes machines seulement » (que
    l'interface regarde) : refusé, avec la raison ; rien ne part chez un inconnu."""
    nothing_here(monkeypatch)
    directory(node(PC, SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        settings.set_value("use_public", False)
        assert ui.get("/api/models", headers=AUTH).json() == []
        assert "Faire calculer mes conversations par des inconnus" in refused(ui)
        settings.set_many({"use_public": True, "public_network": False})
        assert ui.get("/api/models", headers=AUTH).json() == []
        assert "Rejoindre le réseau public" in refused(ui)
        settings.set_value("public_network", True)

        assert "Sur mes machines seulement" in refused(ui, mine_only=True)
        conv_id = conversations.create(mine_only=True)          # gardé avec la conversation
        assert "Sur mes machines seulement" in refused(ui, conversation_id=conv_id)
        # Un autre modèle que celui du réseau public : le refus de toujours
        auto = placement.auto_id(SMALL)
        response = ui.post("/api/chat", headers=AUTH, json={"text": "x", "model": auto,
                                                            "mine_only": True})
        assert response.status_code == 400 and "Aucune machine" in response.json()["detail"]
    assert companion.requests == [] and conversations_count() == 1
    # Un modèle ici, le réseau public coupé : la machine de toujours
    local_models(monkeypatch, SMALL)
    settings.set_value("use_public", False)
    reach = public_consent.reach(auto, None, False)
    assert reach.closed == "off" and placement.plan(auto, Executor(), reach.public) == [SMALL]
    # Un plan sans le réseau public (la passerelle des applis) ne s'en sert jamais
    with pytest.raises(placement.NoMachine):
        placement.plan(BEST, Executor())


# ---------------------------------------------------------------- la réponse
def test_no_model_here_the_best_public_model_first(monkeypatch, companion, opened):
    """Le meilleur modèle de conversation de chaque machine qui prête et a une place, le plus
    gros d'abord, puis le plus rapide ; un refus fait passer à la suivante. Pas de carte d'un
    modèle plus gros, même pas demandée ; la note de la première fois ; la réponse marquée."""
    called = nothing_here(monkeypatch)
    directory(node(PC, SMALL, tps=90), node(OTHER, BIG, "embeddinggemma:latest", tps=20),
              node(THIRD, "mistral:latest", MID, tps=50), node(FOURTH, "bge-m3:latest"),
              node(FIFTH, BIG, free=0), node(SIXTH, BIG, lends=False),
              checkable(SMALL, MID, BIG))
    assert placement.plan(BEST, Executor(), public=True) == [
        at(OTHER, BIG), at(THIRD, MID), at(PC, SMALL)]
    assert settings.get("public_bigger_model") is None
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour", model=BEST)
        assert notices(events) == ["public_first"]               # ni « public_bigger »
        assert routes(events) == [at(OTHER, BIG)]
        route = next(e for e in events if e["type"] == "route")
        assert route["machine"] == "une machine du réseau public"
        assert events[-1]["error"] is None and "Bonjour" in events[-1]["html"]
        sent = companion.requests[-1]
        assert sent["key"] == OTHER and sent["body"]["model"] == BIG
        conv_id = events[0]["conversation_id"]
        conv = ui.get(f"/api/conversations/{conv_id}", headers=AUTH).json()
        assert conv["model"] == BEST and conv["messages"][-1]["model"] == at(OTHER, BIG)

        companion.script = {OTHER: ("refuse", 503, "busy")}     # la première refuse : la suivante
        events = ask(ui, "Encore", model=BEST, conversation_id=conv_id)
        assert routes(events) == [at(OTHER, BIG), at(THIRD, MID)]
        assert events[-1]["error"] is None and notices(events) == []
        assert companion.requests[-1]["body"]["model"] == MID

        settings.set_value("public_bigger_model", False)         # « Non » aux modèles plus gros :
        companion.script = {}                                    # sans objet ici
        assert routes(ask(ui, "Et puis", model=BEST)) == [at(OTHER, BIG)]
        companion.script = {OTHER: ("refuse", 503, "busy"), THIRD: ("refuse", 404, "no_model"),
                            PC: ("refuse", 503, "not_lending")}  # toutes refusent : lisible
        events = ask(ui, "Personne", model=BEST)
        assert events[-1]["error"] == "La machine du réseau public ne prête plus en ce moment."
    assert called == []                                          # jamais le moteur de cette machine


def test_a_name_alone_never_makes_the_best_model(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : « Automatique · réseau public » triait par la taille lue dans
    le nom ; un nœud qui annonçait « qwen3.5:999b » passait toujours premier et, seul à porter
    ce nom, n'avait jamais de machine de contrôle. Seul un modèle contrôlable est pris : chez
    une machine établie qui prête (place libre ou non), ou chez trois groupes indépendants
    (29/10/2026 : deux machines vues ensemble ne suffisent plus) ; une taille invraisemblable
    ne compte pas."""
    from tests.test_collusion import establish
    nothing_here(monkeypatch)
    liar, huge = "qwen3.5:999b", "qwen3.5:2000b"
    plan = lambda: placement.plan(BEST, Executor(), public=True)  # noqa: E731
    directory(node(PC, liar, SMALL), node(OTHER, BIG), checkable(SMALL))
    assert plan() == [at(PC, SMALL)]                    # ni « 999b », ni le 9b, seuls à les avoir
    directory(node(PC, liar, SMALL), node(OTHER, BIG), node(THIRD, BIG, free=0), checkable(SMALL))
    assert plan() == [at(PC, SMALL)]                    # le 9b chez deux machines vues ensemble
    establish(THIRD)
    assert plan() == [at(OTHER, BIG), at(PC, SMALL)]   # chez une machine établie : choisi
    directory(node(PC, huge), node(OTHER, huge), node(THIRD, SMALL), checkable(SMALL))
    assert plan()[0] == at(THIRD, SMALL)                # contrôlable, mais 2 000 milliards : non
    assert companion.requests == []


def test_with_nothing_checkable_a_phone_still_gets_an_answer(monkeypatch, companion, opened):
    """Repli (décision du 28/10/2026, à revoir) : sur un réseau encore minuscule, aucun modèle
    n'est contrôlable ; un téléphone sans modèle prend alors n'importe lequel, une machine qui
    a déjà répondu ici sans faute d'abord, plutôt que de n'avoir aucune réponse."""
    from aiodos.network import reputation
    nothing_here(monkeypatch)
    plan = lambda: placement.plan(BEST, Executor(), public=True)  # noqa: E731
    directory(node(PC, BIG))                                        # un seul PC, le 9b
    assert plan() == [at(PC, BIG)]
    directory(node(PC, "qwen3.5:999b"), node(OTHER, BIG))
    assert plan() == [at(PC, "qwen3.5:999b"), at(OTHER, BIG)]       # le plus gros d'abord
    reputation.note_served(OTHER, 200, 5.0)                         # l'autre a déjà bien servi
    assert plan() == [at(OTHER, BIG), at(PC, "qwen3.5:999b")]
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        assert ui.get("/api/models", headers=AUTH).json()[0]["ready"] is True
    assert companion.requests == []


def test_the_fallback_prefers_a_past_here_then_seniority_then_size(monkeypatch, companion, opened):
    """Repli « rien de contrôlable », revu le 29/10/2026 au soir : un nœud neuf qui annonçait un
    gros modèle passait premier (la taille venait juste après « a déjà répondu »). Désormais :
    une machine établie d'abord, puis une qui a déjà répondu ici sans faute, puis la plus
    ancienne (connue du compagnon, known_s, ou vue ici depuis longtemps, sightings ; à un
    facteur deux près dès 10 min), puis le plus gros modèle."""
    from aiodos.network import reputation
    from tests.test_collusion import establish, seen
    nothing_here(monkeypatch)
    liar = "qwen3.5:999b"
    plan = lambda: placement.plan(BEST, Executor(), public=True)  # noqa: E731

    def known(third_s):
        directory(node(PC, liar), node(OTHER, BIG), dict(node(THIRD, SMALL), known_s=third_s))
    known(0)
    assert plan() == [at(PC, liar), at(OTHER, BIG), at(THIRD, SMALL)]    # tous neufs : la taille
    known(500)
    assert plan()[0] == at(PC, liar)                    # moins de 10 min : pas plus ancienne
    known(1300)                                          # connue du compagnon depuis 20 min
    assert plan() == [at(THIRD, SMALL), at(PC, liar), at(OTHER, BIG)]
    seen(OTHER, 30)                                      # vues ici il y a 30 h et 40 h : à un
    seen(PC, 40)                                         # facteur deux près, la taille
    assert plan() == [at(PC, liar), at(OTHER, BIG), at(THIRD, SMALL)]
    seen(OTHER, 100)                                     # deux fois plus ancienne : devant
    assert plan() == [at(OTHER, BIG), at(PC, liar), at(THIRD, SMALL)]
    reputation.note_served(THIRD, 200, 5.0)              # a déjà répondu ici sans faute : devant
    assert plan()[0] == at(THIRD, SMALL)
    assert [c.known for c in placement.best_public()][0] >= 1300
    # Une machine établie rend son modèle contrôlable : elle répond, sans repli
    establish(PC, models=(liar,))
    assert plan() == [at(PC, liar)]
    # Le repli lui-même la range d'abord (défense en profondeur, si rien n'était contrôlable)
    monkeypatch.setattr(placement, "_controllable", lambda nodes: set())
    assert plan() == [at(PC, liar), at(THIRD, SMALL), at(OTHER, BIG)]
    assert companion.requests == []


def test_automatic_public_is_refused_when_i_have_a_model(monkeypatch, companion, opened):
    """Relecture du 28/10/2026 : « auto:* » envoyé par une autre application, alors que cette
    machine (ou une de mes machines) a un modèle, prenait le plus gros modèle du réseau public
    sans l'accord public_bigger_model. Refusé, avec la raison (avant celle des réglages), même
    avec cet accord ; rien ne part. Sans aucun modèle : comme avant."""
    from aiodos.i18n import catalog
    nothing_here(monkeypatch)
    directory(node(PC, BIG), checkable(BIG))
    own = ("« Automatique · réseau public » ne sert que si ni cette machine ni tes machines "
           "n'ont de modèle de conversation")
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        monkeypatch.setattr(ollama, "list_models",               # un modèle ici
                            lambda max_age=10: [{"name": SMALL, "size": 6}])
        monkeypatch.setattr(ollama, "capabilities_many", lambda n: {m: ["completion"] for m in n})
        assert refused(ui).startswith(own)
        settings.set_value("public_bigger_model", True)
        assert refused(ui).startswith(own)
        settings.set_value("use_public", False)
        assert refused(ui).startswith(own)
        settings.set_value("use_public", True)
        nothing_here(monkeypatch)                                # chez une de mes machines
        peers.save("fixe", "PC fixe", b"k" * 32, "127.0.0.1", 1)
        peers.set_profile("fixe", {"lends_models": True, "ollama": {"models": [
            {"name": MID, "caps": ["completion"], "fits_vram": True, "loaded": False}]}})
        assert refused(ui).startswith(own)
        assert companion.requests == [] and conversations_count() == 0
        db.execute("DELETE FROM peers")                          # plus aucun modèle : permis
        assert routes(ask(ui, "Bonjour", model=BEST)) == [at(PC, BIG)]
    assert len(companion.requests) == 1
    assert catalog.lookup("en", "public_chat.closed_own_models").startswith(
        "“Automatic · public network” is only for when neither this machine nor your machines")


def test_no_public_machine_a_clear_message(monkeypatch, companion, opened):
    nothing_here(monkeypatch)
    directory(node(PC, SMALL, lends=False), node(OTHER, BIG, free=0))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        assert refused(ui) == NO_PUBLIC
        directory()
        assert refused(ui) == NO_PUBLIC
        directory(node(PC, SMALL))
        companion.running = False                                # compagnon arrêté
        assert refused(ui) == NO_PUBLIC
    assert companion.requests == [] and conversations_count() == 0


def test_the_first_minute_says_searching(monkeypatch, companion, opened):
    """Essai réel du 28/10 : la première minute, le téléphone sans modèle disait « aucune
    machine du réseau public ne peut répondre » avant même d'avoir lu l'annuaire. Tant que le
    réseau n'est pas rejoint, ou qu'aucun nœud n'a été vu depuis, et depuis moins de
    public.SEARCH_S secondes : « searching », que l'interface dit « Recherche des machines du
    réseau public… »."""
    from aiodos.i18n import t
    from aiodos.network import public
    nothing_here(monkeypatch)
    entry = public_consent.entry
    monkeypatch.setitem(public._latest, "since", time.monotonic())   # le nœud vient de démarrer
    assert entry()["ready"] is False and entry()["searching"] is True
    directory()                                    # rejoint, aucun nœud vu encore
    assert entry()["searching"] is True
    monkeypatch.setattr(public, "SEARCH_S", 0)     # la première minute passée : « aucune »
    assert entry()["searching"] is False
    monkeypatch.setattr(public, "SEARCH_S", 90)
    directory(node(PC, SMALL, free=0))             # un nœud vu, occupé : on ne cherche plus
    assert entry()["ready"] is False and entry()["searching"] is False
    public._remember(None)                         # compagnon relancé, réseau rejoint à nouveau
    directory()
    assert entry()["searching"] is True
    directory(node(PC, SMALL), checkable(SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        found = ui.get("/api/models", headers=AUTH).json()
    assert found[0]["ready"] is True and found[0]["searching"] is False
    assert t("chat.public_searching") == "Recherche des machines du réseau public…"
    assert t("chat.no_model_searching_phone").endswith("Recherche des machines du réseau public…")


def test_without_a_model_no_knowledge_search_and_no_repeated_warning(
        monkeypatch, companion, opened, caplog, fake_embed):
    """Essai réel du 28/10 : sans aucun modèle, deux avertissements « Recherche dans les
    connaissances impossible » à chaque message. Rien d'indexé : pas de recherche du tout ;
    une recherche qui échoue : dit une fois, puis plus jusqu'à la prochaine réussite."""
    from aiodos.chat import pipeline
    from aiodos.knowledge import store
    nothing_here(monkeypatch)
    calls = []

    def failing(texts, model=None, timeout=None):
        calls.append(texts)
        raise RuntimeError("aucun modèle d'embedding")
    monkeypatch.setattr(ollama, "embed", failing)
    monkeypatch.setitem(pipeline._search_failed, "told", False)
    directory(node(PC, SMALL), checkable(SMALL))
    warned = lambda: [r for r in caplog.records  # noqa: E731
                      if "connaissances impossible" in r.getMessage()
                      and r.levelno >= logging.WARNING]
    with caplog.at_level(logging.DEBUG), TestClient(create_app(TOKEN, {"testserver"})) as ui:
        store.ensure_defaults()
        for text in ("Bonjour", "Encore"):
            assert ask(ui, text, model=BEST, libraries=True)[-1]["error"] is None
        assert calls == [] and warned() == []
        _library("maison", "fusée lunaire notes maison", fake_embed)    # quelque chose d'indexé
        for _ in range(3):
            assert pipeline._knowledge_context("fusée", True, False, []) == []
        assert calls.count(["fusée"]) == 3 and len(warned()) == 1
        monkeypatch.setattr(ollama, "embed", fake_embed)                # réussie : oublié
        pipeline._knowledge_context("fusée lunaire", True, False, [])
        monkeypatch.setattr(ollama, "embed", failing)
        pipeline._knowledge_context("fusée", True, False, [])
        assert len(warned()) == 2


def test_documents_card_and_first_note_as_elsewhere(monkeypatch, companion, opened, fake_embed):
    """La carte des documents : sans accord, rien ne part, et rien d'autre ne répond ; avec,
    les passages utiles partent, et la note de la première fois s'affiche."""
    nothing_here(monkeypatch)
    from aiodos.knowledge import store
    settings.set_value("rag_min_score", 0.1)
    directory(node(PC, SMALL), node(OTHER, BIG), checkable(SMALL, BIG))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        # Après le démarrage : sinon, le modèle d'embedding des vecteurs n'étant pas noté, le
        # nœud les retire et les refait en fond (reindex.schedule), pendant la question
        store.ensure_defaults()
        _library("maison", "fusée lunaire notes maison", fake_embed)
        events = ask(ui, "fusée lunaire", model=BEST, libraries=True)
        assert notices(events) == ["public_documents"]
        assert routes(events) == [at(OTHER, BIG), at(PC, SMALL)]
        assert "tes documents" in events[-1]["error"] and companion.requests == []
        settings.set_value("public_documents", True)
        events = ask(ui, "fusée lunaire", model=BEST, libraries=True)
        assert notices(events) == ["public_first"] and routes(events) == [at(OTHER, BIG)]
        assert "notes maison" in " ".join(
            m["content"] for m in companion.requests[-1]["body"]["messages"])


# --------------------------------------------- le titre, et ce qui se fait en coulisse
def test_the_title_never_goes_to_a_stranger(monkeypatch, companion, opened):
    """Le titre est le début du premier message, sans modèle ; la seule demande partie chez
    l'inconnu est la réponse. Enrichir la description d'une image, retoucher : sans modèle ici,
    la demande telle quelle, jamais chez un inconnu."""
    called = nothing_here(monkeypatch)
    directory(node(PC, SMALL), checkable(SMALL))
    text = "Quel temps fera-t-il demain à Lyon, et faut-il prendre un parapluie pour la sortie ?"
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, text, model=BEST)
    assert events[0]["title"] == text[:60] + "…"
    assert conversations.get(events[0]["conversation_id"])["title"] == text[:60] + "…"
    assert len(companion.requests) == 1
    assert companion.requests[0]["body"]["messages"][-1] == {"role": "user", "content": text}
    assert enrich.enrich("un chat roux", "image", BEST) == "un chat roux"
    assert retouches.rewrite("un chat roux", "en hiver", BEST) == "un chat roux, en hiver"
    assert len(companion.requests) == 1 and called == []


# ------------------------------------------------------------------ pièces jointes
def test_an_attachment_gives_a_clear_message(monkeypatch, companion, opened):
    """Une pièce jointe ne part jamais chez un inconnu : sans modèle ici, le refus le dit (et
    la pièce jointe reste à envoyer) ; une conversation qui en a déjà une, de même."""
    nothing_here(monkeypatch)
    directory(node(PC, SMALL))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        sent = ui.post("/api/attachments", headers={**AUTH, "Content-Type": "text/plain",
                                                    "X-File-Name": "notes.txt"},
                       content="fusée lunaire".encode())
        assert sent.status_code == 200, sent.text
        att = sent.json()["id"]
        message = refused(ui, "Résume", attachments=[att])
        assert message.startswith("Les pièces jointes ne partent jamais chez un inconnu")
        assert db.query_one("SELECT message_id FROM attachments WHERE id=?", (att,))[0] is None

        conv_id = conversations.create(model=BEST)
        asked = conversations.add_message(conv_id, "user", "Regarde")
        db.execute("UPDATE attachments SET message_id=? WHERE id=?", (asked, att))
        assert refused(ui, "Et alors ?", conversation_id=conv_id).startswith(
            "Cette conversation a des pièces jointes")
    assert companion.requests == []


# ------------------------------------------------------------ l'interface (Node.js)
@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_interface_names_automatic_public(tmp_path):
    """models.js : le libellé du choix, la réponse marquée, et rien de proposé à une
    conversation « Sur mes machines seulement »."""
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    for name in ("state.js", "dom.js"):
        (js / name).write_bytes((paths.STATIC_DIR / "js" / name).read_bytes())
    (js / "views" / "models.js").write_bytes(
        (paths.STATIC_DIR / "js" / "views" / "models.js").read_bytes())
    prelude = i18n_js.install(js, module="./js/i18n.js")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    entry = {"id": BEST, "label": "x", "group": "public", "ready": True, "models": [BIG]}
    (tmp_path / "essai.mjs").write_text(prelude + f"""
import {{ state }} from './js/state.js';
import * as models from './js/views/models.js';
state.models = [{json.dumps(entry)}];
console.log(JSON.stringify({{
  id: models.PUBLIC_AUTO, label: models.modelLabel(models.PUBLIC_AUTO),
  answered: models.modelLabel({json.dumps(at(PC, BIG))}),
  open: models.publicChoice(false)?.ready ?? null, mine: models.publicChoice(true),
  kept: models.asDefault(models.PUBLIC_AUTO), offered: models.catalog().length,
}}));""", encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "essai.mjs")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr[:400]
    assert json.loads(done.stdout) == {
        "id": BEST, "label": "Automatique · réseau public",
        "answered": "Répondu par qwen3.5:9b, sur une machine du réseau public",
        "open": True, "mine": None, "kept": BEST, "offered": 0}
