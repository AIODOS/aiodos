"""Les images par le réseau public (décision de l'auteur du 29/10/2026) : le prêteur
(network/public_imaging.py), le demandeur (network/public_image_ask.py), l'accord une fois et
la création dans la conversation (chat/public_creations.py, chat/creations.py), l'unité de la
file (jobs/kinds/public_images.py).

Le prêteur est le vrai serveur entre machines (p2p_app), avec un faux moteur d'images
(imaging/generate.image) ; le compagnon du demandeur est un vrai serveur HTTP local qui tient
le contrat de sa route POST /net/v1/public/ask/<clé>/public/v1/image (compagnon/src/
public_ask.rs) et, selon l'essai, relaie au prêteur (avec une clé jetable par identifiant de
création) ou répond ce qu'on lui dicte. Le demandeur, dans le même processus, n'a ni moteur
ni modèle d'images (comme le téléphone de l'auteur)."""
import base64
import hashlib
import io
import json
import logging
import re
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from aiodos import __version__, db, settings
from aiodos.api.server import create_app
from aiodos.chat import conversations, public_creations
from aiodos.gateway import limits
from aiodos.i18n import t
from aiodos.imaging import catalog, engine, gallery, generate, prompt_filter
from aiodos.jobs import queue
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.kinds import public_images
from aiodos.jobs.registry import Interrupted
from aiodos.network import (lend_queue, p2p_app, placement, public, public_chat,
                            public_image_ask, public_imaging, public_lending, public_refusals,
                            reputation, route)
from aiodos.network.public_chat import PublicError
from aiodos.services import ollama
from tests.test_images import png as uniform_png
from tests.test_public_lending import FORWARD, HEADERS, HERE, KEY, FakeManager, lend

MODEL = "z-image-turbo"
SIDE = 256
SECRET = "le portrait de mon voisin Hippolyte Zébulon"
PATH = public_imaging.PATH
MY_KEY = "00" * 32
PC, OTHER, THIRD = "a1" * 32, "b2" * 32, "c3" * 32
TOKEN = "jeton"
AUTH = {"X-Aiodos-Token": TOKEN}
NET_TOKEN = "jeton-du-compagnon"
TAG = re.compile(r"[A-Za-z0-9_-]{16,64}")


def picture(width=SIDE, height=SIDE) -> bytes:
    """Un PNG en dégradé (pas d'une seule couleur), sans dépendance."""
    raw = b"".join(b"\x00" + bytes((x * 5 + y * 3) % 256 for x in range(width * 3))
                   for y in range(height))

    def chunk(kind_, data):
        return (struct.pack(">I", len(data)) + kind_ + data
                + struct.pack(">I", zlib.crc32(kind_ + data) & 0xFFFFFFFF))
    return (generate.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def params(**extra) -> dict:
    return {"model": MODEL, "prompt": SECRET, "negative": "", "width": SIDE, "height": SIDE,
            "steps": 8, "seed": 42, **extra}


def ask(**extra) -> dict:
    """La demande telle que le prêteur la reçoit."""
    return {"v": 1, **params(), **extra}


# ------------------------------------------------------------ le faux moteur
def _sleep(seconds, stop):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            raise Interrupted()
        time.sleep(0.02)


class Sd:
    """Le faux moteur d'images (imaging/generate.image) : ce qu'il a reçu ; `load` secondes de
    chargement (le modèle est « chargé » ensuite, engine.loaded), `during()` juste après, puis
    `compute` secondes de calcul ; il respecte l'arrêt. made : l'image rendue (une exception :
    levée ; None : un dégradé de la taille demandée)."""

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
        made = self.made if self.made is not None else picture(params["width"], params["height"])
        return {"png": made, "seconds": 0.4}


@pytest.fixture
def sd(monkeypatch):
    fake = Sd()
    monkeypatch.setattr(generate, "image", fake)
    monkeypatch.setattr(engine, "loaded", lambda: fake.loaded)
    # Le modèle complet chez le prêteur seulement (le demandeur, dans le même processus, n'a
    # ni moteur ni modèle : kind.runnable_here reste faux)
    monkeypatch.setattr(public_imaging, "_ready", lambda model: model == MODEL)
    return fake


@pytest.fixture
def lender(sd):
    manager = FakeManager()
    app = p2p_app.create_p2p_app(manager)
    yield manager, TestClient(app, client=HERE, base_url=FORWARD), TestClient(app), sd
    public_lending.forget()
    assert lend_queue.idle()                          # aucune place oubliée


def lend_images(power_share=100):
    """Prêt aux inconnus, et des images (éteints dans les essais), partage actif."""
    lend(power_share)
    settings.set_value("lend_images_public", True)


def post(http, body=None, headers=HEADERS):
    return http.post(PATH, json=ask() if body is None else body, headers=headers)


def events(http, body=None) -> list:
    with http.stream("POST", PATH, json=ask() if body is None else body,
                     headers=HEADERS) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.iter_lines() if line]


def image_of(found) -> tuple:
    header = next(e for e in found if e["type"] == "image")
    return header, b"".join(base64.b64decode(e["data"]) for e in found if e["type"] == "part")


# =================================================================== le prêteur
def test_the_settings_lend_images_and_ask_once():
    assert settings.DEFAULTS["lend_images_public"] is True
    assert "lend_images_public" in settings.OPEN_FROM_THE_START       # éteint en essai
    assert settings.DEFAULTS["public_images"] is None
    assert "public_images" in settings.ASKED_ONCE
    settings.set_value("public_images", "peut-être")                  # ignoré
    assert settings.get("public_images") is None
    assert public_imaging.MAX_PROMPT == kind.MAX_PROMPT
    assert public_imaging.SEED_SPACE == kind.SEED_SPACE


