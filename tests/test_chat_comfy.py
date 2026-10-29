"""Créer avec ComfyUI dans la conversation (chat/comfy_creations.py, chat/creations.py), contre
le faux ComfyUI des essais de jobs/kinds/comfy_create.py : les recettes prêtes proposées comme
des modèles du mode « Créer » (formats, durées), une création qui devient un travail
comfy_create montré dans la réponse, ComfyUI pris quand aucun modèle de sd.cpp ne peut
calculer, une variante qui reste dans ComfyUI, et ce qui est refusé."""
import pytest
from fastapi.testclient import TestClient

from aiodos.api.server import create_app
from aiodos.chat import comfy_creations, conversations, creations
from aiodos.engines import catalogue, comfyui
from aiodos.jobs import queue
from aiodos.jobs.kinds import comfy_create, engine
from tests import comfy_stub
from tests.test_comfy_create import SDXL, WAN, comfy  # noqa: F401  (fixture)
from tests.test_images import wait_for

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}
IMAGE = f"comfyui/{SDXL}"
VIDEO = f"comfyui/{WAN}"


@pytest.fixture
def client(comfy):  # noqa: F811
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


def _no_checkpoint(monkeypatch):
    monkeypatch.setitem(comfy_stub.MODELS, "checkpoints", [])
    comfyui.forget_inventory()
    engine.forget_here()


# ------------------------------------------------------------------ les choix
def test_ready_recipes_are_offered_as_models_with_their_formats(client, monkeypatch):
    listed = {m["id"]: m for m in client.get("/api/chat/create/comfyui", headers=AUTH)
              .json()["models"]}
    assert set(listed) == {IMAGE, VIDEO}
    image, video = listed[IMAGE], listed[VIDEO]
    assert (image["kind"], image["engine"], image["here"], image["public"]) == \
        ("image", "comfyui", True, False)
    assert image["label"] == "SDXL 1.0 · image · ComfyUI"
    # Un carré par défaut : aussi un portrait et un paysage de même surface (9:7)
    assert [s[:2] for s in image["sizes"]] == [[1024, 1024], [896, 1152], [1152, 896]]
    assert image["sizes"][1][2].startswith("Portrait") and image["durations"] == []
    # Un paysage par défaut : son inverse, puis un carré de même surface, sur le pas de 16
    assert [s[:2] for s in video["sizes"]] == [[832, 480], [480, 832], [624, 624]]
    assert video["durations"] == [[17, "1 s"], [33, "2 s"], [49, "3 s"], [81, "5 s"]]
    assert video["default_frames"] == 33
    # Sans le modèle de la recette, ni ici ni chez mes machines : elle n'est plus proposée
    _no_checkpoint(monkeypatch)
    assert [m["id"] for m in comfy_creations.choices()] == [VIDEO]
    assert comfy_creations.choices("image") == []


def test_a_bounded_value_stays_on_the_step_of_its_parameter():
    spec = {"min": 512, "max": 1536, "step": 64}
    assert comfy_creations._bounded(spec, 1161.4) == 1152
    assert comfy_creations._bounded(spec, 4000) == 1536 and comfy_creations._bounded(spec, 3) == 512
    assert comfy_creations._bounded({"min": 5, "max": 79, "step": 4}, 200) == 77


# --------------------------------------------------------- dans la conversation
def test_a_recipe_creates_in_the_conversation(client):
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "un phare sous l'orage", "kind": "image", "model": IMAGE,
        "prompt": "un phare sous l'orage", "width": 896, "height": 1152, "count": 2,
        "seed": 41})
    assert reply.status_code == 200, reply.text
    made = reply.json()
    media = made["media"]
    assert (media["kind"], media["model"], media["engine"], media["count"], media["seed"]) == \
        ("image", IMAGE, "comfyui", 2, 41)
    assert media["label"] == "SDXL 1.0 · image · ComfyUI"
    assert (media["width"], media["height"]) == (896, 1152)
    job = queue.job_summary(made["job_id"])
    assert job["kind"] == comfy_create.KIND and job["reach"] == "here,mine"
    assert job["params"]["conversation_id"] == made["conversation_id"]
    assert job["params"]["creation"]["batch"] == media["batch"]
    assert creations.same_job(job, media)
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    conv = client.get(f"/api/conversations/{made['conversation_id']}", headers=AUTH).json()
    answer = conv["messages"][-1]["media"]
    assert len(answer["images"]) == 2 and answer["job"]["status"] == "done"
    assert all(img["model"] == IMAGE for img in answer["images"])
    # Le modèle de conversation sait avec quoi elles ont été créées
    assert "SDXL 1.0 · image · ComfyUI" in conversations.history(made["conversation_id"])[-1]["content"]
    # La galerie mène à la conversation
    assert {r["conversation_id"] for r in client.get(
        f"/api/images?batch={media['batch']}", headers=AUTH).json()} == {made["conversation_id"]}


