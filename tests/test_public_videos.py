"""Les vidéos par le réseau public (l'auteur, le 29/10/2026 : « il faut que ça soit pareil pour
les vidéos ») : la vidéo reçue, vérifiée et réécrite (imaging/webm.py), le prêteur
(network/public_video.py), le demandeur (network/public_video_ask.py), l'accord une fois, le
même que pour les images, et la création dans la conversation (chat/public_creations.py,
chat/creations.py), l'unité de la file (jobs/kinds/public_videos.py).

Comme pour les images (tests/test_public_imaging.py) : le prêteur est le vrai serveur entre
machines avec un faux moteur de vidéos (imaging/generate.video), qui rend un WebM fait comme
celui de sd-server (vu le 29/10/2026 sur deux vidéos de Wan 2.1 écrites par sd-cli : en-tête,
index, une piste VP8, une grappe par image, chaque image en image clé faite par libwebp, et
l'étiquette COMMENT qui dit la description) ; ici, les images viennent de Pillow (WebP avec
perte : une image clé VP8). Le compagnon du demandeur est le faux compagnon des images, qui
relaie au prêteur ou répond ce qu'on lui dicte. Le demandeur, dans le même processus, n'a ni
moteur ni modèle de vidéos (le téléphone de l'auteur)."""
import base64
import functools
import io
import json
import logging
import struct
import sys
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from aiodos import __version__, db, settings
from aiodos.api.server import create_app
from aiodos.chat import conversations, public_creations
from aiodos.gateway import limits
from aiodos.i18n import t
from aiodos.imaging import catalog, engine, gallery, generate, webm
from aiodos.jobs import queue
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.kinds import public_images, public_videos
from aiodos.jobs.registry import Interrupted
from aiodos.network import (lend_queue, p2p_app, placement, public, public_chat,
                            public_image_ask, public_imaging, public_lending, public_refusals,
                            public_video, public_video_ask, reputation, route)
from aiodos.network.public_chat import PublicError
from aiodos.services import ollama
from tests.test_public_imaging import (AUTH, OTHER, PC, THIRD, TOKEN, Companion, _settle,
                                       directory, lend_images, node, wait_for)
from tests.test_public_lending import FORWARD, HEADERS, HERE, KEY, FakeManager

Image = pytest.importorskip("PIL.Image")

MODEL = "wan2.1-t2v-1.3b"
SIDE = 256
FRAMES = 9                       # 4 × 2 + 1 : un nombre d'images permis par Wan 2.1
FPS = 16
SECRET = "le chat de mon voisin Hippolyte Zébulon qui danse sous la pluie"
PATH = public_video.PATH
MARKED = public_video.MARKER + MODEL
FORBIDDEN = "a naked child, " + SECRET       # comme tests/test_public_imaging.py


def params(**extra) -> dict:
    return {"model": MODEL, "prompt": SECRET, "negative": "", "width": SIDE, "height": SIDE,
            "frames": FRAMES, "steps": 20, "seed": 42, **extra}


def ask(**extra) -> dict:
    """La demande telle que le prêteur la reçoit."""
    return {"v": 1, **params(), **extra}


# ------------------------------------------------------------ une vidéo de sd-server
@functools.lru_cache(maxsize=None)
def vp8_frames(width, height, frames, uniform=False) -> tuple:
    """Les images d'une vidéo, chacune une image clé VP8 (le contenu d'un WebP avec perte,
    comme libwebp les fait pour sd-server) : un dégradé qui bouge, ou une seule couleur."""
    found = []
    x, y = np.meshgrid(np.arange(width), np.arange(height))
    for i in range(frames):
        picture = Image.new("RGB", (width, height), (40, 90, 200))
        if not uniform:
            picture = Image.fromarray(np.stack([(x * 3 + i * 7) % 256, (y * 5) % 256,
                                                (x + y) % 256], axis=-1).astype(np.uint8))
        out = io.BytesIO()
        picture.save(out, "WEBP", quality=40)
        data = out.getvalue()
        assert data[12:16] == b"VP8 "
        size = struct.unpack("<I", data[16:20])[0]
        found.append(data[20:20 + size])
    return tuple(found)


UNKNOWN = b"\x01\xff\xff\xff\xff\xff\xff\xff"


