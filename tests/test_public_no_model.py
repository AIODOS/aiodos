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
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from aiodos import db, paths, settings
from aiodos.api.server import create_app
from aiodos.chat import conversations, enrich, public_consent, retouches
from aiodos.network import peers, placement
from aiodos.services import ollama
from tests import i18n_js
from tests.test_public_chat import (AUTH, BIG, OTHER, PC, SMALL, THIRD, TOKEN, Executor,
                                    _library, ask, at, directory, local_models, node, notices,
                                    routes)
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
    directory(node(PC, SMALL), node(OTHER, BIG, "embeddinggemma:latest"))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        assert ui.get("/api/models", headers=AUTH).json() == [
            {"id": BEST, "label": "Automatique · réseau public", "group": "public",
             "ready": True, "models": [BIG, SMALL]}]
        directory(node(PC, SMALL, free=0))                       # plus de place nulle part
        assert ui.get("/api/models", headers=AUTH).json() == [
            {"id": BEST, "label": "Automatique · réseau public", "group": "public",
             "ready": False, "models": []}]
        directory(node(PC, SMALL))
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
              node(FIFTH, BIG, free=0), node(SIXTH, BIG, lends=False))
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


def test_documents_card_and_first_note_as_elsewhere(monkeypatch, companion, opened, fake_embed):
    """La carte des documents : sans accord, rien ne part, et rien d'autre ne répond ; avec,
    les passages utiles partent, et la note de la première fois s'affiche."""
    nothing_here(monkeypatch)
    from aiodos.knowledge import store
    settings.set_value("rag_min_score", 0.1)
    directory(node(PC, SMALL), node(OTHER, BIG))
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
    directory(node(PC, SMALL))
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
