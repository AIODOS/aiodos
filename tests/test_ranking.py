"""Le meilleur modèle disponible pour chaque genre (docs/conception/modeles-auto.md, étape 1) :
le catalogue classé (imaging/catalog.py : sources épinglées, tailles, empreintes, licences,
réglages de stable-diffusion.cpp par architecture), « Automatique » (imaging/ranking.py :
ici, mes machines, mes amis, le réseau public, « Sur mes machines seulement »), le menu et la
suggestion du meilleur modèle pour ce PC, l'avertissement à qui prête MiniMax H3, et la
version épinglée de stable-diffusion.cpp (tools/engine_release.py)."""
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiodos import settings
from aiodos.api.server import create_app
from aiodos.chat import comfy_creations, creations, public_creations
from aiodos.i18n import t
from aiodos.imaging import catalog, engine, generate, lenders, ranking
from aiodos.jobs.kinds import images as kind
from aiodos.network import peers, public_imaging, public_video, sharing

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}
IMAGES = [m for m, s in catalog.MODELS.items() if s["kind"] == "image"]
VIDEOS = [m for m, s in catalog.MODELS.items() if s["kind"] == "video"]
# Les dépôts officiels à accès conditionné (« gated ») : jamais une adresse du catalogue
GATED = ("black-forest-labs/", "Lightricks/", "MiniMaxAI/", "briaai/", "ideogram-ai/")
FREE_LICENSES = {"Apache-2.0", "MIT"}


# ================================================================== le catalogue
def test_every_file_is_pinned_to_a_public_commit_with_its_size_and_digest():
    names = {}
    for model_id, spec in catalog.MODELS.items():
        for part, f in spec["files"].items():
            assert part in catalog.FLAGS, (model_id, part)
            found = re.fullmatch(r"https://huggingface\.co/([^/]+/[^/]+)/resolve/([0-9a-f]{40})/"
                                 r"(.+)", f["url"])
            assert found, (model_id, f["url"])
            assert not found.group(1).startswith(GATED), (model_id, f["url"])
            assert isinstance(f["size"], int) and f["size"] > 1000
            assert re.fullmatch(r"[0-9a-f]{64}", f["sha256"])
            assert "/" not in f["name"] and "\\" not in f["name"]
            # Un même nom de fichier : le même fichier (téléchargé une fois)
            assert names.setdefault(f["name"], (f["size"], f["sha256"])) == \
                (f["size"], f["sha256"]), f["name"]


def test_the_catalogue_is_ranked_by_power_with_the_memory_each_model_needs():
    assert ranking.ordered(IMAGES)[:6] == ["flux-3-image-klein-q8", "flux-3-image-klein",
                                           "qwen-image-2.1", "ming-image-0.1-design",
                                           "hidream-o1-image", "z-image-turbo"]
    assert ranking.ordered(IMAGES)[-1] == "flux-2-klein-4b"
    assert ranking.ordered(VIDEOS) == ["minimax-h3", "ltx-2.5", "wan2.2-t2v-a14b",
                                       "wan2.2-ti2v-5b", "wan2.1-t2v-1.3b"]
    for genre in (IMAGES, VIDEOS):
        ranks = [catalog.rank(m) for m in genre if not catalog.MODELS[m].get("variant_of")]
        assert len(ranks) == len(set(ranks)) and min(ranks) == 1          # un rang chacun
        for m in genre:
            assert catalog.vram(m) in (6144, 8192, 12288, 16384, 24576), m
            elo = catalog.MODELS[m].get("elo")
            assert elo is None or 800 < elo < 1300
    # Les notes d'Artificial Analysis rangent ceux qui en ont une
    noted = [m for m in IMAGES if catalog.MODELS[m]["elo"]]
    assert sorted(noted, key=catalog.rank) == sorted(noted, key=lambda m: -catalog.MODELS[m]["elo"])
    variant = catalog.MODELS["flux-3-image-klein-q8"]
    assert variant["variant_of"] == "flux-3-image-klein"
    assert catalog.rank("flux-3-image-klein-q8") == catalog.rank("flux-3-image-klein")
    assert catalog.vram("flux-3-image-klein-q8") > catalog.vram("flux-3-image-klein")
    # Z-Image Turbo garde ses fichiers : les machines qui l'ont le gardent
    assert catalog.hashes("z-image-turbo") == sorted([
        "14b375ab4f226bc5378f68f37e899ef3c2242b8541e61e2bc1aff40976086fbd",
        "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
        "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38"])
    assert catalog.hashes("wan2.1-t2v-1.3b") == sorted([
        "be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5",
        "eaea358bb438c5d211721a4feecc162000e3636e9cb96f51e216f1f44ebd12ce",
        "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b"])