def el(ident: int, payload: bytes, unknown=False) -> bytes:
    """Un élément EBML, sa taille sur 8 octets (comme libwebm pour un segment ou une grappe)."""
    head = ident.to_bytes((ident.bit_length() + 7) // 8, "big")
    return head + (UNKNOWN if unknown else b"\x01" + len(payload).to_bytes(7, "big")) + payload


def uint(ident: int, value: int) -> bytes:
    return el(ident, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def track(number=1, kind_=1, codec="V_VP8", width=SIDE, height=SIDE, encoded=False) -> bytes:
    inner = [uint(0xD7, number), uint(0x73C5, 0xD69ECEC8E09F04), uint(0x83, kind_),
             el(0x86, codec.encode())]
    if encoded:
        inner.append(el(0x6D80, el(0x6240, uint(0x5031, 0))))
    inner.append(el(0xE0, uint(0xB0, width) + uint(0xBA, height) + uint(0x54B0, width)
                    + uint(0x54BA, height) + el(0x2383E3, struct.pack(">f", 16.0))))
    return el(0xAE, b"".join(inner))


def sd_webm(width=SIDE, height=SIDE, frames=FRAMES, comment=SECRET, uniform=False, *,
            doc="webm", tracks=None, payloads=None, laced=False, unknown=False,
            trailing=b"") -> bytes:
    """Un WebM fait comme celui de sd-server (cf. le début du module) ; les autres réglages
    le font mentir de mille façons."""
    payloads = list(payloads if payloads is not None else vp8_frames(width, height, frames,
                                                                       uniform))
    header = el(0x1A45DFA3, uint(0x4286, 1) + uint(0x42F7, 1) + uint(0x42F2, 4)
                + uint(0x42F3, 8) + el(0x4282, doc.encode()) + uint(0x4287, 4)
                + uint(0x4285, 2))
    seek = el(0x114D9B74, el(0x4DBB, el(0x53AB, bytes.fromhex("1549a966")) + uint(0x53AC, 110)))
    info = el(0x1549A966, uint(0x2AD7B1, 1_000_000) + el(0x4489, struct.pack(">f", 1000.0))
              + el(0x4D80, b"stable-diffusion.cpp") + el(0x5741, b"stable-diffusion.cpp"))
    tracks = el(0x1654AE6B, tracks if tracks is not None else track(width=width, height=height))
    tags = el(0x1254C367, el(0x7373, el(0x67C8, el(0x45A3, b"COMMENT")
                                        + el(0x4487, comment.encode()))))
    clusters = b"".join(
        el(0x1F43B675, uint(0xE7, round(i * 1000 / FPS))
           + el(0xA3, b"\x81\x00\x00" + (b"\x82" if laced and i == 1 else b"\x80") + payload),
           unknown=unknown)
        for i, payload in enumerate(payloads))
    cues = el(0x1C53BB6B, el(0xBB, uint(0xB3, 0) + el(0xB7, uint(0xF7, 1) + uint(0xF1, 300))))
    segment = el(0x18538067, seek + el(0xEC, bytes(31)) + info + tracks + tags + clusters + cues,
                 unknown=unknown)
    return header + segment + trailing


def frames_in(data: bytes) -> list:
    return webm.frames_of(data, SIDE, SIDE, FRAMES)[1]


# =================================================== la vidéo reçue (imaging/webm.py)
def test_a_video_made_like_sd_server_s_is_rewritten_without_its_tags():
    made = sd_webm()
    assert SECRET.encode() in made
    clean = webm.check(made, SIDE, SIDE, FRAMES, FPS)
    for left in (SECRET.encode(), "Zébulon".encode(), b"COMMENT", b"stable-diffusion"):
        assert left not in clean
    assert [p for _, p in frames_in(clean)] == list(vp8_frames(SIDE, SIDE, FRAMES))
    assert webm.check(clean, SIDE, SIDE, FRAMES, FPS) == clean          # stable
    assert generate.is_webm(clean)
    av = pytest.importorskip("av")
    with av.open(io.BytesIO(clean)) as container:
        stream = container.streams.video[0]
        assert container.duration == FRAMES * 1_000_000 // FPS          # la durée : 9 ÷ 16 s
        times = [round(f.time, 3) for f in container.decode(stream)]
    assert times == [round(i / FPS, 3) for i in range(FRAMES)]         # retimées à la cadence


def test_a_video_with_its_sound_is_kept_as_a_webm_without_it():
    """LTX-2.5 et MiniMax H3 : sd-server écrit leur son en PCM, que le WebM n'admet pas ;
    libwebm marque alors le fichier « matroska » (examples/common/media_io.cpp, master-1027).
    Il est lu, et seule sa piste vidéo reste, en WebM."""
    sound = el(0xAE, uint(0xD7, 2) + uint(0x73C5, 7) + uint(0x83, 2)
               + el(0x86, b"A_PCM/INT/LIT"))
    made = sd_webm(doc="matroska", tracks=track() + sound)
    # L'en-tête de libwebm, compact : sa marque dans les 64 premiers octets
    assert generate.is_webm(bytes.fromhex("1a45dfa39f4286810142f7810142f2810442f381084282")
                            + b"\x88matroska" + bytes(40))
    clean = webm.check(made, SIDE, SIDE, FRAMES, FPS)
    assert b"A_PCM" not in clean and b"matroska" not in clean and generate.is_webm(clean)
    assert [p for _, p in frames_in(clean)] == list(vp8_frames(SIDE, SIDE, FRAMES))


def test_the_segment_and_its_clusters_may_have_an_unknown_size():
    clean = webm.check(sd_webm(unknown=True), SIDE, SIDE, FRAMES, FPS)
    assert len(frames_in(clean)) == FRAMES


def test_a_video_muxed_by_ffmpeg_with_interframes_passes_and_decodes_the_same():
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    out = io.BytesIO()
    with av.open(out, "w", format="webm") as container:
        stream = container.add_stream("libvpx", rate=FPS)
        stream.width, stream.height, stream.pix_fmt = SIDE, SIDE, "yuv420p"
        for i in range(FRAMES):
            pixels = np.zeros((SIDE, SIDE, 3), np.uint8)
            pixels[:, :, 0] = np.arange(SIDE)[None, :]
            pixels[:, :, 1] = (np.arange(SIDE)[:, None] + i * 8) % 256
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    made = out.getvalue()
    clean = webm.check(made, SIDE, SIDE, FRAMES, FPS)
    assert not all(key for key, _ in frames_in(clean))                 # des images non clés

    def decoded(data):
        with av.open(io.BytesIO(data)) as container:
            return [f.to_ndarray(format="rgb24") for f in container.decode(video=0)]
    assert all(np.array_equal(a, b) for a, b in zip(decoded(made), decoded(clean), strict=True))
    assert b"Lavf" not in clean


def _interframe(payload: bytes) -> bytes:
    return bytes([payload[0] | 0x01]) + payload[1:]


REFUSED_FILES = [
    ("autre taille", sd_webm(), (SIDE + 16, SIDE), FRAMES),
    ("moins d'images", sd_webm(frames=FRAMES - 4), (SIDE, SIDE), FRAMES),
    ("plus d'images", sd_webm(frames=FRAMES + 4), (SIDE, SIDE), FRAMES),
    ("ni WebM ni Matroska", sd_webm(doc="avi"), (SIDE, SIDE), FRAMES),
    ("deux pistes vidéo", sd_webm(tracks=track() + track(number=2)), (SIDE, SIDE), FRAMES),
    ("aucune piste vidéo", sd_webm(tracks=track(kind_=2)), (SIDE, SIDE), FRAMES),
    ("un autre codec", sd_webm(tracks=track(codec="V_MPEG4/ISO/AVC")), (SIDE, SIDE), FRAMES),
    ("piste chiffrée", sd_webm(tracks=track(encoded=True)), (SIDE, SIDE), FRAMES),
    ("piste d'une autre taille", sd_webm(tracks=track(width=512)), (SIDE, SIDE), FRAMES),
    ("images regroupées", sd_webm(laced=True), (SIDE, SIDE), FRAMES),
    ("première image non clé", sd_webm(payloads=[_interframe(vp8_frames(SIDE, SIDE, FRAMES)[0]),
                                                 *vp8_frames(SIDE, SIDE, FRAMES)[1:]]),
     (SIDE, SIDE), FRAMES),
    ("image clé d'une autre taille", sd_webm(payloads=[vp8_frames(512, 256, 1)[0],
                                                       *vp8_frames(SIDE, SIDE, FRAMES)[1:]]),
     (SIDE, SIDE), FRAMES),
    ("image clé illisible", sd_webm(payloads=[b"\x00" * 40, *vp8_frames(SIDE, SIDE, FRAMES)[1:]]),
     (SIDE, SIDE), FRAMES),
    ("quelque chose après", sd_webm(trailing=b"\x1a\x45\xdf\xa3\x80"), (SIDE, SIDE), FRAMES),
    ("tronquée", sd_webm()[:-100], (SIDE, SIDE), FRAMES),
    ("coupée au milieu", sd_webm()[:3000], (SIDE, SIDE), FRAMES),
    ("vide", b"", (SIDE, SIDE), FRAMES),
    ("pas une vidéo", b"GIF89a" + bytes(4000), (SIDE, SIDE), FRAMES),
]


@pytest.mark.parametrize("name, data, size, frames", REFUSED_FILES,
                         ids=[case[0] for case in REFUSED_FILES])
def test_what_is_not_the_asked_video_is_refused(name, data, size, frames):
    with pytest.raises(ValueError):
        webm.check(data, *size, frames, FPS)
    with pytest.raises(ValueError):
        webm.check(data, *size, frames, FPS, deep=False)


def test_a_flood_of_elements_or_bytes_is_refused_quickly(monkeypatch):
    made = sd_webm()
    header = made[:made.index(bytes.fromhex("18538067"))]
    voids = el(0xEC, b"") * (webm.MAX_ELEMENTS + 10)          # des vides sans fin, pas d'images
    started = time.monotonic()
    with pytest.raises(ValueError):
        webm.check(header + el(0x18538067, voids), SIDE, SIDE, FRAMES, FPS)
    assert time.monotonic() - started < 5
    monkeypatch.setattr(webm, "MAX_BYTES", len(made) - 1)
    with pytest.raises(ValueError):
        webm.check(made, SIDE, SIDE, FRAMES, FPS)


def test_a_single_colour_video_is_refused_once_decoded():
    pytest.importorskip("av")
    uniform = sd_webm(uniform=True)
    with pytest.raises(ValueError):
        webm.check(uniform, SIDE, SIDE, FRAMES, FPS)
    assert webm.check(uniform, SIDE, SIDE, FRAMES, FPS, deep=False)     # la structure, elle, va


def test_without_pyav_the_structure_is_still_checked(monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)                       # import av : ImportError
    assert webm.check(sd_webm(uniform=True), SIDE, SIDE, FRAMES, FPS)  # pas décodée
    with pytest.raises(ValueError):
        webm.check(sd_webm()[:-100], SIDE, SIDE, FRAMES, FPS)


# ============================================================ le faux moteur
def _sleep(seconds, stop):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            raise Interrupted()
        time.sleep(0.02)


class Vd:
    """Le faux moteur de vidéos (imaging/generate.video) : ce qu'il a reçu ; `load` secondes
    de chargement, `during()`, puis `compute` secondes de calcul ; il respecte l'arrêt. made :
    le WebM rendu (une exception : levée ; None : celui de sd-server, la description dans son
    étiquette COMMENT)."""

    def __init__(self):
        self.calls, self.load, self.compute = [], 0.0, 0.0
        self.loaded, self.made, self.during = None, None, None

    def __call__(self, params, stop=None, waiting=None):
        self.calls.append(dict(params))
        _sleep(self.load, stop)
        self.loaded = params["model"]
        if self.during is not None:
            self.during()
        _sleep(self.compute, stop)
        if stop is not None and stop.is_set():
            raise Interrupted()
        if isinstance(self.made, Exception):
            raise self.made
        made = self.made if self.made is not None else sd_webm(
            params["width"], params["height"], params["frames"], comment=params["prompt"])
        return {"webm": made, "frames": params["frames"], "fps": FPS, "seconds": 0.4}


@pytest.fixture
def vd(monkeypatch):
    fake = Vd()
    monkeypatch.setattr(generate, "video", fake)
    monkeypatch.setattr(engine, "loaded", lambda: fake.loaded)
    # Le modèle complet chez le prêteur seulement (le demandeur, dans le même processus, n'a
    # ni moteur ni modèle : kind.runnable_here reste faux)
    monkeypatch.setattr(public_video, "_ready", lambda model: model == MODEL)
    return fake


@pytest.fixture
def lender(vd):
    manager = FakeManager()
    app = p2p_app.create_p2p_app(manager)
    yield manager, TestClient(app, client=HERE, base_url=FORWARD), TestClient(app), vd
    public_lending.forget()
    assert lend_queue.idle()                          # aucune place oubliée


def post(http, body=None, headers=HEADERS):
    return http.post(PATH, json=ask() if body is None else body, headers=headers)


def events(http, body=None) -> list:
    with http.stream("POST", PATH, json=ask() if body is None else body,
                     headers=HEADERS) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.iter_lines() if line]


def video_of(found) -> tuple:
    header = next(e for e in found if e["type"] == "video")
    return header, b"".join(base64.b64decode(e["data"]) for e in found if e["type"] == "part")


# =================================================================== le prêteur
def test_the_same_settings_as_images():
    """Un seul réglage pour prêter, un seul accord pour demander (cf. network/public_video.py,
    chat/public_creations.py) ; des bornes à la mesure d'une vidéo."""
    assert "lend_videos_public" not in settings.DEFAULTS
    assert "public_videos" not in settings.DEFAULTS
    assert settings.DEFAULTS["lend_images_public"] is True
    assert "lend_images_public" in settings.OPEN_FROM_THE_START
    assert public_video.MARKER == "image:" and public_video.MARKER in placement.RESERVED
    spec = catalog.model(MODEL)
    # MiniMax H3 aussi, malgré sa licence restrictive (décision de l'auteur du 29/10/2026)
    assert public_video.lendable(MODEL) and public_video.lendable("minimax-h3")
    assert not public_video.lendable("z-image-turbo")
    assert all(public_video.fits(spec, w, h) for w, h, _ in spec["sizes"])     # ses formats
    assert all(public_video.frames_allowed(spec, n) for n, _ in spec["durations"])  # ses durées
    assert public_video.max_steps(spec) == spec["steps"] == 20
    assert "video" in public_lending.CONTENT


def test_the_route_is_unknown_without_a_believable_key(lender):
    manager, here, lan, vd = lender
    lend_images()
    assert post(lan).status_code == 404                        # en-tête venu d'ailleurs
    assert post(here, headers={}).status_code == 404
    assert post(here, headers={"X-Aiodos-Peer": KEY}).status_code == 404
    assert vd.calls == []
    assert post(here).status_code == 200 and len(vd.calls) == 1


def test_not_lending_without_both_settings_never_on_battery(lender, monkeypatch):
    manager, here, lan, vd = lender
    assert post(here).json() == {"error": "not_lending"}       # tout éteint (essais)
    lend_images()
    assert post(here).status_code == 200
    settings.set_value("lend_images_public", False)            # le même réglage que les images
    response = post(here)
    assert response.status_code == 503 and response.json() == {"error": "not_lending"}
    lend_images()
    settings.set_value("lend_public", False)
    assert post(here).json() == {"error": "not_lending"}
    lend_images()
    settings.set_value("share_on_ac_only", False)
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "battery")
    assert post(here).json() == {"error": "not_lending"}
    assert len(vd.calls) == 1


