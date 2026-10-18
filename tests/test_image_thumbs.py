"""Vignettes des images créées (imaging/thumbs.py) : WebP de 384 ou 768 pixels de côté pour
les tuiles de la conversation et de la galerie, faites une fois, jamais dans le dossier des
créations ; le PNG entier reste servi sans ?size ; une vidéo n'en a pas ; une image
supprimée perd les siennes, et un numéro repris ne ressert pas l'ancienne."""
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from aiodos.api.server import create_app
from aiodos.imaging import gallery, thumbs

TOKEN = "jeton-de-test"
WEBM = bytes.fromhex("1a45dfa39f42868101428284") + b"webm" + bytes(64)


def _png(width, height, shade=0) -> bytes:
    """Du grain, comme une photo : un PNG lourd (un aplat se compresserait trop bien)."""
    rng = np.random.default_rng(shade)
    pixels = rng.integers(0, 64, (height, width, 3), dtype=np.uint8) + np.uint8(shade % 128)
    out = io.BytesIO()
    Image.fromarray(pixels).save(out, "PNG")
    return out.getvalue()


def _add(width=1024, height=1024, seed=1, shade=0, model="z-image-turbo", data=None):
    payload = {"model": model, "prompt": "un chat", "seed": seed, "width": width,
               "height": height, "batch": f"lot{seed}", "index": 0}
    return gallery.add(payload, data or _png(width, height, shade), machine="PC fixe")


@pytest.fixture
def client():
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


def _get(client, image_id, size=None):
    query = f"?t={TOKEN}" + (f"&size={size}" if size else "")
    return client.get(f"/api/images/{image_id}/file{query}")


def test_tiles_get_a_small_webp_and_the_viewer_the_whole_png(client):
    row = _add(768, 1344)
    whole = _get(client, row["id"])
    assert whole.headers["content-type"] == "image/png"
    small = _get(client, row["id"], 300)
    assert small.status_code == 200 and small.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(small.content)) as image:
        assert image.format == "WEBP" and max(image.size) == 384
        assert image.size == (219, 384)                       # proportions gardées
    with Image.open(io.BytesIO(_get(client, row["id"], 5000).content)) as image:
        assert max(image.size) == 768                         # jamais plus grand
    assert len(small.content) < len(whole.content) / 4
    # Gardées à part, pas dans le dossier des créations
    made = sorted(p.name for p in thumbs.folder().iterdir())
    assert len(made) == 2 and all(p.startswith(f"{row['id']}-") for p in made)
    assert not any(p.suffix == ".webp" for p in gallery.out_dir().iterdir())


def test_a_thumbnail_is_made_once(client, monkeypatch):
    row = _add()
    _get(client, row["id"], 768)
    monkeypatch.setattr(Image, "open", lambda *a, **k: pytest.fail("refaite"))
    assert _get(client, row["id"], 700).headers["content-type"] == "image/webp"


def test_a_video_has_no_thumbnail(client):
    row = _add(832, 480, model="wan2.1-t2v-1.3b", data=WEBM)
    reply = _get(client, row["id"], 384)
    assert reply.headers["content-type"] == "video/webm" and reply.content == WEBM


def test_a_deleted_image_loses_its_thumbnails_and_a_reused_number_shows_the_new_one(client):
    first = _add(seed=1, shade=0)
    old = _get(client, first["id"], 384).content
    assert client.delete(f"/api/images/{first['id']}",
                         headers={"X-Aiodos-Token": TOKEN}).json() == {"ok": True}
    assert not list(thumbs.folder().glob(f"{first['id']}-*"))
    # SQLite reprend le numéro de la dernière ligne supprimée
    second = _add(seed=2, shade=128)
    assert second["id"] == first["id"]
    assert _get(client, second["id"], 384).content != old


def test_an_unreadable_image_falls_back_to_the_file(client):
    row = _add()
    gallery.path_of(row).write_bytes(b"\x89PNG\r\n\x1a\n" + b"abime" * 10)
    reply = _get(client, row["id"], 384)
    assert reply.status_code == 200 and reply.headers["content-type"] == "image/png"
