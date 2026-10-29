"""L'audio dans la conversation (chat/audio_creations.py) : la demande reconnue dans le message
(« Lis ce texte : … », « Crée une musique… », « Fais le bruit d'une porte qui grince »),
« Automatique » pour chaque genre (audio/ranking.py), le travail lancé et la réponse qui garde
la demande, le son rangé dans la galerie et servi comme un WAV ; l'onglet Audio du menu."""
import pytest
from fastapi.testclient import TestClient

from aiodos import settings
from aiodos.api.server import create_app
from aiodos.audio import catalog, generate, ranking
from aiodos.chat import audio_creations, conversations, creations, enrich, model_menu, pipeline
from aiodos.engines import gpu_share
from aiodos.jobs import queue
from tests.test_audio import ready, tiny_audio, tone  # noqa: F401  (fixture)
from tests.test_images import wait_for

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}


@pytest.mark.parametrize("text, expected", [
    ("Lis ce texte : Bonjour à tous, bienvenue.",
     {"kind": "voice", "prompt": "Bonjour à tous, bienvenue."}),
    ("Lis ce texte à voix haute : Il était une fois un petit chat.",
     {"kind": "voice", "prompt": "Il était une fois un petit chat."}),
    ("Lis-moi ce poème : Demain, dès l'aube…", {"kind": "voice", "prompt": "Demain, dès l'aube…"}),
    ("Dis à voix haute « Bonne nuit les petits »",
     {"kind": "voice", "prompt": "Bonne nuit les petits"}),
    ("Read this text aloud: Hello world.", {"kind": "voice", "prompt": "Hello world."}),
    ("Crée une voix qui dit : Attention, la porte va se fermer.",
     {"kind": "voice", "prompt": "Attention, la porte va se fermer."}),
    ("Crée une musique calme pour méditer",
     {"kind": "music", "prompt": "musique calme pour méditer", "lyrics": None}),
    ("Compose une chanson sur la mer, paroles : La mer est belle",
     {"kind": "music", "prompt": "chanson sur la mer", "lyrics": "La mer est belle"}),
    ("Fais-moi une musique de jazz de 30 secondes",
     {"kind": "music", "prompt": "musique de jazz", "seconds": 30.0}),
    ("Crée deux musiques de piano", {"kind": "music", "count": 2}),
    ("Fais le bruit d'une porte qui grince",
     {"kind": "sound", "prompt": "bruit d'une porte qui grince"}),
    ("Génère un effet sonore d'explosion lointaine",
     {"kind": "sound", "prompt": "effet sonore d'explosion lointaine"}),
    ("Make the sound of a creaking door", {"kind": "sound", "prompt": "sound of a creaking door"}),
    ("Create a song about summer", {"kind": "music", "prompt": "song about summer"}),
])
def test_a_request_for_audio_is_recognized(text, expected):
    found = audio_creations.intent(text)
    assert found is not None, text
    assert {k: found[k] for k in expected} == expected


@pytest.mark.parametrize("text", [
    "Lis ce texte et résume-le",                    # une question sur un texte
    "Lis ce texte",
    "Donne-moi des chansons de Brel",               # des titres, pas une création
    "Crée une musique",                             # rien à créer de plus
    "Fais du bruit",
    "Tu me fais une musique ?",
    "Comment créer un effet sonore en Python ?",
    "J'ai créé une musique hier",
    "Crée une image d'un chat",                     # une image : chat/creations.py
])
def test_what_is_not_a_request_for_audio(text):
    assert audio_creations.intent(text) is None


@pytest.fixture
def client():
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


@pytest.fixture
def fake_engine(monkeypatch):
    """audio.cpp simulé : chaque création rend 1,5 s de son."""
    made = []

    def create(payload, stop=None):
        made.append(payload)
        return {"wav": tone(1.5), "seconds": 0.4, "duration": 1.5, "rate": 24000, "channels": 1}
    monkeypatch.setattr(generate, "create", create)
    monkeypatch.setattr(gpu_share, "before_unit", lambda: [])
    return made


# ------------------------------------------------------------------ Automatique
def test_automatic_takes_the_best_ranked_available_and_speaks_the_language(tiny_audio, monkeypatch):
    ready("essai-voix")
    assert ranking.choose("voice", None, "fr").model == "essai-voix"
    assert ranking.choose("voice", None, "en").model is None       # elle ne parle que français
    assert ranking.choose("sound").model is None
    # Un modèle mieux classé, prêt chez une de mes machines : il passe devant
    monkeypatch.setattr(ranking.lenders, "circles", lambda friends=None: {
        "pocket-tts-fr": [{"circle": "mine", "name": "PC fixe", "power": 8192}]})
    choice = ranking.choose("voice", None, "fr")
    assert (choice.model, choice.where, choice.machine) == ("pocket-tts-fr", "mine", "PC fixe")
    # Choisi : passe d'abord ; une licence restrictive chez un ami, seulement acceptée ici
    assert ranking.choose("voice", "essai-voix", "fr").model == "essai-voix"
    monkeypatch.setattr(ranking.lenders, "circles", lambda friends=None: {
        "voxtral-4b-tts": [{"circle": "friend", "name": "Léa", "power": 8192}]})
    assert ranking.choose("voice", None, "fr").model == "essai-voix"
    catalog.accept("voxtral-4b-tts")
    assert ranking.choose("voice", None, "fr").model == "voxtral-4b-tts"


