"""Modèles d'images connus d'AIODOS, et les fichiers qu'en a cette machine.

Un modèle de stable-diffusion.cpp tient en plusieurs fichiers : le cœur (diffusion),
l'encodeur de texte (llm) et le décodeur (vae). Chacun est épinglé ici par son nom, sa
taille et son empreinte SHA-256, avec son adresse sur Hugging Face à un commit précis :
le téléchargement est vérifié, et deux machines qui ont les mêmes empreintes calculent
avec exactement le même modèle (unités confiées, jobs/kinds/images.py).

Les fichiers sont rangés dans data/images/models/ (réglage « images_models_dir » pour un
autre dossier, par exemple pour partager les modèles entre plusieurs copies d'AIODOS).
Leur empreinte n'est recalculée que si leur taille ou leur date changent : le résultat
est noté dans data/images/verified.json.

Les textes d'un modèle (description, licence, formats) sont des clés des textes
(i18n/*/imaging_models.json), écrites dans la langue en cours par about(), notice(),
sizes() et durations() ; son nom (label) reste tel quel.
"""
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from aiodos import i18n, paths, settings
from aiodos.i18n import t

log = logging.getLogger("aiodos.images")

HF = "https://huggingface.co/{repo}/resolve/{commit}/{path}"

# Option de sd-server pour chaque partie d'un modèle
FLAGS = {"diffusion": "--diffusion-model", "llm": "--llm", "vae": "--vae", "model": "--model",
         "t5xxl": "--t5xxl", "clip_l": "--clip_l", "audio_vae": "--audio-vae"}

# Chaque modèle dit aussi ce qu'il sait faire : formats proposés (sizes : largeur, hauteur,
# clé du libellé), côtés en multiples de « multiple » entre les bornes « side », pixels au
# plus ; pour une vidéo, la cadence (fps) et les nombres d'images permis, « frame_step » × k
# + « frame_base » (durations : ceux proposés, avec leur durée en secondes)

