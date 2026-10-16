"""Bibliothèques : stockage, recherche, filtres, sommaire, suppression."""
import pytest

from aiodos.knowledge import chunking, search, store


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


def test_search_during_reindexing(fake_embed):
    """Vecteurs d'un ancien modèle (majoritaires), passages déjà recalculés et
    passages sans vecteur se côtoient : seuls les recalculés sont trouvables."""
    store.ensure_defaults()
    general = store.list_collections()[0]["id"]
    old_model = lambda texts: [v + [1.0] * 16 for v in fake_embed(texts)]     # 80 dimensions
    for i in range(3):
        _add(general, f"ancien{i}.txt", f"fusée lunaire numéro {i}", old_model)
    _add(general, "recalculé.txt", "fusée lunaire recalculée", fake_embed)
    query = fake_embed(["fusée lunaire"])[0]
    assert [h["source"] for h in search.search(query, k=5)] == ["recalculé.txt"]
    store.retire_vectors()                                   # nouveau changement de modèle
    assert search.search(query, k=5) == []
    assert [len(store.chunk_texts(e["id"])) for e in store.list_entries(general)] == [1] * 4


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


def test_current_conversation_is_excluded_from_memory(fake_embed):
    store.ensure_defaults()
    memory = store.conversation_collection_id()
    for conv_id in (7, 8):
        text = f"Utilisateur : parlons de la fusée lunaire numéro {conv_id}"
        store.append_chunks(memory, f"Conv · #{conv_id}", "conversation", str(conv_id),
                            [(conv_id * 1000, text, fake_embed([text])[0])])
    query = fake_embed(["fusée lunaire"])[0]
    every = search.search(query, k=5, conversations=True)
    assert {h["source"] for h in every} == {"Conv · #7", "Conv · #8"}
    others = search.search(query, k=5, conversations=True, exclude_conversation=7)
    assert {h["source"] for h in others} == {"Conv · #8"}


def test_relevance_filter_drops_background_noise():
    from aiodos import settings
    from aiodos.chat.pipeline import select_relevant
    hit = lambda score, name: {"score": score, "source": name}
    threshold = settings.get("rag_min_score")
    assert threshold == settings.RAG_THRESHOLDS["embeddinggemma"] == 0.40
    # Scores réellement mesurés avec embeddinggemma (docs/embeddings.md)
    budget = [hit(0.524, "budget.txt"), hit(0.298, "impots.pdf"), hit(0.294, "devis.eml")]
    assert [h["source"] for h in select_relevant(budget, threshold)] == ["budget.txt"]
    # « Écris une fonction JavaScript… » : rien de pertinent dans la base
    unrelated = [hit(0.360, "conv python"), hit(0.292, "excel.md"), hit(0.267, "conv nano")]
    assert select_relevant(unrelated, threshold) == []
    # La même question déjà posée (mémoire, 0,888) n'éclipse pas la vraie source
    duplicate = [hit(0.888, "mémoire : question"), hit(0.564, "mémoire : réponse"),
                 hit(0.524, "budget.txt"), hit(0.298, "impots.pdf")]
    assert [h["source"] for h in select_relevant(duplicate, threshold)] == [
        "mémoire : question", "mémoire : réponse", "budget.txt"]


def test_chunking_prefers_natural_breaks():
    text = ("Premier paragraphe. " * 30).strip() + "\n\n" + ("Second paragraphe. " * 30)
    pieces = chunking.chunk_text(text, size=400, overlap=50)
    assert len(pieces) > 1
    assert all(p.endswith(".") for p in pieces[:-1])
    assert chunking.chunk_text("   ") == []