@pytest.mark.parametrize("language", ["fr", "en"])
def test_a_restrictive_license_is_shown_with_its_essentials(language):
    """Toute licence qui n'est ni Apache ni MIT a son avertissement, dans les deux langues,
    avec l'adresse de son texte (décision de l'auteur du 29/10/2026)."""
    texts = json.loads((ROOT / "aiodos" / "i18n" / language / "imaging_models.json")
                       .read_text(encoding="utf-8"))
    for model_id, spec in catalog.MODELS.items():
        assert bool(spec.get("notice")) == (spec["license"] not in FREE_LICENSES), model_id
        assert catalog.restricted(model_id) == bool(spec.get("notice"))
        for key in ("about", "notice", "lend_notice"):
            if spec.get(key):
                assert spec[key].removeprefix("imaging_models.") in texts, (model_id, key)
        if spec.get("notice"):
            assert "https://" in texts[spec["notice"].removeprefix("imaging_models.")]
    h3 = texts["notice_minimax_h3"]
    expected = (("Union européenne", "États-Unis", "Royaume-Uni", "Corée du Sud", "service")
                if language == "fr" else
                ("European Union", "United States", "United Kingdom", "South Korea", "service"))
    assert all(word in h3 for word in expected)
    assert "5 million" in texts["notice_flux_3_image_klein"]
    assert "10 million" in texts["notice_ltx_2_5"]
    assert "model-business@notice.qwencloud.com" in texts["notice_qwen_image_2_1"]
    lend = texts["lend_notice_minimax_h3"]
    assert "MiniMax H3" in lend and all(word in lend for word in expected)


def test_every_format_and_duration_follows_the_rules_of_its_model():
    """Les formats et les durées proposés passent la vérification d'une demande ; le premier
    format et la durée par défaut tiennent dans les bornes du réseau public."""
    for model_id, spec in catalog.MODELS.items():
        video = spec["kind"] == "video"
        for width, height, _ in spec["sizes"]:
            kind.normalize({"model": model_id, "prompt": "x", "width": width, "height": height})
        width, height = spec["sizes"][0][:2]
        if video:
            for frames, _ in spec["durations"]:
                kind.normalize({"model": model_id, "prompt": "x", "frames": frames})
            assert public_video.fits(spec, width, height), model_id
            assert public_video.frames_allowed(spec, spec["default_frames"]), model_id
            assert public_video.lendable(model_id) and not public_imaging.lendable(model_id)
        else:
            assert public_imaging.fits(spec, width, height), model_id
            assert public_imaging.lendable(model_id) and not public_video.lendable(model_id)
        assert len(public_imaging.MARKER + model_id) <= 64


# ======================================================= stable-diffusion.cpp
@pytest.fixture
def files_here(monkeypatch, tmp_path):
    """Chaque modèle « complet » ici, ses fichiers dans un dossier à part (sans les écrire)."""
    monkeypatch.setattr(catalog, "files", lambda model_id: {
        part: tmp_path / spec["name"] for part, spec in catalog.model(model_id)["files"].items()})
    monkeypatch.setattr(engine, "accepts", lambda option: False)
    return tmp_path


def _flags(args: list) -> dict:
    return {flag: args[i + 1] for i, flag in enumerate(args)
            if flag.startswith("--") and i + 1 < len(args)}


def test_each_architecture_gets_its_own_files(files_here):
    """Les options de sd-server de chaque architecture (docs/<modèle>.md de
    stable-diffusion.cpp, master-1027)."""
    expected = {
        "flux-3-image-klein": {"--diffusion-model", "--llm", "--vae"},
        "flux-3-image-klein-q8": {"--diffusion-model", "--llm", "--vae"},
        "qwen-image-2.1": {"--diffusion-model", "--llm", "--vae"},
        "ming-image-0.1-design": {"--diffusion-model", "--llm", "--vae", "--tokenizer"},
        "hidream-o1-image": {"--model"},
        "z-image-turbo": {"--diffusion-model", "--llm", "--vae"},
        "flux-2-klein-4b": {"--diffusion-model", "--llm", "--vae"},
        "minimax-h3": {"--diffusion-model", "--llm", "--vae", "--audio-vae", "--rng"},
        "ltx-2.5": {"--diffusion-model", "--llm", "--vae", "--audio-vae"},
        "wan2.2-t2v-a14b": {"--diffusion-model", "--high-noise-diffusion-model", "--t5xxl",
                            "--vae"},
        "wan2.2-ti2v-5b": {"--diffusion-model", "--t5xxl", "--vae"},
        "wan2.1-t2v-1.3b": {"--diffusion-model", "--t5xxl", "--vae"},
    }
    assert set(expected) == set(catalog.MODELS)
    common = {"--listen-ip", "--listen-port", "--log-level", "--backend"}
    for model_id, flags in expected.items():
        found = _flags(engine.arguments(model_id, 1719, "Vulkan1"))
        assert set(found) == flags | common, model_id
    a14b = _flags(engine.arguments("wan2.2-t2v-a14b", 1719, None))
    assert a14b["--diffusion-model"].endswith("LowNoise-Q4_K_M.gguf")
    assert a14b["--high-noise-diffusion-model"].endswith("HighNoise-Q4_K_M.gguf")
    assert _flags(engine.arguments("ming-image-0.1-design", 1719, None))["--tokenizer"] \
        .endswith("ming_image_0.1_tokenizer.json")


