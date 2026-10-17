"""Images et vidéos créées dans la conversation (chat/creations.py) : la demande se
reconnaît dans le message, part dans la file comme une demande de la vue Images, et la
réponse de l'assistant la garde (les images, le travail, la description)."""
import pytest
from fastapi.testclient import TestClient

from aiodos.api.server import create_app
from aiodos.chat import conversations, creations, pipeline
from aiodos.imaging import catalog, engine, generate
from aiodos.jobs import queue
from tests.test_images import png, ready, tiny_model, wait_for  # noqa: F401  (fixture)

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}


@pytest.mark.parametrize("text, expected", [
    ("Crée-moi une image d'un phare sous l'orage",
     {"kind": "image", "prompt": "un phare sous l'orage", "count": 1}),
    ("Crée une image d’un chat roux, style aquarelle",
     {"prompt": "un chat roux, style aquarelle"}),
    ("Génère 4 images de chats en format portrait", {"count": 4, "shape": "portrait"}),
    ("génère des images de dragons", {"prompt": "dragons", "count": 2}),
    ("Peux-tu me créer une image d'une forêt enchantée ?", {"prompt": "une forêt enchantée"}),
    ("Est-ce que tu peux me générer une photo d'un vieux pêcheur ?",
     {"prompt": "photo d'un vieux pêcheur"}),
    ("Fais-moi une image d'un coucher de soleil s'il te plaît",
     {"prompt": "un coucher de soleil"}),
    ("Dessine-moi un mouton", {"prompt": "un mouton, dessin"}),
    ("Je voudrais une image de la tour Eiffel sous la neige",
     {"prompt": "la tour Eiffel sous la neige"}),
    ("Fais une vidéo de 5 secondes d'un renard dans la neige",
     {"kind": "video", "prompt": "un renard dans la neige", "seconds": 5.0}),
    ("Génère une vidéo d'une vague qui se brise, 3 secondes",
     {"kind": "video", "prompt": "une vague qui se brise", "seconds": 3.0}),
    ("Crée un logo moderne pour ma boulangerie", {"prompt": "logo moderne pour ma boulangerie"}),
    ("Génère un fond d'écran de montagne pour mon téléphone", {"shape": "portrait"}),
    ("Génère une image d'un paysage de montagne", {"shape": "landscape"}),
    ("create an image of a cat wearing a hat", {"prompt": "a cat wearing a hat"}),
    ("Génère 30 vidéos d'un chat", {"kind": "video", "count": creations.MAX_VIDEOS}),
])
def test_a_request_to_create_is_recognized(text, expected):
    found = creations.intent(text)
    assert found is not None, text
    assert {k: found[k] for k in expected} == expected


@pytest.mark.parametrize("text", [
    "Comment créer une image Docker ?",
    "Crée une image Docker pour mon application Flask",
    "Peux-tu générer une image ISO bootable ?",
    "Comment générer une image avec Python et Pillow ?",
    "Crée une image disque de ma clé USB",
    "J'ai créé une image hier, elle est floue",
    "Donne-moi une image claire de la situation",
    "Donne-moi une illustration de ce principe",
    "Tu peux créer des images ?",
    "Crée une image",
    "Envoie-moi une photo",
    "Fais une vidéo de 10 s",
    "Dessine-moi un tableau des ventes",
    "Dessine-moi un schéma de l'architecture",
    "Pour faire une image nette, il faut une bonne mise au point",
    "Imagine un monde sans pétrole",
    "Quelle image crées-tu quand tu dors ?",
    "Bonjour !",
])
def test_questions_code_and_vague_phrases_are_not_requests(text):
    assert creations.intent(text) is None


def test_the_shape_and_length_follow_the_model():
    assert creations.size_for("z-image-turbo", "portrait") == (768, 1344)
    assert creations.size_for("z-image-turbo", "landscape") == (1344, 768)
    assert creations.size_for("z-image-turbo", "square") == (1024, 1024)
    assert creations.size_for("z-image-turbo", None) is None
    assert creations.frames_for("wan2.1-t2v-1.3b", 5) == 81
    assert creations.frames_for("wan2.1-t2v-1.3b", 2.4) == 33
    assert creations.frames_for("wan2.1-t2v-1.3b", None) is None


