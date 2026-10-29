"""L'audio (docs/conception/audio.md) : le catalogue épinglé, les fichiers vérifiés, le
téléchargement, audio.cpp (sa version, ses appareils, la commande de chaque modèle, son arrêt),
le WAV contrôlé, le travail « generate_audio » et sa place sur le réseau (mes machines, mes amis
s'ils le proposent), la galerie."""
import base64
import hashlib
import io
import json
import math
import re
import struct
import sys
import threading
import wave

import pytest

from aiodos import i18n, settings
from aiodos.audio import catalog, download, engine, files, generate, wav
from aiodos.imaging import gallery
from aiodos.jobs import queue
from aiodos.jobs.kinds import audio as kind
from aiodos.jobs.registry import Interrupted
from aiodos.network import peers, profile, sharing, work
from tests.test_images import FileServer, wait_for

B = "b" * 32
FRIEND = "f" * 32


def tone(seconds=1.0, rate=24000, amplitude=0.5, channels=1) -> bytes:
    """Un WAV PCM 16 bits : un la (440 Hz)."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(amplitude * 32767 * math.sin(
            2 * math.pi * 440 * i / rate))) * channels for i in range(frames)))
    return out.getvalue()


def float_wav(values, rate=16000, extensible=False) -> bytes:
    """Un WAV d'échantillons flottants 32 bits (format 3, ou WAVE_FORMAT_EXTENSIBLE)."""
    data = struct.pack(f"<{len(values)}f", *values)
    if extensible:
        fmt = struct.pack("<HHIIHHHHIH14s", 0xFFFE, 1, rate, rate * 4, 4, 32, 22, 32, 0, 3,
                          b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71")
    else:
        fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt \
        + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture
def tiny_audio(tmp_path, monkeypatch):
    """Deux petits modèles d'essai (une voix et sa voix « anna », une musique), posés dans un
    dossier à part, et un audio.cpp « présent »."""
    folder = tmp_path / "audio-modeles"
    contents = {"Voix/voix.gguf": b"voix" * 1000, "Voix/embeddings/anna.safetensors": b"anna" * 50,
                "Musique/musique.gguf": b"musique" * 700}
    specs = {}
    for path, content in contents.items():
        target = folder.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        specs[path] = {"path": path, "size": len(content),
                       "sha256": hashlib.sha256(content).hexdigest(),
                       "url": f"http://127.0.0.1:9/{path}"}
    monkeypatch.setitem(catalog.MODELS, "essai-voix", {
        "label": "Voix d'essai", "genre": catalog.VOICE, "family": "pocket_tts", "task": "tts",
        "license": "Apache-2.0", "about": "Pour les essais.",
        "files": {"model": specs["Voix/voix.gguf"],
                  "voice_anna": specs["Voix/embeddings/anna.safetensors"]},
        "rank": 50, "vram": 512, "languages": ["fr"],
        "voices": {"fr": [["anna", "audio_models.voice_female"], ["paul", "audio_models.voice_male"]]},
        "voice_option": "id"})
    monkeypatch.setitem(catalog.MODELS, "essai-musique", {
        "label": "Musique d'essai", "genre": catalog.MUSIC, "family": "stable_audio",
        "task": "gen", "license": "Apache-2.0", "about": "Pour les essais.",
        "files": {"model": specs["Musique/musique.gguf"]}, "rank": 50, "vram": 512,
        "lyrics": None, "seconds": [5, 10], "default_seconds": 5, "max_seconds": 20})
    settings.set_value("audio_models_dir", str(folder))
    monkeypatch.setattr(engine, "folder", lambda: tmp_path)
    monkeypatch.setattr(engine, "info", lambda: {"folder": str(tmp_path), "version": "0.9.3 (essai)"})
    return {"folder": folder, "specs": specs, "contents": contents}


def ready(model_id):
    return wait_for(lambda: files.state(model_id)["state"] == "ready")


# ------------------------------------------------------------------ catalogue
def test_every_model_of_the_catalog_is_pinned_and_described():
    for model_id, spec in catalog.MODELS.items():
        assert spec["genre"] in catalog.GENRES, model_id
        assert spec["family"] and spec["task"] in ("tts", "gen"), model_id
        assert spec["license"] and spec["about"], model_id
        for f in spec["files"].values():
            assert re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) and f["size"] > 0, model_id
            # Une adresse à un commit précis (40 caractères), jamais « main »
            assert re.search(r"/resolve/[0-9a-f]{40}/", f["url"]), f["url"]
            assert f["url"].endswith(f["path"].split("/", 1)[-1] if spec.get("model_dir")
                                     else f["path"]), f["url"]
        assert bool(spec.get("model_dir")) or "model" in spec["files"], model_id
        if spec["genre"] == catalog.VOICE:
            assert spec["languages"] and spec["voice_option"] in ("id", "speaker", "request")
            for language in spec["languages"]:
                assert catalog.voices(model_id, language), (model_id, language)
        else:
            assert spec["default_seconds"] <= spec["max_seconds"]
            assert all(s <= spec["max_seconds"] for s in spec["seconds"])
        for key in (spec["about"], spec.get("notice")):
            assert key is None or i18n.exists(key), key
    # Un fichier commun à deux modèles (MiDashengLM-Gen : musique et effets) : le même
    by_path = {}
    for spec in catalog.MODELS.values():
        for f in spec["files"].values():
            assert by_path.setdefault(f["path"], f["sha256"]) == f["sha256"]


