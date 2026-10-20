"""Le fichier d'un modèle d'Ollama, lu tel quel par llama.cpp, et son en-tête.

Ollama range chaque modèle dans son dossier de modèles : un manifeste par nom
(manifests/<registre>/<espace>/<nom>/<étiquette>) qui désigne ses couches, dont le
fichier GGUF des poids (blobs/sha256-…). Mesuré le 17/10/2026 : llama.cpp b11707 lit ces
fichiers sans conversion (qwen3.5:9b, qwen3.6).

L'en-tête GGUF donne ce qu'il faut pour découper le modèle : nombre de couches, taille de
l'état intermédiaire (ce qui voyage entre deux machines à chaque mot), taille totale.
"""
import json
import os
import struct
from pathlib import Path

MODEL_LAYER = "application/vnd.ollama.image.model"
DEFAULT_REGISTRY = "registry.ollama.ai"
# Taille en octets des valeurs simples d'un en-tête GGUF, par type
_SCALAR_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_SCALAR_FORMATS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
                   10: "<Q", 11: "<q", 12: "<d"}
_STRING, _ARRAY = 8, 9
MAX_KEYS = 100_000


class WeightsError(Exception):
    pass


def models_dir() -> Path:
    """Dossier des modèles d'Ollama (variable OLLAMA_MODELS, sinon ~/.ollama/models)."""
    configured = os.environ.get("OLLAMA_MODELS")
    return Path(configured) if configured else Path.home() / ".ollama" / "models"


def manifest_path(model: str, root: Path | None = None) -> Path:
    """« qwen3.6 » → manifests/registry.ollama.ai/library/qwen3.6/latest ;
    « hf.co/bartowski/Qwen-GGUF:Q4_K_M » → manifests/hf.co/bartowski/Qwen-GGUF/Q4_K_M."""
    name, _, tag = model.strip().partition(":")
    if "/" in tag:                          # « hôte:port/… » : pas une étiquette
        name, tag = model, ""
    parts = [p for p in name.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise WeightsError(f"Nom de modèle illisible : {model!r}")
    if len(parts) == 1:
        parts = [DEFAULT_REGISTRY, "library", *parts]
    elif "." not in parts[0] and ":" not in parts[0]:
        parts = [DEFAULT_REGISTRY, *parts]
    return (root or models_dir()) / "manifests" / Path(*parts) / (tag or "latest")


def gguf_path(model: str, root: Path | None = None) -> Path:
    """Fichier GGUF des poids d'un modèle installé dans Ollama."""
    root = root or models_dir()
    manifest = manifest_path(model, root)
    try:
        layers = json.loads(manifest.read_text(encoding="utf-8")).get("layers") or []
    except (OSError, ValueError) as exc:
        raise WeightsError(f"Modèle « {model} » introuvable dans Ollama.") from exc
    digest = next((layer.get("digest") for layer in layers
                   if layer.get("mediaType") == MODEL_LAYER), None)
    if not digest or not digest.startswith("sha256:"):
        raise WeightsError(f"Le modèle « {model} » n'a pas de fichier de poids lisible.")
    path = root / "blobs" / digest.replace(":", "-")
    if not path.is_file():
        raise WeightsError(f"Fichier de poids absent pour « {model} ».")
    return path


# ------------------------------------------------------------------------ en-tête GGUF
def _read(f, fmt):
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise WeightsError("En-tête GGUF tronqué.")
    return struct.unpack(fmt, data)[0]


def _string(f) -> str:
    return f.read(_read(f, "<Q")).decode("utf-8", "replace")


def _value(f, kind):
    if kind in _SCALAR_FORMATS:
        return _read(f, _SCALAR_FORMATS[kind])
    if kind == _STRING:
        return _string(f)
    if kind == _ARRAY:                       # sautées : seul leur nombre d'éléments compte
        inner, count = _read(f, "<I"), _read(f, "<Q")
        if inner in _SCALAR_SIZES:
            f.seek(_SCALAR_SIZES[inner] * count, os.SEEK_CUR)
        elif inner == _STRING:
            for _ in range(count):
                f.seek(_read(f, "<Q"), os.SEEK_CUR)
        else:
            for _ in range(count):
                _value(f, inner)
        return count
    raise WeightsError(f"Type GGUF inconnu : {kind}")


def header(path) -> dict:
    """Architecture, nombre de couches, taille de l'état intermédiaire et du vocabulaire,
    experts éventuels, taille du fichier."""
    path = Path(path)
    meta = {}
    with path.open("rb") as f:
        if f.read(4) != b"GGUF":
            raise WeightsError("Ce fichier n'est pas au format GGUF.")
        version, _tensors, keys = _read(f, "<I"), _read(f, "<Q"), _read(f, "<Q")
        if version < 2 or keys > MAX_KEYS:
            raise WeightsError("En-tête GGUF illisible.")
        for _ in range(keys):
            key = _string(f)
            meta[key] = _value(f, _read(f, "<I"))
    arch = meta.get("general.architecture") or ""
    get = lambda name, default=0: meta.get(f"{arch}.{name}", default)   # noqa: E731
    return {
        "architecture": arch,
        "layers": int(get("block_count")),
        "embedding": int(get("embedding_length")),
        "experts": int(get("expert_count")),
        "experts_used": int(get("expert_used_count")),
        "vocabulary": int(meta.get("tokenizer.ggml.tokens", 0) or 0),
        "size": path.stat().st_size,
        # Moteur intégré (aiodos/llm/models.py) : un modèle d'embedding déclare sa mise en
        # commun ; le modèle de conversation dit s'il sait réfléchir ou appeler des outils
        "pooling": get("pooling_type", None),
        "template": str(meta.get("tokenizer.chat_template") or ""),
        "name": str(meta.get("general.name") or ""),
    }