def test_the_settings_of_each_model_go_to_sd_server():
    def image(model_id, **extra):
        spec = catalog.model(model_id)
        return generate.request_body({"model": model_id, "prompt": "x", "width": 1024,
                                      "height": 1024, "seed": 1, "steps": spec["steps"],
                                      "cfg": spec["cfg"], **extra})["sample_params"]
    flux = image("flux-3-image-klein")
    assert flux == {"sample_steps": 16, "guidance": {"txt_cfg": 1.0}, "sample_method": "euler"}
    hidream = image("hidream-o1-image")
    assert (hidream["sample_steps"], hidream["guidance"]["txt_cfg"], hidream["flow_shift"]) == \
        (30, 5.0, 3.0)
    # Z-Image Turbo : comme avant, ce que sd-server choisit pour lui
    assert image("z-image-turbo") == {"sample_steps": 8, "guidance": {"txt_cfg": 1.0}}

    def video(model_id):
        spec = catalog.model(model_id)
        return generate.video_body({"model": model_id, "prompt": "x", "width": 832,
                                    "height": 480, "seed": 1, "frames": spec["default_frames"],
                                    "steps": spec["steps"], "cfg": spec["cfg"]})
    a14b = video("wan2.2-t2v-a14b")
    assert a14b["sample_params"]["guidance"]["txt_cfg"] == 3.0
    assert a14b["sample_params"]["flow_shift"] == 12.0
    assert a14b["high_noise_sample_params"] == {
        "sample_steps": -1, "sample_method": "euler", "guidance": {"txt_cfg": 4.0},
        "flow_shift": 12.0}
    assert a14b["fps"] == 16 and a14b["negative_prompt"] == catalog.WAN_NEGATIVE
    ltx = video("ltx-2.5")
    assert (ltx["fps"], ltx["video_frames"], ltx["sample_params"]["sample_steps"]) == (24, 49, 8)
    assert "high_noise_sample_params" not in ltx
    ti2v = video("wan2.2-ti2v-5b")
    assert (ti2v["fps"], ti2v["sample_params"]["flow_shift"]) == (24, 5.0)


def test_sd_server_gets_an_empty_model_folder_when_it_knows_the_option(monkeypatch, tmp_path):
    """Depuis master-1027, l'API de sd-server charge des modèles pris dans son dossier de
    modèles (« ./models » par défaut) : il lui en est donné un vide ; une version plus ancienne,
    qui ne connaît pas l'option, n'en reçoit pas (elle refuserait de démarrer)."""
    program = tmp_path / ("sd-server.exe" if sys.platform == "win32" else "sd-server")
    program.write_bytes(b"")
    monkeypatch.setattr(engine, "info", lambda: {"folder": str(tmp_path), "version": "e7b7ec6"})
    monkeypatch.setattr(catalog, "files", lambda model_id: {})
    helps = {"text": "  --model-dir   model root; repeat to search multiple directories"}
    monkeypatch.setattr(engine, "_run", lambda program_, *args: helps["text"])
    engine._helps.clear()
    args = engine.arguments("z-image-turbo", 1719, None)
    folder = Path(args[args.index("--model-dir") + 1])
    assert folder.is_dir() and not any(folder.iterdir())
    engine._helps.clear()
    helps["text"] = "  --listen-port <int>"                       # f0aac52
    assert "--model-dir" not in engine.arguments("z-image-turbo", 1719, None)
    engine._helps.clear()


def test_the_engine_is_the_last_published_version():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import engine_release
    finally:
        sys.path.remove(str(ROOT / "tools"))
    pinned = engine_release.ENGINES[engine_release.IMAGES]
    assert pinned["build"] == "master-1027-e7b7ec6"
    asset = pinned["assets"]["windows-x86_64"]
    assert asset["name"] == "sd-master-e7b7ec6-bin-win-vulkan-x64.zip"
    assert asset["size"] == 29745686 and re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])


def test_a_video_with_sound_is_taken_as_a_webm():
    """LTX-2.5 et MiniMax H3 : leur son en PCM fait écrire un Matroska à sd-server."""
    header = bytes.fromhex("1a45dfa39f4286810142f7810142f2810442f381084282")
    assert generate.is_webm(header + b"\x84webm" + bytes(40))
    assert generate.is_webm(header + b"\x88matroska" + bytes(40))
    assert not generate.is_webm(header + b"\x83avi" + bytes(40))