def test_licenses_other_than_apache_mit_or_cc_by_are_read_before_download():
    for model_id, spec in catalog.MODELS.items():
        free = spec["license"] in ("Apache-2.0", "MIT", "CC BY 4.0")
        assert catalog.restricted(model_id) == (not free), model_id
    assert catalog.restricted("voxtral-4b-tts") and not catalog.accepted("voxtral-4b-tts")
    catalog.accept("voxtral-4b-tts")
    assert catalog.accepted("voxtral-4b-tts")
    assert catalog.accepted("pocket-tts-fr")                  # libre : rien à accepter
    with pytest.raises(catalog.ModelError):
        catalog.accept("inconnu")


def test_each_genre_is_ranked_from_the_most_powerful():
    from aiodos.audio import ranking
    assert ranking.candidates("voice")[0] == "voxtral-4b-tts"
    assert ranking.candidates("music")[0] == "minimax-music-3"
    assert ranking.candidates("sound")[0] == "stable-audio-3-small-sfx"
    # Une voix, seulement dans les langues qu'elle parle
    assert "pocket-tts-en" not in ranking.candidates("voice", "fr")
    assert "pocket-tts-fr" not in ranking.candidates("voice", "en")
    assert catalog.voices("qwen3-tts-1.7b", "fr")[0][0] == "Serena"     # « * » : toutes


# ------------------------------------------------------------------ fichiers
def test_files_are_verified_once_and_changes_are_seen(tiny_audio):
    assert files.state("essai-voix")["state"] in ("checking", "ready")
    assert ready("essai-voix")
    path = files.model_path("essai-voix")
    assert path == tiny_audio["folder"] / "Voix" / "voix.gguf"
    (tiny_audio["folder"] / "Voix" / "voix.gguf").write_bytes(b"VOIX" * 1000)   # même taille
    assert wait_for(lambda: files.state("essai-voix")["state"] == "bad")
    with pytest.raises(catalog.ModelError):
        files.model_path("essai-voix")


def test_a_model_is_downloaded_into_its_folders_and_resumed(tiny_audio):
    server = FileServer({path: content for path, content in tiny_audio["contents"].items()})
    try:
        for spec in tiny_audio["specs"].values():
            spec["url"] = f"{server.base}/{spec['path']}"
        voice = tiny_audio["folder"] / "Voix"
        for path in voice.rglob("*"):
            if path.is_file():
                path.unlink()
        content = tiny_audio["contents"]["Voix/voix.gguf"]
        (voice / "voix.gguf.part").write_bytes(content[:1500])
        download.start("essai-voix")
        wait_for(lambda: not download.progress()["running"])
        state = download.progress()
        assert state["error"] is None and state["done"] == state["total"]
        assert "bytes=1500-" in server.ranges
        assert (voice / "embeddings" / "anna.safetensors").read_bytes() == b"anna" * 50
        assert files.state("essai-voix", check=False)["state"] == "ready"
    finally:
        server.close()


def test_a_different_file_is_refused(tiny_audio):
    server = FileServer({"Musique/musique.gguf": b"MUSIQUE" * 700})
    try:
        spec = tiny_audio["specs"]["Musique/musique.gguf"]
        spec["url"] = f"{server.base}/Musique/musique.gguf"
        (tiny_audio["folder"] / "Musique" / "musique.gguf").unlink()
        download.start("essai-musique")
        wait_for(lambda: not download.progress()["running"])
        assert "ne correspond pas" in download.progress()["error"]
        assert not (tiny_audio["folder"] / "Musique" / "musique.gguf").exists()
    finally:
        server.close()


