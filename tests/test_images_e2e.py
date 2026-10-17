"""Des images calculées par une autre de mes machines, de bout en bout, sur ce seul PC : la
machine A n'a pas stable-diffusion.cpp ; B l'a, avec Z-Image Turbo, et prête sa puissance.
A demande deux images : B les calcule, A seule les garde.

Il faut stable-diffusion.cpp (variable AIODOS_SDCPP_DIR : le dossier de sd-server) et les
fichiers de Z-Image Turbo (variable AIODOS_IMAGE_MODELS : leur dossier) ; sinon le test
est ignoré. Compter une à deux minutes : B vérifie les empreintes des fichiers (7 Go),
charge le modèle, puis calcule les deux images.
"""
import os
from pathlib import Path

import pytest
import requests

from aiodos.imaging import catalog, generate
from tests.nodes import Node, pair

SDCPP = os.environ.get("AIODOS_SDCPP_DIR")
MODELS = os.environ.get("AIODOS_IMAGE_MODELS")
MODEL = "z-image-turbo"


def _complete() -> bool:
    if not SDCPP or not MODELS:
        return False
    return all((Path(MODELS) / f["name"]).is_file()
               and (Path(MODELS) / f["name"]).stat().st_size == f["size"]
               for f in catalog.MODELS[MODEL]["files"].values())


pytestmark = pytest.mark.skipif(
    not _complete(), reason="il faut AIODOS_SDCPP_DIR (stable-diffusion.cpp) et "
                            "AIODOS_IMAGE_MODELS (fichiers de Z-Image Turbo)")


@pytest.fixture
def machines(tmp_path):
    a = Node(tmp_path / "a", "PC portable").start()
    b = Node(tmp_path / "b", "PC fixe").preset(
        sdcpp_dir=SDCPP, images_models_dir=MODELS, share_enabled=True, share_when_idle=False,
        share_on_ac_only=False).start()
    yield a, b
    a.stop()
    b.stop()


def _model(node) -> dict:
    return next(m for m in node.api("GET", "/images/models")["models"] if m["id"] == MODEL)


def test_my_other_machine_makes_the_images(machines):
    a, b = machines
    _, b_id = pair(a, b)
    b.wait_for(lambda: _model(b)["state"] == "ready", timeout=180,
               what="fichiers du modèle vérifiés chez B")

    def helpers():
        a.api("POST", f"/network/peers/{b_id}/refresh")
        return _model(a)["helpers"]
    assert a.wait_for(helpers, timeout=60, what="B proposée pour les images") == ["PC fixe"]
    assert _model(a)["state"] == "missing"            # A n'a pas le modèle

    job = a.api("POST", "/images/generate",
                {"model": MODEL, "prompt": "A red apple on a wooden table, studio light",
                 "count": 2, "width": 512, "height": 512, "seed": 1234})
    assert job["here"] is False
    rows = a.wait_for(lambda: len(found := a.api("GET", "/images")) == 2 and found,
                      timeout=600, what="deux images calculées par B")
    assert {r["machine"] for r in rows} == {"PC fixe"}
    assert sorted(r["seed"] for r in rows) == [1234, 1235]
    image = requests.get(f"http://127.0.0.1:{a.port}/api/images/{rows[0]['id']}/file",
                         params={"t": a.token}, timeout=30)
    assert generate.png_size(image.content) == (512, 512)
    assert b.api("GET", "/images") == []              # B n'a rien gardé


VIDEO = "wan2.1-t2v-1.3b"


def _video_complete() -> bool:
    return bool(MODELS) and all(
        (Path(MODELS) / f["name"]).is_file()
        and (Path(MODELS) / f["name"]).stat().st_size == f["size"]
        for f in catalog.MODELS[VIDEO]["files"].values())


@pytest.mark.skipif(not _video_complete(), reason="il faut aussi les fichiers de Wan 2.1")
def test_my_other_machine_makes_the_video(machines):
    """Une vidéo courte (1 s, petite taille) : calculée par B, gardée par A, en WebM."""
    a, b = machines
    _, b_id = pair(a, b)
    b.wait_for(lambda: next(m for m in b.api("GET", "/images/models")["models"]
                            if m["id"] == VIDEO)["state"] == "ready",
               timeout=180, what="fichiers de Wan vérifiés chez B")

    def helpers():
        a.api("POST", f"/network/peers/{b_id}/refresh")
        return next(m for m in a.api("GET", "/images/models")["models"]
                    if m["id"] == VIDEO)["helpers"]
    a.wait_for(helpers, timeout=60, what="B proposée pour la vidéo")
    job = a.api("POST", "/images/generate",
                {"model": VIDEO, "prompt": "A paper boat drifting on a calm lake",
                 "width": 512, "height": 288, "frames": 17, "steps": 10, "seed": 3})
    assert job["here"] is False
    rows = a.wait_for(lambda: (found := a.api("GET", "/images")) and found,
                      timeout=900, what="vidéo calculée par B")
    assert rows[0]["media"] == "video" and rows[0]["machine"] == "PC fixe"
    assert rows[0]["frames"] == 17 and rows[0]["fps"] == 16
    video = requests.get(f"http://127.0.0.1:{a.port}/api/images/{rows[0]['id']}/file",
                         params={"t": a.token}, timeout=30)
    assert video.headers["content-type"] == "video/webm" and generate.is_webm(video.content)