MODELS = {
    # Licences vérifiées le 17/10/2026 : Apache-2.0 pour les trois fichiers
    "z-image-turbo": {
        "label": "Z-Image Turbo",
        "license": "Apache-2.0",
        "about": "imaging_models.about_z_image_turbo",
        "files": {
            "diffusion": {
                "name": "z_image_turbo-Q4_K.gguf", "size": 3864250304,
                "sha256": "14b375ab4f226bc5378f68f37e899ef3c2242b8541e61e2bc1aff40976086fbd",
                "url": HF.format(repo="leejet/Z-Image-Turbo-GGUF",
                                 commit="c61c0e422dc8b541b7548cf33a4ef8302b0f8085",
                                 path="z_image_turbo-Q4_K.gguf")},
            "llm": {
                "name": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "size": 2497281120,
                "sha256": "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
                "url": HF.format(repo="unsloth/Qwen3-4B-Instruct-2507-GGUF",
                                 commit="a06e946bb6b655725eafa393f4a9745d460374c9",
                                 path="Qwen3-4B-Instruct-2507-Q4_K_M.gguf")},
            "vae": {
                "name": "ae.safetensors", "size": 335304388,
                "sha256": "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38",
                "url": HF.format(repo="Comfy-Org/z_image_turbo",
                                 commit="6fc90a3b1b653e935a0d175e260736de25b84df5",
                                 path="split_files/vae/ae.safetensors")},
        },
        # Modèle distillé : 8 pas, sans guidage (la consigne négative n'a pas d'effet)
        "kind": "image", "steps": 8, "cfg": 1.0, "negative": False,
        "sizes": [[1024, 1024, "imaging_models.size_square"],
                  [768, 1344, "imaging_models.size_portrait"],
                  [1344, 768, "imaging_models.size_landscape"],
                  [512, 512, "imaging_models.size_fast"]],
        "multiple": 64, "side": [256, 2048], "max_pixels": 1536 * 1536,
    },
    # Licences vérifiées le 17/10/2026 : Apache-2.0 pour les trois fichiers
    "wan2.1-t2v-1.3b": {
        "label": "Wan 2.1", "label_kind": "imaging_models.kind_video",
        "license": "Apache-2.0",
        "about": "imaging_models.about_wan2_1",
        "files": {
            "diffusion": {
                "name": "wan2.1_t2v_1.3B_fp16.safetensors", "size": 2838303560,
                "sha256": "be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5",
                "url": HF.format(repo="Comfy-Org/Wan_2.1_ComfyUI_repackaged",
                                 commit="123acf1cc74bccbb9bfff8ac1ee72edc08c2341d",
                                 path="split_files/diffusion_models/"
                                      "wan2.1_t2v_1.3B_fp16.safetensors")},
            "t5xxl": {
                "name": "umt5-xxl-encoder-Q5_K_M.gguf", "size": 4145878880,
                "sha256": "eaea358bb438c5d211721a4feecc162000e3636e9cb96f51e216f1f44ebd12ce",
                "url": HF.format(repo="city96/umt5-xxl-encoder-gguf",
                                 commit="b535255bee98c2b0a59ea7c0ae2dcd0c6657b3b7",
                                 path="umt5-xxl-encoder-Q5_K_M.gguf")},
            "vae": {
                "name": "wan_2.1_vae.safetensors", "size": 253815318,
                "sha256": "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
                "url": HF.format(repo="Comfy-Org/Wan_2.1_ComfyUI_repackaged",
                                 commit="123acf1cc74bccbb9bfff8ac1ee72edc08c2341d",
                                 path="split_files/vae/wan_2.1_vae.safetensors")},
        },
        # Réglages de l'exemple de stable-diffusion.cpp (docs/wan.md) ; la consigne
        # négative habituelle de Wan, en chinois, quand l'utilisateur n'en donne pas
        "kind": "video", "steps": 20, "cfg": 6.0, "negative": True, "fps": 16,
        "flow_shift": 3.0, "sampler": "euler",
        "sizes": [[832, 480, "imaging_models.size_landscape"],
                  [480, 832, "imaging_models.size_portrait"],
                  [624, 624, "imaging_models.size_square"]],
        "multiple": 16, "side": [256, 1280], "max_pixels": 1024 * 576,
        "frame_step": 4, "frame_base": 1, "max_frames": 161, "default_frames": 33,
        "durations": [[17, 1], [33, 2], [49, 3], [81, 5]],
        "default_negative": "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
                            "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
                            "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
                            "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走",
    },
    # MiniMax H3 : vidéo ET son. Décision de l'utilisateur (17/10/2026), comme dans PANDORA
    # (core/h3_local.py) : AIODOS le propose, affiche sa licence, demande une confirmation
    # avant le téléchargement, et laisse chacun juge. Fichiers et réglages : ceux de
    # stable-diffusion.cpp (docs/minimax_h3.md), en version allégée pour une machine à 32 Go
    # de mémoire vive. Pas encore essayé ici (30 Go à télécharger).
    "minimax-h3": {
        "label": "MiniMax H3", "label_kind": "imaging_models.kind_video_sound",
        "license": "MiniMax H3 Community License",
        "notice": "imaging_models.notice_minimax_h3",
        "about": "imaging_models.about_minimax_h3",
        "files": {
            "diffusion": {
                "name": "minimax_h3_fl2va_pruned-Q4_K_M.gguf", "size": 11420663904,
                "sha256": "dd948e08ad0ba3c71bd42f368e283dd82e790f5122a63b276e22a3e0283d0c10",
                "url": HF.format(repo="leejet/MiniMax-H3-GGUF",
                                 commit="d9c4c6312b4728a68a15a35626d84775a6523783",
                                 path="minimax_h3_fl2va_pruned-Q4_K_M.gguf")},
            "llm": {
                "name": "qwen3vl_32b_minimax_h3-Q2_K_M.gguf", "size": 13102161024,
                "sha256": "a8ccadccd57ef34c838ffb8a7da8368bb554721b2760274a1d3b0df63960b997",
                "url": HF.format(repo="leejet/MiniMax-H3-GGUF",
                                 commit="d9c4c6312b4728a68a15a35626d84775a6523783",
                                 path="qwen3vl_32b_minimax_h3-Q2_K_M.gguf")},
            "vae": {
                "name": "minimax_h3_video_vae_fp16.safetensors", "size": 5207808496,
                "sha256": "7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522",
                "url": HF.format(repo="Comfy-Org/MiniMax-H3",
                                 commit="e5eb578a89295337b8ff433a035929ce0279e0b6",
                                 path="vae/minimax_h3_video_vae_fp16.safetensors")},
            "audio_vae": {
                "name": "minimax_h3_audio_vae_fp32.safetensors", "size": 605254808,
                "sha256": "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
                "url": HF.format(repo="Comfy-Org/MiniMax-H3",
                                 commit="e5eb578a89295337b8ff433a035929ce0279e0b6",
                                 path="vae/minimax_h3_audio_vae_fp32.safetensors")},
        },
        # Sans guidage (cfg 1, comme les exemples) ; bruit tiré sur le processeur (--rng cpu)
        "kind": "video", "steps": 20, "cfg": 1.0, "negative": False, "fps": 24,
        "server_args": ["--rng", "cpu"],
        "sizes": [[864, 480, "imaging_models.size_landscape"],
                  [480, 864, "imaging_models.size_portrait"],
                  [640, 640, "imaging_models.size_square"],
                  [1344, 768, "imaging_models.size_landscape_hd"]],
        "multiple": 32, "side": [256, 1344], "max_pixels": 1344 * 768,
        "frame_step": 17, "frame_base": 5, "max_frames": 260, "default_frames": 56,
        "durations": [[22, 0.9], [56, 2.3], [73, 3], [124, 5.2]],
    },
}