def test_the_route_is_unknown_without_a_believable_key(lender):
    manager, here, lan, sd = lender
    lend_images()
    assert post(lan).status_code == 404                        # en-tête venu d'ailleurs
    assert post(here, headers={}).status_code == 404
    assert post(here, headers={"X-Aiodos-Peer": KEY}).status_code == 404
    response = post(here, headers={"X-Aiodos-Public": "pas-une-cle"})
    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    assert sd.calls == []
    assert post(here).status_code == 200 and len(sd.calls) == 1


def test_not_lending_without_both_settings_never_on_battery(lender, monkeypatch):
    manager, here, lan, sd = lender
    assert post(here).json() == {"error": "not_lending"}       # tout éteint (essais)
    lend()                                                       # conversations seulement
    response = post(here)
    assert response.status_code == 503 and response.json() == {"error": "not_lending"}
    settings.set_value("lend_images_public", True)
    assert post(here).status_code == 200
    settings.set_value("lend_public", False)                     # le prêt aux inconnus coupé
    assert post(here).json() == {"error": "not_lending"}
    lend_images()
    settings.set_value("share_on_ac_only", False)
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "battery")
    assert post(here).json() == {"error": "not_lending"}
    assert len(sd.calls) == 1


def test_a_phone_lends_its_card_only_charging_and_on_wifi(lender, monkeypatch):
    from aiodos.node import android, power
    manager, here, lan, sd = lender
    lend_images()
    supply = {"plugged": True, "level": 100, "temperature": 30.0, "mobile": True,
              "source": "android"}
    wifi = {"on": False}
    monkeypatch.setattr(android, "is_android", lambda: True)
    monkeypatch.setattr(android, "on_wifi", lambda: wifi["on"])
    monkeypatch.setattr(power, "read", lambda: dict(supply))
    assert post(here).json() == {"error": "not_lending"}       # données mobiles
    wifi["on"] = True
    assert events(here)[-1] == {"type": "done"}
    supply["plugged"] = False
    assert post(here).json() == {"error": "not_lending"}
    assert len(sd.calls) == 1


def test_every_bound_is_checked_before_anything_is_taken(lender, monkeypatch):
    """Modèle, description, taille, pas, graine, champs : refusés avant tout calcul, sans
    prendre la place ni le seau de tout le public ; une image de départ ne passe pas."""
    manager, here, lan, sd = lender
    lend_images()
    monkeypatch.setattr(limits, "PER_MINUTE", 2)
    refused = [
        (ask(model="inconnu"), 404, "no_model"),
        (ask(model="wan2.1-t2v-1.3b"), 404, "no_model"),       # une vidéo : hors de cette version
        (ask(model="minimax-h3"), 404, "no_model"),
        (ask(prompt="x" * (public_imaging.MAX_PROMPT + 1)), 413, "too_big"),
        (ask(negative="x" * (public_imaging.MAX_PROMPT + 1)), 413, "too_big"),
        (ask(init_image="data:image/png;base64,AAAA"), 413, "too_big"),
        (ask(strength=0.5), 413, "too_big"),
        (ask(width=300), 413, "too_big"),                      # pas un multiple de 64
        (ask(width=128), 413, "too_big"),                      # sous la borne du modèle
        (ask(width=2048, height=1024), 413, "too_big"),        # deux millions de pixels
        (ask(steps=17), 413, "too_big"),                       # 8 pas pour ce modèle : 16 au plus
        (ask(steps=0), 400, "bad_request"),
        (ask(steps=True), 400, "bad_request"),
        (ask(seed=-1), 400, "bad_request"),
        (ask(seed=2 ** 31), 400, "bad_request"),
        (ask(seed=1.5), 400, "bad_request"),
        (ask(prompt="   "), 400, "bad_request"),
        (ask(prompt=None), 400, "bad_request"),
        (ask(negative=3), 400, "bad_request"),
        (ask(v=2), 400, "bad_request"),
        (ask(model=None), 400, "bad_request"),
        (ask(model="m" * 300), 400, "bad_request"),
    ]
    for body, status, code in refused * 2:
        response = post(here, body)
        assert (response.status_code, response.json()) == (status, {"error": code}), body
    response = here.post(PATH, content=b"[[[[", headers=HEADERS)
    assert response.json() == {"error": "bad_request"}
    big = post(here, ask(prompt="x" * 300_000))                # au-delà de 256 Kio
    assert big.status_code == 413 and big.json() == {"error": "too_big"}
    assert sd.calls == [] and lend_queue.idle()
    assert post(here).status_code == 200
    assert post(here).status_code == 200
    assert post(here).json() == {"error": "busy"}              # les vraies, elles, comptent
    assert len(sd.calls) == 2


