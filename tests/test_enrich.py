"""Une demande courte devient une description détaillée en anglais avant la création
(chat/enrich.py), comme le font les services en ligne : c'est ce qui change le plus le
résultat d'un petit modèle d'images (essai du 21/10/2026 sur Z-Image Turbo, « un samouraï
assis sur la carapace d'un escargot »). Une retouche, une variante, une description déjà
détaillée ou le réglage coupé : rien n'est enrichi."""
from aiodos import settings
from aiodos.chat import enrich, retouches
from tests.test_creations import AUTH, can_draw, client  # noqa: F401  (fixtures)
from tests.test_images import png, ready, tiny_model, wait_for  # noqa: F401  (fixtures)

DETAILED = ("A ginger cat curled up on a sunny windowsill, soft morning light, cozy room, "
            "watercolor illustration, warm palette, highly detailed.")


def _chat_model(monkeypatch, answer=DETAILED, asked=None):
    """Un modèle de conversation installé ici, qui répond `answer`."""
    monkeypatch.setattr(retouches.ollama, "list_models", lambda *a, **k: [{"name": "qwen3.5:9b"}])
    monkeypatch.setattr(retouches.ollama, "capabilities_many",
                        lambda names: {n: ["completion"] for n in names})

    def fake_stream(model, messages, think=None, stop=None, options=None, **kwargs):
        if asked is not None:
            asked.append((model, messages))
        yield "content", answer
    monkeypatch.setattr(enrich.ollama, "chat_stream", fake_stream)


def _create(client, **body):
    reply = client.post("/api/chat/create", headers=AUTH, json=body)
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_a_short_request_becomes_a_detailed_english_description(client, can_draw, monkeypatch):
    asked = []
    _chat_model(monkeypatch, asked=asked)
    made = _create(client, text="Crée une image d'un chat roux", chat_model="qwen3.5:9b")
    assert made["media"]["prompt"] == DETAILED
    model, messages = asked[0]
    assert model == "qwen3.5:9b" and messages[-1]["content"] == "un chat roux"
    assert "anglais" in messages[0]["content"]
    conv = client.get(f"/api/conversations/{made['conversation_id']}", headers=AUTH).json()
    assert conv["messages"][0]["content"] == "Crée une image d'un chat roux"   # le message reste
    # Une variante reprend la description utilisée : rien de plus n'est demandé
    again = _create(client, text="Encore une", followup="variation",
                    conversation_id=made["conversation_id"])
    assert again["media"]["prompt"] == DETAILED and len(asked) == 1


def test_switched_off_or_already_detailed_nothing_changes(client, can_draw, monkeypatch):
    asked = []
    _chat_model(monkeypatch, asked=asked)
    settings.set_value("images_enrich", False)
    assert _create(client, text="Crée une image d'un chat roux")["media"]["prompt"] == "un chat roux"
    settings.set_value("images_enrich", True)
    detailed = " ".join(["détail"] * enrich.DETAILED_WORDS)
    assert _create(client, text=detailed, kind="image", prompt=detailed)["media"]["prompt"] == detailed
    assert asked == []


def test_the_instruction_follows_the_language_of_the_request(monkeypatch):
    """La consigne au modèle est dans la langue de la requête ; la description qu'elle demande
    reste en anglais dans les deux langues."""
    from aiodos import i18n
    asked = []
    _chat_model(monkeypatch, asked=asked)
    with i18n.using("en"):
        enrich.enrich("a ginger cat", "video")
    with i18n.using("fr"):
        enrich.enrich("un chat roux")
    english, french = asked[0][1][0]["content"], asked[1][1][0]["content"]
    assert "in English" in english and "camera" in english and "anglais" not in english
    assert "en anglais" in french and "caméra" not in french


def test_without_a_chat_model_in_time_the_request_goes_as_it_is(monkeypatch):
    def no_ollama(*args, **kwargs):
        raise ConnectionError("Ollama arrêté")
    monkeypatch.setattr(retouches.ollama, "list_models", no_ollama)
    assert enrich.enrich("un chat roux") == "un chat roux"
    _chat_model(monkeypatch, answer="Cat.")              # trop court : la demande reste
    assert enrich.enrich("un chat roux sur un toit") == "un chat roux sur un toit"
    _chat_model(monkeypatch, answer=DETAILED)
    assert enrich.enrich("un chat roux", "video").startswith("A ginger cat")


def test_a_tiny_conversation_model_never_rewrites(monkeypatch):
    """Un modèle de moins de 3 milliards de paramètres trahit la demande (S23 de l'auteur,
    qwen3.5:0.8b : « un samouraï chat » devenu un samouraï poulet) : elle part telle quelle,
    et une retouche s'ajoute à la description."""
    monkeypatch.setattr(retouches.ollama, "list_models",
                        lambda *a, **k: [{"name": "qwen3.5:0.8b"}, {"name": "gemma3:270m"}])
    monkeypatch.setattr(retouches.ollama, "capabilities_many",
                        lambda names: {n: ["completion"] for n in names})

    def never(*args, **kwargs):
        raise AssertionError("un tout petit modèle ne réécrit pas")
    monkeypatch.setattr(retouches.ollama, "chat_stream", never)
    assert retouches._rewriter("qwen3.5:0.8b") is None
    assert enrich.enrich("un samouraï chat", "image", "auto:qwen3.5:0.8b") == "un samouraï chat"
    assert retouches.rewrite("un samouraï chat", "Fais-la de nuit", "qwen3.5:0.8b") == \
        "un samouraï chat, de nuit"
    monkeypatch.setattr(retouches.ollama, "list_models",
                        lambda *a, **k: [{"name": "qwen3.5:0.8b"}, {"name": "qwen3.5:9b"}])
    assert retouches._rewriter("qwen3.5:0.8b") == "qwen3.5:9b"      # le plus grand, s'il est là
