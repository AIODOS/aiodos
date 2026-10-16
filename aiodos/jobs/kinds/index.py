"""Travaux d'indexation : fichiers & dossiers, pages web, mémoire des conversations.

Chaque fichier devient une unité autonome. Fichiers et pages web sont des
tâches portables : le calcul (extraction du texte, découpage, embeddings) peut
se faire sur une autre machine, l'enregistrement dans la bibliothèque se fait
toujours ici. La mémoire des conversations, elle, ne quitte jamais ce nœud.
"""
import json
import os
from collections import Counter
from pathlib import Path

import numpy as np

from aiodos import db, settings
from aiodos.jobs.registry import (Interrupted, UnitSpec, register_job_kind,
                                 register_portable, task)
from aiodos.knowledge import chunking, store
from aiodos.services import extract, media, ollama, web

# Dossiers jamais parcourus (outils de dev, corbeille…)
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "$RECYCLE.BIN",
             "System Volume Information"}

# Ressource utilisée selon la famille de fichier
_RESOURCE = {"text": "embed", "document": "embed", "unknown": "embed",
             "image": "gpu", "media": "cpu"}


# ----------------------------------------------------------- fichiers/dossiers
def _plan_file(path: Path, collection_id, source, kind, skipped: Counter):
    family = extract.classify(path)
    ext = path.suffix.lower() or "(sans extension)"
    ignored = (family == "binary"
               or (family == "image" and not settings.get("media_vision"))
               or (family == "media" and not settings.get("media_transcribe")))
    if ignored:
        skipped[ext] += 1
        return None
    return UnitSpec("index_file", _RESOURCE[family],
                    {"path": str(path), "collection_id": collection_id,
                     "source": source, "kind": kind})


def _walk(folder: Path):
    """Fichiers d'un dossier, récursivement. Les fichiers et dossiers cachés
    (.git, .env…) sont ignorés : ils contiennent souvent des secrets."""
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith("."):
                yield Path(dirpath) / name


def split_index_paths(params):
    collection_id = params["collection_id"]
    paths = [Path(p) for p in params["paths"]]
    units, skipped = [], Counter()
    folders = [p for p in paths if p.is_dir()]
    for path in paths:
        if path.is_dir():
            root = path.name or str(path)
            for f in _walk(path):
                unit = _plan_file(f, collection_id,
                                  f"{root}/{f.relative_to(path).as_posix()}",
                                  "folder", skipped)
                if unit:
                    units.append(unit)
        elif path.is_file():
            unit = _plan_file(path, collection_id, path.name, "file", skipped)
            if unit:
                units.append(unit)
    target = store.collection_name(collection_id)
    if len(paths) == 1 and folders:
        title = f"Indexer le dossier « {folders[0].name} » → {target}"
    else:
        title = f"Indexer {len(paths)} élément(s) → {target}"
    info = {"skipped": sum(skipped.values()), "skipped_types": dict(skipped)}
    if not units:
        info["message"] = "Aucun fichier exploitable dans la sélection."
    return title, units, info


def index_options() -> dict:
    """Réglages du propriétaire, figés au moment du calcul : une machine qui
    calcule pour une autre applique ceux de l'autre, pas les siens."""
    vision = None
    if settings.get("media_vision"):
        vision = {"model": settings.get("vision_model"),
                  "mode": settings.get("media_vision_mode")}
    return {"embed_model": settings.get("embed_model"), "vision": vision,
            "whisper": settings.get("whisper_model") if settings.get("media_transcribe") else None}


def requires_index(unit, options) -> dict:
    """Ce qu'il faut à une machine pour calculer cette unité."""
    need = {"embed": options["embed_model"]}
    if unit["resource"] == "gpu" and options.get("vision"):
        need["vision"] = options["vision"]["model"]
    if unit["resource"] == "cpu" and options.get("whisper"):
        need["whisper"] = options["whisper"]
    return need


EMBED_BATCH = 16


def _embed(pieces, options, stop) -> dict:
    vectors = []
    for start in range(0, len(pieces), EMBED_BATCH):
        if stop is not None and stop.is_set():
            raise Interrupted()
        vectors.extend(ollama.embed(pieces[start:start + EMBED_BATCH],
                                    model=options["embed_model"]))
    return {"pieces": pieces, "vectors": np.asarray(vectors, dtype=np.float32)}


def compute_index_file(payload, file, options, stop=None):
    path = Path(file)
    if not path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {payload.get('path') or payload['source']}")
    vision = transcribe = None
    if options.get("vision"):
        model, mode = options["vision"]["model"], options["vision"]["mode"]
        vision = lambda p: media.vision_extract(p, model, mode)
    if options.get("whisper"):
        transcribe = lambda p: media.transcribe(p, options["whisper"], stop=stop)
    text = extract.extract_text(path, vision=vision, transcribe=transcribe)
    if stop is not None and stop.is_set():
        raise Interrupted()
    if not text or not text.strip():
        return {"skipped": True, "ext": path.suffix.lower() or "(sans extension)",
                "reason": "aucun texte exploitable"}
    return _embed(chunking.chunk_text(text), options, stop)


def assimilate_index_file(payload, output):
    if output.get("skipped"):
        return output
    store.upsert_entry(payload["collection_id"], payload["source"], payload["kind"],
                       payload["path"], list(zip(output["pieces"], output["vectors"])))
    return {"chunks": len(output["pieces"])}