def is_video(model_id: str) -> bool:
    return MODELS.get(model_id, {}).get("kind") == "video"

_lock = threading.Lock()
_checking = set()                 # fichiers dont l'empreinte se calcule en ce moment


class ModelError(Exception):
    pass


def folder() -> Path:
    configured = str(settings.get("images_models_dir") or "").strip()
    return Path(configured) if configured else paths.data_dir() / "images" / "models"


def model(model_id: str) -> dict:
    found = MODELS.get(model_id)
    if found is None:
        raise ModelError(t("imaging.unknown_model_id", model=model_id))
    return found


def total_size(model_id: str) -> int:
    return sum(f["size"] for f in model(model_id)["files"].values())


def hashes(model_id: str) -> list:
    """Empreintes des fichiers du modèle : une machine qui les a toutes calcule pareil."""
    return sorted(f["sha256"] for f in model(model_id)["files"].values())


# ------------------------------------------------------------ textes affichés
def _text(value, **params):
    """Un texte d'un modèle dans la langue en cours : une clé des textes (imaging_models.json),
    traduite ; un texte tel quel sinon (modèle ajouté à la main, essais)."""
    return t(value, **params) if i18n.exists(value) else value


def about(model_id: str) -> str:
    return _text(model(model_id)["about"])


def notice(model_id: str) -> str | None:
    """L'avertissement d'une licence restrictive, lu et accepté avant le téléchargement."""
    found = model(model_id).get("notice")
    return _text(found) if found else None


def sizes(model_id: str) -> list:
    """[[largeur, hauteur, libellé]] des formats proposés : « Carré · 1024 × 1024 »."""
    return [[width, height, _text(label, width=width, height=height)]
            for width, height, label in model(model_id)["sizes"]]


def durations(model_id: str) -> list:
    """[[nombre d'images, libellé]] des durées proposées d'une vidéo : « 2,3 s » (« 2.3 s »
    en anglais). Un libellé déjà écrit reste tel quel."""
    return [[frames, seconds if isinstance(seconds, str) else _seconds(seconds)]
            for frames, seconds in model(model_id).get("durations") or []]


def _seconds(value) -> str:
    digits = 0 if float(value).is_integer() else 1
    return t("format.seconds", n=i18n.number(value, digits))


# ------------------------------------------------------------ vérification
def cache_file_path() -> Path:
    return paths.data_dir() / "images" / "verified.json"