def test_an_image_is_answered_in_ndjson(lender, monkeypatch):
    manager, here, lan, sd = lender
    lend_images()
    monkeypatch.setattr(public_imaging, "PART", 500)
    found = events(here, ask(negative="flou"))
    made = picture()
    assert found[0] == {"type": "meta", "model": MODEL, "version": __version__}
    header, data = image_of(found)
    assert header == {"type": "image", "format": "png", "bytes": len(made), "width": SIDE,
                      "height": SIDE, "seconds": 0.4}
    assert data == made
    parts = [e for e in found if e["type"] == "part"]
    assert len(parts) == -(-len(made) // 500) > 1
    assert found[-1] == {"type": "done"}
    # Le guidage du modèle, jamais celui de la demande ; pas de consigne négative pour un
    # modèle qui n'en a pas
    assert sd.calls == [{"model": MODEL, "prompt": SECRET, "negative": "", "width": SIDE,
                         "height": SIDE, "steps": 8, "seed": 42,
                         "cfg": catalog.model(MODEL)["cfg"]}]
    # Mon travail de fond n'est pas suspendu pour un inconnu
    assert manager.executor.interactive == 0 and lend_queue.idle()


def test_signs_of_life_while_the_model_loads_then_while_it_computes(lender, monkeypatch):
    """Le chargement du modèle ne compte pas dans la pause de la puissance prêtée ; le calcul,
    si."""
    manager, here, lan, sd = lender
    lend_images(power_share=60)
    counted = []
    monkeypatch.setattr(public_lending, "_rest_after", lambda m, seconds: counted.append(seconds))
    monkeypatch.setattr(public_lending, "WAIT_S", 0.1)
    sd.load, sd.compute = 1.0, 0.6
    found = events(here)
    stages = [e["stage"] for e in found if e["type"] == "wait"]
    assert stages[0] == "loading" and stages[-1] == "computing"
    assert set(stages) == {"loading", "computing"}
    first = [e["type"] for e in found].index("image")
    assert all(e["type"] != "wait" for e in found[first:])
    assert found[-1] == {"type": "done"}
    assert len(counted) == 1 and 0.4 <= counted[0] < 1.3


def test_busy_while_someone_else_wants_this_machine(lender):
    manager, here, lan, sd = lender
    lend_images()
    manager.executor.interactive = 1                          # l'utilisateur converse ici
    assert post(here).json() == {"error": "busy"}
    manager.executor.interactive = 0
    manager.executor.busy["gpu"] = 1                          # mes images, ou celles de mes amis
    assert post(here).json() == {"error": "busy"}
    manager.executor.busy["gpu"] = 0
    with lend_queue.turn("une-de-mes-machines"):              # conversation prêtée en cours
        assert post(here).json() == {"error": "busy"}
    assert sd.calls == []


def test_one_place_for_the_whole_public(lender, monkeypatch):
    """Une image en cours : une autre image, ou une conversation publique, est refusée."""
    manager, here, lan, sd = lender
    lend_images()
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": "qwen3.5:9b"}])
    monkeypatch.setattr(ollama, "capabilities", lambda model: ["completion"])
    chat = {"v": 1, "model": "qwen3.5:9b", "messages": [{"role": "user", "content": "Salut"}]}
    seen = {}
    sd.during = lambda: seen.update(image=post(here), free=public_lending.free(manager),
                                    chat=here.post("/public/v1/chat", json=chat, headers=HEADERS))
    found = events(here)
    assert seen["image"].json() == {"error": "busy"} and seen["chat"].json() == {"error": "busy"}
    assert seen["free"] is False
    assert found[-1] == {"type": "done"} and len(sd.calls) == 1


def test_the_user_comes_back_and_the_loan_stops(lender):
    manager, here, lan, sd = lender
    lend_images()
    sd.compute = 5.0
    sd.during = manager.executor.interactive_begin
    started = time.monotonic()
    found = events(here)
    assert found[-1] == {"type": "error", "error": "busy"}
    assert all(e["type"] != "image" for e in found)
    assert time.monotonic() - started < 3 and lend_queue.idle()
    manager.executor.interactive_end()


def test_an_engine_failure_or_a_wrong_image_is_a_code(lender, monkeypatch, caplog):
    manager, here, lan, sd = lender
    lend_images()
    with caplog.at_level(logging.DEBUG):
        sd.made = RuntimeError(f"sd-server a lu « {SECRET} » puis s'est arrêté")
        assert events(here)[-2:] == [{"type": "meta", "model": MODEL, "version": __version__},
                                     {"type": "error", "error": "engine"}]
        sd.made = b"pas un png"
        assert events(here)[-1] == {"type": "error", "error": "engine"}
        sd.made = None
        monkeypatch.setattr(public_imaging, "MAX_IMAGE", 1000)        # trop grosse
        assert events(here)[-1] == {"type": "error", "error": "engine"}
    assert all("Zébulon" not in r.getMessage() for r in caplog.records)
    assert lend_queue.idle()


def test_nothing_is_kept_neither_on_disk_nor_in_the_base_nor_in_the_log(lender, caplog,
                                                                         data_dir):
    manager, here, lan, sd = lender
    lend_images()
    with caplog.at_level(logging.DEBUG):
        found = events(here)
    header, made = image_of(found)
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"Image pour le réseau public : {MODEL}, {SIDE} × {SIDE}, 8 pas, ")
               for m in said)
    for message in said:
        assert "Zébulon" not in message and KEY not in message
    for row in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        for stored in db.query(f'SELECT * FROM "{row["name"]}"'):
            text = repr(tuple(stored))
            assert "Zébulon" not in text and KEY not in text, row["name"]
    sample = made[60:260]                                    # un morceau des pixels compressés
    for path in data_dir.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert "Zébulon".encode() not in content and KEY.encode() not in content, path
            assert sample not in content, path
    assert public_lending.serving() == []


def test_the_public_profile_announces_the_lent_image_models(monkeypatch):
    monkeypatch.setattr(ollama, "list_models",
                        lambda max_age=10: [{"name": "qwen3.5:9b"}, {"name": "image:piege"}])
    monkeypatch.setattr(public_imaging, "_ready", lambda model: model == MODEL)
    assert public._models() == ["qwen3.5:9b"]                # images non prêtées (essais)
    settings.set_value("lend_images_public", True)
    assert public._models() == ["qwen3.5:9b", "image:z-image-turbo"]
    many = [{"name": f"modele-{i:02d}:1b"} for i in range(40)]
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: many)
    found = public._models()
    assert len(found) == public.MAX_MODELS and found[-1] == "image:z-image-turbo"

    def down(max_age=10):
        raise RuntimeError("Ollama arrêté")
    monkeypatch.setattr(ollama, "list_models", down)
    assert public._models() == ["image:z-image-turbo"]
    # Ni une vidéo (les siennes : public_video.offered, tests/test_public_videos.py), ni un
    # modèle à licence restrictive, n'est prêté comme modèle d'images
    monkeypatch.setattr(public_imaging, "_ready", lambda model: True)
    assert public._models() == ["image:z-image-turbo"]
    assert all(len(public_imaging.MARKER + m) <= public.MAX_TEXT for m in catalog.MODELS)


