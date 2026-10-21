"""Le fichier d'un modèle d'Ollama, lu tel quel par llama.cpp, et son en-tête.

Ollama range chaque modèle dans son dossier de modèles : un manifeste par nom
(manifests/<registre>/<espace>/<nom>/<étiquette>) qui désigne ses couches, dont le
fichier GGUF des poids (blobs/sha256-…). Mesuré le 17/10/2026 : llama.cpp b11707 lit ces
fichiers sans conversion (qwen3.5:9b, qwen3.6).

L'en-tête GGUF donne ce qu'il faut pour découper le modèle : nombre de couches, taille de
l'état intermédiaire (ce qui voyage entre deux machines à chaque mot), taille totale.

Le nom d'un modèle vient d'ailleurs (« reparti:<modèle> » demandé par l'interface ou par
un outil branché sur la passerelle) : il ne devient un chemin que s'il suit les règles de
nom d'Ollama (types/model/name.go), et l'empreinte lue dans le manifeste que si c'est
bien « sha256: » et 64 chiffres hexadécimaux. Sous Windows, « ..\\ », « C:\\… » ou
« \\\\machine\\partage » feraient sinon lire un manifeste, puis charger par llama.cpp un
fichier, pris n'importe où (y compris sur un partage réseau, qui recevrait au passage
l'identité Windows de l'utilisateur).
"""
import json
import os
import re
import struct
from pathlib import Path, PurePath

from aiodos.i18n import t

MODEL_LAYER = "application/vnd.ollama.image.model"
DEFAULT_REGISTRY = "registry.ollama.ai"
# Règles de nom d'Ollama : chaque partie commence par une lettre, un chiffre ou « _ »,
# puis lettres, chiffres, « _ », « - » et « . » (pas de « . » dans l'espace de noms) ;
# l'hôte peut finir par « :port »
_HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,349}(:[0-9]{1,5})?")
_NAMESPACE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,159}")
_MODEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
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


def _unreadable(model) -> WeightsError:
    return WeightsError(t("cluster.model_name_unreadable", name=repr(str(model)[:120])))


def manifest_path(model: str, root: Path | None = None) -> Path:
    """« qwen3.6 » → manifests/registry.ollama.ai/library/qwen3.6/latest ;
    « hf.co/bartowski/Qwen-GGUF:Q4_K_M » → manifests/hf.co/bartowski/Qwen-GGUF/Q4_K_M.
    WeightsError pour un nom hors des règles d'Ollama (chemin, « .. », lecteur, partage
    réseau…) : le chemin rendu reste toujours dans le dossier des manifestes."""
    text = str(model or "").strip()
    # L'étiquette suit le dernier « : » du dernier élément (« hôte:port/espace/modèle »
    # n'en a pas), comme chez Ollama
    head, slash, last = text.rpartition("/")
    base, _, tag = last.partition(":")
    parts = (head + slash + base).split("/")
    if len(parts) == 1:
        parts = [DEFAULT_REGISTRY, "library", *parts]
    elif len(parts) == 2:
        parts = [DEFAULT_REGISTRY, *parts]
    if len(parts) != 3 or (tag and not _TAG.fullmatch(tag)):
        raise _unreadable(model)
    host, namespace, base = parts
    if not (_HOST.fullmatch(host) and _NAMESPACE.fullmatch(namespace)
            and _MODEL.fullmatch(base)):
        raise _unreadable(model)
    folder = (root or models_dir()) / "manifests"
    names = (host, namespace, base, tag or "latest")
    path = folder.joinpath(*names)
    # Sous Windows, « C:5 » (un hôte « C » et un port) serait un lecteur, « nom:flux » un
    # flux NTFS, « con » ou « nul » un périphérique : seul un simple nom de dossier passe
    if any(PurePath(p).name != p or _reserved(p) for p in names) \
            or not path.is_relative_to(folder):
        raise _unreadable(model)
    return path


# Noms de périphériques de Windows, réservés quelle que soit leur extension (« con.txt »)
_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{kind}{n}" for kind in ("COM", "LPT") for n in "0123456789¹²³"}


def _reserved(name: str) -> bool:
    """Un nom que Windows ne prend pas pour un simple dossier : périphérique (« con » serait
    ouvert comme la console, et la lecture du manifeste attendrait sans fin), « : » (flux
    NTFS), caractère de contrôle ou interdit, point ou espace final (que Windows retire).
    os.path.isreserved n'existe qu'à partir de Python 3.13 : ses règles, reprises ici,
    valent aussi avant (postes de développement)."""
    if os.name != "nt":
        return False
    check = getattr(os.path, "isreserved", None)
    if check is not None and check(name):
        return True
    if name[-1:] in (".", " "):
        return name not in (".", "..")
    if any(c in '*?"<>/\\:|' or ord(c) < 32 for c in name):
        return True
    return name.partition(".")[0].rstrip(" ").upper() in _DEVICES


def blob_path(digest, root: Path | None = None) -> Path:
    """Fichier d'une couche d'Ollama (blobs/sha256-…) d'après son empreinte, telle qu'un
    manifeste la donne ; WeightsError si ce n'est pas « sha256: » et 64 chiffres
    hexadécimaux (« sha256:..\\..\\autre-fichier » sortirait du dossier)."""
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise WeightsError(t("cluster.digest_unreadable"))
    return (root or models_dir()) / "blobs" / digest.replace(":", "-")


def gguf_path(model: str, root: Path | None = None) -> Path:
    """Fichier GGUF des poids d'un modèle installé dans Ollama."""
    root = root or models_dir()
    manifest = manifest_path(model, root)
    try:
        layers = json.loads(manifest.read_text(encoding="utf-8")).get("layers") or []
        digest = next((layer.get("digest") for layer in layers
                       if isinstance(layer, dict) and layer.get("mediaType") == MODEL_LAYER),
                      None)
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise WeightsError(t("cluster.not_in_ollama", name=model)) from exc
    try:
        path = blob_path(digest, root)
    except WeightsError:
        raise WeightsError(t("cluster.no_weights_file", name=model)) from None
    if not path.is_file():
        raise WeightsError(t("cluster.weights_missing", name=model))
    return path


# ------------------------------------------------------------------------ en-tête GGUF
def _read(f, fmt):
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise WeightsError(t("cluster.gguf_truncated"))
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
    raise WeightsError(t("cluster.gguf_unknown_type", kind=kind))


def header(path) -> dict:
    """Architecture, nombre de couches, taille de l'état intermédiaire et du vocabulaire,
    experts éventuels, taille du fichier."""
    path = Path(path)
    meta = {}
    with path.open("rb") as f:
        if f.read(4) != b"GGUF":
            raise WeightsError(t("cluster.not_gguf"))
        version, _tensors, keys = _read(f, "<I"), _read(f, "<Q"), _read(f, "<Q")
        if version < 2 or keys > MAX_KEYS:
            raise WeightsError(t("cluster.gguf_unreadable"))
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
