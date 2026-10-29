"""Le menu des modèles et « Automatique » pour le texte (docs/conception/modeles-auto.md,
étape 2 ; chat/model_menu.py, GET /api/models/menu ; network/placement.py, BEST).

« Automatique » (« auto:best ») prend le modèle de conversation le plus puissant disponible (le
plus grand nombre de paramètres lu dans le nom, sinon estimé d'après la taille), ici, chez mes
machines, chez mes amis s'ils sont acceptés ; ses machines se choisissent comme pour
« auto:<modèle> », le réseau public compris si la conversation le permet (même modèle, modèle
plus gros accepté, règle des petits réseaux), jamais pour « Sur mes machines seulement » ; puis,
en repli, les modèles plus modestes. Sans modèle à moi : le meilleur du réseau public. C'est le
choix par défaut d'une nouvelle conversation ; les autres identifiants ne changent pas.

Le menu : pour chaque genre, ce que prendrait « Automatique », les modèles du plus puissant au
plus modeste avec où ils tournent, ceux à télécharger ; images et vidéos par imaging/ranking.py
s'il est là, sinon d'après le catalogue, les recettes de ComfyUI ajoutées ; l'audio vide.

Le compagnon simulé et l'annuaire public sont ceux de test_public_chat.py ; cette machine et mes
machines sont simulées comme dans test_placement.py."""
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from aiodos import db, paths, settings
from aiodos.api.routes import images as image_routes
from aiodos.api.server import create_app
from aiodos.chat import comfy_creations, model_menu, pipeline, public_consent
from aiodos.gateway import answer as gateway_answer
from aiodos.gateway import model_card
from aiodos.network import peers, placement, remote_models
from aiodos.node import speed
from aiodos.services import ollama
from tests import i18n_js
from tests.test_public_chat import (AUTH, BIG, PC, SMALL, TOKEN, Executor, ask, at, checkable,
                                    directory, measured, node, notices, routes)
from tests.test_public_chat import companion, opened  # noqa: F401 (fixtures)

BEST = placement.BEST
HUGE = "qwen3.6:27b"
PUBLIC_ONLY = "gpt-oss:20b"
SIZES = {SMALL: 1_000_000_000, BIG: 6_600_000_000, HUGE: 17_000_000_000,
         "llama3:latest": 4_700_000_000}
KEYS = {"id", "label", "rank", "kind", "where", "ready", "download", "license", "restricted",
        "notice"}
NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def clean():
    placement.forget()
    yield
    placement.forget()


def here(monkeypatch, *models):
    """Cette machine : ces modèles (profil et moteur), qui répondent « Ici (<modèle>). »."""
    profile = {"name": "Portable", "gpus": [{"utilization": 0}], "ollama": {"models": [
        {"name": m, "caps": ["completion"], "fits_vram": True, "loaded": False,
         "size": SIZES.get(m, 1)} for m in models]}}
    monkeypatch.setattr(placement, "_local_profile", lambda: profile)
    monkeypatch.setattr(ollama, "list_models",
                        lambda max_age=10: [{"name": m, "size": SIZES.get(m, 1)} for m in models])
    monkeypatch.setattr(ollama, "capabilities_many", lambda names: {n: ["completion"] for n in names})

    def answer(model, messages, think=None, stop=None, **kw):
        yield "content", f"Ici ({model})."
    monkeypatch.setattr(ollama, "chat_stream", answer)


def machine(node_id, name, *models, trust="mine", images=()):
    """Une machine appairée en ligne qui prête ces modèles (et calcule ces modèles d'images)."""
    peers.save(node_id, name, b"k" * 32, "127.0.0.1", 1, trust=trust)
    profile = {"lends_models": True, "gpus": [{"utilization": 0}], "ollama": {"models": [
        {"name": m, "caps": ["completion"], "fits_vram": True, "loaded": False,
         "size": SIZES.get(m, 1)} for m in models]}}
    if images:
        profile["images"] = {"engine": "1.0", "shares": True, "models": list(images)}
    peers.set_profile(node_id, profile)


def at_peer(node_id, model):
    return remote_models.model_id(node_id, model)