# ================================================================ « Automatique »
@pytest.fixture
def world(monkeypatch):
    """Qui a quoi : ce PC (here), mes machines et mes amis (circles), le réseau public
    (public), l'accord du réseau public, ComfyUI ; ouvert au réseau public par défaut."""
    state = {"here": set(), "circles": {}, "public": [], "open": True, "recipes": {},
             "owners": []}
    monkeypatch.setattr(kind, "runnable_here", lambda model: model in state["here"])
    monkeypatch.setattr(lenders, "circles", lambda friends=None: state["circles"])
    monkeypatch.setattr(public_creations, "offered", lambda media_kind="image": list(
        m for m in state["public"] if catalog.MODELS.get(m, {}).get("kind") == media_kind))
    monkeypatch.setattr(ranking, "_public_open",
                        lambda mine_only, pictures=(): state["open"] and not mine_only
                        and not pictures)
    monkeypatch.setattr(comfy_creations, "_owners", lambda: iter(state["owners"]))
    monkeypatch.setattr(comfy_creations, "_owner", lambda model_id: dict(state["owners"])[model_id])
    monkeypatch.setattr(comfy_creations, "choices", lambda media_kind=None: [
        r for r in state["recipes"].values() if r["kind"] == media_kind])
    monkeypatch.setattr(settings, "get", _settings({"public_images": True,
                                                    "node_name": "Ce portable"}))
    return state


def _settings(values):
    original = settings.get

    def get(key):
        return values[key] if key in values else original(key)
    get.values = values
    return get


def _consent(value):
    settings.get.values["public_images"] = value


def mine(name="PC fixe"):
    return {"circle": "mine", "name": name, "power": 16384}


def friend(name="PC de Léa"):
    return {"circle": "friend", "name": name, "power": 24576}


def test_the_most_powerful_model_wins_wherever_it_is(world):
    world["here"] = {"z-image-turbo"}
    assert ranking.choose("image") == ranking.Choice("z-image-turbo", "here", "Ce portable")
    world["circles"] = {"ming-image-0.1-design": [friend()]}
    assert ranking.choose("image") == ranking.Choice("ming-image-0.1-design", "friend",
                                                     "PC de Léa")
    world["public"] = ["hidream-o1-image", "z-image-turbo"]
    assert ranking.choose("image").model == "ming-image-0.1-design"   # mieux que HiDream
    world["public"] = ["flux-2-klein-4b", "z-image-turbo"]
    world["circles"] = {}
    assert ranking.choose("image").where == "here"                 # même modèle : le plus proche
    # Un inconnu qui a un modèle plus puissant passe devant ce PC (la demande de l'auteur)
    world["public"] = ["hidream-o1-image"]
    assert ranking.choose("image") == ranking.Choice("hidream-o1-image", "public", None)
    # Les vidéos ont leur propre classement
    world["here"] |= {"wan2.1-t2v-1.3b"}
    world["circles"] = {"wan2.2-ti2v-5b": [mine()]}
    assert ranking.choose("video") == ranking.Choice("wan2.2-ti2v-5b", "mine", "PC fixe")


def test_at_equal_model_the_closest_machine_then_the_most_powerful(world):
    world["here"] = {"ming-image-0.1-design"}
    world["circles"] = {"ming-image-0.1-design": [mine()], "z-image-turbo": [mine()]}
    world["public"] = ["ming-image-0.1-design"]
    assert ranking.choose("image").where == "here"
    world["here"] = set()
    assert ranking.choose("image").where == "mine"
    world["circles"] = {}
    assert ranking.choose("image").where == "public"


def test_lenders_put_my_machines_first_then_the_most_powerful(monkeypatch):
    profiles = {
        "a": {"name": "Portable", "level": sharing.MINE, "gpus": [{"vram_total": 8 << 30}]},
        "b": {"name": "Tour", "level": sharing.MINE, "gpus": [{"vram_total": 24 << 30}]},
        "c": {"name": "PC de Léa", "level": "friend", "gpus": [{"vram_total": 32 << 30}]},
        "d": {"name": "Éteint", "level": sharing.MINE, "gpus": [], "online": False},
        "e": {"name": "Sans prêt", "level": sharing.MINE, "gpus": [], "shares": False},
    }
    monkeypatch.setattr(peers, "list_peers", lambda: [
        {"node_id": key, "name": p["name"], "online": p.get("online", True),
         "profile": {"gpus": p["gpus"], "images": {"engine": "e7b7ec6",
                                                   "shares": p.get("shares", True),
                                                   "models": ["z-image-turbo"]}}}
        for key, p in profiles.items()])
    monkeypatch.setattr(sharing, "level", lambda node_id: profiles[node_id]["level"])
    found = lenders.circles(friends=True)["z-image-turbo"]
    assert [(m["circle"], m["name"]) for m in found] == [
        ("mine", "Tour"), ("mine", "Portable"), ("friend", "PC de Léa")]
    assert [m["name"] for m in lenders.circles(friends=False)["z-image-turbo"]] == \
        ["Tour", "Portable"]
    assert lenders.helpers(friends=True)["z-image-turbo"] == \
        ["Tour", "Portable", t("imaging.friend_name", name="PC de Léa")]