def test_a_video_recipe_takes_the_duration_asked(client):
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Fais une vidéo de 3 secondes d'un renard dans la neige", "model": VIDEO,
        "kind": "video"})
    assert reply.status_code == 200, reply.text
    media = reply.json()["media"]
    assert (media["kind"], media["frames"], media["engine"]) == ("video", 49, "comfyui")
    job = queue.job_summary(reply.json()["job_id"])
    assert job["params"]["creation"]["frames"] == 49
    assert "un renard dans la neige" in job["params"]["creation"]["prompt"]


def test_comfyui_is_taken_when_no_model_of_aiodos_can_compute(client):
    pre = client.post("/api/chat/preflight", headers=AUTH, json={
        "text": "Crée une image d'un chat roux", "model": ""}).json()["create"]
    assert (pre["model"], pre["label"]) == (IMAGE, "SDXL 1.0 · image · ComfyUI")
    made = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux en format portrait", "auto": True}).json()
    assert made["media"]["model"] == IMAGE and made["media"]["auto"] is True
    assert (made["media"]["width"], made["media"]["height"]) == (896, 1152)


def test_a_variation_stays_with_the_recipe(client, monkeypatch):
    first = client.post("/api/chat/create", headers=AUTH, json={
        "text": "un chat roux", "kind": "image", "model": IMAGE, "prompt": "un chat roux"}).json()
    conv = first["conversation_id"]
    pre = client.post("/api/chat/preflight", headers=AUTH, json={
        "text": "Encore une", "model": "", "conversation_id": conv}).json()["create"]
    assert (pre["followup"], pre["model"]) == ("variation", IMAGE)
    again = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Encore une", "followup": "variation", "conversation_id": conv}).json()["media"]
    assert again["model"] == IMAGE and again["prompt"] == first["media"]["prompt"]
    assert again["batch"] != first["media"]["batch"]
    # Une retouche ne part pas de l'image précédente : la recette n'en prend pas
    wait_for(lambda: queue.job_summary(first["job_id"])["status"] == "done")
    monkeypatch.setattr(creations.retouches, "rewrite",
                        lambda prompt, text, model=None: f"{prompt}, la nuit")
    edit = creations._followed({"followup": "edit"}, "Fais-la de nuit", conv, None)
    assert edit["model"] == IMAGE and "init_path" not in edit


def test_what_is_refused(client, monkeypatch):
    # Plus de quatre vidéos : les bornes de la recette
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "un renard", "kind": "video", "model": VIDEO, "count": 8})
    assert reply.status_code == 400 and "4" in reply.json()["detail"]
    # Une recette qui ne peut plus se calculer, sans autre modèle : rien n'est ajouté
    _no_checkpoint(monkeypatch)
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "un chat roux", "kind": "image", "model": IMAGE})
    assert reply.status_code == 409, reply.text
    assert conversations.list_tree()["conversations"] == []
    with pytest.raises(comfy_creations.Unavailable) as caught:
        comfy_creations.ready(IMAGE)
    assert "SDXL 1.0" in str(caught.value)


def test_nothing_is_offered_without_comfyui(monkeypatch):
    catalogue.forget()
    assert comfy_creations.choices() == []
    assert comfy_creations.first("image") is None
    assert not comfy_creations.is_comfy("essai") and comfy_creations.is_comfy(IMAGE)


def test_a_recipe_gone_gives_way_to_one_that_can_compute(client):
    made = client.post("/api/chat/create", headers=AUTH, json={
        "text": "un chat roux", "kind": "image", "model": "comfyui/nulle/part"}).json()
    assert made["media"]["model"] == IMAGE