def menu(ui, mine_only=False):
    response = ui.get(f"/api/models/menu?mine_only={str(mine_only).lower()}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------- « Automatique » (texte)
def test_automatic_takes_the_most_powerful_model_then_the_smaller_ones(monkeypatch):
    """Le plus grand nombre de paramètres d'abord, où qu'il soit chez moi ; un nom qui n'en dit
    rien : sa taille ; les plus modestes en repli. Une machine occupée garde son modèle devant
    (elle répondra bientôt) ; une machine partie, ou celle d'un ami non accepté, ne compte pas."""
    here(monkeypatch, SMALL)
    assert placement.own_ranked() == [SMALL]
    assert placement.plan(BEST, Executor()) == [SMALL]
    machine("fixe", "PC fixe", BIG, SMALL)
    assert placement.own_ranked() == [BIG, SMALL]
    assert placement.plan(BEST, Executor()) == [at_peer("fixe", BIG), SMALL, at_peer("fixe", SMALL)]
    placement.note_load("fixe", {"chats": 1, "units": 0})            # occupée : toujours devant
    assert placement.plan(BEST, Executor())[0] == at_peer("fixe", BIG)
    machine("vieux", "Vieux PC", "llama3:latest")                   # 4,7 Go : ~7 milliards
    assert placement.own_ranked() == [BIG, "llama3:latest", SMALL]
    machine("ami", "PC de Léa", HUGE, trust="friend")
    assert placement.best_name() == BIG                              # un ami : pas d'office
    settings.set_value("auto_route_friends", True)                   # accepté dans les Réglages
    assert placement.best_name() == HUGE
    assert placement.plan(BEST, Executor())[0] == at_peer("ami", HUGE)
    db.execute("UPDATE peers SET last_seen=0")                      # toutes parties
    assert placement.plan(BEST, Executor()) == [SMALL]
    here(monkeypatch)                                                # plus rien du tout
    with pytest.raises(placement.NoMachine, match="Aucun modèle de conversation"):
        placement.plan(BEST, Executor())


def test_automatic_prefers_a_model_that_fits_in_a_graphics_card(monkeypatch):
    """« Une machine qui peut le faire tourner » (l'auteur, 29/10/2026) : un modèle qui ne tient
    dans la mémoire vidéo d'aucune machine passe après ceux qui tiennent, même plus puissant."""
    here(monkeypatch, SMALL)
    machine("fixe", "PC fixe", BIG)
    peers.set_profile("fixe", {"lends_models": True, "gpus": [{"utilization": 0}],
                               "ollama": {"models": [                # trop gros pour sa carte
                                   {"name": BIG, "caps": ["completion"], "fits_vram": False,
                                    "loaded": False, "size": SIZES.get(BIG, 1)}]}})
    assert placement.own_ranked() == [SMALL, BIG]
    assert placement.best_name() == SMALL


def test_the_existing_identifiers_do_not_change(monkeypatch):
    """« auto:<modèle> », « auto:* », un modèle précis, une machine, un modèle réparti : comme
    avant ; « auto:best » n'est pas un modèle qu'on chercherait par son nom."""
    here(monkeypatch, SMALL)
    machine("fixe", "PC fixe", BIG, SMALL)
    assert placement.plan(placement.auto_id(SMALL), Executor()) == [SMALL, at_peer("fixe", SMALL)]
    assert placement.plan(placement.auto_id(BIG), Executor()) == [at_peer("fixe", BIG)]
    for precise in (SMALL, at_peer("fixe", BIG), "reparti:qwen3.6:35b"):
        assert placement.plan(precise, Executor()) == [precise]
    with pytest.raises(placement.NoMachine):                         # sans le réseau public
        placement.plan(placement.PUBLIC_BEST, Executor())
    assert placement.parse(BEST) == "best" and placement.reserved(BEST)
    assert BEST not in {m["id"] for m in pipeline.available_models()}   # pas dans GET /api/models


def test_automatic_is_the_default_and_apps_see_the_model_it_takes(monkeypatch):
    """Une nouvelle conversation : « Automatique » ; la passerelle des applis dit le modèle
    qu'il prend (« default »), sans jamais le réseau public."""
    assert settings.DEFAULTS["default_model"] == BEST and settings.get("default_model") == BEST
    assert settings.get("default_image_model") == "" and settings.get("default_video_model") == ""
    here(monkeypatch, SMALL)
    machine("fixe", "PC fixe", BIG)
    assert model_card.default_of("chat") == BIG
    assert gateway_answer.plan(BEST, Executor()) == [at_peer("fixe", BIG), SMALL]
    here(monkeypatch)
    db.execute("DELETE FROM peers")
    assert model_card.default_of("chat") is None


def test_automatic_with_the_public_network(monkeypatch, companion, opened):
    """Le modèle plus gros d'une machine du réseau public se compare au plus puissant des miens
    (la carte d'accord le nomme) ; accepté, il passe devant ; « Sur mes machines seulement » :
    jamais, sans carte. La conversation garde « Automatique », la réponse dit qui l'a calculée."""
    here(monkeypatch, SMALL)
    directory(node(PC, BIG, tps=60), checkable(BIG))
    reach = public_consent.reach(BEST, None, False)
    assert reach.public and not reach.bigger and reach.ask_bigger == BIG
    assert placement.plan(BEST, Executor(), True, False) == [SMALL]
    assert placement.plan(BEST, Executor(), True, True) == [at(PC, BIG), SMALL]
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour", model=BEST)
        assert notices(events) == ["public_bigger"] and routes(events) == [SMALL]
        conv_id = events[0]["conversation_id"]
        settings.set_value("public_bigger_model", True)
        events = ask(ui, "Encore", model=BEST, conversation_id=conv_id)
        assert routes(events) == [at(PC, BIG)] and "public_bigger" not in notices(events)
        conv = ui.get(f"/api/conversations/{conv_id}", headers=AUTH).json()
        assert conv["model"] == BEST and conv["messages"][-1]["model"] == at(PC, BIG)
        sent = len(companion.requests)
        events = ask(ui, "Chez moi", model=BEST, mine_only=True)       # jamais chez un inconnu
        assert routes(events) == [SMALL] and notices(events) == []
        assert len(companion.requests) == sent


def test_automatic_without_any_model_of_mine(monkeypatch, companion, opened):
    """Rien ici ni sur mes machines : le meilleur du réseau public, sans carte (rien à quoi
    comparer) ; « Sur mes machines seulement » : refusé, avec la raison."""
    here(monkeypatch)
    directory(node(PC, SMALL), node("b2" * 32, BIG), checkable(SMALL, BIG))
    assert public_consent.reach(BEST, None, False).ask_bigger is None
    assert placement.plan(BEST, Executor(), public=True) == [at("b2" * 32, BIG), at(PC, SMALL)]
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour", model=BEST)
        assert routes(events) == [at("b2" * 32, BIG)] and "public_bigger" not in notices(events)
        refused = ui.post("/api/chat", headers=AUTH, json={"text": "x", "model": BEST,
                                                           "mine_only": True})
        assert refused.status_code == 400 and "Sur mes machines seulement" in refused.json()["detail"]


def test_automatic_keeps_the_small_network_rule(monkeypatch, companion, opened):
    """Aucune machine établie ne prête ce modèle : une machine publique mesurée ici deux fois
    plus rapide passe devant, marquée « non vérifiable pour l'instant », comme pour
    « auto:<modèle> » ; la marque suit la réponse."""
    here(monkeypatch, SMALL)
    directory(node(PC, SMALL, tps=50))
    speed.note(100, 10)
    measured(PC, 50, established=False)
    marked = set()
    assert placement.plan(BEST, Executor(), True, False, marked) == [at(PC, SMALL), SMALL]
    assert marked == {at(PC, SMALL)}
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour", model=BEST)
        route = next(e for e in events if e["type"] == "route")
        assert route["model"] == at(PC, SMALL) and route["unverified"] is True
        conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
        assert conv["messages"][-1]["unverified"] is True


# ------------------------------------------------------------------------ le menu
def test_the_menu_of_text_models(monkeypatch, companion, opened):
    """Quatre genres ; le texte : « Automatique » et ce qu'il prend, chaque modèle une fois, du
    plus puissant au plus modeste, avec où il tourne et l'identifiant à envoyer (un modèle
    qu'un ami seul prête : sa machine), ceux du réseau public qu'une autre machine peut
    contrôler, puis ceux à télécharger ; « Sur mes machines seulement » : rien du réseau
    public. L'audio : rien encore."""
    here(monkeypatch, SMALL)
    machine("fixe", "PC fixe", BIG)
    machine("ami", "Léa", HUGE, trust="friend")
    directory(node(PC, PUBLIC_ONLY, BIG), checkable(PUBLIC_ONLY))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        found = menu(ui)
        assert set(found) == {"text", "image", "video", "audio"}
        assert found["audio"] == {"auto": {"model": None, "label": None, "where": None,
                                           "machine": None}, "suggest": None, "models": []}
        text = found["text"]
        assert text["auto"] == {"model": BIG, "label": BIG, "where": "mine", "machine": "PC fixe"}
        ready = [m for m in text["models"] if m["ready"]]
        # BIG, que prête une de mes machines, est aussi contrôlable : le réseau public l'a
        assert [(m["id"], [w["circle"] for w in m["where"]]) for m in ready] == [
            (at_peer("ami", HUGE), ["friend"]), (placement.auto_id(PUBLIC_ONLY), ["public"]),
            (placement.auto_id(BIG), ["mine", "public"]), (placement.auto_id(SMALL), ["here"])]
        assert ready[0]["where"][0]["name"] == "Léa"
        later = [m for m in text["models"] if not m["ready"]]
        assert later and all(m["download"]["size"] > 0 and m["where"] == [] for m in later)
        assert SMALL not in {m["id"] for m in later}                  # déjà là : pas à télécharger
        assert [m["rank"] for m in text["models"]] == list(range(1, len(text["models"]) + 1))
        assert all(KEYS <= set(m) for kind in ("text", "image", "video")
                   for m in found[kind]["models"])
        mine = menu(ui, mine_only=True)["text"]
        assert placement.auto_id(PUBLIC_ONLY) not in {m["id"] for m in mine["models"]}


def test_the_menu_without_any_model_says_what_automatic_would_take(monkeypatch, companion,
                                                                    opened):
    """Rien ici ni sur mes machines : « Automatique » prend le meilleur du réseau public, et le
    dit ; rien n'est noté pour autant (une machine proposée ne l'est qu'à l'envoi)."""
    here(monkeypatch)
    directory(node(PC, BIG), checkable(BIG))
    noted = []
    monkeypatch.setattr(placement.public_refusals, "offered", lambda *a: noted.append(a))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        assert menu(ui)["text"]["auto"] == {"model": BIG, "label": BIG, "where": "public",
                                            "machine": "réseau public"}
        assert menu(ui, mine_only=True)["text"]["auto"]["model"] is None
    assert noted == []


def test_images_and_videos_from_the_catalogue_until_the_ranking_arrives(monkeypatch):
    """Sans imaging/ranking.py : les modèles de GET /api/images/models, ceux à licence
    restrictive en dernier (ce que le nœud choisit sans modèle demandé), à télécharger tant que
    rien ne peut les calculer ; où ils peuvent calculer (mes machines…), « Automatique » sur le
    premier qui le peut ; les recettes de ComfyUI ensuite."""
    monkeypatch.setattr(model_menu, "_ranking", lambda: None)
    monkeypatch.setattr(comfy_creations, "choices", lambda kind=None: [])
    listed = image_routes._models()["models"]
    for kind in ("image", "video"):
        expected = sorted((m for m in listed if m["kind"] == kind), key=lambda m: bool(m["notice"]))
        found = model_menu.media(kind)
        assert [m["id"] for m in found["models"]] == [m["id"] for m in expected]
        assert [m["restricted"] for m in found["models"]] == [bool(m["notice"]) for m in expected]
        assert all(not m["ready"] and m["download"]["size"] > 0 for m in found["models"])
        assert found["auto"]["model"] is None and found["suggest"] is None
    first = model_menu.media("image")["models"][0]
    machine("fixe", "PC fixe", images=[first["id"]])               # une de mes machines l'a
    image = model_menu.media("image")
    assert image["models"][0]["ready"] and image["models"][0]["where"] == [
        {"circle": "mine", "name": "PC fixe"}]
    assert image["auto"] == {"model": first["id"], "label": first["label"], "where": "mine",
                             "machine": "PC fixe"}
    monkeypatch.setattr(comfy_creations, "choices", lambda kind=None: [
        {"id": "comfy:recette:portrait", "label": "Portrait (ComfyUI)", "kind": "image",
         "here": True, "helpers": []}])
    image = model_menu.media("image")
    recipe = image["models"][-1]
    assert recipe["id"] == "comfy:recette:portrait" and recipe["engine"] == "comfyui"
    assert recipe["rank"] == len(image["models"]) and recipe["ready"]


def test_the_ranking_is_used_when_it_exists(monkeypatch):
    """imaging/ranking.menu, quand il est là, donne le menu des images et des vidéos tel quel ;
    « Sur mes machines seulement » en retire le réseau public, « Automatique » compris."""
    ranked = {"auto": {"model": "flux3", "label": "FLUX 3", "where": "public",
                       "machine": "réseau public"},
              "suggest": {"model": "flux3", "label": "FLUX 3", "size": 9, "vram": 8},
              "models": [
                  {"id": "flux3", "label": "FLUX 3", "rank": 1, "kind": "image",
                   "where": [{"circle": "public", "name": "réseau public"}], "ready": True,
                   "download": {"size": 9, "vram": 8, "fits_here": True},
                   "license": "FLUX", "restricted": True, "notice": "lis-la"},
                  {"id": "z-image-turbo", "label": "Z-Image Turbo", "rank": 2, "kind": "image",
                   "where": [{"circle": "here", "name": "ici"}], "ready": True,
                   "download": None, "license": "Apache-2.0", "restricted": False,
                   "notice": None}]}
    monkeypatch.setattr(model_menu, "_ranking", lambda: lambda kind: json.loads(json.dumps(ranked)))
    monkeypatch.setattr(comfy_creations, "choices", lambda kind=None: [])
    assert model_menu.media("image") == ranked
    mine = model_menu.media("image", mine_only=True)
    assert mine["auto"]["model"] == "z-image-turbo" and mine["auto"]["where"] == "here"
    assert not mine["models"][0]["ready"] and mine["models"][0]["where"] == []
    monkeypatch.setattr(model_menu, "_ranking", lambda: lambda kind: 1 / 0)   # illisible
    assert [m["id"] for m in model_menu.media("image")["models"]] == [
        m["id"] for m in sorted((m for m in image_routes._models()["models"] if m["kind"] == "image"),
                                key=lambda m: bool(m["notice"]))]


def test_a_kind_that_cannot_be_read_stays_empty(monkeypatch):
    """Un genre illisible (moteur arrêté…) n'empêche pas les autres."""
    monkeypatch.setattr(model_menu, "text", lambda *a: 1 / 0)
    monkeypatch.setattr(comfy_creations, "choices", lambda kind=None: [])
    found = model_menu.menu()
    assert found["text"] == model_menu.empty() and found["image"]["models"]


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_interface_names_automatic(tmp_path):
    """models.js : « Automatique » se nomme ainsi, et reste le choix par défaut tel quel."""
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    for name in ("state.js", "dom.js"):
        (js / name).write_bytes((paths.STATIC_DIR / "js" / name).read_bytes())
    (js / "views" / "models.js").write_bytes(
        (paths.STATIC_DIR / "js" / "views" / "models.js").read_bytes())
    prelude = i18n_js.install(js, module="./js/i18n.js")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "essai.mjs").write_text(prelude + """
import * as models from './js/views/models.js';
console.log(JSON.stringify({ id: models.BEST_AUTO, label: models.modelLabel(models.BEST_AUTO),
  kept: models.asDefault(models.BEST_AUTO), name: models.modelName('auto:qwen3.5:9b') }));""",
                                          encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "essai.mjs")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr[:400]
    assert json.loads(done.stdout) == {"id": BEST, "label": "Automatique", "kept": BEST,
                                       "name": "qwen3.5:9b"}


def test_the_old_default_model_gives_way_to_automatic(tmp_path, monkeypatch):
    """Migration 32 : le choix par défaut d'avant (enregistré à chaque changement de modèle dans
    une conversation) est oublié une fois ; « Automatique » le remplace."""
    settings.set_value("default_model", "auto:mistral:7b")
    assert settings.get("default_model") == "auto:mistral:7b"
    db.connect().executescript(db.MIGRATIONS[32])
    assert settings.get("default_model") == BEST
