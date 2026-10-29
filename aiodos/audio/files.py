"""Les fichiers des modèles d'audio qu'a cette machine, et leur vérification.

Ils sont rangés dans data/audio/models/ (réglage « audio_models_dir » pour un autre dossier),
au chemin que leur donne le catalogue (audio/catalog.py). Leur empreinte n'est recalculée que
si leur taille ou leur date changent : le résultat est noté dans data/audio/verified.json, comme
pour les images (imaging/catalog.py, dont le calcul et l'écriture sont repris).
"""
import json
import logging
import threading
from pathlib import Path

from aiodos import paths, settings
from aiodos.audio import catalog
from aiodos.i18n import t
from aiodos.imaging import catalog as images_catalog

log = logging.getLogger("aiodos.audio")

_lock = threading.Lock()
_checking = set()                 # fichiers dont l'empreinte se calcule en ce moment


def folder() -> Path:
    configured = str(settings.get("audio_models_dir") or "").strip()
    return Path(configured) if configured else paths.data_dir() / "audio" / "models"


def cache_file_path() -> Path:
    return paths.data_dir() / "audio" / "verified.json"


def path_of(spec: dict, base: Path | None = None) -> Path:
    """Le chemin d'un fichier du catalogue sur cette machine (ses « / » suivent le système)."""
    return (base or folder()).joinpath(*spec["path"].split("/"))


def _load(cache_file: Path) -> dict:
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _stamp(path: Path) -> list:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns]


def record(path: Path, sha256: str, cache_file: Path | None = None):
    images_catalog.record(path, sha256, cache_file or cache_file_path())


def verify(path: Path, spec: dict, cache_file: Path | None = None) -> bool:
    """Calcule l'empreinte maintenant (quelques secondes par Go) et la note."""
    target = cache_file or cache_file_path()
    digest = images_catalog.sha256_of(path)
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
    threading.Thread(target=run, daemon=True, name="aiodos-audio-empreinte").start()


def file_state(spec: dict, check: bool = True) -> str:
    """ok | missing | partial (téléchargement commencé) | checking | bad. Un fichier présent
    mais pas encore vérifié l'est en arrière-plan (check)."""
    path = path_of(spec)
    try:
        stamp = _stamp(path)
    except OSError:
        return "partial" if path.with_name(path.name + ".part").exists() else "missing"
    if stamp[0] != spec["size"]:
        return "bad"
    with _lock:
        busy = str(path) in _checking
    known = _load(cache_file_path()).get(str(path))
    if known and known.get("stamp") == stamp:
        return "ok" if known.get("sha256") == spec["sha256"] else "bad"
    if check and not busy:
        _verify_later(path, spec)
    return "checking"


def state(model_id: str, check: bool = True) -> dict:
    """Ce que cette machine a du modèle : « ready » s'il est complet et vérifié."""
    found = [{"part": part, "name": spec["path"].rsplit("/", 1)[-1], "size": spec["size"],
              "state": file_state(spec, check)}
             for part, spec in catalog.model(model_id)["files"].items()]
    states = {f["state"] for f in found}
    if states == {"ok"}:
        overall = "ready"
    elif "bad" in states:
        overall = "bad"
    elif "checking" in states:
        overall = "checking"
    else:
        overall = "missing"
    missing = sum(f["size"] for f in found if f["state"] != "ok")
    return {"state": overall, "files": found, "size": catalog.total_size(model_id),
            "missing_bytes": missing}


def ready(model_id: str) -> bool:
    return model_id in catalog.MODELS and state(model_id)["state"] == "ready"


def model_path(model_id: str) -> Path:
    """Ce qu'audio.cpp reçoit en --model : le dossier du paquet (model_dir), sinon le fichier
    « model » ; catalog.ModelError si le modèle n'est pas complet et vérifié ici."""
    if not ready(model_id):
        raise catalog.ModelError(t("audio.model_incomplete", name=catalog.label(model_id)))
    spec = catalog.model(model_id)
    if spec.get("model_dir"):
        return folder() / spec["model_dir"]
    return path_of(spec["files"]["model"])


def available() -> list:
    """Modèles complets et vérifiés ici (profil de la machine, unités qu'elle peut calculer)."""
    return [m for m in catalog.MODELS if ready(m)]


def verified_hashes() -> list:
    """Les empreintes des fichiers vérifiés ici (ce qu'une machine dit savoir calculer)."""
    return sorted({spec["sha256"] for m in catalog.MODELS.values()
                   for spec in m["files"].values() if file_state(spec) == "ok"})