# ============================================================ le compagnon simulé
class Companion:
    """Ce que le demandeur lit du compagnon (running, listen, headers, supports), et sa route
    locale, servie pour de bon. script[clé] : ("relay",) au prêteur (par défaut), avec la clé
    jetable d'un identifiant de création ; ("refuse", statut, code) ; ("events", [(attente,
    événement)…]) : chaque événement tel quel, après son attente en secondes."""

    def __init__(self, lender_http=None):
        self.lender = lender_http
        self.requests, self.script, self.callers = [], {}, []
        self.features = {"public-ask"}
        self.running = True
        self.headers = {"X-Aiodos-Net-Token": NET_TOKEN}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.listen = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def supports(self, feature):
        return feature in self.features

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        companion = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, data: bytes, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                prefix = "/net/v1/public/ask/"
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("X-Aiodos-Net-Token") != NET_TOKEN:
                    return self._send(401, b'{"detail": "jeton"}')
                key, _, tail = self.path[len(prefix):].partition("/")
                tag = self.headers.get("X-Aiodos-Conversation") or ""
                if not self.path.startswith(prefix) or not TAG.fullmatch(tag):
                    return self._send(400, b'{"error": "conversation"}')
                companion.requests.append({"key": key, "tail": tail, "tag": tag,
                                           "body": json.loads(raw)})
                act = companion.script.get(key, ("relay",))
                if act[0] == "refuse":
                    return self._send(act[1], json.dumps({"error": act[2]}).encode())
                if act[0] == "relay":
                    # Une clé jetable par identifiant de création ; le prêteur ne voit jamais
                    # l'identifiant lui-même
                    caller = hashlib.sha256(tag.encode()).hexdigest()
                    companion.callers.append(caller)
                    reply = companion.lender.post(
                        "/" + tail, content=raw,
                        headers={"X-Aiodos-Public": caller, "Content-Type": "application/json"})
                    return self._send(reply.status_code, reply.content,
                                      reply.headers.get("content-type") or "application/json")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for pause, event in act[1]:
                        time.sleep(pause)
                        line = event if isinstance(event, bytes) else json.dumps(event).encode()
                        self.wfile.write(line + b"\n")
                        self.wfile.flush()
                except OSError:                  # le demandeur a coupé : c'est permis
                    pass
        return Handler


@pytest.fixture
def companion(lender):
    manager, here, lan, sd = lender
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


def node(key, *models, lends=True, free=1, tps=40.0, seen=5):
    return {"key": key, "version": "1.0.0", "models": list(models), "lends": lends,
            "free": free, "tps": tps, "seen_s": seen}


def directory(*nodes):
    """L'annuaire public tel que network/public.py le garde après l'avoir lu au compagnon."""
    public._remember(public._typed({"enabled": True, "key": MY_KEY, "nodes": list(nodes)}))


def good(made=None, model=MODEL, side=SIDE) -> list:
    """Les événements d'une image honnête (cf. public_imaging.py)."""
    made = made if made is not None else picture(side, side)
    return [(0, {"type": "meta", "model": model, "version": "1.0.0"}),
            (0, {"type": "image", "format": "png", "bytes": len(made), "width": side,
                 "height": side, "seconds": 2.5}),
            (0, {"type": "part", "data": base64.b64encode(made).decode()}),
            (0, {"type": "done"})]


# ================================================================== le demandeur
def test_lenders_are_those_that_lend_this_model_with_a_place(companion, opened):
    directory(node(PC, "qwen3.5:9b", "image:z-image-turbo", tps=50),
              node(OTHER, "image:z-image-turbo", tps=10),
              node(THIRD, "image:z-image-turbo", free=0),              # pas de place
              node("d4" * 32, "image:z-image-turbo", lends=False),     # ne prête pas
              node("e5" * 32, "qwen3.5:9b"),                           # pas d'images
              node("f6" * 32, "image:wan2.1-t2v-1.3b", "image:minimax-h3", "image:inconnu"),
              node("a7" * 32, "image:z-image-turbo"))                  # mise de côté
    reputation.set_aside("a7" * 32)
    assert public_image_ask.offered_models() == [MODEL]
    assert public_image_ask.lenders(MODEL) == [(PC, MODEL), (OTHER, MODEL)]
    reputation.note_served(OTHER, 0, 3.0)        # elle a déjà servi ici, sans faute : d'abord
    assert public_image_ask.lenders(MODEL) == [(OTHER, MODEL), (PC, MODEL)]
    # Une image en cours chez elle : sa seule place est prise
    with placement._occupy(public_chat.model_id(OTHER, "image:" + MODEL), None):
        assert public_image_ask.lenders(MODEL) == [(PC, MODEL)]
    companion.features.clear()                   # un compagnon d'avant : personne
    assert public_image_ask.offered_models() == [] and public_image_ask.lenders(MODEL) == []


def test_a_lender_without_a_place_right_now_is_still_offered(companion, opened):
    """La seule machine qui prête le modèle est occupée (ou sa mémoire juste sous le seuil) :
    la création est quand même proposée, son unité attendra son tour (in_turn). S23 de
    l'auteur, 29/10/2026 : le PC prêtait z-image-turbo avec « free » à 0, le téléphone disait
    « aucun modèle »."""
    directory(node(PC, "image:z-image-turbo", free=0))
    assert public_image_ask.offered_models() == [MODEL] and public_image_ask.lent(MODEL)
    assert public_image_ask.lenders(MODEL) == []                 # personne à qui l'envoyer… encore
    directory(node(PC, "image:z-image-turbo", free=0, lends=False))
    assert public_image_ask.offered_models() == []               # elle ne prête plus : rien