# ------------------------------------------------------------------ WAV
def test_a_wav_is_read_and_checked():
    info = wav.check(tone(1.5))
    assert info["seconds"] == pytest.approx(1.5) and info["rate"] == 24000
    assert info["peak"] == pytest.approx(0.5, abs=0.01)
    stereo = wav.check(tone(0.5, rate=48000, channels=2))
    assert stereo["channels"] == 2 and stereo["seconds"] == pytest.approx(0.5)
    floats = [0.3 * math.sin(i / 10) for i in range(16000)]
    assert wav.check(float_wav(floats))["seconds"] == pytest.approx(1.0)
    assert wav.check(float_wav(floats, extensible=True))["format"] == wav.FLOAT


@pytest.mark.parametrize("data, code", [
    (b"pas un wav", "not_wav"),
    (tone(1.0, amplitude=0.0), "silent"),
    (tone(0.05), "too_short"),
    (float_wav([0.0] * 16000), "silent"),
], ids=["not_wav", "silent", "too_short", "silent_float"])
def test_a_bad_wav_is_refused(data, code):
    with pytest.raises(wav.WavError) as error:
        wav.check(data)
    assert str(error.value) == code
    assert i18n.exists(f"audio.wav_{code}")


# ------------------------------------------------------------------ audio.cpp
def test_version_and_devices_are_read():
    assert engine.parse_version("audio.cpp 0.9.3\ngit: 41c1f8ff 2026-10-26\n") == "0.9.3 (41c1f8ff)"
    assert engine.parse_version("rien") is None
    text = ('available_devices=3\nVulkan:0 "Intel(R) RaptorLake-S Mobile Graphics Controller" [IGPU]\n'
            'Vulkan:1 "NVIDIA GeForce RTX 4070 Laptop GPU" [GPU]\nCPU:0 "Intel(R) Core(TM) i9" [CPU]\n')
    devices = engine.parse_devices(text)
    assert [d["kind"] for d in devices] == ["IGPU", "GPU", "CPU"]
    assert engine.best_device(devices)["index"] == 1            # jamais la carte intégrée
    assert engine.best_device(devices[:1])["index"] == 0       # sauf s'il n'y a qu'elle
    assert engine.best_device(devices[2:]) is None             # le processeur
    settings.set_value("audio_device", "cpu:0")
    assert engine.best_device(devices)["backend"] == "cpu"


def test_each_family_gets_its_own_command(tiny_audio, monkeypatch):
    ready("essai-voix")
    ready("essai-musique")
    monkeypatch.setattr(engine, "device_args", lambda: ["--backend", "vulkan", "--device", "1"])
    voice = generate.command(kind.checked({**kind.normalize(
        {"model": "essai-voix", "text": "Bonjour", "voice": "paul", "seed": 7}), "seed": 7}), "o.wav")
    assert voice[:4] == ["--task", "tts", "--family", "pocket_tts"]
    assert voice[voice.index("--text") + 1] == "Bonjour"
    assert voice[voice.index("--voice-id") + 1] == "paul" and "--seed" in voice
    music = generate.command(kind.normalize({"model": "essai-musique", "prompt": "calm piano",
                                             "duration": 10, "seed": 3}), "o.wav")
    assert music[music.index("--duration-seconds") + 1] == "10"
    # MiDashengLM-Gen : la description par couches, la durée en option de requête
    spec = dict(catalog.MODELS["essai-musique"], family="midashenglm_gen", prompt="midasheng")
    monkeypatch.setitem(catalog.MODELS, "essai-musique", spec)
    mixed = generate.command(kind.normalize({"model": "essai-musique", "prompt": "rain",
                                             "duration": 5, "seed": 3}), "o.wav")
    text = mixed[mixed.index("--text") + 1]
    assert text.startswith("<|caption|> rain") and "<|music|> rain" in text
    assert "<|sfx|> <|unknown|>" in text and "duration_sec=5" in mixed
    # MiniMax Music 3 : des paroles, toujours
    monkeypatch.setitem(catalog.MODELS, "essai-musique",
                        dict(spec, family="minimax_music3", prompt=None, lyrics="required"))
    song = generate.command(kind.normalize({"model": "essai-musique", "prompt": "pop",
                                            "duration": 5}), "o.wav")
    assert "lyrics=[Instrumental]" in song


def test_the_engine_runs_stops_and_reports_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "program", lambda: __import__("pathlib").Path(sys.executable))
    out = tmp_path / "x.txt"
    engine.run(["-c", f"open(r'{out}', 'w').write('ok')"])
    assert out.read_text() == "ok"
    with pytest.raises(engine.EngineError) as failure:
        engine.run(["-c", "import sys; print('modèle introuvable'); sys.exit(3)"])
    assert "introuvable" in str(failure.value)
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    with pytest.raises(Interrupted):
        engine.run(["-c", "import time; time.sleep(30)"], stop)
    with pytest.raises(engine.EngineError):
        engine.run(["-c", "import time; time.sleep(30)"], timeout=0.5)
    monkeypatch.setattr(engine, "program", lambda: None)
    with pytest.raises(engine.EngineError):
        engine.run([])