def test_every_bound_is_checked_before_anything_is_taken(lender, monkeypatch):
    """Modèle, description, taille, nombre d'images, pas, graine, champs : refusés avant tout
    calcul, sans prendre la place ni le seau de tout le public ; pas d'image de départ."""
    manager, here, lan, vd = lender
    lend_images()
    monkeypatch.setattr(limits, "PER_MINUTE", 2)
    refused = [
        (ask(model="inconnu"), 404, "no_model"),
        (ask(model="z-image-turbo"), 404, "no_model"),         # une image : sa route à elle
        (ask(model="minimax-h3"), 404, "no_model"),            # pas sur cette machine
        (ask(prompt="x" * (public_video.MAX_PROMPT + 1)), 413, "too_big"),
        (ask(negative="x" * (public_video.MAX_PROMPT + 1)), 413, "too_big"),
        (ask(init_image="data:image/png;base64,AAAA"), 413, "too_big"),
        (ask(strength=0.5), 413, "too_big"),
        (ask(fps=60), 413, "too_big"),                         # la cadence : celle du modèle
        (ask(width=300), 413, "too_big"),                      # pas un multiple de 16
        (ask(width=240), 413, "too_big"),                      # sous la borne du modèle
        (ask(width=832, height=832), 413, "too_big"),          # plus de 864 × 480 pixels
        (ask(frames=10), 413, "too_big"),                      # pas 4k + 1
        (ask(frames=85), 413, "too_big"),                      # plus de 5 s
        (ask(frames=161), 413, "too_big"),                     # permis ici, pas pour un inconnu
        (ask(steps=21), 413, "too_big"),                       # les 20 pas du modèle, au plus
        (ask(steps=0), 400, "bad_request"),
        (ask(frames=0), 400, "bad_request"),
        (ask(frames=True), 400, "bad_request"),
        (ask(frames=9.0), 400, "bad_request"),
        (ask(seed=-1), 400, "bad_request"),
        (ask(seed=2 ** 31), 400, "bad_request"),
        (ask(prompt="   "), 400, "bad_request"),
        (ask(negative=3), 400, "bad_request"),
        (ask(v=2), 400, "bad_request"),
        (ask(model=None), 400, "bad_request"),
    ]
    for body, status, code in refused * 2:
        response = post(here, body)
        assert (response.status_code, response.json()) == (status, {"error": code}), body
    response = here.post(PATH, content=b"[[[[", headers=HEADERS)
    assert response.json() == {"error": "bad_request"}
    assert vd.calls == [] and lend_queue.idle()
    assert post(here).status_code == 200
    assert post(here).status_code == 200
    assert post(here).json() == {"error": "busy"}              # les vraies, elles, comptent
    assert len(vd.calls) == 2