def check_embedded(payload, output):
    """Contrôle d'une sortie calculée sur une autre machine, avant de l'intégrer :
    forme, valeurs finies, et même dimension que les vecteurs déjà stockés."""
    if output.get("skipped"):
        return
    pieces, vectors = output.get("pieces"), output.get("vectors")
    if not isinstance(pieces, list) or not pieces or not all(isinstance(p, str) for p in pieces):
        raise ValueError("passages absents ou invalides")
    if not isinstance(vectors, np.ndarray) or vectors.ndim != 2 or len(vectors) != len(pieces):
        raise ValueError("vecteurs absents ou en nombre différent des passages")
    if not np.isfinite(vectors).all() or (np.linalg.norm(vectors, axis=1) < 1e-6).any():
        raise ValueError("vecteurs invalides (valeurs nulles ou non finies)")
    dim = store.embedding_dim(payload["collection_id"])
    if dim and vectors.shape[1] != dim:
        raise ValueError(f"vecteurs de dimension {vectors.shape[1]} au lieu de {dim} "
                         "(modèle d'embedding différent ?)")


register_portable("index_file", compute_index_file, assimilate_index_file, file_key="path",
                  options=index_options, requires=requires_index, validate=check_embedded)


def finalize_index(job_id, params, units):
    skipped = Counter()
    for unit in units:
        if unit["status"] == "skipped":
            skipped[unit["result"].get("ext", "?")] += 1
    job = db.query_one("SELECT result FROM jobs WHERE id=?", (job_id,))
    planned = json.loads(job["result"] or "{}").get("skipped_types", {})
    for ext, n in planned.items():
        skipped[ext] += n
    return {"indexed": sum(1 for u in units if u["status"] == "done"),
            "chunks": sum(u["result"].get("chunks", 0) for u in units),
            "skipped_types": dict(skipped.most_common())}


register_job_kind("index_paths", split_index_paths, finalize_index)


# ------------------------------------------------------------------------ web
def split_index_urls(params):
    urls = [u.strip() for u in params["urls"] if u.strip()]
    units = [UnitSpec("index_url", "net",
                      {"url": u, "collection_id": params["collection_id"]}) for u in urls]
    target = store.collection_name(params["collection_id"])
    return f"Lire {len(units)} page(s) web → {target}", units, {}


def compute_index_url(payload, file, options, stop=None):
    title, text = web.fetch_url_text(payload["url"])
    if not text.strip():
        return {"skipped": True, "ext": "url", "reason": "page vide"}
    output = _embed(chunking.chunk_text(f"{title}\n\n{text}"), options, stop)
    output["title"] = title
    return output


def assimilate_index_url(payload, output):
    if output.get("skipped"):
        return output
    store.upsert_entry(payload["collection_id"], payload["url"], "url", payload["url"],
                       list(zip(output["pieces"], output["vectors"])))
    return {"chunks": len(output["pieces"]), "title": output.get("title")}


register_portable("index_url", compute_index_url, assimilate_index_url,
                  options=index_options, requires=requires_index, validate=check_embedded)

register_job_kind("index_urls", split_index_urls, finalize_index)


# ---------------------------------------------- mémoire des conversations
def split_index_conversation(params):
    row = db.query_one("SELECT title FROM conversations WHERE id=?",
                       (params["conversation_id"],))
    title = row["title"] if row else f"#{params['conversation_id']}"
    return (f"Mémoriser la conversation « {title} »",
            [UnitSpec("index_conversation", "embed", dict(params))], {})


@task("index_conversation")
def run_index_conversation(payload):
    """Indexe seulement les nouveaux messages (pas de ré-embedding du début),
    sauf en reconstruction (payload « rebuild », après un changement de modèle
    d'embedding) : tous les messages sont recalculés et remplacent les anciens.
    Chaque passage porte l'id du message dans son index : idx = id×1000 + n."""
    conv_id = payload["conversation_id"]
    rebuild = bool(payload.get("rebuild"))
    collection_id = store.conversation_collection_id()
    conv = db.query_one("SELECT title FROM conversations WHERE id=?", (conv_id,))
    if conv is None:
        store.remove_by_origin(collection_id, str(conv_id))     # mémoire orpheline
        return {"skipped": True, "ext": "conversation", "reason": "supprimée"}
    entry = store.entry_by_origin(collection_id, str(conv_id))
    if rebuild and entry is None:
        return {"skipped": True, "ext": "conversation", "reason": "mémoire vidée"}
    last_message = store.max_chunk_index(entry["id"]) // 1000 if entry and not rebuild else -1
    messages = db.query("SELECT id, role, content FROM messages WHERE conversation_id=? "
                        "AND id > ? ORDER BY id", (conv_id, last_message))
    pieces = []
    for m in messages:
        who = "Utilisateur" if m["role"] == "user" else "Assistant"
        for n, piece in enumerate(chunking.chunk_text(f"{who} : {m['content']}")[:999]):
            pieces.append((m["id"] * 1000 + n, piece))
    if not pieces:
        if rebuild:                       # plus aucun message : rien à garder en mémoire
            store.remove_by_origin(collection_id, str(conv_id))
        return {"chunks": 0}
    vectors = ollama.embed([p for _, p in pieces])
    store.append_chunks(collection_id, f"{conv['title']} · #{conv_id}", "conversation",
                        str(conv_id), [(idx, text, vec)
                                       for (idx, text), vec in zip(pieces, vectors)],
                        replace=rebuild)
    return {"chunks": len(pieces)}


register_job_kind("index_conversation", split_index_conversation)