def test_a_creation_writes_reads_and_cleans_its_wav(tiny_audio, monkeypatch, tmp_path):
    ready("essai-voix")
    monkeypatch.setattr(engine, "device_args", lambda: ["--backend", "cpu"])
    seen = {}

    def fake_run(args, stop=None, timeout=0, log_file=None):
        seen["args"] = args
        out = args[args.index("--out") + 1]
        with open(out, "wb") as f:
            f.write(tone(2.0))
        return "audio_out=" + out
    monkeypatch.setattr(engine, "run", fake_run)
    from aiodos.engines import gpu_share
    monkeypatch.setattr(gpu_share, "before_unit", lambda: [])
    made = generate.create(kind.normalize({"model": "essai-voix", "text": "Bonjour"}))
    assert made["duration"] == pytest.approx(2.0) and wav.read(made["wav"])["rate"] == 24000
    assert not any((tmp_path / "tmp" / "audio").iterdir())           # dossier de calcul effacé
    monkeypatch.setattr(engine, "run", lambda args, *a, **k: open(
        args[args.index("--out") + 1], "wb").write(tone(1.0, amplitude=0)))
    with pytest.raises(engine.EngineError) as silent:
        generate.create(kind.normalize({"model": "essai-voix", "text": "Bonjour"}))
    assert "silence" in str(silent.value)


# ------------------------------------------------------------------ le travail
def test_a_request_is_checked_and_completed(tiny_audio):
    voice = kind.normalize({"model": "essai-voix", "text": "  Bonjour   à tous  "})
    assert voice["text"] == "Bonjour à tous" and voice["voice"] == "anna"
    assert voice["genre"] == "voice" and 0 <= voice["seed"] < kind.SEED_SPACE
    music = kind.normalize({"model": "essai-musique", "prompt": "piano", "count": 2})
    assert music["duration"] == 5 and music["count"] == 2 and music["lyrics"] == ""
    for bad in ({"model": "inconnu", "text": "x"}, {"model": "essai-voix", "text": ""},
                {"model": "essai-voix", "text": "x", "voice": "personne"},
                {"model": "essai-musique", "prompt": "x", "duration": 60},
                {"model": "essai-musique", "prompt": "x", "count": 9},
                {"model": "essai-voix", "text": "x" * (catalog.MAX_TEXT + 1)}):
        with pytest.raises(ValueError):
            kind.normalize(bad)


def test_each_sound_is_a_unit_with_its_own_seed(tiny_audio):
    params = kind.normalize({"model": "essai-musique", "prompt": "piano", "count": 3})
    title, units, info = kind.split(params)
    assert [u.payload["seed"] for u in units] == [params["seed"] + i for i in range(3)]
    assert all(u.task == "generate_audio" and u.resource == "gpu" for u in units)
    assert info == {"batch": params["batch"]} and title[0] == "audio.job_music"
    with i18n.using("fr"):
        assert i18n.render(title) == "Musique : « piano » ×3"


def test_only_a_machine_with_audio_cpp_and_the_same_files_can_compute(tiny_audio):
    need = kind.requires({"payload": {"model": "essai-voix"}}, {})
    hashes = need["audio"]["files"]
    assert len(hashes) == 2
    assert work.can_serve(need, {"audio": {"engine": "0.9.3", "files": hashes}}, {})
    assert not work.can_serve(need, {"audio": {"engine": "0.9.3", "files": hashes[1:]}}, {})
    assert not work.can_serve(need, {"audio": {"engine": None, "files": hashes}}, {})
    assert not work.can_serve(need, {"images": {"engine": "x", "files": hashes}}, {})


class _Executor:
    def publish_job(self, job_id, force=False):
        pass


class _Manager:
    executor = _Executor()

    def name(self):
        return "PC portable"