def test_a_video_is_answered_in_ndjson_rewritten(lender, monkeypatch):
    manager, here, lan, vd = lender
    lend_images()
    monkeypatch.setattr(public_video, "PART", 700)
    found = events(here, ask(negative="flou"))
    assert found[0] == {"type": "meta", "model": MODEL, "version": __version__}
    header, data = video_of(found)
    clean = webm.check(sd_webm(), SIDE, SIDE, FRAMES, FPS, deep=False)
    assert header == {"type": "video", "format": "webm", "bytes": len(clean), "width": SIDE,
                      "height": SIDE, "frames": FRAMES, "fps": FPS, "seconds": 0.4}
    assert data == clean and SECRET.encode() not in data       # sans l'étiquette COMMENT
    parts = [e for e in found if e["type"] == "part"]
    assert len(parts) == -(-len(clean) // 700) > 1
    assert found[-1] == {"type": "done"}
    # Le guidage du modèle, jamais celui de la demande ; sa consigne négative passe (Wan en a)
    assert vd.calls == [{"model": MODEL, "prompt": SECRET, "negative": "flou", "width": SIDE,
                         "height": SIDE, "frames": FRAMES, "steps": 20, "seed": 42,
                         "cfg": catalog.model(MODEL)["cfg"]}]
    assert manager.executor.interactive == 0 and lend_queue.idle()


def test_signs_of_life_for_minutes_then_the_video(lender, monkeypatch):
    """Une vidéo prend des minutes : un signe de vie toutes les WAIT_S s, « loading » puis
    « computing », jusqu'à l'en-tête de la vidéo ; le chargement ne compte pas dans la pause."""
    manager, here, lan, vd = lender
    lend_images(power_share=60)
    counted = []
    monkeypatch.setattr(public_lending, "_rest_after", lambda m, seconds: counted.append(seconds))
    monkeypatch.setattr(public_lending, "WAIT_S", 0.1)
    vd.load, vd.compute = 0.6, 1.2
    found = events(here)
    stages = [e["stage"] for e in found if e["type"] == "wait"]
    assert stages[0] == "loading" and stages[-1] == "computing" and len(stages) >= 12
    first = [e["type"] for e in found].index("video")
    assert all(e["type"] != "wait" for e in found[first:])
    assert found[-1] == {"type": "done"}
    assert len(counted) == 1 and 1.0 <= counted[0] < 1.9


def test_one_place_for_the_whole_public(lender, monkeypatch):
    """Une vidéo en cours : une autre vidéo, ou une image, est refusée."""
    manager, here, lan, vd = lender
    lend_images()
    monkeypatch.setattr(public_imaging, "_ready", lambda model: model == "z-image-turbo")
    image = {"v": 1, "model": "z-image-turbo", "prompt": "un phare", "negative": "",
             "width": 512, "height": 512, "steps": 8, "seed": 1}
    seen = {}
    vd.during = lambda: seen.update(video=post(here), free=public_lending.free(manager),
                                    image=here.post("/public/v1/image", json=image,
                                                    headers=HEADERS))
    found = events(here)
    assert seen["video"].json() == {"error": "busy"} and seen["image"].json() == {"error": "busy"}
    assert seen["free"] is False
    assert found[-1] == {"type": "done"} and len(vd.calls) == 1


def test_the_user_comes_back_and_the_video_stops(lender):
    manager, here, lan, vd = lender
    lend_images()
    vd.compute = 5.0
    vd.during = manager.executor.interactive_begin
    started = time.monotonic()
    found = events(here)
    assert found[-1] == {"type": "error", "error": "busy"}
    assert all(e["type"] != "video" for e in found)
    assert time.monotonic() - started < 3 and lend_queue.idle()
    manager.executor.interactive_end()


def test_an_engine_failure_or_a_wrong_video_is_a_code(lender, monkeypatch, caplog):
    manager, here, lan, vd = lender
    lend_images()
    with caplog.at_level(logging.DEBUG):
        vd.made = RuntimeError(f"sd-server a lu « {SECRET} » puis s'est arrêté")
        assert events(here)[-2:] == [{"type": "meta", "model": MODEL, "version": __version__},
                                     {"type": "error", "error": "engine"}]
        vd.made = b"pas un webm"
        assert events(here)[-1] == {"type": "error", "error": "engine"}
        vd.made = sd_webm(frames=FRAMES + 4)                   # pas celle demandée
        assert events(here)[-1] == {"type": "error", "error": "engine"}
        vd.made = None
        monkeypatch.setattr(public_video, "MAX_VIDEO", 1000)   # trop grosse
        assert events(here)[-1] == {"type": "error", "error": "engine"}
    assert all("Zébulon" not in r.getMessage() for r in caplog.records)
    assert lend_queue.idle()


def test_nothing_is_kept_neither_on_disk_nor_in_the_base_nor_in_the_log(lender, caplog,
                                                                         data_dir):
    manager, here, lan, vd = lender
    lend_images()
    with caplog.at_level(logging.DEBUG):
        found = events(here)
    header, made = video_of(found)
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"Vidéo pour le réseau public : {MODEL}, {SIDE} × {SIDE}, "
                            f"{FRAMES} images, 20 pas, ") for m in said)
    for message in said:
        assert "Zébulon" not in message and KEY not in message
    for row in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        for stored in db.query(f'SELECT * FROM "{row["name"]}"'):
            text = repr(tuple(stored))
            assert "Zébulon" not in text and KEY not in text, row["name"]
    sample = vp8_frames(SIDE, SIDE, FRAMES)[3][100:300]       # un morceau d'une image
    assert sample in made
    for path in data_dir.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert "Zébulon".encode() not in content and KEY.encode() not in content, path
            assert sample not in content, path
    assert public_lending.serving() == []