def _load(cache_file: Path | None = None) -> dict:
    try:
        return json.loads((cache_file or cache_file_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _stamp(path: Path) -> list:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def record(path: Path, sha256: str, cache_file: Path | None = None):
    """Note l'empreinte d'un fichier (calculée au téléchargement ou après coup), dans
    `cache_file` : celui des données d'AIODOS au moment où le calcul a commencé."""
    target = cache_file or cache_file_path()
    with _lock:
        cache = _load(target)
        cache[str(path)] = {"stamp": _stamp(path), "sha256": sha256}
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".tmp")
        part.write_bytes(json.dumps(cache, indent=1).encode("utf-8"))
        os.replace(part, target)


def sha256_of(path: Path, stop=None) -> str | None:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            if stop is not None and stop.is_set():
                return None
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path, spec: dict, cache_file: Path | None = None) -> bool:
    """Calcule l'empreinte maintenant (quelques secondes par Go) et la note."""
    target = cache_file or cache_file_path()       # avant le calcul, qui dure
    digest = sha256_of(path)
    record(path, digest, target)
    if digest != spec["sha256"]:
        log.warning("Empreinte inattendue pour %s", path)
    return digest == spec["sha256"]


def _verify_later(path: Path, spec: dict):
    with _lock:
        if str(path) in _checking:
            return
        _checking.add(str(path))

    target = cache_file_path()        # fixé au lancement : le fil peut survivre à son contexte

    def run():
        try:
            verify(path, spec, target)
        except OSError as exc:
            log.warning("Vérification impossible de %s : %s", path, exc)
        finally:
            with _lock:
                _checking.discard(str(path))
    threading.Thread(target=run, daemon=True, name="aiodos-images-empreinte").start()


def file_state(spec: dict, check: bool = True) -> str:
    """ok | missing | partial (téléchargement commencé) | checking | bad.
    Un fichier présent mais pas encore vérifié l'est en arrière-plan (check)."""
    path = folder() / spec["name"]
    try:
        stamp = _stamp(path)
    except OSError:
        return "partial" if path.with_name(path.name + ".part").exists() else "missing"
    if stamp[0] != spec["size"]:
        return "bad"
    with _lock:
        known = _load().get(str(path))
        busy = str(path) in _checking
    if known and known.get("stamp") == stamp:
        return "ok" if known.get("sha256") == spec["sha256"] else "bad"
    if check and not busy:
        _verify_later(path, spec)
    return "checking"


def state(model_id: str, check: bool = True) -> dict:
    """Ce que cette machine a du modèle : « ready » s'il est complet et vérifié."""
    files = [{"part": part, "name": spec["name"], "size": spec["size"],
              "state": file_state(spec, check)}
             for part, spec in model(model_id)["files"].items()]
    states = {f["state"] for f in files}
    if states == {"ok"}:
        overall = "ready"
    elif "bad" in states:
        overall = "bad"
    elif "checking" in states:
        overall = "checking"
    else:
        overall = "missing"
    missing = sum(f["size"] for f in files if f["state"] != "ok")
    return {"state": overall, "files": files, "size": total_size(model_id),
            "missing_bytes": missing}


def label(model_id: str) -> str:
    """Le nom affiché d'un modèle : son nom propre, et ce qu'il crée quand ce n'est pas
    une image (« Wan 2.1 · vidéo »), dans la langue en cours."""
    spec = model(model_id)
    kind = spec.get("label_kind")
    return f"{spec['label']} · {t(kind)}" if kind else spec["label"]


def ready(model_id: str) -> bool:
    return model_id in MODELS and state(model_id)["state"] == "ready"


def files(model_id: str) -> dict:
    """{partie : chemin} d'un modèle complet et vérifié ; ModelError sinon."""
    if not ready(model_id):
        raise ModelError(t("imaging.model_incomplete", name=label(model_id)))
    return {part: folder() / spec["name"] for part, spec in model(model_id)["files"].items()}


def available() -> list:
    """Modèles complets et vérifiés ici (profil de la machine, unités qu'elle peut calculer)."""
    return [m for m in MODELS if ready(m)]