def test_the_audio_tab_of_the_menu(tiny_audio):
    ready("essai-musique")
    found = model_menu.audio()
    assert [g["genre"] for g in found["genres"]] == ["voice", "music", "sound"]
    music = found["genres"][1]["auto"]
    assert music["model"] == "essai-musique" and music["where"] == "here"
    item = next(m for m in found["models"] if m["id"] == "essai-musique")
    assert item["ready"] and item["genre"] == "music" and item["kind"] == "audio"
    assert item["seconds"] == [[5, "5 s"], [10, "10 s"]] and item["download"] is None
    voxtral = next(m for m in found["models"] if m["id"] == "voxtral-4b-tts")
    assert voxtral["restricted"] and voxtral["notice"] and not voxtral["ready"]
    assert voxtral["download"]["size"] == catalog.total_size("voxtral-4b-tts")
    # Le même dans le menu complet, sans faire tomber les autres genres
    assert model_menu.menu()["audio"]["genres"][1]["auto"]["model"] == "essai-musique"


def test_a_suggestion_for_this_pc(tiny_audio, monkeypatch):
    ready("essai-voix")
    # Une carte de 8 Go : Voxtral (8 Go) est mieux classé que la voix d'essai
    assert ranking.suggest("voice", 8192)["model"] == "voxtral-4b-tts"
    assert ranking.suggest("voice", 2048)["model"] == "pocket-tts-fr"
    assert ranking.suggest("voice", 0) is None


# ------------------------------------------------------------------ dans la conversation
def test_a_voice_is_asked_computed_and_kept(tiny_audio, fake_engine, client):
    ready("essai-voix")
    pre = pipeline.preflight("Lis ce texte : Bonjour à tous", "auto:best")["create"]
    assert pre["kind"] == "voice" and pre["model"] == "essai-voix"
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Lis ce texte : Bonjour à tous", "kind": "voice", "prompt": pre["prompt"],
        "model": None, "auto": True})
    assert reply.status_code == 200, reply.text
    made = reply.json()
    media = made["media"]
    assert media["kind"] == "voice" and media["media"] == "audio"
    assert media["model"] == "essai-voix" and media["prompt"] == "Bonjour à tous"
    assert media["voice"] == "anna" and media["auto"]
    job = queue.job_summary(made["job_id"])
    assert job["kind"] == "generate_audio" and creations.same_job(job, media)
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done", 20)
    assert fake_engine[0]["text"] == "Bonjour à tous"
    rows = client.get(f"/api/images?batch={media['batch']}", headers=AUTH).json()
    assert len(rows) == 1 and rows[0]["media"] == "audio" and rows[0]["duration"] == 1.5
    assert rows[0]["conversation_id"] == made["conversation_id"]
    assert rows[0]["label"] == "Voix d'essai"                     # pour la visionneuse
    sound = client.get(f"/api/images/{rows[0]['id']}/file?t={TOKEN}")
    assert sound.status_code == 200 and sound.headers["content-type"] == "audio/wav"
    # Ce que lit le modèle de conversation, et la réponse relue avec son son
    history = conversations.history(made["conversation_id"])
    assert "Bonjour à tous" in history[-1]["content"] and "Voix d'essai" in history[-1]["content"]
    shown = creations.view(media)
    assert shown["images"][0]["id"] == rows[0]["id"]


def test_music_from_the_create_menu_with_its_lyrics_and_duration(tiny_audio, fake_engine,
                                                                 client, monkeypatch):
    ready("essai-musique")
    spec = dict(catalog.MODELS["essai-musique"], lyrics="optional")
    monkeypatch.setitem(catalog.MODELS, "essai-musique", spec)
    asked = []
    monkeypatch.setattr(enrich, "enrich", lambda prompt, kind="image", model=None:
                        asked.append((prompt, kind)) or f"{prompt} (en)")
    settings.set_value("images_enrich", True)
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Une chanson sur la mer, paroles : La mer est belle", "kind": "music",
        "prompt": "Une chanson sur la mer, paroles : La mer est belle", "seconds": 10})
    assert reply.status_code == 200, reply.text
    media = reply.json()["media"]
    assert media["lyrics"] == "La mer est belle" and media["duration"] == 10
    assert asked == [("Une chanson sur la mer", "music")]          # traduite, sans les paroles
    assert media["prompt"] == "Une chanson sur la mer (en)"
    wait_for(lambda: fake_engine, 20)
    assert fake_engine[0]["lyrics"] == "La mer est belle"


def test_a_text_to_read_is_never_rewritten(tiny_audio, fake_engine, client, monkeypatch):
    ready("essai-voix")
    monkeypatch.setattr(enrich, "enrich", lambda *a, **k: pytest.fail("réécrit"))
    settings.set_value("images_enrich", True)
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Un texte court", "kind": "voice", "prompt": "Un texte court", "voice": "paul",
        "model": "essai-voix"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["media"]["voice"] == "paul"


def test_nothing_can_compute_this_genre(tiny_audio, client):
    reply = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Fais le bruit d'une porte qui grince", "auto": True})
    assert reply.status_code == 409
    assert "effets sonores" in reply.json()["detail"]
    assert queue.list_jobs() == []


def test_the_chat_model_knows_audio_can_be_created(tiny_audio):
    assert creations.capability_note() is None
    ready("essai-voix")
    note = creations.capability_note()
    assert note and "audio" in note


def test_no_retouch_after_a_sound(tiny_audio, fake_engine, client):
    ready("essai-voix")
    made = client.post("/api/chat/create", headers=AUTH, json={
        "text": "Lis ce texte : Bonjour", "kind": "voice", "prompt": "Bonjour"}).json()
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done", 20)
    assert creations.preflight("Encore une", made["conversation_id"]) is None