def test_the_public_network_counts_only_once_accepted(world):
    """L'accord demandé une fois : pas encore donné, le réseau public ne compte pas tant que
    ce PC ou mes machines peuvent calculer ; seul, il porte la carte d'accord (ask)."""
    world["here"] = {"z-image-turbo"}
    world["public"] = ["ming-image-0.1-design"]
    _consent(None)
    assert ranking.choose("image").model == "z-image-turbo"
    world["here"] = set()
    assert ranking.choose("image") == ranking.Choice("ming-image-0.1-design", "public", None,
                                                     ask=True)
    _consent(False)
    assert ranking.choose("image") == ranking.Choice()
    _consent(True)
    assert ranking.choose("image").where == "public"


def test_on_my_machines_only_nothing_goes_to_the_public_network(world):
    world["public"] = ["flux-2-klein-4b"]
    world["here"] = {"wan2.1-t2v-1.3b"}
    assert ranking.choose("image").where == "public"
    assert ranking.choose("image", mine_only=True) == ranking.Choice()
    # Une image jointe ne part jamais chez un inconnu, ni dans une recette de ComfyUI
    assert ranking.choose("image", pictures=[{"kind": "image"}]) == ranking.Choice()


def test_on_my_machines_only_with_the_real_settings(monkeypatch):
    """Les vrais réglages et le vrai « Sur mes machines seulement » (public_consent.closed)."""
    monkeypatch.setattr(kind, "runnable_here", lambda model: False)
    monkeypatch.setattr(lenders, "circles", lambda friends=None: {})
    monkeypatch.setattr(public_creations, "offered", lambda media_kind="image": (
        ["z-image-turbo"] if media_kind == "image" else []))
    settings.set_many({"use_public": True, "public_network": True, "public_images": True})
    assert ranking.choose("image", comfy=False) == ranking.Choice("z-image-turbo", "public")
    assert ranking.choose("image", mine_only=True, comfy=False) == ranking.Choice()
    settings.set_value("use_public", False)
    assert ranking.choose("image", comfy=False) == ranking.Choice()


def test_friends_count_only_when_I_accept_them(world, monkeypatch):
    """lenders.circles suit le réglage images_from_friends : faux, aucun ami."""
    world["circles"] = {"ming-image-0.1-design": [friend()]}
    assert ranking.choose("image").where == "friend"
    seen = []
    monkeypatch.setattr(lenders, "circles", lambda friends=None: seen.append(friends) or {})
    assert ranking.choose("image") == ranking.Choice() and seen == [None]


def test_a_restrictive_license_is_taken_only_where_it_was_accepted(world):
    """Automatique ne prend un modèle à licence restrictive que sur ce PC ou mes machines
    (téléchargé : sa licence lue et acceptée) ; chez un ami ou un inconnu, seulement choisi."""
    world["circles"] = {"flux-3-image-klein": [friend()]}
    world["public"] = ["flux-3-image-klein", "z-image-turbo"]
    assert ranking.choose("image").model == "z-image-turbo"
    assert ranking.choose("image", "flux-3-image-klein") == \
        ranking.Choice("flux-3-image-klein", "friend", "PC de Léa")
    world["circles"] = {"flux-3-image-klein": [mine()]}
    assert ranking.choose("image") == ranking.Choice("flux-3-image-klein", "mine", "PC fixe")
    world["here"] = {"minimax-h3"}
    assert ranking.choose("video") == ranking.Choice("minimax-h3", "here", "Ce portable")
    # Sans accord encore demandé : la carte ne propose jamais une licence restrictive
    world["circles"], world["here"] = {}, set()
    world["public"] = ["flux-3-image-klein"]
    _consent(None)
    assert ranking.choose("image") == ranking.Choice()
    assert ranking.choose("image", "flux-3-image-klein").ask