def test_the_lender_refuses_a_forbidden_description_before_anything(lender, monkeypatch,
                                                                     caplog):
    manager, here, lan, vd = lender
    lend_images()
    monkeypatch.setattr(limits, "PER_MINUTE", 1)        # un refus qui en prendrait : « busy »
    with caplog.at_level(logging.DEBUG):
        for body in (ask(prompt=FORBIDDEN), ask(prompt="ma voisine nue, " + SECRET),
                     ask(negative="clothes, adult", prompt="a child at the beach")):
            response = post(here, body)
            assert (response.status_code, response.json()) == (403, {"error": "refused_content"})
    assert vd.calls == [] and lend_queue.idle() and public_lending.serving() == []
    said = [r.getMessage() for r in caplog.records]
    assert "Vidéo refusée par le filtre (minor_sexual)" in said
    for message in said:
        assert "Zébulon" not in message and "naked" not in message and KEY not in message
    assert post(here).status_code == 200 and len(vd.calls) == 1   # place et seau intacts


def test_the_public_profile_announces_the_lent_video_models(monkeypatch):
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": "qwen3.5:9b"}])
    monkeypatch.setattr(public_video, "_ready", lambda model: model == MODEL)
    assert public._models() == ["qwen3.5:9b"]                # rien de prêté (essais)
    settings.set_value("lend_images_public", True)
    assert public._models() == ["qwen3.5:9b", MARKED]
    # Un modèle à licence restrictive aussi (MiniMax H3, LTX-2.5), complet ici : décision de
    # l'auteur du 29/10/2026 (qui le prête a un avertissement, api/routes/images.lend_warnings)
    monkeypatch.setattr(public_video, "_ready", lambda model: True)
    videos = [m for m, spec in catalog.MODELS.items() if spec.get("kind") == "video"]
    assert public._models() == ["qwen3.5:9b"] + [public_video.MARKER + m for m in videos]
    assert "image:minimax-h3" in public._models() and MARKED in public._models()
    assert all(len(public_video.MARKER + m) <= public.MAX_TEXT for m in catalog.MODELS)


# ================================================================== le demandeur
@pytest.fixture
def companion(lender):
    manager, here, lan, vd = lender
    fake = Companion(here)
    route.attach(fake)
    placement.forget()
    yield fake
    route.detach(fake)
    placement.forget()
    fake.close()


@pytest.fixture
def opened(monkeypatch):
    """Le réseau public rejoint et mes créations confiables aux inconnus (activés par défaut,
    éteints dans les essais) ; aucun modèle de conversation ici (le téléphone de l'auteur)."""
    settings.set_many({"public_network": True, "use_public": True})
    monkeypatch.setattr(placement.android, "is_android", lambda: False)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [])
    monkeypatch.setattr(ollama, "capabilities_many", lambda names: {})


def good(made=None, model=MODEL, side=SIDE, frames=FRAMES) -> list:
    """Les événements d'une vidéo honnête (cf. public_video.py)."""
    made = made if made is not None else sd_webm(side, side, frames)
    return [(0, {"type": "meta", "model": model, "version": "1.0.0"}),
            (0, {"type": "video", "format": "webm", "bytes": len(made), "width": side,
                 "height": side, "frames": frames, "fps": FPS, "seconds": 95.5}),
            (0, {"type": "part", "data": base64.b64encode(made).decode()}),
            (0, {"type": "done"})]


def test_lenders_are_those_that_lend_this_video_model(companion, opened):
    directory(node(PC, "qwen3.5:9b", MARKED, tps=50),
              node(OTHER, "image:z-image-turbo", tps=90),             # des images seulement
              node(THIRD, MARKED, free=0),                             # pas de place
              node("d4" * 32, MARKED, lends=False),                    # ne prête pas
              node("e5" * 32, "image:inconnu", "video:" + MODEL),      # ni l'un ni l'autre
              node("f6" * 32, MARKED, "image:z-image-turbo", tps=10))
    assert public_video_ask.offered_models() == [MODEL]
    assert public_video_ask.lenders(MODEL) == [(PC, MODEL), ("f6" * 32, MODEL)]
    # Les images ne voient pas les vidéos, ni l'inverse
    assert public_image_ask.offered_models() == ["z-image-turbo"]
    assert [key for key, _ in public_image_ask.lenders("z-image-turbo")] == [OTHER, "f6" * 32]
    with placement._occupy(public_chat.model_id(PC, MARKED), None):   # une vidéo en cours
        assert public_video_ask.lenders(MODEL) == [("f6" * 32, MODEL)]
    companion.features.clear()                   # un compagnon d'avant : personne
    assert public_video_ask.offered_models() == []


