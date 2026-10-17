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
"""
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from aiodos import paths, settings

log = logging.getLogger("aiodos.images")

HF = "https://huggingface.co/{repo}/resolve/{commit}/{path}"

# Option de sd-server pour chaque partie d'un modèle
FLAGS = {"diffusion": "--diffusion-model", "llm": "--llm", "vae": "--vae", "model": "--model",
         "t5xxl": "--t5xxl", "clip_l": "--clip_l"}

MODELS = {
    # Licences vérifiées le 17/10/2026 : Apache-2.0 pour les trois fichiers
    "z-image-turbo": {
        "label": "Z-Image Turbo",
        "license": "Apache-2.0",
        "about": "Tongyi-MAI (Alibaba), 6 milliards de paramètres, 8 pas : rapide, "
                 "photoréaliste, sait écrire du texte dans l'image.",
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
        "steps": 8, "cfg": 1.0, "negative": False,
    },
}

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
        raise ModelError(f"Modèle d'images inconnu : {model_id}")
    return found


def total_size(model_id: str) -> int:
    return sum(f["size"] for f in model(model_id)["files"].values())


def hashes(model_id: str) -> list:
    """Empreintes des fichiers du modèle : une machine qui les a toutes calcule pareil."""
    return sorted(f["sha256"] for f in model(model_id)["files"].values())


# ------------------------------------------------------------ vérification
def _cache_path() -> Path:
    return paths.data_dir() / "images" / "verified.json"


def _load() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _stamp(path: Path) -> list:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def record(path: Path, sha256: str):
    """Note l'empreinte d'un fichier (calculée au téléchargement ou après coup)."""
    with _lock:
        cache = _load()
        cache[str(path)] = {"stamp": _stamp(path), "sha256": sha256}
        target = _cache_path()
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


def verify(path: Path, spec: dict) -> bool:
    """Calcule l'empreinte maintenant (quelques secondes par Go) et la note."""
    digest = sha256_of(path)
    record(path, digest)
    if digest != spec["sha256"]:
        log.warning("Empreinte inattendue pour %s", path)
    return digest == spec["sha256"]


def _verify_later(path: Path, spec: dict):
    with _lock:
        if str(path) in _checking:
            return
        _checking.add(str(path))

    def run():
        try:
            verify(path, spec)
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


def ready(model_id: str) -> bool:
    return model_id in MODELS and state(model_id)["state"] == "ready"


def files(model_id: str) -> dict:
    """{partie : chemin} d'un modèle complet et vérifié ; ModelError sinon."""
    if not ready(model_id):
        raise ModelError(f"Le modèle « {model(model_id)['label']} » n'est pas complet sur "
                         "cette machine.")
    return {part: folder() / spec["name"] for part, spec in model(model_id)["files"].items()}


def available() -> list:
    """Modèles complets et vérifiés ici (profil de la machine, unités qu'elle peut calculer)."""
    return [m for m in MODELS if ready(m)]