def test_an_image_model_is_never_taken_for_a_conversation_model(companion, opened):
    directory(node(PC, "image:z-image-turbo"), node(OTHER, "llama3:latest", "image:z-image-turbo"))
    assert public_imaging.MARKER in placement.RESERVED
    assert [c.model for c in placement.best_public()] == [public_chat.model_id(OTHER,
                                                                                "llama3:latest")]


def test_the_request_stays_within_the_lender_bounds():
    data = json.loads(public_image_ask.body(params(init_path="/tmp/x.png", strength=0.5)))
    assert data == {"v": 1, **params()}           # ni image de départ, ni force
    for wrong, code in ((params(steps=17), "too_big"), (params(width=2048, height=1024), "too_big"),
                        (params(prompt="x" * 2001), "too_big"), (params(model="wan2.1-t2v-1.3b"),
                                                                 "bad_request"),
                        (params(prompt=" "), "bad_request"), (params(width="large"), "bad_request")):
        with pytest.raises(PublicError) as found:
            public_image_ask.body(wrong)
        assert found.value.code == code, wrong


def test_an_image_goes_through_the_companion_and_comes_back_checked(companion, opened):
    from PIL import Image
    directory(node(PC, "image:z-image-turbo"))
    lend_images()
    made = public_image_ask.image(PC, params(), conversation="creation-aaaaaaaaaaaa")
    assert made["seconds"] == 0.4
    with Image.open(io.BytesIO(made["png"])) as found:
        assert found.format == "PNG" and found.size == (SIDE, SIDE)
    sent = companion.requests[-1]
    assert (sent["key"], sent["tail"], sent["tag"]) == (PC, "public/v1/image",
                                                         "creation-aaaaaaaaaaaa")
    assert sent["body"] == ask()
    assert reputation.get(PC)["served"] == 1 and not reputation.benched(PC)
    # Sans identifiant : une clé jetable pour cette seule image
    public_image_ask.image(PC, params())
    assert companion.requests[-1]["tag"] != "creation-aaaaaaaaaaaa"
    assert len(set(companion.callers)) == 2


@pytest.mark.parametrize("name, script, code", [
    ("autre taille", [good()[0], (0, {"type": "image", "format": "png", "bytes": 10,
                                      "width": 512, "height": 512, "seconds": 1})], "unreadable"),
    ("une seule couleur", good(uniform_png(SIDE, SIDE)), "unreadable"),
    ("pas un png", good(b"GIF89a" + bytes(4000)), "unreadable"),
    ("tronquée", good()[:2] + [(0, {"type": "part", "data": base64.b64encode(
        picture()[:-30]).decode()}), (0, {"type": "done"})], "unreadable"),
    ("plus longue qu'annoncée", good()[:3] + [(0, {"type": "part", "data": "AAAA"}),
                                              (0, {"type": "done"})], "unreadable"),
    ("trop grosse", [good()[0], (0, {"type": "image", "format": "png",
                                     "bytes": public_imaging.MAX_IMAGE + 1, "width": SIDE,
                                     "height": SIDE, "seconds": 1})], "too_long"),
    ("un autre modèle", good(model="autre-modele"), "unreadable"),
    ("base64 illisible", good()[:2] + [(0, {"type": "part", "data": "%%%"})], "unreadable"),
    ("ligne illisible", [good()[0], (0, b"{pas du json")], "unreadable"),
    ("moteur arrêté", [good()[0], (0, {"type": "error", "error": "engine"})], "engine"),
    ("coupée", good()[:3], "cut"),
])
def test_a_lender_that_returns_anything_but_a_valid_image_is_set_aside(companion, opened,
                                                                        name, script, code):
    directory(node(PC, "image:z-image-turbo"))
    companion.script[PC] = ("events", script)
    with pytest.raises(PublicError) as found:
        public_image_ask.image(PC, params())
    assert found.value.code == code, name
    assert reputation.benched(PC), name
    assert public_image_ask.lenders(MODEL) == []               # écartée : plus candidate


def test_a_refusal_is_not_a_fault_but_repeated_ones_after_an_offer_are(companion, opened):
    directory(node(PC, "image:z-image-turbo"))
    companion.script[PC] = ("refuse", 503, "busy")
    with pytest.raises(PublicError):                           # pas proposée : rien de compté
        public_image_ask.image(PC, params())
    for i in range(3):
        assert public_image_ask.lenders(MODEL) == [(PC, MODEL)]  # proposée (place annoncée)
        with pytest.raises(PublicError) as found:
            public_image_ask.image(PC, params())
        assert found.value.code == "busy"
        assert reputation.benched(PC) is (i == 2)


def test_a_silent_lender_is_cut_and_signs_of_life_give_it_time(companion, opened, monkeypatch):
    directory(node(PC, "image:z-image-turbo"))
    monkeypatch.setattr(public_image_ask, "FIRST_EVENT_S", 1)
    monkeypatch.setattr(public_image_ask, "ALIVE_GRACE_S", 0.6)
    meta, *rest = good()
    waits = [(0.3, {"type": "wait", "stage": "loading"})] * 4 \
        + [(0.3, {"type": "wait", "stage": "computing"})] * 2
    companion.script[PC] = ("events", [meta, *waits, *rest])        # 1,8 s : vivant
    assert public_image_ask.image(PC, params())["seconds"] == 2.5
    companion.script[PC] = ("events", [meta, (3, {"type": "wait", "stage": "loading"})])
    started = time.monotonic()
    with pytest.raises(PublicError) as found:
        public_image_ask.image(PC, params())
    assert found.value.code == "slow" and time.monotonic() - started < 2.5
    assert reputation.benched(PC)


def test_stopping_cuts_the_image_without_blaming_the_lender(companion, opened):
    directory(node(PC, "image:z-image-turbo"))
    meta, *rest = good()
    companion.script[PC] = ("events", [meta, (2, {"type": "wait", "stage": "loading"}), *rest])
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    assert public_image_ask.image(PC, params(), stop=stop) is None
    assert not reputation.benched(PC)