def test_sounds_go_to_my_machines_and_to_a_friend_only_if_allowed(tiny_audio, monkeypatch):
    monkeypatch.setattr(work.ollama, "list_models", lambda max_age=10: [])
    monkeypatch.setattr(engine, "folder", lambda: None)            # rien ici : à une autre
    peers.save(B, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(FRIEND, "PC d'un ami", b"a" * 32, "127.0.0.1", 2, trust="friend")
    settings.set_value("images_from_friends", False)
    params = kind.normalize({"model": "essai-voix", "text": "Bonjour", "count": 2})
    queue.create_job(kind.KIND, params)
    caps = {"work_api": work.WORK_API,
            "audio": {"engine": "0.9.3", "files": catalog.hashes("essai-voix")}}
    ask = lambda node: work.lease(_Manager(), node, {"resources": ["gpu"], "caps": caps})
    assert ask(FRIEND)["unit"] is None                             # mon accord d'abord
    settings.set_value("images_from_friends", True)
    assert ask(FRIEND)["unit"] is None                             # ... et l'ami le propose
    peers.set_profile(FRIEND, {"audio": {"engine": "0.9.3", "shares": True}})
    order = ask(FRIEND)["unit"]
    assert order["task"] == "generate_audio" and order["payload"]["text"] == "Bonjour"
    assert "batch" not in order["payload"] and "index" not in order["payload"]
    assert ask(B)["unit"]["task"] == "generate_audio"
    # Un résultat de cette taille est attendu d'elles (un WAV)
    assert work.result_limit(B) > 100 * 1024 ** 2


def test_a_friend_sees_my_audio_only_if_i_lend_my_card():
    mine = {"node_id": "a" * 32, "name": "PC", "audio": {"engine": "0.9.3", "models": ["x"],
                                                          "shares": True}}
    peers.save(FRIEND, "Ami", b"a" * 32, "127.0.0.1", 2, trust="friend")
    settings.set_value("lend_images_friends", False)
    assert "audio" not in sharing.profile_for(FRIEND, mine)
    settings.set_value("lend_images_friends", True)
    assert sharing.profile_for(FRIEND, mine)["audio"]["models"] == ["x"]
    # Le profil reçu garde l'audio (décrit, borné)
    cleaned = profile.clean({"audio": {"engine": "0.9.3", "models": ["x", 3], "shares": True,
                                       "autre": 1}})
    assert cleaned["audio"] == {"engine": "0.9.3", "models": ["x"], "shares": True}


def test_a_sound_from_elsewhere_is_checked_then_kept_here(tiny_audio):
    params = kind.normalize({"model": "essai-musique", "prompt": "piano", "duration": 5})
    payload = {**params, "seed": params["seed"], "index": 0}
    good = {"wav": base64.b64encode(tone(5.0)).decode(), "seconds": 3.5, "machine": "PC fixe"}
    for bad in ({"wav": "!!"}, {"wav": base64.b64encode(b"pas un wav").decode()},
                {"wav": base64.b64encode(tone(1.0, amplitude=0)).decode()},
                {"wav": base64.b64encode(tone(30.0, rate=8000)).decode()}):  # bien trop long
        with pytest.raises(ValueError):
            kind.check(payload, bad)
    kind.check(payload, good)
    result = kind.assimilate(payload, good)
    row = gallery.get(result["image_id"])
    assert row["media"] == "audio" and row["duration"] == pytest.approx(5.0)
    assert row["prompt"] == "piano" and row["machine"] == "PC fixe" and row["seconds"] == 3.5
    assert gallery.path_of(row).suffix == ".wav" and gallery.path_of(row).read_bytes()[:4] == b"RIFF"
    assert gallery.MEDIA_TYPES[".wav"] == "audio/wav"
    from aiodos.imaging import thumbs
    assert thumbs.thumbnail(row, 256) is None                  # ni vignette, ni Pillow


def test_the_unit_is_computed_here_with_the_engine(tiny_audio, monkeypatch):
    ready("essai-voix")
    monkeypatch.setattr(generate, "create", lambda payload, stop=None: {
        "wav": tone(1.2), "seconds": 0.8, "duration": 1.2, "rate": 24000, "channels": 1})
    params = kind.normalize({"model": "essai-voix", "text": "Bonjour"})
    payload = {**{k: params[k] for k in kind.SHARED if k in params}, "seed": params["seed"],
               "index": 0}
    assert kind.runnable_here("essai-voix")
    output = kind.compute(payload, None, {})
    assert wav.read(base64.b64decode(output["wav"]))["seconds"] == pytest.approx(1.2)
    with pytest.raises(ValueError):                              # un ordre trafiqué
        kind.compute({**payload, "model": "essai-musique"}, None, {})


def test_the_profile_says_what_this_machine_can_compute_in_audio(tiny_audio):
    ready("essai-voix")
    from aiodos.node import identity
    found = identity._audio()
    assert found["engine"] == "0.9.3 (essai)" and "essai-voix" in found["models"]
    caps = engine.capabilities()
    assert set(catalog.hashes("essai-voix")) <= set(caps["files"])
    assert json.dumps(found)                                     # sérialisable
