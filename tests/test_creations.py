"""Images et vidéos créées dans la conversation (chat/creations.py) : la demande se
reconnaît dans le message, part dans la file comme une demande de la vue Images, et la
réponse de l'assistant la garde (les images, le travail, la description)."""
import time

import pytest
from fastapi.testclient import TestClient

from aiodos.api.server import create_app
from aiodos.chat import conversations, creations, pipeline, retouches
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
    # Sans accents ni apostrophe, avec la faute « créé », sans verbe, format dans la phrase
    ("genere une image d un chat", {"prompt": "un chat"}),
    ("Créé une image d'un chat", {"prompt": "un chat"}),
    ("Une image de chat stp", {"prompt": "chat"}),
    ("J'aimerais une affiche pour mon concert", {"prompt": "affiche pour mon concert"}),
    ("Génère une image en 16:9 d'une ville futuriste",
     {"prompt": "une ville futuriste", "shape": "landscape"}),
    ("Crée une image au format portrait d'un vieux marin",
     {"prompt": "un vieux marin", "shape": "portrait"}),
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
    "J'ai créé une image hier, elle est floue",
    "On a créé une image de marque",
    "Une image vaut mille mots",
    "Une photo de famille, ça te dit ?",
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
        # Les images en cours de calcul finissent avant la fin de l'essai : un fil qui
        # écrirait après coup ouvrirait la base de l'essai suivant (« database is locked »)
        _settle()


def _settle(timeout=15):
    from aiodos import db
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not db.query_one("SELECT COUNT(*) AS n FROM units WHERE status='running'")["n"]:
            return
        time.sleep(0.05)


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
    # La galerie mène à la conversation où elles ont été demandées
    assert {r["conversation_id"] for r in client.get("/api/images", headers=AUTH).json()} == \
        {made["conversation_id"]}


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


# ------------------------------------------------------------- retouches et variantes
@pytest.mark.parametrize("text, expected", [
    ("Fais-la de nuit", "edit"), ("Ajoute un chat sur le toit", "edit"),
    ("Peux-tu la rendre plus lumineuse ?", "edit"), ("Encore une !", "variation"),
    ("Une autre", "variation"), ("Pareil", "variation"),
    ("Encore une, mais en noir et blanc", "edit"), ("avec un chien", "edit"),
    ("Pourquoi est-elle floue ?", None), ("Merci beaucoup", None), ("Bonjour", None),
    ("Sans rire, c'est génial", None), ("Avec plaisir", None),
    ("Plus de détails sur ce modèle ?", None), ("Ajoute un script Python", None),
])
def test_a_follow_up_of_the_last_creation_is_recognized(text, expected):
    assert retouches.detect(text) == expected


def test_without_a_chat_model_the_request_is_added_to_the_description(monkeypatch):
    def no_ollama(*args, **kwargs):
        raise ConnectionError("Ollama arrêté")
    monkeypatch.setattr(retouches.ollama, "list_models", no_ollama)
    assert retouches.rewrite("un phare sous l'orage", "Fais-la de nuit") == \
        "un phare sous l'orage, de nuit"


def test_the_chat_model_rewrites_the_description(monkeypatch):
    asked = []
    monkeypatch.setattr(retouches.ollama, "list_models", lambda *a, **k: [{"name": "qwen3.5:9b"}])
    monkeypatch.setattr(retouches.ollama, "capabilities_many", lambda names: {n: ["completion"] for n in names})

    def fake_stream(model, messages, think=None, stop=None, options=None, **kwargs):
        asked.append((model, messages[-1]["content"]))
        yield "content", "« Un phare sous l'orage, la nuit, "
        yield "content", "éclairé par la lune. »"
    monkeypatch.setattr(retouches.ollama, "chat_stream", fake_stream)
    assert retouches.rewrite("un phare sous l'orage", "Fais-la de nuit", "auto:qwen3.5:9b") == \
        "Un phare sous l'orage, la nuit, éclairé par la lune."
    assert asked[0][0] == "qwen3.5:9b" and "Fais-la de nuit" in asked[0][1]


def test_a_variation_and_an_edit_follow_the_last_creation(client, can_draw, monkeypatch):
    first = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux", "width": 1024, "height": 1024}).json()
    conv = first["conversation_id"]
    pre = client.post("/api/chat/preflight", headers=AUTH, json={
        "text": "Encore une", "model": "", "conversation_id": conv}).json()["create"]
    assert (pre["followup"], pre["kind"], pre["model"]) == ("variation", "image", "essai")
    again = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Encore une", "followup": "variation", "conversation_id": conv}).json()["media"]
    assert again["prompt"] == "un chat roux" and again["batch"] != first["media"]["batch"]
    monkeypatch.setattr(retouches, "rewrite", lambda prompt, text, model=None: f"{prompt}, la nuit")
    edited = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Fais-la de nuit", "followup": "edit", "conversation_id": conv}).json()["media"]
    assert edited["prompt"] == "un chat roux, la nuit" and edited["seed"] == again["seed"]
    assert len(conversations.get(conv)["messages"]) == 6