def test_a_video_model_is_never_taken_for_a_conversation_model(companion, opened):
    """Sous la marque des images : réservée partout depuis le 29/10/2026 au matin. Sans elle,
    « 1.3b » se lirait comme la taille d'un modèle de conversation."""
    directory(node(PC, MARKED, "qwen3.5:0.8b"))
    assert public_chat.params(MARKED) == 1.3 and placement.reserved(MARKED)
    assert [c.model for c in placement.best_public()] == [public_chat.model_id(PC,
                                                                                "qwen3.5:0.8b")]


def test_the_request_stays_within_the_lender_bounds():
    data = json.loads(public_video_ask.body(params(init_path="/tmp/x.png", strength=0.5)))
    assert data == ask()                          # ni image de départ, ni force
    for wrong, code in ((params(frames=85), "too_big"), (params(frames=161), "too_big"),
                        (params(frames=10), "too_big"), (params(steps=21), "too_big"),
                        (params(width=832, height=832), "too_big"),
                        (params(prompt="x" * 2001), "too_big"),
                        (params(model="z-image-turbo"), "bad_request"),
                        (params(model="inconnu"), "bad_request"),
                        (params(prompt=" "), "bad_request"), (params(frames="long"), "bad_request"),
                        (params(prompt=FORBIDDEN), "refused_content")):
        with pytest.raises(PublicError) as found:
            public_video_ask.body(wrong)
        assert found.value.code == code, wrong


def test_a_video_goes_through_the_companion_and_comes_back_checked(companion, opened):
    directory(node(PC, MARKED))
    lend_images()
    made = public_video_ask.video(PC, params(), conversation="creation-aaaaaaaaaaaa")
    assert made["frames"] == FRAMES and made["fps"] == FPS and made["seconds"] == 0.4
    assert webm.check(made["webm"], SIDE, SIDE, FRAMES, FPS) == made["webm"]
    assert SECRET.encode() not in made["webm"]
    sent = companion.requests[-1]
    assert (sent["key"], sent["tail"], sent["tag"]) == (PC, "public/v1/video",
                                                         "creation-aaaaaaaaaaaa")
    assert sent["body"] == ask()
    assert reputation.get(PC)["served"] == 1 and not reputation.benched(PC)
    public_video_ask.video(PC, params())                       # sans identifiant : une clé jetable
    assert companion.requests[-1]["tag"] != "creation-aaaaaaaaaaaa"
    assert len(set(companion.callers)) == 2


BAD_LENDERS = [
    ("autre taille", [good()[0], (0, {"type": "video", "format": "webm", "bytes": 10,
                                      "width": 512, "height": 512, "frames": FRAMES,
                                      "seconds": 1})], "unreadable"),
    ("autre durée annoncée", [good()[0], (0, {"type": "video", "format": "webm", "bytes": 10,
                                              "width": SIDE, "height": SIDE, "frames": 81,
                                              "seconds": 1})], "unreadable"),
    ("moins d'images qu'annoncé", good(sd_webm(frames=5)), "unreadable"),
    ("pas un webm", good(b"GIF89a" + bytes(4000)), "unreadable"),
    ("une image", [good()[0], (0, {"type": "image", "format": "png", "bytes": 10,
                                   "width": SIDE, "height": SIDE, "seconds": 1})], "cut"),
    ("tronquée", good()[:2] + [(0, {"type": "part", "data": base64.b64encode(
        sd_webm()[:-30]).decode()}), (0, {"type": "done"})], "unreadable"),
    ("plus longue qu'annoncée", good()[:3] + [(0, {"type": "part", "data": "AAAA"}),
                                              (0, {"type": "done"})], "unreadable"),
    ("trop grosse", [good()[0], (0, {"type": "video", "format": "webm",
                                     "bytes": public_video.MAX_VIDEO + 1, "width": SIDE,
                                     "height": SIDE, "frames": FRAMES, "seconds": 1})],
     "too_long"),
    ("un autre modèle", good(model="autre-modele"), "unreadable"),
    ("base64 illisible", good()[:2] + [(0, {"type": "part", "data": "%%%"})], "unreadable"),
    ("moteur arrêté", [good()[0], (0, {"type": "error", "error": "engine"})], "engine"),
    ("coupée", good()[:3], "cut"),
]


@pytest.mark.parametrize("name, script, code", BAD_LENDERS,
                         ids=[case[0] for case in BAD_LENDERS])
def test_a_lender_that_returns_anything_but_the_asked_video_is_set_aside(
        companion, opened, name, script, code):
    directory(node(PC, MARKED))
    companion.script[PC] = ("events", script)
    with pytest.raises(PublicError) as found:
        public_video_ask.video(PC, params())
    assert found.value.code == code, name
    assert reputation.benched(PC), name
    assert public_video_ask.lenders(MODEL) == []               # écartée : plus candidate


def test_a_single_colour_video_sets_the_lender_aside(companion, opened):
    pytest.importorskip("av")
    directory(node(PC, MARKED))
    companion.script[PC] = ("events", good(sd_webm(uniform=True)))
    with pytest.raises(PublicError) as found:
        public_video_ask.video(PC, params())
    assert found.value.code == "unreadable" and reputation.benched(PC)


def test_signs_of_life_keep_a_long_video_waiting_but_the_total_is_bounded(companion, opened,
                                                                          monkeypatch):
    directory(node(PC, MARKED))
    monkeypatch.setattr(public_video_ask, "FIRST_EVENT_S", 1)
    monkeypatch.setattr(public_video_ask, "ALIVE_GRACE_S", 0.6)
    meta, *rest = good()
    waits = [(0.3, {"type": "wait", "stage": "loading"})] * 3 \
        + [(0.3, {"type": "wait", "stage": "computing"})] * 9
    companion.script[PC] = ("events", [meta, *waits, *rest])        # 3,6 s : vivant
    assert public_video_ask.video(PC, params())["seconds"] == 95.5
    # Des signes de vie sans fin : coupée à TOTAL_S, et écartée
    monkeypatch.setattr(public_video_ask, "TOTAL_S", 1.5)
    companion.script[PC] = ("events", [meta, *waits, *rest])
    started = time.monotonic()
    with pytest.raises(PublicError) as found:
        public_video_ask.video(PC, params())
    assert found.value.code == "too_long" and time.monotonic() - started < 3
    assert reputation.benched(PC)
    reputation.forget()
    # Muette : coupée à l'échéance du premier événement
    monkeypatch.setattr(public_video_ask, "TOTAL_S", 60)
    companion.script[PC] = ("events", [meta, (3, {"type": "wait", "stage": "loading"})])
    started = time.monotonic()
    with pytest.raises(PublicError) as found:
        public_video_ask.video(PC, params())
    assert found.value.code == "slow" and time.monotonic() - started < 2.5


