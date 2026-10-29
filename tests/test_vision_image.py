"""Images envoyées au modèle de vision (services/vision_image.py, 29/10/2026 : « Failed to load
image or audio file » sur le PC de l'auteur) : ce que le moteur intégré ne lit pas est réécrit
en JPEG ; ce que personne ne lit est sauté avec sa raison, pas compté en échec."""
import base64
import io

import pytest
from PIL import Image

from aiodos.jobs.kinds import index
from aiodos.services import media, ollama, vision_image

JPEG, PNG = b"\xff\xd8", b"\x89PNG\r\n\x1a\n"


def _picture(mode="RGB", size=(64, 48)):
    return Image.new(mode, size, "white" if mode in ("RGB", "RGBA", "L") else 0)


def _save(path, picture, fmt, **options):
    picture.save(path, fmt, **options)
    return path


def _decoded(data: bytes):
    with Image.open(io.BytesIO(data)) as found:
        found.load()
        return found.format, found.mode, found.size


def test_plain_jpeg_and_png_go_as_they_are(tmp_path):
    jpeg = _save(tmp_path / "photo.jpg", _picture(), "JPEG")
    png = _save(tmp_path / "scan.png", _picture("L"), "PNG")
    assert vision_image.prepared(jpeg) == jpeg.read_bytes()
    assert vision_image.prepared(png) == png.read_bytes()


@pytest.mark.parametrize("name, fmt, mode", [
    ("image.webp", "WEBP", "RGB"),
    ("scan.tif", "TIFF", "RGB"),
    ("transparent.png", "PNG", "RGBA"),
    ("dessin.bmp", "BMP", "RGB"),
    ("anime.gif", "GIF", "P"),
    ("imprimerie.jpg", "JPEG", "CMYK"),
])
def test_other_images_are_rewritten_as_jpeg(tmp_path, name, fmt, mode):
    path = _save(tmp_path / name, _picture(mode), fmt)
    data = vision_image.prepared(path)
    assert data[:2] == JPEG
    found_format, found_mode, size = _decoded(data)
    assert found_format == "JPEG" and found_mode in ("RGB", "L") and size == (64, 48)


def test_sixteen_bit_png_becomes_eight_bit(tmp_path):
    path = _save(tmp_path / "profond.png", Image.new("I;16", (40, 30), 40000), "PNG")
    found_format, found_mode, _ = _decoded(vision_image.prepared(path))
    assert found_format == "JPEG" and found_mode == "L"


def test_very_large_images_are_reduced(tmp_path, monkeypatch):
    monkeypatch.setattr(vision_image, "MAX_SIDE", 100)
    path = _save(tmp_path / "panorama.jpg", _picture(size=(400, 100)), "JPEG")
    assert _decoded(vision_image.prepared(path))[2] == (100, 25)


def test_a_photo_turned_by_its_exif_is_put_upright(tmp_path):
    picture = _picture(size=(80, 40))
    exif = picture.getexif()
    exif[vision_image.ORIENTATION] = 6                         # tournée de 90° (téléphone)
    path = _save(tmp_path / "portrait.jpg", picture, "JPEG", exif=exif.tobytes())
    assert _decoded(vision_image.prepared(path))[2] == (40, 80)


def test_unknown_or_damaged_images_say_why(tmp_path):
    heic = tmp_path / "iphone.heic"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 200)
    with pytest.raises(vision_image.Unreadable, match="HEIC"):
        vision_image.prepared(heic)
    full = _save(tmp_path / "entiere.jpg", _picture(size=(300, 300)), "JPEG", quality=95)
    cut = tmp_path / "coupee.jpg"
    cut.write_bytes(full.read_bytes()[:400])
    with pytest.raises(vision_image.Unreadable, match="abîmée"):
        vision_image.prepared(cut)


def test_the_vision_model_receives_a_readable_image(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ollama, "generate_with_images",
                        lambda model, prompt, images, timeout=300: sent.append(images) or "Un chat")
    path = _save(tmp_path / "chat.webp", _picture(), "WEBP")
    text = media.vision_extract(path, "qwen3.5:9b")
    assert "Un chat" in text
    assert base64.b64decode(sent[0][0])[:2] == JPEG


def test_an_engine_refusal_becomes_a_clear_skip(tmp_path, monkeypatch, fake_embed):
    """Le moteur refuse quand même l'image : l'unité est sautée avec une raison claire (pas un
    échec obscur compté deux fois)."""
    def refuse(model, prompt, images, timeout=300):
        raise RuntimeError("Moteur intégré : Failed to load image or audio file")
    monkeypatch.setattr(ollama, "generate_with_images", refuse)
    path = _save(tmp_path / "photo.jpg", _picture(), "JPEG")
    options = {"embed_model": "x", "vision": {"model": "qwen3.5:9b", "mode": "both"},
               "whisper": None, "lang": "fr"}
    out = index.compute_index_file({"path": str(path), "source": "photo.jpg"}, str(path), options)
    assert out["skipped"] and out["ext"] == ".jpg"
    assert "vision" in out["reason"]


def test_an_unread_format_is_skipped_with_its_reason(tmp_path, monkeypatch, fake_embed):
    monkeypatch.setattr(ollama, "generate_with_images",
                        lambda *a, **k: pytest.fail("le modèle de vision n'est pas appelé"))
    heic = tmp_path / "iphone.heic"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 200)
    options = {"embed_model": "x", "vision": {"model": "qwen3.5:9b", "mode": "both"},
               "whisper": None, "lang": "fr"}
    out = index.compute_index_file({"path": str(heic), "source": "iphone.heic"}, str(heic),
                                   options)
    assert out == {"skipped": True, "ext": ".heic",
                   "reason": "format d'image non lu (HEIC) : convertis-la en JPEG ou en PNG"}