def test_friends_who_could_compute_are_offered(client, monkeypatch):
    from aiodos.imaging import lenders
    monkeypatch.setattr(lenders, "helpers", lambda friends=None: (
        {"z-image-turbo": ["PC de Léa (ami)"]} if friends else {}))
    reply = client.post("/api/chat/create", headers=AUTH,
                        json={"text": "Crée une image d'un chat roux"})
    assert reply.status_code == 409
    assert reply.json()["friends"] == ["PC de Léa (ami)"] and "PC de Léa" in reply.json()["detail"]


def test_a_creation_says_how_long_it_usually_takes(tiny_model):  # noqa: F811
    from aiodos.imaging import gallery
    from aiodos.jobs.kinds import images as kind
    made = {"png": __import__("base64").b64encode(png(256, 384)).decode()}
    payload = {"model": "essai", "prompt": "un chat", "negative": "", "width": 256, "height": 384,
               "steps": 4, "cfg": 1.0, "index": 0, "batch": "abc123"}
    for seed, seconds in ((1, 20.0), (2, 30.0), (3, 22.0)):
        kind.assimilate_image({**payload, "seed": seed}, {**made, "seconds": seconds})
    assert gallery.typical_seconds("essai", 256, 384) == 22.0
    assert gallery.typical_seconds("essai", 1024, 1024) is None
    assert creations.view({"model": "essai", "width": 256, "height": 384, "batch": "abc123",
                           "count": 1})["expected_s"] == 22.0


# ------------------------------------------------------- relecture du 18/10 : faux positifs
@pytest.mark.parametrize("text", [
    "Encore merci pour ton aide", "Encore une fois, merci !",
    "Une autre question : explique-moi la photosynthèse.", "Change de sujet : parle-moi de Rome.",
    "Plus précisément, explique le fonctionnement.", "Sans vouloir te déranger, résume-moi ce texte.",
    "Supprimer les doublons d'une liste", "Le même jour j'ai vu Paul", "Retirement planning tips",
    "De rien", "En fait je préfère les chiens", "Ajoute une conclusion à mon texte",
])
def test_ordinary_messages_after_a_creation_are_not_follow_ups(text):
    assert retouches.detect(text) is None


@pytest.mark.parametrize("text", [
    "Je voudrais une image de marque plus forte pour mon entreprise, des idées ?",
    "Je dois créer une image de marque pour ma boîte",
    "Il me faut une photo d'identité, tu sais où en faire ?",
    "Je veux une vidéo de mon mariage, qui contacter ?",
])
def test_questions_about_images_are_not_requests(text):
    assert creations.intent(text) is None


def test_a_variation_of_several_makes_one_when_asked_for_one():
    assert retouches.variation_count("Encore une", 4) == 1
    assert retouches.variation_count("Refais-les", 4) == 4


def test_a_description_never_grows_past_the_limit():
    long = "un phare " * 300
    grown = retouches._appended(long, "Fais-la de nuit")
    assert len(grown) <= 2000 and grown.endswith(", de nuit")


def test_an_old_creation_does_not_take_the_job_that_reused_its_number(client, can_draw):
    made = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux"}).json()
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    queue.clear_finished()                       # « Retirer les terminés » : le numéro se libère
    other = queue.create_job("generate_images", __import__(
        "aiodos.jobs.kinds.images", fromlist=["x"]).normalize({"model": "essai", "prompt": "autre"}))
    assert other == made["job_id"]               # SQLite reprend le numéro
    conv = client.get(f"/api/conversations/{made['conversation_id']}", headers=AUTH).json()
    assert conv["messages"][-1]["media"]["job"] is None


def test_answering_as_text_removes_the_creation(client, can_draw):
    made = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux", "auto": True}).json()
    assert made["media"]["auto"] is True
    undone = client.post(f"/api/chat/create/{made['message_id']}/undo", headers=AUTH).json()
    assert undone["text"] == "Crée une image d'un chat roux"
    assert conversations.get(made["conversation_id"])["messages"] == []
    assert client.post(f"/api/chat/create/{made['message_id']}/undo",
                       headers=AUTH).status_code == 404