def test_stopping_cuts_the_video_without_blaming_the_lender(companion, opened):
    directory(node(PC, MARKED))
    meta, *rest = good()
    companion.script[PC] = ("events", [meta, (2, {"type": "wait", "stage": "computing"}), *rest])
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    assert public_video_ask.video(PC, params(), stop=stop) is None
    assert not reputation.benched(PC)


def test_a_lender_filter_refusal_is_never_held_against_it(companion, opened):
    directory(node(PC, MARKED, tps=90), node(OTHER, MARKED, tps=10))
    companion.script[PC] = ("refuse", 403, "refused_content")
    for _ in range(public_refusals.LIMIT + 1):
        assert public_video_ask.lenders(MODEL)[0] == (PC, MODEL)
        with pytest.raises(PublicError) as found:
            public_video_ask.video(PC, params())
        assert found.value.code == "refused_content"
    assert not reputation.benched(PC) and public_refusals.count(PC) == 0


# ================================================== l'accord et la création
@pytest.fixture
def ui(companion, opened):
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c
        _settle()


def create(ui, text="Crée une vidéo d'un chat roux", **extra):
    """Une vidéo petite et courte, pour des essais rapides (le format et la durée choisis)."""
    return ui.post("/api/chat/create", headers=AUTH, json={
        "text": text, "model": MODEL, "width": SIDE, "height": SIDE, "frames": FRAMES, **extra})


def payload(**extra) -> dict:
    return {"model": MODEL, "prompt": "un renard dans la neige", "negative": "", "width": SIDE,
            "height": SIDE, "frames": FRAMES, "steps": 20, "cfg": 6.0, "batch": "lot000000001",
            "seed": 5, "index": 0, "public": True, "conversation_id": None, **extra}


def test_the_consent_is_asked_once_then_the_video_comes_from_the_public_network(ui, companion):
    directory(node(PC, MARKED))
    lend_images()
    reply = create(ui)                                    # pas encore demandé : la carte
    assert reply.status_code == 409
    assert reply.json()["detail"] == t("public_videos.ask") and reply.json()["public"] == "ask"
    assert "vidéo" in reply.json()["detail"] and "inconnu" in reply.json()["detail"]
    settings.set_value("public_images", False)            # Non : gardé, la raison dite
    reply = create(ui)
    assert reply.json()["detail"] == t("public_videos.closed_refused")
    assert reply.json()["public"] is None
    assert conversations.list_tree()["conversations"] == [] and companion.requests == []
    assert ui.put("/api/settings", headers=AUTH, json={"public_images": True}).json()[
        "public_images"] is True                          # Oui : le même accord que les images
    made = create(ui).json()
    media = made["media"]
    assert media["public"] is True and media["model"] == MODEL and media["kind"] == "video"
    units = db.query("SELECT task, resource, local_only FROM units WHERE job_id=?",
                     (made["job_id"],))
    assert [tuple(u) for u in units] == [(public_videos.TASK, "net", 1)]
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    rows = gallery.batch(media["batch"])
    assert len(rows) == 1 and rows[0]["machine"] == t("public_images.machine")
    assert rows[0]["media"] == "video" and rows[0]["frames"] == FRAMES and rows[0]["fps"] == FPS
    kept = gallery.path_of(rows[0])
    assert kept.suffix == ".webm"
    data = kept.read_bytes()
    assert webm.check(data, SIDE, SIDE, FRAMES, FPS) == data
    assert b"chat roux" not in data and b"COMMENT" not in data      # ni sa description
    conv = ui.get(f"/api/conversations/{made['conversation_id']}", headers=AUTH).json()
    answer = conv["messages"][-1]["media"]
    assert answer["public"] is True and len(answer["images"]) == 1
    sent = [r for r in companion.requests if r["tail"] == "public/v1/video"]
    assert len(sent) == 1 and sent[0]["body"]["prompt"] == "un chat roux"
    assert set(sent[0]["body"]) == {"v", "model", "prompt", "negative", "width", "height",
                                    "frames", "steps", "seed"}
    assert len(companion.requests) == 1                   # rien d'autre n'est parti


def test_a_yes_given_for_images_covers_videos(ui, companion):
    directory(node(PC, MARKED))
    lend_images()
    settings.set_value("public_images", True)             # dit oui pour une image, ce matin
    made = create(ui).json()
    assert made["media"]["public"] is True
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    assert [r["tail"] for r in companion.requests] == ["public/v1/video"]


def test_mine_only_and_the_settings_keep_videos_off_the_public_network(ui, companion):
    directory(node(PC, MARKED))
    lend_images()
    settings.set_value("public_images", True)
    reply = create(ui, mine_only=True)                     # une conversation neuve
    assert reply.status_code == 409
    assert reply.json()["detail"] == t("public_videos.closed_mine_only")
    conv_id = conversations.create(mine_only=True)         # une conversation qui le garde
    assert create(ui, conversation_id=conv_id).json()["detail"] == t(
        "public_videos.closed_mine_only")
    settings.set_value("use_public", False)
    assert create(ui).json()["detail"] == t("public_videos.closed_off")
    settings.set_value("use_public", True)
    choice = public_creations.choose(MODEL, False, [{"kind": "image", "id": "x"}], "video")
    assert choice == public_creations.Choice(message=t("public_videos.closed_attachments"))
    assert companion.requests == []


def test_nobody_lends_a_video_model(ui, companion):
    directory(node(PC, "image:z-image-turbo"))             # des images seulement
    reply = create(ui)
    assert reply.json()["detail"] == t("chat_create.nothing_ready_videos")   # comme avant
    settings.set_value("public_images", True)
    assert create(ui).json()["detail"] == t("public_videos.nobody")      # le réseau public non plus
    assert companion.requests == []


def test_a_video_too_big_for_the_public_network_is_refused_here(ui, companion):
    directory(node(PC, MARKED))
    lend_images()
    settings.set_value("public_images", True)
    for extra in ({"frames": 161}, {"width": 1024, "height": 576}):
        reply = create(ui, **extra)
        assert reply.status_code == 400 and reply.json()["detail"] == t("public_videos.too_big")
    assert companion.requests == []


def test_a_forbidden_video_description_is_refused_and_nothing_is_sent(ui, companion):
    directory(node(PC, MARKED))
    lend_images()
    settings.set_value("public_images", True)
    for text in ("Crée une vidéo d'un enfant nu", "Crée une vidéo d'une ado en lingerie"):
        reply = create(ui, text)
        assert reply.status_code == 403
        assert reply.json()["detail"] == t("public_videos.refused_content")
    assert conversations.list_tree()["conversations"] == [] and companion.requests == []


