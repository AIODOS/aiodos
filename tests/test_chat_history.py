"""L'historique d'une conversation, tel qu'il part vers la machine qui répond : seulement ce
qu'elle peut recevoir (mémoire, bibliothèques gardées ici, passages d'autres machines), sans
les réponses qui ne sont qu'une erreur ; la mémoire mise à jour une fois par réponse, sans
doublon ; le titre donné une seule fois. Sans réseau ni Ollama."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from aiodos import db
from aiodos.api.server import create_app
from aiodos.chat import conversations, pipeline
from aiodos.jobs.kinds import index
from aiodos.jobs.models import RUNNING
from aiodos.knowledge import store

TOKEN = {"X-Aiodos-Token": "jeton"}

MEMORY = {"type": "conversation", "label": "Vacances · #3", "collection": "Mémoire"}
WEB = {"type": "web", "label": "meteo.fr", "url": "https://meteo.fr", "title": "Météo"}
OTHERS = {"type": "remote", "label": "fusee.txt", "collection": "Cours", "peer": "PC de Léa"}


def _library(name, scope):
    collection_id = store.create_collection(name)
    store.set_scope(collection_id, scope)
    return {"type": "knowledge", "label": f"{name}.txt", "collection": name,
            "collection_id": collection_id}


@pytest.fixture
def answers():
    """Une conversation : une réponse par genre de source, dans cet ordre."""
    store.ensure_defaults()
    cited = {"mémoire": [MEMORY], "journal": [_library("Journal", "local")],
             "projets": [_library("Projets", "mine")],
             "recettes": [_library("Recettes", "friends")], "autres": [OTHERS],
             "web": [WEB], "renommée": [{"type": "knowledge", "label": "a.txt",
                                         "collection": "Ancien nom"}], "rien": []}
    conv = conversations.create()
    for name, sources in cited.items():
        conversations.add_message(conv, "user", f"Question {name}")
        conversations.add_message(conv, "assistant", f"Réponse {name}", sources=sources)
    return conv


def _received(conv, destination, peers_ok) -> list:
    history = conversations.as_history(conversations.history_rows(conv),
                                       pipeline.history_filter(destination, peers_ok))
    assert all(f"Question {name}" in json.dumps(history, ensure_ascii=False) for name in (
        "mémoire", "journal", "projets", "recettes", "autres", "web", "renommée", "rien"))
    return sorted(m["content"].split()[-1] for m in history
                  if m["role"] == "assistant" and m["content"] != conversations.WITHHELD)


def test_each_machine_receives_only_the_answers_it_may_see(answers):
    every = ["autres", "journal", "mémoire", "projets", "recettes", "renommée", "rien", "web"]
    # Cette machine : tout, sauf les passages d'autres machines si le modèle n'y a pas droit
    assert _received(answers, None, True) == every
    assert _received(answers, None, False) == [a for a in every if a != "autres"]
    # Une de mes machines : ni la mémoire, ni ce qui reste sur ce PC, ni une bibliothèque
    # qu'on ne retrouve plus (renommée depuis)
    assert _received(answers, "mine", True) == ["autres", "projets", "recettes", "rien", "web"]
    # La machine d'un ami : seulement ce que je partage avec mes amis
    assert _received(answers, "friend", False) == ["recettes", "rien", "web"]


def test_a_renamed_library_or_a_reused_name_changes_nothing():
    """Une source retient l'identifiant de sa bibliothèque : renommée, elle reste ce qu'elle
    était ; son ancien nom repris par une bibliothèque partagée n'ouvre rien."""
    store.ensure_defaults()
    secret = _library("Secret", "local")
    conv = conversations.create()
    conversations.add_message(conv, "user", "Que dit mon journal ?")
    conversations.add_message(conv, "assistant", "Le journal dit ceci.", sources=[secret])
    store.rename_collection(secret["collection_id"], "Ancien secret")
    _library("Secret", "friends")
    shared = _library("Partagée", "friends")
    store.rename_collection(shared["collection_id"], "Partagée depuis")
    conversations.add_message(conv, "user", "Et la partagée ?")
    conversations.add_message(conv, "assistant", "Elle dit cela.", sources=[shared])
    history = conversations.as_history(conversations.history_rows(conv),
                                       pipeline.history_filter("friend", False))
    assert [m["content"] for m in history if m["role"] == "assistant"] == [
        conversations.WITHHELD, "Elle dit cela."]


def test_a_withheld_answer_keeps_its_place(answers):
    history = conversations.as_history(conversations.history_rows(answers),
                                       pipeline.history_filter("friend", False))
    assert [m["role"] for m in history] == ["user", "assistant"] * 8
    assert history[1] == {"role": "assistant", "content": conversations.WITHHELD}


