"""Bibliothèques : stockage, recherche, filtres, sommaire, suppression."""
import pytest

from coeur.knowledge import chunking, search, store


def _add(collection_id, source, text, embed, kind="file"):
    pieces = chunking.chunk_text(text)
    store.upsert_entry(collection_id, source, kind, None, list(zip(pieces, embed(pieces))))


def test_defaults_and_system_collection():
    store.ensure_defaults()
    names = [c["name"] for c in store.list_collections()]
    assert names == [store.DEFAULT_COLLECTION]
    system = [c for c in store.list_collections(include_system=True) if c["system"]]
    assert system[0]["name"] == store.CONV_COLLECTION


def test_search_finds_the_relevant_passage(fake_embed):
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    _add(general, "budget.txt", "Le budget 2024 est de cinquante mille euros.", fake_embed)
    _add(general, "chat.txt", "Le chat noir dort sur le canapé rouge.", fake_embed)
    hits = search.search(fake_embed(["quel est le budget 2024"])[0], k=1)
    assert hits[0]["source"] == "budget.txt"


def test_disabled_library_and_memory_flags(fake_embed):
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    memory = store.conversation_collection_id()
    _add(general, "doc.txt", "fusée lunaire", fake_embed)
    _add(memory, "Conv · #1", "fusée lunaire souvenir", fake_embed, kind="conversation")
    query = fake_embed(["fusée lunaire"])[0]
    assert {h["source"] for h in search.search(query, k=5)} == {"doc.txt"}
    both = search.search(query, k=5, libraries=True, conversations=True)
    assert {h["source"] for h in both} == {"doc.txt", "Conv · #1"}
    store.set_enabled(general, False)
    assert search.search(query, k=5) == []


def test_upsert_replaces_same_source(fake_embed):
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    _add(general, "notes.md", "première version", fake_embed)
    _add(general, "notes.md", "seconde version", fake_embed)
    entries = store.list_entries(general)
    assert len(entries) == 1
    assert search.search(fake_embed(["seconde"])[0], k=1)[0]["text"] == "seconde version"


def test_manifest_lists_sources(fake_embed):
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    _add(general, "projet/a.md", "alpha", fake_embed, kind="folder")
    _add(general, "projet/b.md", "bêta", fake_embed, kind="folder")
    summary = search.manifest()
    assert "Général (2 éléments)" in summary and "projet/a.md" in summary


def test_remove_imported_folder(fake_embed):
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    _add(general, "projet/a.md", "alpha", fake_embed, kind="folder")
    _add(general, "projet/sous/b.md", "bêta", fake_embed, kind="folder")
    _add(general, "projet_bis.md", "autre", fake_embed)       # même préfixe de nom !
    assert store.remove_prefix(general, "projet") == 2
    assert [e["source"] for e in store.list_entries(general)] == ["projet_bis.md"]


def test_collection_rules():
    store.ensure_defaults()
    cid = store.create_collection("2024")
    with pytest.raises(ValueError):
        store.create_collection("2024")
    store.rename_collection(cid, "Archives 2024")
    assert store.collection_name(cid) == "Archives 2024"
    with pytest.raises(ValueError):
        store.delete_collection(store.conversation_collection_id())
    store.delete_collection(cid)
    assert "Archives 2024" not in [c["name"] for c in store.list_collections()]


def test_chunking_prefers_natural_breaks():
    text = ("Premier paragraphe. " * 30).strip() + "\n\n" + ("Second paragraphe. " * 30)
    pieces = chunking.chunk_text(text, size=400, overlap=50)
    assert len(pieces) > 1
    assert all(p.endswith(".") for p in pieces[:-1])
    assert chunking.chunk_text("   ") == []
