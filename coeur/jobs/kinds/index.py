"""Travaux d'indexation : fichiers & dossiers, pages web, mémoire des conversations.

Chaque fichier devient une unité autonome (son payload suffit à la traiter),
ce qui permettra en phase 1 de les répartir entre plusieurs machines.
"""
import json
import os
from collections import Counter
from pathlib import Path

from coeur import db, settings
from coeur.jobs.registry import UnitSpec, register_job_kind, task
from coeur.knowledge import chunking, store
from coeur.services import extract, media, ollama, web

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


@task("index_file")
def run_index_file(payload):
    path = Path(payload["path"])
    if not path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {path}")
    vision = transcribe = None
    if settings.get("media_vision"):
        model, mode = settings.get("vision_model"), settings.get("media_vision_mode")
        vision = lambda p: media.vision_extract(p, model, mode)
    if settings.get("media_transcribe"):
        whisper_model = settings.get("whisper_model")
        transcribe = lambda p: media.transcribe(p, whisper_model)
    text = extract.extract_text(path, vision=vision, transcribe=transcribe)
    if not text or not text.strip():
        return {"skipped": True, "ext": path.suffix.lower() or "(sans extension)",
                "reason": "aucun texte exploitable"}
    pieces = chunking.chunk_text(text)
    vectors = ollama.embed(pieces)
    store.upsert_entry(payload["collection_id"], payload["source"], payload["kind"],
                       str(path), list(zip(pieces, vectors)))
    return {"chunks": len(pieces)}


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


@task("index_url")
def run_index_url(payload):
    title, text = web.fetch_url_text(payload["url"])
    if not text.strip():
        return {"skipped": True, "ext": "url", "reason": "page vide"}
    pieces = chunking.chunk_text(f"{title}\n\n{text}")
    store.upsert_entry(payload["collection_id"], payload["url"], "url", payload["url"],
                       list(zip(pieces, ollama.embed(pieces))))
    return {"chunks": len(pieces), "title": title}


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
