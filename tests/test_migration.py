"""Import des données de la v1 (fichiers JSON) dans la base SQLite."""
import json

from coeur import db, migrate_legacy, settings
from coeur.knowledge import store


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_v1_data_is_imported_once(tmp_path):
    legacy = tmp_path / "legacy_data"
    _write(legacy / "config.json", {"default_model": "qwen2.5-coder:7b",
                                    "web_search_enabled": True, "mistral_key": ""})
    _write(legacy / "sessions" / "_folders.json", ["Projet X"])
    _write(legacy / "sessions" / "1717000000000.json", {
        "id": "1717000000000", "title": "Ma conversation", "created": 1717000000.0,
        "model": "mistral:7b", "folder": "Projet X",
        "messages": [{"role": "user", "content": "Bonjour"},
                     {"role": "assistant", "content": "Salut !"}]})
    _write(legacy / "knowledge" / "collections.json",
           {"Général": {"enabled": True}, "2024": {"enabled": False},
            "💬 Conversations": {"enabled": True}})
    _write(legacy / "knowledge" / "chunks.json", [
        {"collection": "2024", "source": "sous\\b.md", "kind": "folder",
         "text": "Budget 2024", "embedding": [0.1, 0.2, 0.3]},
        {"collection": "2024", "source": "sous\\b.md", "kind": "folder",
         "text": "suite", "embedding": [0.3, 0.2, 0.1]},
        {"collection": "💬 Conversations", "source": "Ma conversation", "sid": "x",
         "text": "souvenir", "embedding": [1, 0, 0]},
    ])
    store.ensure_defaults()

    stats = migrate_legacy.run_if_needed(legacy)
    assert stats["conversations"] == 1 and stats["messages"] == 2
    assert stats["entries"] == 1 and stats["chunks"] == 2
    assert settings.get("default_model") == "qwen2.5-coder:7b"
    conv = db.query_one("SELECT c.title, f.name AS folder FROM conversations c "
                        "JOIN folders f ON f.id = c.folder_id")
    assert (conv["title"], conv["folder"]) == ("Ma conversation", "Projet X")
    collections = {c["name"]: c for c in store.list_collections()}
    assert not collections["2024"]["enabled"]
    assert [e["source"] for e in store.list_entries(collections["2024"]["id"])] == ["sous/b.md"]
    memory = store.conversation_collection_id()
    assert store.list_entries(memory, include_system=True) == []    # reconstruite plus tard

    assert migrate_legacy.run_if_needed(legacy) is None              # une seule fois