def test_the_unit_rechecks_the_consent_and_tries_the_next_lender(companion, opened):
    directory(node(PC, MARKED, tps=90), node(OTHER, MARKED, tps=10))
    lend_images()
    settings.set_value("public_images", True)
    conv_id = conversations.create()
    conversations.set_mine_only(conv_id, True)              # changé depuis la demande
    with pytest.raises(ValueError, match="ne partent plus"):
        public_videos.compute(payload(conversation_id=conv_id), None, {}, threading.Event())
    with pytest.raises(ValueError, match="rien n'est parti"):   # la base du filtre, avant tout
        public_videos.compute(payload(prompt=FORBIDDEN), None, {}, threading.Event())
    assert companion.requests == []
    companion.script[PC] = ("events", good(sd_webm(frames=5)))   # pas la vidéo demandée
    done = public_videos.compute(payload(), None, {}, threading.Event())
    assert [r["key"] for r in companion.requests] == [PC, OTHER]
    assert done["public"] is True and reputation.benched(PC) and not reputation.benched(OTHER)
    row = gallery.get(done["image_id"])
    assert row["media"] == "video" and row["machine"] == t("public_images.machine")
    # Le filtre d'un prêteur refuse : aucune autre machine
    companion.script[OTHER] = ("refuse", 403, "refused_content")
    companion.requests.clear()
    with pytest.raises(ValueError, match="filtre la juge interdite"):
        public_videos.compute(payload(index=1), None, {}, threading.Event())
    assert [r["key"] for r in companion.requests] == [OTHER]
    # Arrêtée avant de commencer : rendue, rien d'envoyé
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(Interrupted):
        public_videos.compute(payload(), None, {}, stopped)
    assert len(companion.requests) == 1


def test_two_videos_for_one_lender_wait_their_turn(ui, companion, vd, monkeypatch):
    """« Deux vidéos » et un seul prêteur, qui n'a qu'une place : la seconde attend la fin de
    la première (public_images.in_turn), au lieu d'échouer aussitôt faute de place."""
    monkeypatch.setattr(public_images, "TURN_S", 0.1)
    directory(node(PC, MARKED))
    lend_images()
    settings.set_value("public_images", True)
    vd.compute = 0.6
    made = create(ui, "Crée deux vidéos d'un renard dans la neige").json()
    assert made["media"]["count"] == 2
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] in ("done", "failed"))
    assert queue.job_summary(made["job_id"])["status"] == "done"
    assert len(gallery.batch(made["media"]["batch"])) == 2
    assert [r["tail"] for r in companion.requests] == ["public/v1/video"] * 2
    assert len(vd.calls) == 2 and not reputation.benched(PC)


def test_a_unit_waits_its_turn_only_while_someone_lends_the_model(companion, opened,
                                                                  monkeypatch):
    monkeypatch.setattr(public_images, "TURN_S", 0.05)
    halt = threading.Event()
    assert public_images.in_turn(public_video_ask, MODEL, halt) == ([], None)   # personne
    directory(node(PC, MARKED), node(OTHER, "image:z-image-turbo"))
    for asker, name, key in ((public_video_ask, MODEL, PC),
                             (public_image_ask, "z-image-turbo", OTHER)):
        busy = placement._occupy(public_chat.model_id(key, asker.MARKER + name), None)
        busy.__enter__()                                  # une autre de mes créations, en cours
        threading.Timer(0.3, busy.__exit__, (None, None, None)).start()
        started = time.monotonic()
        found, held = public_images.in_turn(asker, name, halt)
        assert found == [(key, name)] and time.monotonic() - started >= 0.25
        assert asker.lenders(name) == []                  # réservée d'un seul geste
        public_images._release(held)
        assert asker.lenders(name) == [(key, name)]
    with placement._occupy(public_chat.model_id(PC, MARKED), None):
        threading.Timer(0.2, halt.set).start()            # le travail est annulé en attendant
        with pytest.raises(Interrupted):
            public_images.in_turn(public_video_ask, MODEL, halt)
        monkeypatch.setattr(public_video_ask, "TOTAL_S", 0.2)
        assert public_images.in_turn(public_video_ask, MODEL, threading.Event()) == ([], None)
    assert public_video_ask.lenders(MODEL) == [(PC, MODEL)]   # rien de gardé réservé


def test_the_units_of_a_batch_never_take_the_same_place_together(companion, opened, vd,
                                                                  monkeypatch):
    """Deux unités d'un même lot, lancées ensemble, un seul prêteur : la seconde attend la
    fin de la première ; jamais deux envois à la fois, aucun refus « busy » compté."""
    monkeypatch.setattr(public_images, "TURN_S", 0.05)
    directory(node(PC, MARKED))
    lend_images()
    settings.set_value("public_images", True)
    vd.compute = 0.4
    results, errors = [], []

    def unit(index):
        try:
            results.append(public_videos.compute(payload(index=index, seed=index), None, {},
                                                 threading.Event()))
        except Exception as exc:  # noqa: BLE001 — dit par l'essai
            errors.append(exc)
    threads = [threading.Thread(target=unit, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == [] and len(results) == 2
    assert len(vd.calls) == 2 and public_refusals.count(PC) == 0 and not reputation.benched(PC)


def test_the_create_bar_knows_the_public_network_can_make_videos(ui, companion):
    def offered():
        found = ui.get("/api/images/models", headers=AUTH).json()["models"]
        return {m["id"]: m["public"] for m in found}
    assert offered()[MODEL] is False                       # personne ne le prête
    directory(node(PC, MARKED))
    assert offered()[MODEL] is True and offered()["z-image-turbo"] is False
    assert offered()["minimax-h3"] is False
    settings.set_value("public_images", False)
    assert offered()[MODEL] is False


def test_a_public_video_is_a_video_unit_of_this_machine_only():
    found = kind.normalize({"model": MODEL, "prompt": "un renard", "frames": FRAMES})
    title, units, info = kind.split_generate({**found, "public": True, "conversation_id": 3})
    assert {(u.task, u.resource) for u in units} == {(public_videos.TASK, "net")}
    assert all(u.payload["frames"] == FRAMES and u.payload["conversation_id"] == 3 for u in units)
    title, units, info = kind.split_generate(found)
    assert {u.task for u in units} == {"generate_image"}


def test_the_engine_is_asked_not_to_write_the_description_in_the_video():
    body = generate.video_body({"model": MODEL, "prompt": SECRET, "width": SIDE,
                                "height": SIDE, "seed": 1, "frames": FRAMES})
    assert body["embed_image_metadata"] is False