# ================================================== l'accord et la création
@pytest.fixture
def ui(companion, opened):
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c
        _settle()


def _settle(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not db.query_one("SELECT COUNT(*) AS n FROM units WHERE status='running'")["n"]:
            return
        time.sleep(0.05)


def wait_for(predicate, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("délai dépassé")


def create(ui, text="Crée une image d'un chat roux", **extra):
    return ui.post("/api/chat/create", headers=AUTH, json={"text": text, **extra})


def test_without_a_public_machine_the_message_stays_as_before(ui, companion):
    reply = create(ui)
    assert reply.status_code == 409
    assert reply.json()["detail"] == t("chat_create.nothing_ready_images")
    assert reply.json()["public"] is None
    settings.set_value("public_images", True)
    assert create(ui).json()["detail"] == t("public_images.nobody")
    assert conversations.list_tree()["conversations"] == [] and companion.requests == []


def test_the_consent_is_asked_once_then_the_image_comes_from_the_public_network(ui, companion):
    directory(node(PC, "image:z-image-turbo"))
    lend_images()
    reply = create(ui)                                    # pas encore demandé : la carte
    assert reply.status_code == 409
    assert reply.json()["detail"] == t("public_images.ask") and reply.json()["public"] == "ask"
    assert "inconnu" in reply.json()["detail"]
    settings.set_value("public_images", False)            # Non : gardé, la raison dite
    reply = create(ui)
    assert reply.json()["detail"] == t("public_images.closed_refused")
    assert reply.json()["public"] is None
    assert conversations.list_tree()["conversations"] == [] and companion.requests == []
    assert ui.put("/api/settings", headers=AUTH, json={"public_images": True}).json()[
        "public_images"] is True                          # Oui
    made = create(ui).json()
    media = made["media"]
    assert media["public"] is True and media["model"] == MODEL
    job = queue.job_summary(made["job_id"])
    assert job["params"]["public"] is True
    units = db.query("SELECT task, resource, local_only FROM units WHERE job_id=?",
                     (made["job_id"],))
    assert [tuple(u) for u in units] == [(public_images.TASK, "net", 1)]
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    rows = gallery.batch(media["batch"])
    assert len(rows) == 1 and rows[0]["machine"] == t("public_images.machine")
    assert gallery.path_of(rows[0]).read_bytes().startswith(generate.PNG_SIGNATURE)
    conv = ui.get(f"/api/conversations/{made['conversation_id']}", headers=AUTH).json()
    answer = conv["messages"][-1]["media"]
    assert answer["public"] is True and len(answer["images"]) == 1
    # La description telle quelle (aucun modèle de conversation ici) ; rien de la conversation
    sent = [r for r in companion.requests if r["tail"] == "public/v1/image"]
    assert len(sent) == 1 and sent[0]["body"]["prompt"] == "un chat roux"
    assert set(sent[0]["body"]) == {"v", "model", "prompt", "negative", "width", "height",
                                    "steps", "seed"}
    assert len(companion.requests) == 1                   # rien d'autre n'est parti


def test_mine_only_and_the_settings_keep_images_off_the_public_network(ui, companion):
    directory(node(PC, "image:z-image-turbo"))
    lend_images()
    settings.set_value("public_images", True)
    reply = create(ui, mine_only=True)                     # une conversation neuve
    assert reply.status_code == 409
    assert reply.json()["detail"] == t("public_images.closed_mine_only")
    conv_id = conversations.create(mine_only=True)         # une conversation qui le garde
    reply = create(ui, conversation_id=conv_id)
    assert reply.json()["detail"] == t("public_images.closed_mine_only")
    settings.set_value("use_public", False)
    assert create(ui).json()["detail"] == t("public_images.closed_off")
    settings.set_value("use_public", True)
    choice = public_creations.choose(MODEL, False, [{"kind": "image", "id": "x"}])
    assert choice == public_creations.Choice(message=t("public_images.closed_attachments"))
    # Une vidéo, sans machine du réseau public qui prête un modèle de vidéos (l'accord donné) :
    # le réseau public ne peut pas non plus (le reste : tests/test_public_videos.py)
    reply = create(ui, "Crée une vidéo d'un chat roux")
    assert reply.json()["detail"] == t("public_videos.nobody")
    assert companion.requests == []


def test_the_unit_rechecks_the_consent_before_sending(companion, opened):
    directory(node(PC, "image:z-image-turbo"))
    lend_images()
    settings.set_value("public_images", True)
    conv_id = conversations.create()
    payload = {"model": MODEL, "prompt": SECRET, "negative": "", "width": SIDE,
               "height": SIDE, "steps": 8, "cfg": 1.0, "batch": "abcdef123456", "seed": 5,
               "index": 0, "public": True, "conversation_id": conv_id}
    conversations.set_mine_only(conv_id, True)              # changé depuis la demande
    with pytest.raises(ValueError, match="ne partent plus"):
        public_images.compute(payload, None, {}, threading.Event())
    conversations.set_mine_only(conv_id, False)
    settings.set_value("public_images", False)
    with pytest.raises(ValueError, match="ne partent plus"):
        public_images.compute(payload, None, {}, threading.Event())
    settings.set_value("public_images", True)
    with pytest.raises(ValueError):                         # pas marquée « public »
        public_images.compute(dict(payload, public=False), None, {}, threading.Event())
    assert companion.requests == []
    done = public_images.compute(payload, None, {}, threading.Event())
    assert done["public"] is True and done["machine"] == t("public_images.machine")
    assert gallery.get(done["image_id"])["prompt"] == SECRET
    # Deux images d'une même création : le même identifiant, donc la même clé jetable
    public_images.compute(dict(payload, index=1, seed=6), None, {}, threading.Event())
    assert len({r["tag"] for r in companion.requests}) == 1
    # Arrêtée avant de commencer : rendue, rien d'envoyé
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(Interrupted):
        public_images.compute(payload, None, {}, stopped)
    assert len(companion.requests) == 2


def test_the_next_lender_answers_when_the_first_fails(companion, opened):
    directory(node(PC, "image:z-image-turbo", tps=90), node(OTHER, "image:z-image-turbo", tps=10))
    lend_images()
    settings.set_value("public_images", True)
    companion.script[PC] = ("events", good()[:3])            # coupée : écartée
    payload = {"model": MODEL, "prompt": "un phare", "negative": "", "width": SIDE,
               "height": SIDE, "steps": 8, "cfg": 1.0, "batch": "lot000000001", "seed": 5,
               "index": 0, "public": True, "conversation_id": None}
    done = public_images.compute(payload, None, {}, threading.Event())
    assert [r["key"] for r in companion.requests] == [PC, OTHER]
    assert done["public"] is True and reputation.benched(PC) and not reputation.benched(OTHER)
    companion.script[OTHER] = ("refuse", 503, "busy")
    with pytest.raises(ValueError, match="aucune machine du réseau public n'a pu"):
        public_images.compute(dict(payload, index=1), None, {}, threading.Event())


def test_the_description_is_enriched_here_never_by_a_stranger(ui, companion, monkeypatch):
    directory(node(PC, "image:z-image-turbo"))
    lend_images()
    settings.set_value("public_images", True)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": "qwen3.5:0.8b"}])
    monkeypatch.setattr(ollama, "capabilities_many",
                        lambda names: {n: ["completion"] for n in names})
    asked = []

    def here(model, messages, **kw):
        filtering = messages[0]["content"] == prompt_filter.SYSTEM
        asked.append((model, "filter" if filtering else "enrich"))
        yield "content", "NO" if filtering else \
            "A detailed watercolor of a ginger cat sitting on a windowsill"
    monkeypatch.setattr(ollama, "chat_stream", here)
    made = create(ui).json()
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    # Le modèle de cette machine : il enrichit, puis le filtre lui pose sa question
    assert asked == [("qwen3.5:0.8b", "enrich"), ("qwen3.5:0.8b", "filter")]
    assert [r["tail"] for r in companion.requests] == ["public/v1/image"]
    assert companion.requests[0]["body"]["prompt"].startswith("A detailed watercolor")


def test_the_create_bar_knows_the_public_network_can_do_it(ui, companion):
    def offered():
        found = ui.get("/api/images/models", headers=AUTH).json()["models"]
        return {m["id"]: m["public"] for m in found}
    assert offered()[MODEL] is False                       # personne ne le prête
    directory(node(PC, "image:z-image-turbo"))
    assert offered()[MODEL] is True                        # accord pas encore demandé : la carte
    assert offered()["wan2.1-t2v-1.3b"] is False
    settings.set_value("public_images", False)
    assert offered()[MODEL] is False
    settings.set_many({"public_images": True, "use_public": False})
    assert offered()[MODEL] is False


def test_the_images_api_never_sends_to_the_public_network():
    """Seule la conversation, après l'accord, marque une création « public » : la demande de la
    vue Images (et de l'API) passe par normalize, qui ne la garde pas."""
    found = kind.normalize({"model": MODEL, "prompt": "un chat", "public": True})
    assert "public" not in found
    title, units, info = kind.split_generate(found)
    assert {u.task for u in units} == {"generate_image"}
    title, units, info = kind.split_generate({**found, "public": True, "conversation_id": 3,
                                              "init_path": "/x.png", "strength": 0.5})
    assert {(u.task, u.resource) for u in units} == {(public_images.TASK, "net")}
    assert all("init_path" not in u.payload and u.payload["conversation_id"] == 3 for u in units)


# ================================================== le filtre des descriptions
# La demande interdite la plus neutre qui suffise (tests/test_prompt_filter.py : la règle)
FORBIDDEN = "a naked child, " + SECRET


def test_the_lender_refuses_a_forbidden_description_before_anything(lender, monkeypatch,
                                                                     caplog, data_dir):
    """Le filtre passe avant la place et le modèle : 403 « refused_content », aucun appel au
    moteur, la place et le seau de tout le public libres, rien de gardé ; le journal ne dit
    que le code."""
    manager, here, lan, sd = lender
    lend_images()
    monkeypatch.setattr(limits, "PER_MINUTE", 1)        # un refus qui en prendrait : « busy »
    with caplog.at_level(logging.DEBUG):
        for body in (ask(prompt=FORBIDDEN), ask(prompt="un enfant nu, " + SECRET),
                     ask(prompt="lolicon"), ask(prompt="ma voisine nue, " + SECRET)):
            response = post(here, body)
            assert (response.status_code, response.json()) == (403, {"error": "refused_content"})
    assert sd.calls == [] and lend_queue.idle() and public_lending.serving() == []
    said = [r.getMessage() for r in caplog.records]
    assert "Image refusée par le filtre (minor_sexual)" in said
    assert "Image refusée par le filtre (real_person_intimate)" in said
    for message in said:
        assert "Zébulon" not in message and "naked" not in message and KEY not in message
    for row in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        for stored in db.query(f'SELECT * FROM "{row["name"]}"'):
            assert "Zébulon" not in repr(tuple(stored)), row["name"]
    for path in data_dir.rglob("*"):
        if path.is_file():
            assert "Zébulon".encode() not in path.read_bytes(), path
    assert post(here).status_code == 200 and len(sd.calls) == 1   # place et seau intacts


def test_the_lender_asks_its_model_only_when_already_loaded_and_free(lender, monkeypatch):
    manager, here, lan, sd = lender
    lend_images()
    asked = []

    def model(name, messages, **kw):
        asked.append(name)
        yield "content", "YES"
    monkeypatch.setattr(ollama, "chat_stream", model)
    monkeypatch.setattr(prompt_filter, "warm_model", lambda: None)     # rien de chargé
    assert post(here).status_code == 200 and asked == []
    monkeypatch.setattr(prompt_filter, "warm_model", lambda: "qwen3.5:9b")
    manager.executor.interactive = 1                    # l'utilisateur converse : pas de modèle
    assert post(here).json() == {"error": "busy"} and asked == []
    manager.executor.interactive = 0
    response = post(here)                               # le modèle dit « YES » : refusée
    assert (response.status_code, response.json()) == (403, {"error": "refused_content"})
    assert asked == ["qwen3.5:9b"] and len(sd.calls) == 1 and lend_queue.idle()


def test_the_asker_never_sends_a_forbidden_description(companion, opened):
    directory(node(PC, "image:z-image-turbo"))
    lend_images()
    with pytest.raises(PublicError) as found:
        public_image_ask.body(params(prompt=FORBIDDEN))
    assert found.value.code == "refused_content"
    assert public_image_ask.lenders(MODEL) == [(PC, MODEL)]
    with pytest.raises(PublicError):
        public_image_ask.image(PC, params(prompt=FORBIDDEN))
    assert companion.requests == []
    assert not reputation.benched(PC) and public_refusals.count(PC) == 0
    # La consigne négative : ce modèle n'en a pas, elle ne part pas (sinon, filtrée avec la
    # description : tests/test_prompt_filter.py)
    sent = public_image_ask.body(params(prompt="a child at the beach", negative="clothes"))
    assert json.loads(sent)["negative"] == ""


def test_a_lender_filter_refusal_is_never_held_against_it(companion, opened):
    """« refused_content » : ni un échec qui l'écarte, ni un refus suspect compté, même
    proposée et répété ; la création s'arrête là, sans passer à une autre machine."""
    directory(node(PC, "image:z-image-turbo", tps=90), node(OTHER, "image:z-image-turbo", tps=10))
    companion.script[PC] = ("refuse", 403, "refused_content")
    for _ in range(public_refusals.LIMIT + 1):
        assert public_image_ask.lenders(MODEL)[0] == (PC, MODEL)       # proposée
        with pytest.raises(PublicError) as found:
            public_image_ask.image(PC, params())
        assert found.value.code == "refused_content"
        assert str(found.value) == t("public_chat.error_refused_content")
    assert not reputation.benched(PC) and public_refusals.count(PC) == 0
    assert public_refusals.refused(PC, "refused_content") is False
    settings.set_value("public_images", True)
    payload = {"model": MODEL, "prompt": "un phare", "negative": "", "width": SIDE,
               "height": SIDE, "steps": 8, "cfg": 1.0, "batch": "lot000000002", "seed": 5,
               "index": 0, "public": True, "conversation_id": None}
    companion.requests.clear()
    with pytest.raises(ValueError, match="filtre la juge interdite"):
        public_images.compute(payload, None, {}, threading.Event())
    assert [r["key"] for r in companion.requests] == [PC]        # pas chez OTHER ensuite


def test_the_unit_filters_before_choosing_a_lender(companion, opened):
    directory(node(PC, "image:z-image-turbo"))
    settings.set_value("public_images", True)
    payload = {"model": MODEL, "prompt": FORBIDDEN, "negative": "", "width": SIDE,
               "height": SIDE, "steps": 8, "cfg": 1.0, "batch": "lot000000003", "seed": 5,
               "index": 0, "public": True, "conversation_id": None}
    with pytest.raises(ValueError, match="rien n'est parti"):
        public_images.compute(payload, None, {}, threading.Event())
    assert companion.requests == []


def test_the_conversation_says_it_and_sends_nothing(ui, companion, monkeypatch):
    directory(node(PC, "image:z-image-turbo"))
    lend_images()
    settings.set_value("public_images", True)
    for text in ("Crée une image d'un enfant nu", "Crée une image d'une ado en lingerie"):
        reply = create(ui, text)
        assert reply.status_code == 403
        assert reply.json()["detail"] == t("public_images.refused_content")
        assert "ni AIODOS" in reply.json()["detail"]
    # Le modèle de conversation de cette machine juge la description qui partirait (enrichie)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": "qwen3.5:0.8b"}])
    monkeypatch.setattr(ollama, "capabilities_many",
                        lambda names: {n: ["completion"] for n in names})
    verdict = ["YES"]

    def here(model, messages, **kw):
        if messages[0]["content"] == prompt_filter.SYSTEM:
            yield "content", verdict[0]
        else:
            yield "content", "A detailed portrait of a famous singer, soft light"
    monkeypatch.setattr(ollama, "chat_stream", here)
    assert create(ui, "Crée une image d'une chanteuse célèbre").status_code == 403
    assert conversations.list_tree()["conversations"] == [] and companion.requests == []
    verdict[0] = "NO"
    made = create(ui, "Crée une image d'une chanteuse célèbre").json()
    wait_for(lambda: queue.job_summary(made["job_id"])["status"] == "done")
    assert len(companion.requests) == 1


def test_my_own_machines_are_not_filtered(monkeypatch):
    """Une image créée ici ou par mes machines ne passe pas par le filtre (seul le réseau
    public le demande)."""
    from aiodos.chat import creations

    def called(*args, **kwargs):
        pytest.fail("filtre appelé pour mes machines")
    monkeypatch.setattr(creations, "pick", lambda kind_, preferred=None: MODEL)
    monkeypatch.setattr(prompt_filter, "check", called)
    monkeypatch.setattr(prompt_filter, "base", called)
    settings.set_value("images_enrich", False)
    plan = creations.prepare("Crée une image d'un phare sous l'orage", {})
    assert "public" not in plan["params"] and plan["params"]["prompt"] == "un phare sous l'orage"