# ------------------------------------------------------------- erreurs
def test_an_error_is_shown_but_neither_reused_nor_remembered(fake_embed):
    store.ensure_defaults()
    conv = conversations.create()
    conversations.add_message(conv, "user", "Bonjour")
    conversations.add_message(conv, "assistant", f"{conversations.ERROR_MARK}Ollama arrêté",
                              sources=[conversations.ERROR_SOURCE])
    conversations.add_message(conv, "user", "Tu es là ?")
    conversations.add_message(conv, "assistant", "Oui, je suis là.")
    assert conversations.history(conv) == [
        {"role": "user", "content": "Bonjour"}, {"role": "user", "content": "Tu es là ?"},
        {"role": "assistant", "content": "Oui, je suis là."}]
    index.run_index_conversation({"conversation_id": conv})
    memory = " ".join(store.chunk_texts(store.entry_by_origin(
        store.conversation_collection_id(), str(conv))["id"]))
    assert "je suis là" in memory and "Ollama" not in memory


def test_an_answer_that_begins_with_a_warning_sign_is_still_an_answer(fake_embed):
    store.ensure_defaults()
    conv = conversations.create()
    conversations.add_message(conv, "user", "Que faut-il savoir ?")
    conversations.add_message(conv, "assistant",
                              f"{conversations.ERROR_MARK}Attention : ne mélange pas les deux.")
    assert conversations.history(conv)[-1]["content"].endswith("ne mélange pas les deux.")
    index.run_index_conversation({"conversation_id": conv})
    memory = " ".join(store.chunk_texts(store.entry_by_origin(
        store.conversation_collection_id(), str(conv))["id"]))
    assert "ne mélange pas" in memory


def test_errors_saved_before_their_mark_receive_it_once_at_the_upgrade(fake_embed):
    """Migrations 14 et 15 : une erreur enregistrée par une version précédente (« ⚠ … »,
    avec ou sans sources) reçoit sa marque une fois, à la mise à jour, et ses passages
    quittent la mémoire des conversations ; le reste ne change pas."""
    store.ensure_defaults()
    conv = conversations.create()
    conversations.add_message(conv, "user", "Bonjour")
    old = conversations.add_message(conv, "assistant", f"{conversations.ERROR_MARK}Ollama arrêté")
    failed = conversations.add_message(conv, "assistant", f"{conversations.ERROR_MARK}Modèle "
                                       "introuvable", sources=[WEB])
    fine = conversations.add_message(conv, "assistant", "Tout va bien.", sources=[WEB])
    odd = conversations.add_message(conv, "assistant", f"{conversations.ERROR_MARK}Coupé")
    db.execute("UPDATE messages SET sources=? WHERE id=?", ('["web"]', odd))   # pas un objet
    memory = store.conversation_collection_id()
    store.upsert_entry(memory, "Essai · #1", "conversation", "1", [
        ("Assistant : ⚠ Ollama arrêté", fake_embed(["Ollama arrêté"])[0]),
        ("Assistant : ⚠️ Attention aux champignons", fake_embed(["champignons"])[0]),
        ("Utilisateur : Bonjour", fake_embed(["Bonjour"])[0])])
    path = db.connect().execute("PRAGMA database_list").fetchone()[2]
    db.execute("PRAGMA user_version=13")
    db.close()
    db._initialized.discard(path)
    rows = {r["id"]: json.loads(r["sources"]) for r in db.query("SELECT id, sources FROM messages")}
    assert rows[old] == [conversations.ERROR_SOURCE]
    assert rows[failed] == [WEB, conversations.ERROR_SOURCE] and rows[fine] == [WEB]
    assert rows[odd] == ["web", conversations.ERROR_SOURCE]
    assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION
    assert conversations.history(conv) == [{"role": "user", "content": "Bonjour"},
                                           {"role": "assistant", "content": "Tout va bien."}]
    # Une vraie réponse qui commence par l'émoji « ⚠️ » reste en mémoire
    assert store.chunk_texts(store.entry_by_origin(memory, "1")["id"]) == [
        "Assistant : ⚠️ Attention aux champignons", "Utilisateur : Bonjour"]


# ------------------------------------------------------------- mémoire
def _memory_jobs(conv) -> list:
    return [r["status"] for r in db.query(
        "SELECT status FROM jobs WHERE kind='index_conversation' AND "
        "json_extract(params, '$.conversation_id')=? ORDER BY id", (conv,))]


def test_one_memory_update_waits_per_conversation():
    store.ensure_defaults()
    conv, other = conversations.create(), conversations.create()
    first = index.queue_conversation(conv)
    assert first is not None
    assert index.queue_conversation(conv) is None          # celle qui attend lira tout
    assert index.queue_conversation(other) is not None
    # Commencée : elle a peut-être déjà lu les messages, la suivante en a besoin
    db.execute("UPDATE jobs SET status=? WHERE id=?", (RUNNING, first))
    assert index.queue_conversation(conv) is not None
    assert len(_memory_jobs(conv)) == 2