def test_a_license_accepted_here_lets_automatic_take_the_model_elsewhere(world):
    """Sa licence lue et acceptée ici (choisi une fois dans le menu, ou téléchargé) :
    « Automatique » prend ce modèle chez un ami ou un inconnu (le téléphone sans modèle, le PC
    de l'auteur qui a FLUX 3 Klein)."""
    world["circles"] = {"flux-3-image-klein": [friend()]}
    world["public"] = ["flux-3-image-klein", "z-image-turbo"]
    assert ranking.choose("image").model == "z-image-turbo"
    assert not catalog.accepted("flux-3-image-klein") and catalog.accepted("z-image-turbo")
    catalog.accept("flux-3-image-klein")
    assert catalog.accepted("flux-3-image-klein")
    assert ranking.choose("image") == ranking.Choice("flux-3-image-klein", "friend", "PC de Léa")
    world["circles"] = {}
    assert ranking.choose("image") == ranking.Choice("flux-3-image-klein", "public", None)
    assert ranking.public_choices(["z-image-turbo", "flux-3-image-klein"])[0] ==         "flux-3-image-klein"
    catalog.accept("flux-3-image-klein")                       # une fois suffit
    assert settings.get("licenses_accepted") == ["flux-3-image-klein"]


def test_the_bigger_variant_of_the_same_model_goes_first(world):
    world["here"] = {"flux-3-image-klein"}
    world["circles"] = {"flux-3-image-klein-q8": [mine()]}
    assert ranking.choose("image").model == "flux-3-image-klein-q8"


def test_a_chosen_model_goes_first_if_it_can_compute(world):
    world["here"] = {"z-image-turbo", "flux-2-klein-4b"}
    world["public"] = ["ming-image-0.1-design"]
    assert ranking.choose("image", "flux-2-klein-4b").model == "flux-2-klein-4b"
    assert ranking.choose("image", "auto").model == "ming-image-0.1-design"
    assert ranking.choose("image", "").model == "ming-image-0.1-design"
    assert ranking.choose("image", "hidream-o1-image").model == "ming-image-0.1-design"
    assert ranking.choose("image", "z-image-turbo", public=False).model == "z-image-turbo"
    assert ranking.choose("image", public=False).model == "z-image-turbo"


SDXL = "comfyui/comfy-templates/image_sdxl_simple"
WAN = "comfyui/comfy-templates/text_to_video_wan"


def _recipes(world, here=True):
    world["owners"] = [
        (SDXL, {"kind": "image", "title": {"fr": "SDXL 1.0 · image"},
                "models": [{"name": "sd_xl_base_1.0.safetensors", "license": "CreativeML"}]}),
        (WAN, {"kind": "video", "title": {"fr": "Wan 2.1 1.3B · vidéo"},
               "models": [{"name": "wan2.1_t2v_1.3B_fp16.safetensors"},
                          {"name": "wan_2.1_vae.safetensors"}]}),
        ("comfyui/workflow/7", {"kind": "image", "name": "Mon workflow"})]
    world["recipes"] = {model_id: {"id": model_id, "kind": owner["kind"], "here": here,
                                   "helpers": [] if here else ["PC fixe"]}
                        for model_id, owner in world["owners"]}


def test_comfyui_recipes_are_ranked_by_their_model(world):
    """SDXL sous tout le catalogue ; la recette Wan 2.1 au rang de Wan 2.1, après
    stable-diffusion.cpp ; un workflow inconnu en dernier."""
    _recipes(world)
    ranked = [c.id for c in ranking.candidates("image")]
    assert ranked[-3:] == ["flux-2-klein-4b", SDXL, "comfyui/workflow/7"]
    videos = [c.id for c in ranking.candidates("video")]
    assert videos[-2:] == ["wan2.1-t2v-1.3b", WAN]
    assert ranking.choose("image") == ranking.Choice(SDXL, "here", "Ce portable", "comfyui")
    # Un modèle plus puissant du réseau public passe devant (avant le 29/10 : la recette)
    world["public"] = ["z-image-turbo"]
    assert ranking.choose("image") == ranking.Choice("z-image-turbo", "public", None)
    world["here"] = {"wan2.1-t2v-1.3b"}
    assert ranking.choose("video").engine == "sdcpp"
    world["here"] = set()
    assert ranking.choose("video") == ranking.Choice(WAN, "here", "Ce portable", "comfyui")
    assert ranking.choose("video", comfy=False) == ranking.Choice()
    assert ranking.choose("image", pictures=[{"kind": "image"}]) == ranking.Choice()


# ================================================================ le menu
CONTRACT = {"id", "label", "rank", "kind", "where", "ready", "download", "license",
            "restricted", "notice", "accepted", "engine"}


