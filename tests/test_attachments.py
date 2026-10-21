"""Pièces jointes d'une conversation (chat/attachments.py, api/routes/attachments.py) : une
image de référence, un document ou un texte joint à un message. Envoyées avant le message,
liées à lui, lues une fois ; le modèle reçoit leur texte avec la question, une image telle
quelle s'il la voit ; une création d'image part de l'image jointe ; rien ne s'exécute dans
l'interface ; elles partent avec leur conversation. Sans Ollama."""
import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from aiodos import db
from aiodos.api.server import create_app
from aiodos.chat import attachments, conversations, pipeline
from aiodos.services import ollama
from tests.test_creations import can_draw  # noqa: F401  (fixture)
from tests.test_images import png, ready, tiny_model, wait_for  # noqa: F401  (fixtures)

TOKEN = {"X-Aiodos-Token": "jeton"}


@pytest.fixture
def http():
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as client:
        yield client


def _upload(http, name, data: bytes, kind="application/octet-stream"):
    return http.post("/api/attachments", headers={**TOKEN, "Content-Type": kind,
                                                  "X-File-Name": name}, content=data)


def _photo(width=640, height=480, fmt="JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 120, 40)).save(out, fmt)
    return out.getvalue()


def _stream(http, **body):
    with http.stream("POST", "/api/chat", headers=TOKEN,
                     json=dict({"libraries": False, "conversations": False}, **body)) as reply:
        return reply.status_code, [json.loads(line[6:]) for line in reply.iter_lines()
                                   if line.startswith("data: ")]


@pytest.fixture
def heard(monkeypatch):
    """Ce que reçoit le modèle, à chaque réponse."""
    seen = []

    def generate(model, messages, think=True, stop=None):
        seen.append(messages)
        yield "content", "Vu."
    monkeypatch.setattr(pipeline, "generate", generate)
    return seen


def test_sent_kept_shown_and_never_run_in_the_page(http):
    made = _upload(http, "../../Rapport final.txt", "Budget 2025 : 42 000 €".encode())
    assert made.status_code == 200, made.text
    att = made.json()
    assert att["name"] == "Rapport final.txt" and att["kind"] == "text"
    assert attachments.path_of(attachments.get(att["id"])).parent.parent == attachments.folder()
    page = _upload(http, "piege.html", b"<script>alert(1)</script>", "text/html").json()
    reply = http.get(f"/api/attachments/{page['id']}", headers=TOKEN)
    assert reply.headers["content-type"] == "application/octet-stream"
    assert "attachment" in reply.headers["content-disposition"]
    picture = _upload(http, "photo.jpg", _photo(), "image/jpeg").json()
    shown = http.get(f"/api/attachments/{picture['id']}?t=jeton")
    assert shown.headers["content-type"] == "image/jpeg" and shown.content[:2] == b"\xff\xd8"
    assert http.delete(f"/api/attachments/{page['id']}", headers=TOKEN).status_code == 200
    assert http.get(f"/api/attachments/{page['id']}", headers=TOKEN).status_code == 404
    assert _upload(http, "vide.txt", b"").status_code == 400


def test_a_file_too_big_is_refused_and_not_kept(http, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_BYTES", 1000)
    reply = _upload(http, "gros.bin", b"x" * 5000)
    assert reply.status_code == 413
    assert [p.name for p in attachments.folder().iterdir()] == []


def test_the_model_reads_a_document_with_the_question_and_later(http, heard):
    att = _upload(http, "notes.md", "# Projet Orion\nLancement prévu le 12 mars.".encode()).json()
    status, events = _stream(http, text="Quand est le lancement ?", model="local-test",
                             attachments=[att["id"]])
    assert status == 200 and events[0]["type"] == "start", events
    question = heard[0][-1]
    assert question["role"] == "user" and question["content"].endswith("Quand est le lancement ?")
    assert "Fichier joint « notes.md »" in question["content"] and "12 mars" in question["content"]
    conv = conversations.get(events[0]["conversation_id"])
    assert conv["messages"][0]["content"] == "Quand est le lancement ?"        # tel qu'écrit
    assert [a["name"] for a in conv["messages"][0]["attachments"]] == ["notes.md"]
    # La question suivante garde ce qu'on en a lu ; la pièce jointe ne repart pas deux fois
    _stream(http, text="Et le nom du projet ?", model="local-test",
            conversation_id=conv["id"])
    assert any("Projet Orion" in m["content"] for m in heard[1] if m["role"] == "user")
    again = _stream(http, text="Encore", model="local-test", attachments=[att["id"]])
    assert again[0] == 400


def test_an_image_goes_as_it_is_to_a_model_that_sees_it(http, heard, monkeypatch):
    monkeypatch.setattr(ollama, "capabilities", lambda model: ["completion", "vision"])
    att = _upload(http, "chat.webp", _photo(fmt="WEBP"), "image/webp").json()
    status, _ = _stream(http, text="Que vois-tu ?", model="local-test", attachments=[att["id"]])
    question = heard[0][-1]
    assert status == 200 and len(question["images"]) == 1
    assert base64.b64decode(question["images"][0])[:2] == b"\xff\xd8"     # WebP remis en JPEG
    assert "Image jointe « chat.webp » (ci-jointe)" in question["content"]


def test_an_image_is_described_to_a_model_that_does_not_see_it(http, heard, monkeypatch):
    from aiodos.services import media
    monkeypatch.setattr(ollama, "capabilities", lambda model: ["completion"])
    monkeypatch.setattr(media, "vision_extract", lambda path, model, mode="both":
                        "Un chat roux sur un toit.")
    att = _upload(http, "chat.png", _photo(fmt="PNG"), "image/png").json()
    _stream(http, text="Décris-la", model="local-test", attachments=[att["id"]])
    question = heard[0][-1]
    assert "images" not in question and "Un chat roux sur un toit." in question["content"]


def test_a_creation_starts_from_the_joined_image(http, can_draw, monkeypatch):  # noqa: F811
    from aiodos.imaging import catalog
    monkeypatch.setitem(catalog.MODELS["essai"], "sizes",
                        [[512, 512, "Carré"], [768, 448, "Paysage"], [448, 768, "Portrait"]])
    att = _upload(http, "reference.jpg", _photo(900, 600), "image/jpeg").json()
    reply = http.post("/api/chat/create", headers=TOKEN, json={
        "text": "Crée une image d'un samouraï sur un escargot", "kind": "image",
        "attachments": [att["id"]]})
    assert reply.status_code == 200, reply.text
    made = reply.json()
    assert made["media"].get("reference") is True
    params = db.query_one("SELECT params FROM jobs WHERE id=?", (made["job_id"],))["params"]
    params = json.loads(params)
    start = Image.open(params["init_path"])
    assert start.format == "PNG" and start.size[0] % 64 == 0 and start.size[0] > start.size[1]
    conv = conversations.get(made["conversation_id"])
    assert [a["id"] for a in conv["messages"][0]["attachments"]] == [att["id"]]
    video = http.post("/api/chat/create", headers=TOKEN, json={
        "text": "Crée une vidéo", "kind": "video",
        "attachments": [_upload(http, "b.jpg", _photo(), "image/jpeg").json()["id"]]})
    assert video.status_code >= 400


def test_they_leave_with_their_conversation_or_when_never_sent(http, heard):
    att = _upload(http, "notes.txt", b"Secret").json()
    _, events = _stream(http, text="Lis-le", model="local-test", attachments=[att["id"]])
    folder = attachments.path_of(attachments.get(att["id"])).parent
    forgotten = _upload(http, "brouillon.txt", b"Jamais envoye").json()
    conversations.delete(events[0]["conversation_id"])
    assert not folder.exists() and attachments.get(att["id"]) is None
    assert attachments.get(forgotten["id"]) is not None                  # récente : gardée
    assert attachments.purge(now=db.now() + 2 * 86400) == 1
    assert attachments.get(forgotten["id"]) is None


@pytest.mark.parametrize("name, safe", [("..\\..\\x.txt", "x.txt"), ("a/b/c.pdf", "c.pdf"),
                                        ('in:val<id>?.png', "in_val_id__.png"), ("", "fichier")])
def test_names_never_leave_their_folder(name, safe):
    assert attachments.safe_name(name) == safe