# ------------------------------------------------------------------------ de bout en bout
@pytest.fixture
def client():
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


@pytest.fixture
def can_draw(tiny_model, monkeypatch):  # noqa: F811
    """Le petit modèle « essai » complet ici, un moteur, et des images faites sur le champ."""
    ready()
    monkeypatch.setattr(engine, "folder", lambda: tiny_model["folder"])
    monkeypatch.setattr(generate, "image",
                        lambda p, stop=None, waiting=None: {"png": png(p["width"], p["height"]),
                                                            "seconds": 0.5})
    return tiny_model


def test_preflight_names_the_model_that_would_compute(client, tiny_model, monkeypatch):  # noqa: F811
    def asked():
        return client.post("/api/chat/preflight", headers=AUTH, json={
            "text": "Crée une image d'un chat roux", "model": ""}).json()["create"]
    assert asked()["model"] is None                    # rien pour la calculer
    ready()
    monkeypatch.setattr(engine, "folder", lambda: tiny_model["folder"])
    assert asked() == {"kind": "image", "prompt": "un chat roux", "count": 1, "shape": None,
                       "seconds": None, "model": "essai", "label": "Essai"}
    assert client.post("/api/chat/preflight", headers=AUTH,
                       json={"text": "Bonjour", "model": ""}).json()["create"] is None


def test_an_image_is_created_in_the_conversation(client, can_draw):
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée-moi deux images d'un chat roux", "chat_model": "qwen3.5:9b"})
    assert reply.status_code == 200, reply.text
    made = reply.json()
    assert made["title"] == "Crée-moi deux images d'un chat roux"
    media = made["media"]
    assert (media["kind"], media["model"], media["prompt"], media["count"]) == \
        ("image", "essai", "un chat roux", 2)
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    conv = client.get(f"/api/conversations/{made['conversation_id']}", headers=AUTH).json()
    assert conv["model"] == "qwen3.5:9b"               # le modèle de conversation reste
    user, answer = conv["messages"]
    assert user["content"] == "Crée-moi deux images d'un chat roux"
    assert len(answer["media"]["images"]) == 2 and answer["media"]["job"]["status"] == "done"
    assert all(img["batch"] == media["batch"] and img["path"] for img in answer["media"]["images"])
    # Le modèle de conversation sait ce qui a été créé
    history = conversations.history(made["conversation_id"])
    assert history[-1] == {"role": "assistant", "content":
                           "(Création de 2 images avec Essai, d'après la description : "
                           "« un chat roux ».)"}
    batch = client.get(f"/api/images?batch={media['batch']}", headers=AUTH).json()
    assert [r["id"] for r in batch] == [r["id"] for r in answer["media"]["images"]]


def test_a_chosen_format_and_an_explicit_request_are_kept(client, can_draw, monkeypatch):
    monkeypatch.setitem(catalog.MODELS["essai"], "sizes",
                        [[1024, 1024, "Carré"], [768, 1344, "Portrait"]])
    made = client.post("/api/chat/create", headers=AUTH, json={
        "text": "un phare en format paysage", "kind": "image", "model": "essai",
        "prompt": "un phare", "width": 768, "height": 1344, "count": 1, "seed": 7}).json()
    job = queue.job_summary(made["job_id"])
    assert (job["params"]["width"], job["params"]["height"], job["params"]["seed"]) == (768, 1344, 7)
    assert job["params"]["prompt"] == "un phare"
    assert job["params"]["conversation_id"] == made["conversation_id"]


def test_nothing_is_added_when_no_model_can_compute(client):
    reply = client.post("/api/chat/create", headers=AUTH,
                        json={"text": "Crée une image d'un chat roux"})
    assert reply.status_code == 409 and "Images et vidéos" in reply.json()["detail"]
    assert conversations.list_tree()["conversations"] == []


def test_the_chat_model_knows_aiodos_can_create_images(tiny_model, monkeypatch):  # noqa: F811
    def system():
        messages, _ = pipeline.build_messages([], "Tu sais faire des images ?", libraries=False,
                                              conversations=False, web_search=False)
        return " ".join(m["content"] for m in messages if m["role"] == "system")
    assert "sait aussi créer" not in system()
    ready()
    monkeypatch.setattr(engine, "folder", lambda: tiny_model["folder"])
    assert "sait aussi créer des images :" in system()