def test_the_menu_follows_its_contract(world, monkeypatch, tmp_path):
    monkeypatch.setattr(ranking, "vram_here", lambda max_age=0: 8188)
    monkeypatch.setattr(engine, "folder", lambda: tmp_path)          # stable-diffusion.cpp ici
    monkeypatch.setattr(catalog, "ready", lambda model_id: model_id == "z-image-turbo")
    world["here"] = {"z-image-turbo"}
    world["circles"] = {"qwen-image-2.1": [mine(), friend()]}
    world["public"] = ["minimax-h3"]
    _recipes(world)
    found = ranking.menu("image")
    assert set(found) == {"auto", "suggest", "models"}
    assert found["auto"] == {"model": "qwen-image-2.1", "label": "Qwen-Image-2.1",
                             "where": "mine", "machine": "PC fixe"}
    models = found["models"]
    assert all(set(m) == CONTRACT for m in models)
    assert [m["rank"] for m in models] == sorted(m["rank"] for m in models)
    assert {m["id"] for m in models} == set(IMAGES) | {SDXL, "comfyui/workflow/7"}
    by_id = {m["id"]: m for m in models}
    qwen = by_id["qwen-image-2.1"]
    assert qwen["where"] == [{"circle": "mine", "name": "PC fixe"},
                             {"circle": "friend", "name": "PC de Léa"}]
    assert qwen["ready"] and qwen["restricted"] and qwen["notice"] == \
        catalog.notice("qwen-image-2.1")
    assert qwen["download"]["vram"] == 12288 and qwen["download"]["fits_here"] is False
    assert qwen["download"]["size"] == catalog.total_size("qwen-image-2.1")
    flux = by_id["flux-3-image-klein"]
    assert flux["ready"] is False and flux["where"] == []
    assert flux["download"]["fits_here"] is True                    # 8 Go, à 512 Mio près
    zimage = by_id["z-image-turbo"]
    assert zimage["download"] is None and zimage["where"][0] == {"circle": "here",
                                                                 "name": "Ce portable"}
    assert zimage["license"] == "Apache-2.0" and zimage["notice"] is None
    assert by_id[SDXL]["engine"] == "comfyui" and by_id[SDXL]["download"] is None
    assert by_id[SDXL]["license"] == "CreativeML"
    # La suggestion : le meilleur que cette carte de 8 Go fait tourner, pas encore ici
    assert found["suggest"] == {"model": "flux-3-image-klein", "label": "FLUX 3 Image Klein",
                                "size": catalog.total_size("flux-3-image-klein"),
                                "vram": 8192}
    json.dumps(found)                                                  # tel quel en JSON
    monkeypatch.setattr(engine, "folder", lambda: None)               # l'appli Android
    assert ranking.menu("image")["suggest"] is None
    videos = ranking.menu("video")
    assert [m["id"] for m in videos["models"]][:5] == ranking.ordered(VIDEOS)
    h3 = next(m for m in videos["models"] if m["id"] == "minimax-h3")
    assert h3["where"] == [{"circle": "public", "name": None}] and h3["restricted"]
    assert videos["auto"] == {"model": WAN, "label": by_id_label(videos, WAN), "where": "here",
                              "machine": "Ce portable"}


def by_id_label(found, model_id):
    return next(m["label"] for m in found["models"] if m["id"] == model_id)


def test_the_menu_without_the_public_network(world, monkeypatch):
    monkeypatch.setattr(ranking, "vram_here", lambda max_age=0: 0)
    world["public"] = ["z-image-turbo"]
    assert ranking.menu("image")["auto"]["where"] == "public"
    found = ranking.menu("image", mine_only=True)
    assert found["auto"] == {"model": None, "label": None, "where": None, "machine": None}
    assert not any(w["circle"] == "public" for m in found["models"] for w in m["where"])
    assert found["suggest"] is None                                    # carte inconnue
    _consent(None)                       # pas encore demandé : montré, la carte viendra
    found = ranking.menu("image")
    assert found["auto"]["where"] == "public"
    assert {"circle": "public", "name": None} in next(
        m for m in found["models"] if m["id"] == "z-image-turbo")["where"]
    _consent(False)
    assert ranking.menu("image")["auto"]["model"] is None


def test_the_suggestion_is_the_best_model_this_card_can_run(monkeypatch):
    here = set()
    monkeypatch.setattr(catalog, "ready", lambda model_id: model_id in here)
    assert ranking.suggest("image", 8188)["model"] == "flux-3-image-klein"
    assert ranking.suggest("image", 12288)["model"] == "flux-3-image-klein-q8"
    assert ranking.suggest("image", 6144)["model"] == "z-image-turbo"
    assert ranking.suggest("image", 4096) is None and ranking.suggest("image", 0) is None
    assert ranking.suggest("video", 8188)["model"] == "wan2.1-t2v-1.3b"
    assert ranking.suggest("video", 24576)["model"] == "minimax-h3"
    here.add("flux-3-image-klein")
    assert ranking.suggest("image", 8188) is None                     # déjà le meilleur ici
    assert ranking.suggest("image", 12288)["model"] == "flux-3-image-klein-q8"
    here.add("z-image-turbo")
    here.discard("flux-3-image-klein")
    assert ranking.suggest("image", 8188)["model"] == "flux-3-image-klein"
    assert ranking.fits_here("flux-3-image-klein", 8188)
    assert not ranking.fits_here("ltx-2.5", 16384)