def test_the_same_passages_twice_double_nothing():
    store.ensure_defaults()
    memory = store.conversation_collection_id()
    passages = [(1000, "Utilisateur : Bonjour", [1.0, 0.0]),
                (2000, "Assistant : Salut", [0.0, 1.0])]
    store.append_chunks(memory, "Essai · #1", "conversation", "1", passages)
    store.append_chunks(memory, "Essai · #1", "conversation", "1",
                        passages + [(3000, "Utilisateur : Merci", [1.0, 1.0])])
    entry = store.entry_by_origin(memory, "1")["id"]
    assert store.chunk_texts(entry) == ["Utilisateur : Bonjour", "Assistant : Salut",
                                        "Utilisateur : Merci"]


def test_a_new_conversation_is_titled_once():
    conv = conversations.create()
    assert conversations.auto_title(conv, "  Le   voyage  à Lisbonne ") == "Le voyage à Lisbonne"
    assert conversations.auto_title(conv, "Autre chose") is None
    assert conversations.get(conv)["title"] == "Le voyage à Lisbonne"
    conversations.rename(conv, "Lisbonne")
    assert conversations.auto_title(conv, "Encore") is None


# ------------------------------------------------------------- de bout en bout
@pytest.fixture
def api(fake_embed):
    store.ensure_defaults()
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        yield http


def _chat(http, **body) -> list:
    with http.stream("POST", "/api/chat", json=dict({"model": "local-test",
                                                     "libraries": False}, **body),
                     headers=TOKEN) as response:
        return [json.loads(line[6:]) for line in response.iter_lines()
                if line.startswith("data: ")]