def test_only_the_last_creation_can_become_a_text_answer(client, can_draw):
    first = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux", "auto": True}).json()
    conversations.add_message(first["conversation_id"], "user", "Merci !")
    reply = client.post(f"/api/chat/create/{first['message_id']}/undo", headers=AUTH)
    assert reply.status_code == 409


def test_an_image_retouch_starts_from_the_last_image(client, can_draw, monkeypatch):
    first = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux", "width": 1024, "height": 1024}).json()
    wait_for(lambda: queue.job_summary(first["job_id"])["status"] == "done")
    monkeypatch.setattr(retouches, "rewrite", lambda prompt, text, model=None: f"{prompt}, la nuit")
    edited = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Fais-la de nuit", "followup": "edit",
        "conversation_id": first["conversation_id"]}).json()
    job = queue.job_summary(edited["job_id"])
    path = first["media"]["images"][0]["path"] if first["media"]["images"] else None
    started = job["params"]["init_path"]
    assert started and started.endswith(".png") and (path is None or started == path)
    assert job["params"]["strength"] == retouches.AMBIANCE            # « de nuit » : une ambiance
    assert edited["media"]["retouch"] is True                            # dit sous l'image
    assert retouches.strength_for("Ajoute un chat sur le toit") == retouches.KEEP


def test_retouching_several_images_redraws_each_from_its_description(client, can_draw, monkeypatch):
    first = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée deux images d'un chat roux", "width": 1024, "height": 1024}).json()
    wait_for(lambda: queue.job_summary(first["job_id"])["status"] == "done")
    monkeypatch.setattr(retouches, "rewrite", lambda prompt, text, model=None: f"{prompt}, la nuit")
    edited = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Fais-les de nuit", "followup": "edit",
        "conversation_id": first["conversation_id"]}).json()
    params = queue.job_summary(edited["job_id"])["params"]
    assert params["count"] == 2 and "init_path" not in params
    assert params["seed"] == first["media"]["seed"]              # chacune garde sa graine


def test_editing_a_retouch_starts_again_from_the_creation_before_it(client, can_draw,
                                                                   monkeypatch):
    """« Modifier » une retouche (« Fais-la de nuit » → « Fais-la sous la neige ») : la
    nouvelle retouche part de la première image, et l'échange de la nuit laisse la place."""
    first = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux", "width": 1024, "height": 1024}).json()
    wait_for(lambda: queue.job_summary(first["job_id"])["status"] == "done")
    monkeypatch.setattr(retouches, "rewrite", lambda prompt, text, model=None: f"{prompt} ({text})")
    conv = first["conversation_id"]
    night = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Fais-la de nuit", "followup": "edit", "conversation_id": conv}).json()
    wait_for(lambda: queue.job_summary(night["job_id"])["status"] == "done")
    pre = client.post("/api/chat/preflight", headers=AUTH, json={
        "text": "Fais-la sous la neige", "model": "", "conversation_id": conv,
        "replace_from": night["user_message_id"]}).json()["create"]
    assert pre["followup"] == "edit"
    snow = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Fais-la sous la neige", "followup": "edit", "conversation_id": conv,
        "replace_from": night["user_message_id"], "replace_text": "Fais-la de nuit"}).json()
    job = queue.job_summary(snow["job_id"])
    assert job["params"]["prompt"] == "un chat roux (Fais-la sous la neige)"
    from aiodos.imaging import gallery
    first_image = gallery.path_of(gallery.batch(first["media"]["batch"])[0])
    assert job["params"]["init_path"] == str(first_image)          # pas l'image de nuit
    assert [m["content"] for m in conversations.get(conv)["messages"] if m["role"] == "user"] \
        == ["Crée une image d'un chat roux", "Fais-la sous la neige"]


def test_replacing_a_creation_stops_its_job(client, can_draw, monkeypatch):
    import threading
    gate = threading.Event()

    def slow(params, stop=None, waiting=None):
        gate.wait(10)
        return {"png": png(params["width"], params["height"]), "seconds": 0.5}
    monkeypatch.setattr(generate, "image", slow)
    made = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Crée une image d'un chat roux"}).json()
    reply = client.post(f"/api/conversations/{made['conversation_id']}/rewind", headers=AUTH,
                        json={"message_id": made["user_message_id"],
                              "expect": "Crée une image d'un chat roux"})
    assert reply.status_code == 200
    assert queue.job_summary(made["job_id"])["status"] == "cancelled"
    gate.set()
    assert conversations.get(made["conversation_id"])["messages"] == []
