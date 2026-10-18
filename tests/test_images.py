"""Images réparties : demande, unités, fichiers du modèle, moteur (faux sd-server), contrôle
et galerie, partage du travail avec mes machines (jamais un ami), téléchargement."""
import base64
import hashlib
import json
import re
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.imaging import catalog, download, engine, gallery, generate
from aiodos.jobs import kinds, queue, registry  # noqa: F401  (enregistre les tâches)
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.registry import Interrupted
from aiodos.network import peers, work

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}
B, FRIEND = "machine-b", "machine-ami"


# Une vidéo WebM minimale : en-tête EBML, type de document « webm »
WEBM = bytes.fromhex("1a45dfa39f42868101428284") + b"webm" + bytes(64)


def png(width, height, color=(40, 90, 200)) -> bytes:
    """PNG minimal (une couleur), sans dépendance."""
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(kind_, data):
        return (struct.pack(">I", len(data)) + kind_ + data
                + struct.pack(">I", zlib.crc32(kind_ + data) & 0xFFFFFFFF))
    return (generate.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def tiny_model(tmp_path, monkeypatch):
    """Un petit modèle « essai » de trois fichiers, posés dans un dossier à part."""
    folder = tmp_path / "modeles"
    folder.mkdir()
    files, contents = {}, {}
    for part, content in (("diffusion", b"coeur" * 1000), ("llm", b"texte" * 500),
                          ("vae", b"vae" * 100)):
        name = f"{part}.gguf"
        (folder / name).write_bytes(content)
        contents[name] = content
        files[part] = {"name": name, "size": len(content),
                       "sha256": hashlib.sha256(content).hexdigest(),
                       "url": f"http://127.0.0.1:9/{name}"}
    monkeypatch.setitem(catalog.MODELS, "essai", {
        "label": "Essai", "license": "Apache-2.0", "about": "Pour les essais.", "files": files,
        "kind": "image", "steps": 4, "cfg": 1.0, "negative": True,
        "sizes": [[1024, 1024, "Carré"]], "multiple": 64, "side": [256, 2048],
        "max_pixels": 1536 * 1536})
    settings.set_value("images_models_dir", str(folder))
    return {"folder": folder, "files": files, "contents": contents}


def wait_for(predicate, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("délai dépassé")


def ready(model_id="essai"):
    return wait_for(lambda: catalog.state(model_id)["state"] == "ready")


# ------------------------------------------------------------------ demande
def test_a_request_is_checked_and_completed(tiny_model):
    params = kind.normalize({"model": "essai", "prompt": "  un chat  ", "negative": "flou",
                             "width": 512, "height": 768, "count": 3})
    assert params["prompt"] == "un chat" and params["negative"] == "flou"
    assert params["steps"] == 4 and 0 <= params["seed"] < kind.SEED_SPACE
    assert re.fullmatch(r"[0-9a-f]{12}", params["batch"])
    # Modèle distillé : la consigne négative n'a pas d'effet, elle n'est pas gardée
    assert kind.normalize({"model": "z-image-turbo", "prompt": "x", "negative": "flou"})[
        "negative"] == ""
    assert kind.normalize({"model": "essai", "prompt": "x", "seed": 7})["seed"] == 7
    for wrong, message in (({"prompt": ""}, "Décris"), ({"width": 1000}, "multiples de 64"),
                           ({"width": 2048, "height": 2048}, "trop grande"),
                           ({"count": 17}, "De 1 à 16"), ({"model": "autre"}, "inconnu"),
                           ({"seed": "abc"}, "invalides")):
        with pytest.raises(ValueError, match=message):
            kind.normalize({"model": "essai", "prompt": "x", **wrong})


def test_each_image_is_a_unit_with_its_own_seed(tiny_model):
    params = kind.normalize({"model": "essai", "prompt": "Un phare sous l'orage",
                             "count": 3, "seed": kind.SEED_SPACE - 1})
    title, units, info = kind.split_generate(params)
    assert [u.payload["seed"] for u in units] == [kind.SEED_SPACE - 1, 0, 1]
    assert [u.payload["index"] for u in units] == [0, 1, 2]
    assert {u.resource for u in units} == {"gpu"} and {u.task for u in units} == {"generate_image"}
    assert title == "Images : « Un phare sous l'orage » ×3" and info["batch"] == params["batch"]


# ------------------------------------------------------------- fichiers
def test_model_files_are_verified_once_and_changes_are_seen(tiny_model):
    first = catalog.state("essai")
    assert first["state"] in ("checking", "ready")
    ready()
    cache = json.loads((Path(db.paths.data_dir()) / "images" / "verified.json").read_text())
    assert len(cache) == 3
    assert catalog.files("essai")["vae"] == tiny_model["folder"] / "vae.gguf"
    # Même taille, contenu différent : revérifié, puis refusé
    vae = tiny_model["folder"] / "vae.gguf"
    vae.write_bytes(b"VAE" * 100)
    wait_for(lambda: catalog.state("essai")["state"] == "bad")
    vae.write_bytes(b"vae" * 50)                     # taille différente : refusé tout de suite
    assert catalog.state("essai", check=False)["state"] == "bad"
    vae.unlink()
    (tiny_model["folder"] / "vae.gguf.part").write_bytes(b"v")
    assert catalog.file_state(tiny_model["files"]["vae"]) == "partial"
    assert catalog.state("essai")["state"] == "missing"
    assert catalog.state("essai")["missing_bytes"] == 300
    with pytest.raises(catalog.ModelError):
        catalog.files("essai")


# ----------------------------------------------------- partage du travail
def test_only_a_machine_with_the_same_files_can_compute_an_image(tiny_model):
    need = kind.requires_image({"payload": {"model": "essai"}}, {})
    hashes = need["images"]["files"]
    assert len(hashes) == 3
    assert work.can_serve(need, {"images": {"engine": "f0aac52", "files": hashes}}, {})
    assert not work.can_serve(need, {"images": {"engine": "f0aac52", "files": hashes[1:]}}, {})
    assert not work.can_serve(need, {"images": {"engine": None, "files": hashes}}, {})
    assert not work.can_serve(need, {"models": {}}, {})


class _Executor:
    def publish_job(self, job_id, force=False):
        pass


class _Manager:
    executor = _Executor()

    def name(self):
        return "PC portable"


def test_images_go_to_my_machines_never_to_a_friend(tiny_model, monkeypatch):
    """Sans Ollama ici, une image se confie quand même (elle n'en a pas besoin)."""
    def down(max_age=10):
        raise ConnectionError("Ollama arrêté")
    monkeypatch.setattr(work.ollama, "list_models", down)
    peers.save(B, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(FRIEND, "PC d'un ami", b"a" * 32, "127.0.0.1", 2, trust="friend")
    params = kind.normalize({"model": "essai", "prompt": "un chat", "count": 2})
    queue.create_job("generate_images", params)
    hashes = catalog.hashes("essai")
    caps = {"work_api": work.WORK_API, "images": {"engine": "f0aac52", "files": hashes}}
    ask = lambda node, c=caps: work.lease(_Manager(), node, {"resources": ["gpu"], "caps": c})
    assert ask(FRIEND)["unit"] is None
    assert ask(B, {**caps, "images": {"engine": "f0aac52", "files": []}})["unit"] is None
    order = ask(B)["unit"]
    assert order["task"] == "generate_image" and order["blob"] is None
    assert order["payload"]["prompt"] == "un chat" and order["payload"]["seed"] == params["seed"]


def test_a_friend_gets_images_only_if_the_owner_allows_it(tiny_model, monkeypatch):
    """Réglage images_from_friends : mes images peuvent partir chez un ami, avec la consigne
    et les réglages seulement (shared_keys), pas le lot ni l'ordre dans le lot."""
    monkeypatch.setattr(work.ollama, "list_models", lambda max_age=10: [])
    peers.save(FRIEND, "PC d'un ami", b"a" * 32, "127.0.0.1", 2, trust="friend")
    params = kind.normalize({"model": "essai", "prompt": "un chat", "count": 1})
    queue.create_job("generate_images", params)
    caps = {"work_api": work.WORK_API, "images": {"engine": "f0aac52", "files": catalog.hashes("essai")}}
    ask = lambda: work.lease(_Manager(), FRIEND, {"resources": ["gpu"], "caps": caps})
    assert ask()["unit"] is None                                       # par défaut : non
    settings.set_value("images_from_friends", True)
    order = ask()["unit"]
    assert order["task"] == "generate_image"
    assert set(order["payload"]) <= {"model", "prompt", "negative", "width", "height", "steps",
                                     "cfg", "frames", "seed"}
    assert order["payload"]["prompt"] == "un chat"


def test_an_image_this_machine_cannot_compute_waits_for_another(tiny_model, monkeypatch):
    ready()
    queue.create_job("generate_images", kind.normalize({"model": "essai", "prompt": "x"}))
    monkeypatch.setattr(engine, "folder", lambda: None)          # pas de stable-diffusion.cpp
    assert queue.claim_next(["gpu"], "local") is None
    unit = db.query_one("SELECT status FROM units")
    assert unit["status"] == "queued"
    monkeypatch.setattr(engine, "folder", lambda: tiny_model["folder"])
    assert queue.claim_next(["gpu"], "local")["task"] == "generate_image"


# ---------------------------------------------------------- faux sd-server
class FakeSD:
    """sd-server imité : file de calculs, état, annulation."""

    def __init__(self, polls=2, fail=None):
        self.polls, self.fail = polls, fail
        self.jobs, self.bodies, self.cancelled = {}, [], []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/sdcpp/v1/capabilities":
                    return self._send(200, {"modes": ["img_gen"]})
                job = fake.jobs.get(self.path.rsplit("/", 1)[-1])
                if job is None:
                    return self._send(404, {"error": "inconnu"})
                job["seen"] += 1
                if job["cancelled"]:
                    return self._send(200, {"status": "cancelled",
                                            "error": {"message": "job cancelled by client"}})
                if fake.polls is None or job["seen"] < fake.polls:
                    return self._send(200, {"status": "generating"})
                if fake.fail:
                    return self._send(200, {"status": "failed", "error": {"message": fake.fail}})
                if job.get("frames"):
                    return self._send(200, {"status": "completed", "result": {
                        "output_format": "webm", "mime_type": "video/webm", "fps": 16,
                        "frame_count": job["frames"], "b64_json": base64.b64encode(WEBM).decode()}})
                image = base64.b64encode(png(job["w"], job["h"])).decode()
                return self._send(200, {"status": "completed", "result": {
                    "output_format": "png", "images": [{"index": 0, "b64_json": image}]}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                if self.path.endswith("/cancel"):
                    job_id = self.path.split("/")[-2]
                    fake.cancelled.append(job_id)
                    fake.jobs[job_id]["cancelled"] = True
                    return self._send(200, {"status": "cancelled"})
                fake.bodies.append(data)
                job_id = f"job_{len(fake.jobs) + 1}"
                fake.jobs[job_id] = {"seen": 0, "cancelled": False, "path": self.path,
                                     "w": data["width"], "h": data["height"],
                                     "frames": data.get("video_frames")}
                return self._send(202, {"id": job_id, "status": "queued"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_sd(monkeypatch):
    servers = []

    def make(**options):
        fake = FakeSD(**options)
        servers.append(fake)
        monkeypatch.setattr(engine, "acquire", lambda model_id, stop=None, waiting=None: fake.url)
        monkeypatch.setattr(engine, "release", lambda: None)
        monkeypatch.setattr(generate, "POLL_S", 0.02)
        return fake
    yield make
    for fake in servers:
        fake.close()


def _payload(**extra):
    return {"model": "essai", "prompt": "un chat", "negative": "", "width": 256, "height": 384,
            "steps": 4, "cfg": 1.0, "seed": 42, "index": 0, "batch": "abc123", **extra}


def test_an_image_is_asked_followed_and_received(tiny_model, fake_sd):
    fake = fake_sd(polls=3)
    made = generate.image(_payload())
    assert generate.png_size(made["png"]) == (256, 384)
    body = fake.bodies[0]
    assert body["seed"] == 42 and body["sample_params"]["sample_steps"] == 4
    assert body["embed_image_metadata"] is False          # la consigne ne part pas avec l'image


def test_stopping_cancels_the_image_there(tiny_model, fake_sd):
    fake = fake_sd(polls=None)                            # ne finit jamais
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    with pytest.raises(Interrupted):
        generate.image(_payload(), stop=stop)
    assert fake.cancelled == ["job_1"]


def test_a_failure_there_is_reported(tiny_model, fake_sd):
    fake_sd(fail="out of memory")
    with pytest.raises(generate.ImageError, match="out of memory"):
        generate.image(_payload())


# ------------------------------------------------------- contrôle, galerie
def test_an_image_from_elsewhere_is_checked_then_kept_here(tiny_model):
    payload = _payload()
    good = {"png": base64.b64encode(png(256, 384)).decode(), "seconds": 3.24, "machine": "PC fixe"}
    kind.check_image(payload, good)
    for wrong in ({"png": base64.b64encode(png(256, 256)).decode()},
                  {"png": base64.b64encode(b"pas une image").decode()},
                  {"png": "%%%"}):
        with pytest.raises(ValueError):
            kind.check_image(payload, wrong)
    result = kind.assimilate_image(payload, good)
    row = gallery.get(result["image_id"])
    assert row["machine"] == "PC fixe" and row["seconds"] == 3.24 and row["seed"] == 42
    assert (row["width"], row["height"]) == (256, 384)
    assert gallery.path_of(row).read_bytes() == png(256, 384)
    assert [r["id"] for r in gallery.list_images()] == [row["id"]]
    assert gallery.delete(row["id"]) and not gallery.path_of(row).exists()
    assert gallery.list_images() == []


def test_new_images_go_to_the_chosen_folder_and_older_ones_stay_found(tiny_model, tmp_path):
    made = {"png": base64.b64encode(png(256, 384)).decode()}
    old = gallery.get(kind.assimilate_image(_payload(), made)["image_id"])
    assert gallery.path_of(old).parent == gallery.default_dir()
    chosen = tmp_path / "Mes créations"
    settings.set_value("images_out_dir", str(chosen))
    new = gallery.get(kind.assimilate_image(_payload(seed=43), made)["image_id"])
    assert gallery.path_of(new).parent == chosen and gallery.path_of(new).is_file()
    assert gallery.path_of(old).read_bytes() == png(256, 384)     # toujours là où elle a été écrite
    other = tmp_path / "Autres créations"                        # d'un dossier choisi à un autre
    settings.set_value("images_out_dir", str(other))
    assert gallery.path_of(gallery.get(new["id"])).read_bytes() == png(256, 384)
    blocker = tmp_path / "un-fichier"
    blocker.write_text("pas un dossier")
    settings.set_value("images_out_dir", str(blocker / "dessous"))   # disque débranché…
    assert gallery.list_images() and gallery.path_of(gallery.get(new["id"])).is_file()


# -------------------------------------------------------------------- API
@pytest.fixture
def client():
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


def test_api_lists_models_and_refuses_what_nobody_can_compute(client):
    data = client.get("/api/images/models", headers=AUTH).json()
    assert data["engine"]["found"] is False
    turbo = next(m for m in data["models"] if m["id"] == "z-image-turbo")
    assert turbo["state"] == "missing" and turbo["license"] == "Apache-2.0"
    reply = client.post("/api/images/generate", headers=AUTH,
                        json={"model": "z-image-turbo", "prompt": "un chat"})
    assert reply.status_code == 409 and "télécharge" in reply.json()["detail"]
    assert client.post("/api/images/generate", headers=AUTH,
                       json={"model": "z-image-turbo", "prompt": ""}).status_code == 400


def test_api_makes_the_images_here_and_serves_them(tiny_model, monkeypatch, client):
    ready()
    monkeypatch.setattr(engine, "folder", lambda: tiny_model["folder"])
    monkeypatch.setattr(generate, "image",
                        lambda p, stop=None, waiting=None: {"png": png(p["width"], p["height"]),
                                                            "seconds": 0.5})
    reply = client.post("/api/images/generate", headers=AUTH,
                        json={"model": "essai", "prompt": "un chat", "count": 2,
                              "width": 256, "height": 256})
    assert reply.status_code == 200 and reply.json()["here"]
    rows = wait_for(lambda: len(client.get("/api/images", headers=AUTH).json()) == 2
                    and client.get("/api/images", headers=AUTH).json())
    assert {r["prompt"] for r in rows} == {"un chat"}
    first = rows[0]["id"]
    image = client.get(f"/api/images/{first}/file?t={TOKEN}")      # balise <img> : jeton en paramètre
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert client.get(f"/api/images/{first}/file").status_code == 401
    assert client.delete(f"/api/images/{first}", headers=AUTH).json() == {"ok": True}
    assert len(client.get("/api/images", headers=AUTH).json()) == 1
    job = queue.list_jobs()[0]
    wait_for(lambda: queue.job_summary(job["id"])["status"] == "done")
    assert queue.job_summary(job["id"])["result"]["images"] == 2


# ----------------------------------------------------------- téléchargement
class FileServer:
    """Serveur de fichiers qui comprend « Range » (reprise), et note ce qu'on lui demande."""

    def __init__(self, contents: dict):
        self.contents, self.ranges = contents, []
        files = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = files.contents.get(self.path.lstrip("/"))
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                start = 0
                asked = self.headers.get("Range")
                files.ranges.append(asked)
                if asked:
                    start = int(re.match(r"bytes=(\d+)-", asked).group(1))
                self.send_response(206 if asked else 200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def _point_to(tiny_model, server):
    for spec in tiny_model["files"].values():
        spec["url"] = f"{server.base}/{spec['name']}"
        (tiny_model["folder"] / spec["name"]).unlink()


def test_a_model_is_downloaded_checked_and_resumed(tiny_model):
    server = FileServer(dict(tiny_model["contents"]))
    try:
        _point_to(tiny_model, server)
        # Un téléchargement coupé à moitié reprend où il en était
        coeur = tiny_model["contents"]["diffusion.gguf"]
        (tiny_model["folder"] / "diffusion.gguf.part").write_bytes(coeur[:2000])
        download.start("essai")
        wait_for(lambda: not download.progress()["running"])
        state = download.progress()
        assert state["error"] is None and state["done"] == state["total"] == catalog.total_size("essai")
        assert "bytes=2000-" in server.ranges
        assert catalog.state("essai", check=False)["state"] == "ready"   # vérifié en route
    finally:
        server.close()


def test_a_different_file_is_refused(tiny_model):
    contents = dict(tiny_model["contents"])
    contents["llm.gguf"] = b"TEXTE" * 500                   # même taille, autre contenu
    server = FileServer(contents)
    try:
        _point_to(tiny_model, server)
        download.start("essai")
        wait_for(lambda: not download.progress()["running"])
        assert "différent" in download.progress()["error"]
        assert not (tiny_model["folder"] / "llm.gguf").exists()
        assert not (tiny_model["folder"] / "llm.gguf.part").exists()
    finally:
        server.close()


# ----------------------------------------------------- carte graphique partagée
def test_a_memory_shortage_restarts_the_engine_once(tiny_model, fake_sd, monkeypatch):
    """Ollama a repris la carte entre-temps : sd-server relancé (nouveau plan de placement),
    et l'image refaite une fois."""
    fake = fake_sd(fail="generate_image returned no results")
    stopped = []
    monkeypatch.setattr(engine, "stop", lambda: (stopped.append(1), setattr(fake, "fail", None)))
    output = kind.compute_image(_payload(), None, {})
    assert stopped == [1] and len(fake.bodies) == 2
    assert generate.png_size(base64.b64decode(output["png"])) == (256, 384)


class _Process:
    port, alive = 1, True

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_a_chat_starting_here_takes_the_card_back(monkeypatch):
    from aiodos.jobs import pacing
    process = _Process()
    monkeypatch.setitem(engine._state, "process", process)
    monkeypatch.setitem(engine._state, "model", "essai")
    monkeypatch.setitem(engine._state, "busy", 1)          # une image en cours : elle finit
    engine._yield_to_chat()
    assert not process.stopped
    engine._state["busy"] = 0
    pacing.set_interactive(1)                              # une conversation commence
    try:
        wait_for(lambda: process.stopped)
        assert engine._state["process"] is None
    finally:
        pacing.set_interactive(0)


def test_only_chat_models_leave_the_card(monkeypatch):
    from aiodos.services import ollama
    monkeypatch.setattr(ollama, "loaded_models", lambda: [
        {"name": "qwen3:8b", "size": 5, "size_vram": 5},
        {"name": "embeddinggemma:latest", "size": 1, "size_vram": 1},
        {"name": "llama3:8b", "size": 5, "size_vram": 0}])              # déjà hors de la carte
    monkeypatch.setattr(ollama, "capabilities", lambda name: (
        ["embedding"] if "embedding" in name else ["completion"]))
    sent = []
    monkeypatch.setattr(ollama.requests, "post", lambda url, json, timeout: sent.append(json))
    assert ollama.unload_chat_models() == ["qwen3:8b"]
    assert sent == [{"model": "qwen3:8b", "keep_alive": 0}]


# ------------------------------------------------- copie depuis une de mes machines
def test_model_files_are_served_to_my_machines_only(tiny_model):
    from aiodos.imaging import share
    from aiodos.network.client import PeerError
    from aiodos.network.envelope import b64d
    ready()
    peers.save(B, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(FRIEND, "PC d'un ami", b"a" * 32, "127.0.0.1", 2, trust="friend")
    spec = tiny_model["files"]["diffusion"]
    reply = share.serve_file(None, B, {"sha256": spec["sha256"], "offset": 10, "size": 20})
    assert b64d(reply["data"]) == tiny_model["contents"]["diffusion.gguf"][10:30]
    with pytest.raises(PeerError, match="mes machines"):
        share.serve_file(None, FRIEND, {"sha256": spec["sha256"]})
    with pytest.raises(PeerError, match="indisponible"):              # pas dans le catalogue
        share.serve_file(None, B, {"sha256": "0" * 64})
    (tiny_model["folder"] / "diffusion.gguf").write_bytes(b"x" * spec["size"])   # abîmé
    with pytest.raises(PeerError, match="indisponible"):
        share.serve_file(None, B, {"sha256": spec["sha256"]})


def test_a_model_is_copied_from_one_of_my_machines(tiny_model, tmp_path, monkeypatch):
    """La machine B a le modèle ; on le copie chez nous par morceaux, empreintes vérifiées."""
    from aiodos.imaging import share
    ready()
    source_folder, here = tiny_model["folder"], tmp_path / "ici"
    here.mkdir()
    peers.save(B, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    monkeypatch.setattr(share, "CHUNK", 1000)
    monkeypatch.setattr(share.route, "lan_first", lambda peer: True)
    asked = []

    def call(my_id, peer, action, payload, key=None, timeout=10):
        """Ce que B répond (son dossier de modèles), le temps de la demande."""
        asked.append(payload["offset"])
        settings.set_value("images_models_dir", str(source_folder))
        try:
            return share.HANDLERS[action](None, B, payload)
        finally:
            settings.set_value("images_models_dir", str(here))
    monkeypatch.setattr(share.client, "call", call)
    monkeypatch.setattr(share.identity, "node_id", lambda: "machine-a")
    settings.set_value("images_models_dir", str(here))
    assert catalog.state("essai")["state"] == "missing"
    state = download.start("essai", source=B)
    assert state["source"] == "PC fixe"
    wait_for(lambda: not download.progress()["running"])
    assert download.progress()["error"] is None
    assert catalog.state("essai", check=False)["state"] == "ready"
    assert (here / "diffusion.gguf").read_bytes() == tiny_model["contents"]["diffusion.gguf"]
    assert asked[:6] == [0, 1000, 2000, 3000, 4000, 0]                   # 5000 octets en 5 fois
    with pytest.raises(download.DownloadError, match="des miennes"):
        download.start("essai", source="inconnue")


def test_the_profile_never_wakes_the_card(tmp_path, monkeypatch):
    """Le profil lit souvent la version : « --version » seulement, une fois par programme ;
    « --list-devices » (qui charge les pilotes) n'est lu que pour choisir la carte."""
    from aiodos.updates import platforms
    folder = tmp_path / "sd"
    folder.mkdir()
    (folder / platforms.executable("sd-server")).write_bytes(b"MZ")
    settings.set_value("sdcpp_dir", str(folder))
    ran = []

    def run(program, *args):
        ran.append(args[0])
        return ("stable-diffusion.cpp version unknown, commit f0aac52" if args[0] == "--version"
                else "Vulkan0\tIntel(R) UHD Graphics\nVulkan1\tNVIDIA GeForce RTX 4070\nCPU\tIntel")
    monkeypatch.setattr(engine, "_run", run)
    for _ in range(3):
        assert engine.info()["version"] == "f0aac52"
        assert engine.capabilities()["engine"] == "f0aac52"
    assert ran == ["--version"]
    assert engine.best_device(engine.devices())["name"] == "Vulkan1"   # pas l'intégrée
    engine.devices()
    assert ran == ["--version", "--list-devices"]
    settings.set_value("images_device", "CPU")                        # choisi dans les réglages
    assert engine.best_device(engine.devices())["name"] == "CPU"


# ------------------------------------------------------------ relecture du 17/10
def test_images_waiting_for_another_machine_hold_nothing_back(tiny_model, monkeypatch):
    """32 images que cette machine ne sait pas calculer, en urgence, devant une unité
    processeur : celle-ci passe quand même, et la carte reste prêtable aux autres."""
    from aiodos.jobs import leases
    from aiodos.jobs.registry import UnitSpec, register_job_kind, task
    monkeypatch.setattr(engine, "folder", lambda: None)               # pas de moteur ici

    @task("essai_cpu")
    def run(payload):
        return {}
    register_job_kind("essai_cpu", lambda params: ("Essai", [UnitSpec("essai_cpu", "cpu")], {}))
    for _ in range(2):
        queue.create_job("generate_images",
                         kind.normalize({"model": "essai", "prompt": "x", "count": 16}), 1)
    queue.create_job("essai_cpu", {}, 3)
    unit = queue.claim_next(["gpu", "cpu"], "local")
    assert unit is not None and unit["task"] == "essai_cpu"
    assert leases.local_demand(["gpu", "cpu"]) == set()             # rien d'autre en attente


def test_a_machine_asking_for_work_sees_past_what_it_cannot_do(tiny_model):
    """Une machine sans le modèle d'images reçoit l'unité qui suit les 250 images."""
    from aiodos.jobs import leases
    for _ in range(16):
        queue.create_job("generate_images",
                         kind.normalize({"model": "essai", "prompt": "x", "count": 16}), 1)
    params = kind.normalize({"model": "essai", "prompt": "la bonne", "count": 1})
    queue.create_job("generate_images", params, 2)
    unit = leases.claim_remote(["gpu"], B, lambda u: u["payload"]["prompt"] == "la bonne")
    assert unit is not None and unit["payload"]["prompt"] == "la bonne"


def test_stopping_while_the_engine_loads_hands_the_unit_back(tiny_model, monkeypatch):
    """Arrêtée pendant le chargement de sd-server (conversation, AIODOS qui s'arrête…),
    l'unité est rendue (Interrupted), pas comptée en échec."""
    from aiodos.cluster.engine import EngineError
    ready()
    monkeypatch.setattr(engine, "info", lambda: {"folder": str(tiny_model["folder"]),
                                                 "version": "f0aac52"})
    monkeypatch.setattr(engine, "devices", lambda max_age=0: [])
    monkeypatch.setattr(engine.ollama, "unload_chat_models", lambda: [])

    class Loading:
        def __init__(self, *args, **kwargs):
            pass

        def start(self, timeout, ready=None, stop=None, waiting=None):
            raise EngineError("Chargement interrompu.")
    monkeypatch.setattr(engine, "Process", Loading)
    stop = threading.Event()
    stop.set()
    with pytest.raises(Interrupted):
        engine.acquire("essai", stop=stop)
    with pytest.raises(EngineError):                                  # sans arrêt : un échec
        engine.acquire("essai", stop=threading.Event())


def test_one_image_at_a_time_and_waiting_can_be_stopped(tiny_model, fake_sd):
    fake = fake_sd(polls=None)                                        # la première ne finit pas
    first_stop, second_stop = threading.Event(), threading.Event()
    outcome = {}

    def first():
        try:
            generate.image(_payload(), stop=first_stop)
        except Interrupted:
            outcome["first"] = "rendue"
    runner = threading.Thread(target=first)
    runner.start()
    time.sleep(0.3)
    threading.Timer(0.3, second_stop.set).start()
    with pytest.raises(Interrupted):                                  # attendait son tour
        generate.image(_payload(seed=43), stop=second_stop)
    assert len(fake.bodies) == 1                                      # jamais envoyée
    first_stop.set()
    runner.join(5)
    assert outcome == {"first": "rendue"}


def test_an_image_order_is_checked_by_the_machine_that_computes_it(tiny_model, fake_sd):
    fake_sd()
    for wrong in ({"width": 4096}, {"steps": 1000}, {"prompt": ""}, {"model": "autre"}):
        with pytest.raises(ValueError):
            kind.compute_image(_payload(**wrong), None, {})
    assert kind.compute_image(_payload(), None, {})["png"]


def test_an_image_order_from_a_friend_is_refused(tiny_model, monkeypatch):
    from aiodos.network import worker as worker_module
    peers.save(FRIEND, "PC d'un ami", b"a" * 32, "127.0.0.1", 2, trust="friend")
    sent = []

    class Manager:
        executor = None

    w = worker_module.Worker.__new__(worker_module.Worker)
    w.m = Manager()
    w._gpu_rest_until = 0.0
    monkeypatch.setattr(w, "_quiet", lambda a, action, **extra: sent.append((action, extra)),
                        raising=False)
    order = {"id": 1, "task": "generate_image", "resource": "gpu", "payload": _payload(),
             "options": {}, "blob": None}
    outcome = w._process(worker_module.Assignment(FRIEND, "PC d'un ami", order))
    assert "réservée à mes machines" in outcome
    assert sent and sent[0][0] == "work/fail"
    # Avec le réglage « Prêter ma carte graphique à mes amis pour les images », calculée
    settings.set_value("lend_images_friends", True)
    monkeypatch.setattr(worker_module.Worker, "_send_result",
                        lambda self, a, output, compute_s: {"accepted": True}, raising=False)
    monkeypatch.setattr(w, "_rest_gpu", lambda a, s: None, raising=False)
    monkeypatch.setattr(kind.generate, "image", lambda payload, halt: {"png": png(64, 64), "seconds": 0.1})
    assert w._process(worker_module.Assignment(FRIEND, "PC d'un ami", order)).startswith("faite")


def test_a_friend_sees_my_images_only_if_i_lend_them():
    from aiodos.network import p2p_app, sharing
    peers.save(FRIEND, "PC d'un ami", b"a" * 32, "127.0.0.1", 2, trust="friend")
    full = {"node_id": "moi", "name": "PC", "images": {"engine": "x", "models": ["essai"], "shares": True}}
    assert "images" not in sharing.profile_for(FRIEND, full)
    settings.set_value("lend_images_friends", True)
    assert sharing.profile_for(FRIEND, full)["images"]["models"] == ["essai"]


# --------------------------------------------------------------------- vidéos
@pytest.fixture
def tiny_video(tiny_model, monkeypatch):
    """Le modèle « essai » devenu un modèle vidéo à la façon de Wan (4n + 1 images)."""
    spec = dict(catalog.MODELS["essai"], kind="video", cfg=6.0, fps=16, flow_shift=3.0,
                sampler="euler", default_negative="flou", sizes=[[832, 480, "Paysage"]],
                multiple=16, side=[256, 1280], max_pixels=1024 * 576, frame_step=4,
                frame_base=1, max_frames=161, default_frames=33, durations=[[33, "2 s"]])
    monkeypatch.setitem(catalog.MODELS, "video", spec)
    return spec


def test_a_video_request_follows_the_rules_of_its_model(tiny_video):
    params = kind.normalize({"model": "video", "prompt": "un renard dans la neige"})
    assert (params["width"], params["height"], params["frames"]) == (832, 480, 33)
    assert kind.normalize({"model": "video", "prompt": "x", "frames": 81})["frames"] == 81
    for wrong, message in (({"frames": 34}, r"4 × k \+ 1"), ({"width": 830}, "multiples de 16"),
                           ({"width": 1280, "height": 720}, "trop grande")):
        with pytest.raises(ValueError, match=message):
            kind.normalize({"model": "video", "prompt": "x", **wrong})
    title, units, _ = kind.split_generate(kind.normalize(
        {"model": "video", "prompt": "un renard", "count": 2}))
    assert title.startswith("Vidéos : ") and {u.payload["frames"] for u in units} == {33}
    # H3 : 17 k + 5 images, côtés en multiples de 32
    h3 = catalog.MODELS["minimax-h3"]
    assert kind.normalize({"model": "minimax-h3", "prompt": "x"})["frames"] == 56
    with pytest.raises(ValueError, match="17 × k"):
        kind.normalize({"model": "minimax-h3", "prompt": "x", "frames": 57})
    assert h3["notice"] and "Union européenne" in h3["notice"]


def test_a_video_is_made_checked_and_kept(tiny_video, fake_sd):
    fake = fake_sd()
    payload = {**_payload(model="video", width=832, height=480), "frames": 33, "negative": ""}
    output = kind.compute_image(payload, None, {})
    assert fake.jobs["job_1"]["path"].endswith("/vid_gen")
    body = fake.bodies[0]
    assert body["video_frames"] == 33 and body["fps"] == 16 and body["output_format"] == "webm"
    assert body["negative_prompt"] == "flou"                        # consigne négative du modèle
    assert body["sample_params"]["flow_shift"] == 3.0
    assert output["frames"] == 33 and output["fps"] == 16
    kind.check_image(payload, output)
    with pytest.raises(ValueError, match="WebM"):
        kind.check_image(payload, {"webm": base64.b64encode(png(8, 8)).decode()})
    row = gallery.get(kind.assimilate_image(payload, output)["image_id"])
    assert row["media"] == "video" and row["frames"] == 33 and row["fps"] == 16
    assert row["file"].endswith(".webm") and gallery.path_of(row).read_bytes() == WEBM


def test_the_api_serves_a_video_as_webm(tiny_video, client):
    payload = {**_payload(model="video", width=832, height=480), "frames": 33}
    row = gallery.add(payload, WEBM, machine="PC fixe", seconds=60.0, frames=33, fps=16)
    reply = client.get(f"/api/images/{row['id']}/file?t={TOKEN}")
    assert reply.status_code == 200 and reply.headers["content-type"] == "video/webm"
    models = {m["id"]: m for m in client.get("/api/images/models", headers=AUTH).json()["models"]}
    assert models["wan2.1-t2v-1.3b"]["kind"] == "video" and models["wan2.1-t2v-1.3b"]["durations"]
    assert models["minimax-h3"]["notice"]


def test_a_late_verification_writes_where_it_started(tiny_model, tmp_path, monkeypatch):
    """Une empreinte calculée en arrière-plan finit après son contexte (fin d'un essai) :
    elle s'écrit dans le cache des données où elle a commencé, jamais ailleurs."""
    spec = tiny_model["files"]["vae"]
    path = tiny_model["folder"] / spec["name"]
    first = catalog.cache_file_path()
    gate = threading.Event()
    real = catalog.sha256_of

    def slow(target, stop=None):
        gate.wait(5)
        return real(target, stop)
    monkeypatch.setattr(catalog, "sha256_of", slow)
    catalog._verify_later(path, spec)
    monkeypatch.setenv("AIODOS_DATA_DIR", str(tmp_path / "ailleurs"))   # le contexte change
    gate.set()
    wait_for(lambda: first.exists() and str(path) in json.loads(first.read_text(encoding="utf-8")))
    assert not (tmp_path / "ailleurs" / "images" / "verified.json").exists()