def test_a_failed_answer_is_saved_once_and_left_out_afterwards(api, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("Ollama arrêté")
        yield
    monkeypatch.setattr(pipeline, "generate", down)
    events = _chat(api, text="Bonjour", conversations=True)
    conv = events[0]["conversation_id"]
    assert events[-1]["type"] == "done" and "Ollama arrêté" in str(events[-1]["error"])
    saved = conversations.get(conv)["messages"]
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[1]["content"].startswith(conversations.ERROR_MARK)
    assert _memory_jobs(conv) == []                       # une erreur n'a rien à retenir

    sent = []
    monkeypatch.setattr(pipeline, "generate",
                        lambda model, messages, *a, **k: sent.append(messages)
                        or iter([("content", "Me voici.")]))
    _chat(api, text="Tu es là ?", conversation_id=conv, conversations=True)
    said = [m["content"] for m in sent[0] if m["role"] != "system"]
    assert said == ["Bonjour", "Tu es là ?"]
    deadline = time.time() + 10
    while time.time() < deadline and len(conversations.get(conv)["messages"]) < 4:
        time.sleep(0.05)
    assert [m["content"] for m in conversations.get(conv)["messages"]][-1] == "Me voici."
    assert len(_memory_jobs(conv)) == 1


# ------------------------------------------------- noms vus dans le sommaire
def test_an_answer_that_names_what_the_summary_showed_follows_its_library(api, monkeypatch,
                                                                          fake_embed):
    """Le sommaire des bibliothèques montre au modèle les noms des fichiers ; une réponse
    qui les cite ne part pas, dans l'historique, là où ces noms ne doivent pas aller."""
    secret = store.create_collection("Banque")
    store.set_scope(secret, "local")
    store.upsert_entry(secret, "releve-compte-2026.pdf", "file", None,
                       [("solde créditeur", fake_embed(["solde créditeur"])[0])])
    replies = iter(["Tu as releve-compte-2026.pdf dans tes bibliothèques.", "Bonjour !"])
    monkeypatch.setattr(pipeline, "generate",
                        lambda *a, **k: iter([("content", next(replies))]))
    events = _chat(api, text="Qu'as-tu comme fichiers ?", libraries=True)
    conv = events[0]["conversation_id"]
    shown = next(e["sources"] for e in events if e["type"] == "sources")
    assert all(s["type"] != "summary" for s in shown)            # rien de montré à l'écran
    _chat(api, text="Salut", conversation_id=conv, libraries=True)
    saved = conversations.get(conv)["messages"]
    assert {"type": "summary", "collection_ids": [secret]} in saved[1]["sources"]
    assert not any(s["type"] == "summary" for s in saved[3]["sources"])  # ne cite rien
    # … mais elle a reçu la première dans son historique, et a pu la reprendre
    assert {"type": "inherited", "collection_ids": [secret], "memory": False,
            "remote": False} in saved[3]["sources"]
    rows = conversations.history_rows(conv)
    here = conversations.as_history(rows, pipeline.history_filter(None, True))
    mine = conversations.as_history(rows, pipeline.history_filter("mine", True))
    assert "releve-compte" in here[1]["content"] and here[3]["content"] == "Bonjour !"
    assert mine[1]["content"] == mine[3]["content"] == conversations.WITHHELD


def test_an_answer_inherits_only_what_its_model_was_shown():
    """« Comme dit : releve-compte-2026.pdf… » : une réponse qui reprend une réponse
    précédente en reçoit les marques ; une réponse retirée de l'historique ne lui a rien
    montré, elle ne lui impose rien."""
    store.ensure_defaults()
    secret = _library("Banque", "local")
    rows = [{"role": "user", "content": "Qu'as-tu ?", "sources": "[]"},
            {"role": "assistant", "content": "releve-compte-2026.pdf", "sources": json.dumps(
                [{"type": "summary", "collection_ids": [secret["collection_id"]]}])},
            {"role": "user", "content": "Et la mémoire ?", "sources": "[]"},
            {"role": "assistant", "content": "Ta conversation sur Lisbonne.",
             "sources": json.dumps([MEMORY])},
            {"role": "assistant", "content": f"{conversations.ERROR_MARK}Ollama arrêté",
             "sources": json.dumps([conversations.ERROR_SOURCE, OTHERS])}]
    assert pipeline.inherited(rows, pipeline.history_filter(None, True)) == {
        "collection_ids": [secret["collection_id"]], "memory": True, "remote": False}
    assert pipeline.inherited(rows, pipeline.history_filter("mine", True)) == {}
    keep = pipeline.history_filter("mine", True)
    marked = {"type": "inherited", "collection_ids": [], "memory": True, "remote": False}
    assert not keep([marked]) and pipeline.history_filter(None, True)([marked])
    assert conversations.cites_others([{"type": "inherited", "remote": True}])


def test_names_are_recognized_in_their_usual_forms():
    shown = {1: ["Projet/sous/releve-compte-2026.pdf"], 2: ["Divorce de Paul · #12"],
             3: ["a.c", "tarte.txt", "2025.pdf"], 4: ["Bonjour · #3"]}
    assert pipeline.summary_mentioned(shown, "Voir RELEVE-COMPTE-2026, page 2.") == [1]
    assert pipeline.summary_mentioned(shown, "Ta conversation « Divorce de Paul »…") == [2]
    assert pipeline.summary_mentioned(shown, "Un fichier a.c, ou une tarte ?") == []
    assert pipeline.summary_mentioned(shown, "Ta recette tarte.txt") == [3]
    # Ni un mot ou un nombre courants, ni un titre d'un seul mot, ni un bout de mot
    assert pipeline.summary_mentioned(shown, "Bonjour ! En 2025, Paris est la capitale.") == []
    assert pipeline.summary_mentioned(shown, "Une tartelette.txt ? Non.") == []


def test_the_current_conversation_is_not_a_name_to_watch(fake_embed):
    store.ensure_defaults()
    conv = conversations.create()
    memory = store.conversation_collection_id()
    store.upsert_entry(memory, f"Voyage à Lisbonne · #{conv}", "conversation", str(conv),
                       [("souvenir", fake_embed(["souvenir"])[0])])
    store.upsert_entry(memory, "Recette des crêpes · #999", "conversation", "999",
                       [("crêpes", fake_embed(["crêpes"])[0])])
    shown = {}
    pipeline.build_messages([], "Bonjour", libraries=False, conversations=True,
                            web_search=False, conversation_id=conv, summary=shown)
    assert shown == {memory: ["Recette des crêpes · #999"]}


# ------------------------------------------------- réponse arrêtée, puis la suite
def test_the_next_question_waits_for_the_stopped_answer(api, monkeypatch):
    """Après Stop, la réponse partielle s'enregistre hors de la boucle du serveur ; la
    question suivante, « Modifier » ou « Régénérer » attendent qu'elle soit en place."""
    from aiodos.chat import pending
    conv = conversations.create()
    done = pending.begin(conv)
    order = []

    def late_save():
        time.sleep(0.5)
        order.append(conversations.add_message(conv, "user", "Première question"))
        order.append(conversations.add_message(conv, "assistant", "Réponse arrê"))
        pending.end(conv, done)
    import threading
    threading.Thread(target=late_save).start()
    sent = []
    monkeypatch.setattr(pipeline, "generate", lambda model, messages, *a, **k:
                        sent.append(messages) or iter([("content", "Suite.")]))
    _chat(api, text="Et ensuite ?", conversation_id=conv)
    said = [m["content"] for m in sent[0] if m["role"] != "system"]
    assert said == ["Première question", "Réponse arrê", "Et ensuite ?"]
    assert [m["content"] for m in conversations.get(conv)["messages"]] == [
        "Première question", "Réponse arrê", "Et ensuite ?", "Suite."]
    assert pending.wait(conv, 0)                   # plus rien en attente
