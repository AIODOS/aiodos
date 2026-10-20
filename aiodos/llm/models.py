"""Les modèles du moteur intégré : ceux qu'AIODOS a téléchargés, et ceux d'Ollama que
llama.cpp sait lire tels quels, sans lancer Ollama.

- AIODOS : data/models/<dossier>/model.json (nom, fichiers, licence, source) à côté de ses
  fichiers GGUF (les poids, et le projecteur d'images s'il y en a un) ;
- Ollama : son dossier de modèles (manifestes et fichiers, cluster/weights.py). Mesuré le
  20/10/2026 avec llama.cpp b11707 : la plupart se chargent tels quels (qwen3.5, mistral,
  qwen2.5, bge-m3, nomic-embed-text, qwen3-embedding…), pas ceux qu'Ollama a convertis à
  sa façon (embeddinggemma, qwen2.5vl, snowflake-arctic-embed2). Un modèle qui ne se charge
  pas est noté (data/llm/incompatibles.json) et n'est plus proposé ; un modèle d'AIODOS du
  même nom passe toujours avant celui d'Ollama.

Les noms sont ceux d'Ollama (« qwen3.5:9b », « embeddinggemma:latest ») : réglages et
conversations n'ont pas à changer. Le routeur de llama.cpp, lui, reçoit un identifiant
sans « : » (il met en majuscules ce qui suit, comme une quantification).
"""
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from aiodos import paths
from aiodos.cluster import weights

log = logging.getLogger("aiodos.llm")

PROJECTOR_LAYER = "application/vnd.ollama.image.projector"
CONFIG_TTL = 10                    # la liste est relue au plus toutes les 10 s
_cache = {"at": 0.0, "models": None}
_headers = {}                      # (chemin, taille, date) -> en-tête GGUF
_lock = threading.Lock()


@dataclass(frozen=True)
class Model:
    name: str                      # « qwen3.5:9b »
    id: str                        # identifiant donné au routeur de llama.cpp
    path: Path                     # poids (GGUF)
    mmproj: Path | None            # projecteur d'images : le modèle voit les images
    embedding: bool
    size: int
    digest: str
    family: str
    parameters: str
    quantization: str
    template: str                  # modèle de conversation (réflexion, outils)
    source: str                    # « aiodos » ou « ollama »
    license: str = ""


def models_dir() -> Path:
    return paths.data_dir() / "models"


def _state_file() -> Path:
    return paths.data_dir() / "llm" / "incompatibles.json"


def full_name(name: str) -> str:
    name = str(name or "").strip()
    return name if ":" in name else f"{name}:latest"


def router_id(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", full_name(name).lower()).strip("-")


def _header(path: Path) -> dict:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime)
    if key not in _headers:
        _headers[key] = weights.header(path)
    return _headers[key]


def _model(name, path, mmproj, source, digest="", family="", parameters="", quantization="",
           license="", embedding=None) -> Model | None:
    try:
        head = _header(path)
    except (OSError, weights.WeightsError) as exc:
        log.info("Modèle « %s » illisible : %s", name, exc)
        return None
    if embedding is None:
        embedding = head.get("pooling") is not None
    return Model(name=full_name(name), id=router_id(name), path=path, mmproj=mmproj,
                 embedding=bool(embedding), size=head["size"], digest=digest,
                 family=family or head.get("architecture", ""), parameters=parameters,
                 quantization=quantization, template=head.get("template", ""),
                 source=source, license=license)


def _from_aiodos() -> list:
    found = []
    root = models_dir()
    if not root.is_dir():
        return found
    for meta_file in sorted(root.glob("*/model.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        folder = meta_file.parent
        weights_file = folder / str(meta.get("file") or "")
        if not meta.get("name") or not weights_file.is_file():
            continue
        projector = folder / str(meta.get("mmproj") or "")
        model = _model(meta["name"], weights_file,
                       projector if meta.get("mmproj") and projector.is_file() else None,
                       "aiodos", digest=str(meta.get("sha256") or ""),
                       family=str(meta.get("family") or ""),
                       parameters=str(meta.get("parameters") or ""),
                       quantization=str(meta.get("quantization") or ""),
                       license=str(meta.get("license") or ""),
                       embedding=meta.get("kind") == "embedding" if meta.get("kind") else None)
        if model:
            found.append(model)
    return found


def _from_ollama() -> list:
    root = weights.models_dir()
    manifests = root / "manifests"
    found = []
    if not manifests.is_dir():
        return found
    for manifest in sorted(p for p in manifests.rglob("*") if p.is_file()):
        parts = manifest.relative_to(manifests).parts
        if len(parts) < 3:
            continue
        registry, *names, tag = parts
        if registry == weights.DEFAULT_REGISTRY and names[:1] == ["library"]:
            names = names[1:]
        name = "/".join(names if registry == weights.DEFAULT_REGISTRY else [registry, *names])
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        layers = {l.get("mediaType"): l.get("digest") for l in data.get("layers") or []}
        blob = lambda digest: root / "blobs" / str(digest).replace(":", "-")   # noqa: E731
        model_digest = layers.get(weights.MODEL_LAYER)
        if not model_digest or not blob(model_digest).is_file():
            continue
        config = {}
        if (data.get("config") or {}).get("digest"):
            try:
                config = json.loads(blob(data["config"]["digest"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                config = {}
        projector = layers.get(PROJECTOR_LAYER)
        model = _model(f"{name}:{tag}", blob(model_digest),
                       blob(projector) if projector and blob(projector).is_file() else None,
                       "ollama", digest=str(model_digest).removeprefix("sha256:"),
                       family=str(config.get("model_family") or ""),
                       parameters=str(config.get("model_type") or ""),
                       quantization=str(config.get("file_type") or ""))
        if model:
            found.append(model)
    return found


def incompatible() -> dict:
    """Fichiers de modèles qui ne se chargent pas dans llama.cpp : {chemin: {nom, raison}}
    (par fichier : la version standard retéléchargée sous le même nom passe)."""
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark_incompatible(model: Model, reason: str):
    """Noté une fois pour toutes, pour ce fichier : plus proposé."""
    with _lock:
        state = incompatible()
        state[str(model.path)] = {"name": model.name, "reason": str(reason)[:300]}
        _state_file().parent.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        _cache["at"] = 0.0
    log.warning("Modèle « %s » écarté du moteur intégré : %s", model.name, reason)


def available(max_age=CONFIG_TTL) -> list:
    """Les modèles servis par le moteur intégré : ceux d'AIODOS, puis ceux d'Ollama d'un
    autre nom, sans les incompatibles."""
    with _lock:
        if _cache["models"] is not None and time.monotonic() - _cache["at"] < max_age:
            return list(_cache["models"])
    bad = incompatible()
    chosen, seen, ids = [], set(), set()
    for model in _from_aiodos() + _from_ollama():
        if model.name in seen or str(model.path) in bad or model.id in ids:
            continue
        seen.add(model.name)
        ids.add(model.id)
        chosen.append(model)
    with _lock:
        _cache.update(at=time.monotonic(), models=chosen)
    return list(chosen)


def find(name: str) -> Model | None:
    wanted = full_name(name)
    return next((m for m in available() if m.name == wanted), None)


def forget_cache():
    with _lock:
        _cache["at"] = 0.0
