"""Import unique des données de la v1 (legacy/data, fichiers JSON) dans SQLite.

Conversations, dossiers, bibliothèques (embeddings réutilisés tels quels, pas
de recalcul) et réglages. La mémoire des conversations de la v1 n'est pas
reprise : elle est reconstruite proprement par la file de tâches.
"""
import json
import logging
from collections import OrderedDict
from pathlib import Path

from aiodos import db, paths, settings
from aiodos.i18n import t
from aiodos.knowledge import store

log = logging.getLogger("aiodos.migration")

_MARKER = "legacy_migrated"
_V1_CONV_COLLECTION = "💬 Conversations"
_SETTING_KEYS = ("default_model", "embed_model", "knowledge_enabled",
                 "conversations_enabled", "web_search_enabled", "web_results",
                 "cloud_confirm", "mistral_key", "qwen_key", "mistral_cloud_model",
                 "qwen_cloud_model", "media_vision", "media_vision_mode", "vision_model",
                 "media_transcribe", "whisper_model", "system_prompt")


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _mark(stats):
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
               (_MARKER, json.dumps(stats, ensure_ascii=False)))


def run_if_needed(source: Path = paths.LEGACY_DATA_DIR):
    if db.query_one("SELECT 1 FROM settings WHERE key=?", (_MARKER,)):
        return None
    if not source.exists() or db.query_one("SELECT 1 FROM conversations LIMIT 1"):
        _mark({"skipped": True})
        return None
    stats = migrate(source)
    _mark(stats)
    log.info("Données v1 importées : %s", stats)
    return stats


def migrate(source: Path) -> dict:
    stats = {"settings": 0, "folders": 0, "conversations": 0, "messages": 0,
             "collections": 0, "entries": 0, "chunks": 0}

    config = _read(source / "config.json", {})
    values = {k: config[k] for k in _SETTING_KEYS if k in config}
    settings.set_many(values)
    stats["settings"] = len(values)

    # Dossiers et conversations
    folder_ids = {}
    for name in _read(source / "sessions" / "_folders.json", []):
        if name and name not in folder_ids:
            folder_ids[name] = db.execute("INSERT OR IGNORE INTO folders(name, created) "
                                          "VALUES(?, ?)", (name, db.now())).lastrowid
            stats["folders"] += 1
    new_conversations = []
    for f in sorted((source / "sessions").glob("*.json")):
        if f.name.startswith("_"):
            continue
        session = _read(f, None)
        if not session or not session.get("messages"):
            continue
        folder = session.get("folder") or ""
        if folder and folder not in folder_ids:
            folder_ids[folder] = db.execute("INSERT INTO folders(name, created) VALUES(?, ?)",
                                            (folder, db.now())).lastrowid
            stats["folders"] += 1
        created = float(session.get("created") or db.now())
        with db.transaction() as c:
            conv_id = c.execute(
                "INSERT INTO conversations(title, folder_id, model, created, updated) "
                "VALUES(?, ?, ?, ?, ?)",
                (session.get("title") or t("legacy.imported_conversation"), folder_ids.get(folder),
                 session.get("model") or None, created, created)).lastrowid
            for m in session["messages"]:
                c.execute("INSERT INTO messages(conversation_id, role, content, model, "
                          "sources, created) VALUES(?, ?, ?, ?, '[]', ?)",
                          (conv_id, m["role"], m["content"], session.get("model"), created))
                stats["messages"] += 1
        new_conversations.append(conv_id)
        stats["conversations"] += 1

    # Bibliothèques : on regroupe les passages par (collection, source)
    collections = _read(source / "knowledge" / "collections.json", {})
    groups = OrderedDict()
    for chunk in _read(source / "knowledge" / "chunks.json", []):
        collection = chunk.get("collection") or "Général"     # i18n: tel quel (nom de la v1)
        if collection == _V1_CONV_COLLECTION or not chunk.get("embedding"):
            continue
        source_name = (chunk.get("source") or "?").replace("\\", "/")
        groups.setdefault((collection, source_name), []).append(chunk)
    collection_ids = {c["name"]: c["id"] for c in store.list_collections()}
    for name, meta in collections.items():
        if name == _V1_CONV_COLLECTION:
            continue
        if name not in collection_ids:
            collection_ids[name] = store.create_collection(name)
            stats["collections"] += 1
        store.set_enabled(collection_ids[name], meta.get("enabled", True))
    for (collection, source_name), chunks in groups.items():
        if collection not in collection_ids:
            collection_ids[collection] = store.create_collection(collection)
            stats["collections"] += 1
        store.upsert_entry(collection_ids[collection], source_name,
                           chunks[0].get("kind", "file"), None,
                           [(c["text"], c["embedding"]) for c in chunks])
        stats["entries"] += 1
        stats["chunks"] += len(chunks)

    if settings.get("conversations_enabled"):
        from aiodos.jobs.kinds import index          # enregistre « index_conversation »
        for conv_id in new_conversations:
            index.queue_conversation(conv_id)
    return stats