# ====================================================== la création, le réseau public
def test_the_public_network_takes_the_most_powerful_model_without_a_restrictive_license(
        monkeypatch):
    found = {"image": ["z-image-turbo", "flux-3-image-klein", "flux-2-klein-4b"],
             "video": ["minimax-h3"]}
    monkeypatch.setattr(public_creations, "offered",
                        lambda media_kind="image": found[media_kind])
    monkeypatch.setattr(public_creations.public_consent, "closed",
                        lambda conv_id, mine_only, joined=(): None)
    settings.set_value("public_images", True)
    assert public_creations.choose().model == "z-image-turbo"
    assert public_creations.choose("flux-3-image-klein").model == "flux-3-image-klein"
    choice = public_creations.choose(media_kind="video")
    assert choice.model is None and choice.message == t(
        "public_videos.only_restricted", models=catalog.label("minimax-h3"))


def test_a_creation_goes_where_automatic_takes_it(monkeypatch):
    """chat/creations.prepare suit imaging/ranking.choose : le réseau public quand il y a le
    plus puissant (la description passe le filtre, l'image part sans image de départ)."""
    from aiodos.imaging import prompt_filter
    monkeypatch.setattr(ranking, "choose", lambda *args, **kwargs: ranking.Choice(
        "qwen-image-2.1", ranking.PUBLIC, None))
    monkeypatch.setattr(prompt_filter, "base", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_filter, "check", lambda *args, **kwargs: None)
    settings.set_value("images_enrich", False)
    plan = creations.prepare("Crée une image d'un phare sous l'orage", {})
    assert plan["model_id"] == "qwen-image-2.1" and plan["params"]["public"] is True
    assert (plan["params"]["width"], plan["params"]["height"]) == (1024, 1024)
    monkeypatch.setattr(ranking, "choose", lambda *args, **kwargs: ranking.Choice(
        "z-image-turbo", ranking.HERE, "Ce portable"))
    plan = creations.prepare("Crée une image d'un phare sous l'orage", {})
    assert plan["model_id"] == "z-image-turbo" and "public" not in plan["params"]
    monkeypatch.setattr(ranking, "choose", lambda *args, **kwargs: ranking.Choice(
        "qwen-image-2.1", ranking.PUBLIC, None, ask=True))
    monkeypatch.setattr(public_creations, "offered", lambda media_kind="image": [
        "qwen-image-2.1"])
    monkeypatch.setattr(public_creations.public_consent, "closed",
                        lambda conv_id, mine_only, joined=(): None)
    with pytest.raises(creations.CreationError) as refused:
        creations.prepare("Crée une image d'un phare sous l'orage", {})
    assert refused.value.public == public_creations.ASK and refused.value.status == 409


# ============================================================ l'API des images
@pytest.fixture
def client():
    with TestClient(create_app(TOKEN, {"testserver"})) as c:
        yield c


def test_who_lends_minimax_h3_is_warned(client, monkeypatch):
    monkeypatch.setattr(ranking, "vram_here", lambda max_age=0: 16384)
    monkeypatch.setattr(engine, "info", lambda: {"folder": "x", "version": "e7b7ec6"})
    monkeypatch.setattr(engine, "devices", lambda max_age=0: [])
    monkeypatch.setattr(catalog, "ready", lambda model_id: model_id == "minimax-h3")
    settings.set_many({"lend_images_public": True})
    found = client.get("/api/images/models", headers=AUTH).json()
    assert found["vram"] == 16384
    assert found["lend_warnings"] == [{"model": "minimax-h3", "label": catalog.label("minimax-h3"),
                                       "text": catalog.lend_notice("minimax-h3")}]
    h3 = next(m for m in found["models"] if m["id"] == "minimax-h3")
    assert (h3["rank"], h3["vram"], h3["fits_here"]) == (1, 16384, True)
    assert h3["lend_notice"] == catalog.lend_notice("minimax-h3")
    assert "Tu prêtes MiniMax H3" in h3["lend_notice"]
    settings.set_many({"lend_images_public": False, "lend_images_friends": False})
    assert client.get("/api/images/models", headers=AUTH).json()["lend_warnings"] == []


def test_an_image_asked_without_a_model_takes_automatic(client, monkeypatch):
    asked = []
    monkeypatch.setattr(ranking, "choose", lambda *args, **kwargs: asked.append(
        (args, kwargs)) or ranking.Choice())
    reply = client.post("/api/images/generate", headers=AUTH,
                        json={"model": "auto", "prompt": "un phare"})
    assert reply.status_code == 409 and asked[0][0][0] == "image"
    assert asked[0][1] == {"comfy": False, "public": False}
    client.post("/api/images/generate", headers=AUTH,
                json={"model": "", "prompt": "un phare", "frames": 33})
    assert asked[1][0][0] == "video"
