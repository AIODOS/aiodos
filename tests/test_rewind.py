"""« Régénérer » et « Modifier » : la conversation reprend à un message de l'utilisateur, qui
en est retiré avec tout ce qui le suit, mémoire comprise ; seul un message de
l'utilisateur de cette conversation peut servir de point de reprise. Sans Ollama."""
import json

import pytest
from fastapi.testclient import TestClient

from aiodos.api.server import create_app
from aiodos.chat import conversations, pipeline
from aiodos.jobs.kinds import index
from aiodos.knowledge import store

TOKEN = {"X-Aiodos-Token": "jeton"}


def _memory(conv) -> list:
    entry = store.entry_by_origin(store.conversation_collection_id(), str(conv))
    return store.chunk_texts(entry["id"]) if entry else []


@pytest.fixture
def talk(fake_embed):
    """Deux échanges, déjà en mémoire."""
    store.ensure_defaults()
    conv = conversations.create()
    ids = [conversations.add_message(conv, "user", "Quelle est la capitale du Portugal ?"),
           conversations.add_message(conv, "assistant", "Lisbonne."),
           conversations.add_message(conv, "user", "Et sa population ?"),
           conversations.add_message(conv, "assistant", "Environ 550 000 habitants.")]
    index.run_index_conversation({"conversation_id": conv})
    return conv, ids


def test_the_conversation_resumes_at_a_message_memory_included(talk):
    conv, ids = talk
    assert conversations.rewind(conv, ids[2]) == "Et sa population ?"
    assert [m["content"] for m in conversations.get(conv)["messages"]] == [
        "Quelle est la capitale du Portugal ?", "Lisbonne."]
    memory = " ".join(_memory(conv))
    assert "Lisbonne" in memory and "population" not in memory and "550" not in memory
    # La suite s'indexe comme d'habitude, même si SQLite reprend les numéros retirés
    conversations.add_message(conv, "user", "Et sa superficie ?")
    conversations.add_message(conv, "assistant", "Cent kilomètres carrés.")
    index.run_index_conversation({"conversation_id": conv})
    assert "superficie" in " ".join(_memory(conv))


def test_resuming_at_the_first_message_empties_the_memory(talk):
    conv, ids = talk
    conversations.rewind(conv, ids[0])
    assert conversations.get(conv)["messages"] == []
    assert store.entry_by_origin(store.conversation_collection_id(), str(conv)) is None


def test_only_a_message_of_the_user_in_this_conversation(talk):
    conv, ids = talk
    other = conversations.create()
    assert conversations.rewind(conv, ids[1]) is None              # une réponse
    assert conversations.rewind(other, ids[2]) is None             # une autre conversation
    assert conversations.rewind(conv, 99999) is None
    assert len(conversations.get(conv)["messages"]) == 4


def test_regenerate_through_the_api(talk, monkeypatch):
    conv, ids = talk
    sent = []
    monkeypatch.setattr(pipeline, "generate",
                        lambda model, messages, *a, **k: sent.append(messages)
                        or iter([("content", "Plutôt 545 000.")]))
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        assert http.post(f"/api/conversations/{conv}/rewind", json={"message_id": ids[1]},
                         headers=TOKEN).status_code == 404
        back = http.post(f"/api/conversations/{conv}/rewind", json={"message_id": ids[2]},
                         headers=TOKEN).json()
        assert back == {"text": "Et sa population ?"}
        with http.stream("POST", "/api/chat", headers=TOKEN, json={
                "text": back["text"], "model": "local-test", "conversation_id": conv,
                "libraries": False, "conversations": False}) as response:
            events = [json.loads(line[6:]) for line in response.iter_lines()
                      if line.startswith("data: ")]
    assert events[-1]["type"] == "done"
    # Le modèle ne revoit pas l'ancienne réponse
    said = [m["content"] for m in sent[0] if m["role"] != "system"]
    assert said == ["Quelle est la capitale du Portugal ?", "Lisbonne.", "Et sa population ?"]
    assert [m["content"] for m in conversations.get(conv)["messages"]][-2:] == [
        "Et sa population ?", "Plutôt 545 000."]


# ------------------------------------------------- mémoire : numéros repris
def test_a_number_taken_again_is_remembered_with_its_new_text(talk):
    """Une mise à jour de la mémoire partie avant « Régénérer » finit après : elle remet
    les passages de l'ancienne réponse, sous des numéros que SQLite redonne aux nouveaux
    messages. La mise à jour suivante les remplace."""
    conv, ids = talk
    memory = store.conversation_collection_id()
    conversations.rewind(conv, ids[2])
    store.append_chunks(memory, "Essai", "conversation", str(conv),
                        [(ids[2] * 1000, "Utilisateur : Et sa population ?", [1.0] * 64),
                         (ids[3] * 1000, "Assistant : Environ 550 000 habitants.", [1.0] * 64)])
    again = [conversations.add_message(conv, "user", "Et sa superficie ?"),
             conversations.add_message(conv, "assistant", "Cent kilomètres carrés.")]
    assert again == ids[2:]                         # SQLite reprend les numéros
    index.run_index_conversation({"conversation_id": conv})
    text = " ".join(_memory(conv))
    assert "superficie" in text and "Cent kilomètres" in text
    assert "population" not in text and "550" not in text


def test_removed_messages_leave_the_memory_and_nothing_is_computed_twice(talk, monkeypatch):
    conv, ids = talk
    computed = []
    real = index.pacing.embed
    monkeypatch.setattr(index.pacing, "embed", lambda texts, **k: computed.extend(texts)
                        or real(texts, **k))
    assert index.run_index_conversation({"conversation_id": conv}) == {"chunks": 0}
    assert computed == []                           # rien de changé, rien recalculé
    conversations.remove_messages(conv, ids[2:])     # « Répondre en texte », par exemple
    assert index.run_index_conversation({"conversation_id": conv}) == {"chunks": 0}
    assert "population" not in " ".join(_memory(conv)) and "Lisbonne" in " ".join(_memory(conv))


def test_a_long_message_changed_only_at_the_end_is_indexed_again(talk):
    conv, ids = talk
    start = " ".join(f"Phrase numéro {n} du long texte collé." for n in range(60))
    first = conversations.add_message(conv, "user", start + " Fin : version un.")
    index.run_index_conversation({"conversation_id": conv})
    assert "version un" in " ".join(_memory(conv))
    conversations.rewind(conv, first)
    assert conversations.add_message(conv, "user", start + " Fin : version deux.") == first
    index.run_index_conversation({"conversation_id": conv})
    text = " ".join(_memory(conv))
    assert "version deux" in text and "version un" not in text


def test_what_is_removed_during_the_computation_never_enters_the_memory(talk, monkeypatch):
    """« Régénérer » pendant que la mémoire se calcule : rien de l'échange retiré n'y entre,
    même si personne ne renvoie de question ensuite ; une conversation supprimée pendant le
    calcul ne laisse pas de mémoire orpheline."""
    conv, ids = talk
    asked = conversations.add_message(conv, "user", "Et son maire ?")
    conversations.add_message(conv, "assistant", "Carlos Moedas.")
    real = index.pacing.embed

    def rewound_meanwhile(texts, **kwargs):
        conversations.rewind(conv, asked)
        return real(texts, **kwargs)
    monkeypatch.setattr(index.pacing, "embed", rewound_meanwhile)
    index.run_index_conversation({"conversation_id": conv})
    text = " ".join(_memory(conv))
    assert "Lisbonne" in text and "maire" not in text and "Moedas" not in text

    conversations.add_message(conv, "user", "Et son fleuve ?")

    def deleted_meanwhile(texts, **kwargs):
        conversations.delete(conv)
        return real(texts, **kwargs)
    monkeypatch.setattr(index.pacing, "embed", deleted_meanwhile)
    index.run_index_conversation({"conversation_id": conv})
    assert store.entry_by_origin(store.conversation_collection_id(), str(conv)) is None


# ------------------------------------------------- remplacer, une fois accepté
def _stream(http, **body):
    with http.stream("POST", "/api/chat", headers=TOKEN,
                     json=dict({"libraries": False, "conversations": False}, **body)) as reply:
        return reply.status_code, [json.loads(line[6:]) for line in reply.iter_lines()
                                   if line.startswith("data: ")]


def test_the_old_exchange_goes_only_once_the_new_question_is_accepted(talk, monkeypatch):
    conv, ids = talk
    monkeypatch.setattr(pipeline, "generate", lambda *a, **k: iter([("content", "545 000.")]))
    before = [m["content"] for m in conversations.get(conv)["messages"]]
    replace = {"conversation_id": conv, "replace_from": ids[2],
               "replace_text": "Et sa population ?"}
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        # Refusée avant l'envoi (aucune machine n'a ce modèle) : rien n'est retiré
        assert _stream(http, text="Et sa population ?", model="auto:absent:7b", **replace)[0] == 400
        # Pas le message attendu (autre fenêtre, numéro repris) : rien n'est retiré
        assert _stream(http, text="Et sa population ?", model="local-test",
                       **dict(replace, replace_text="Autre chose"))[0] == 412
        assert [m["content"] for m in conversations.get(conv)["messages"]] == before
        status, events = _stream(http, text="Et sa population exacte ?", model="local-test",
                                 **replace)
    assert status == 200 and events[-1]["type"] == "done"
    assert [m["content"] for m in conversations.get(conv)["messages"]] == [
        "Quelle est la capitale du Portugal ?", "Lisbonne.", "Et sa population exacte ?",
        "545 000."]


def test_replacing_the_first_message_gives_its_title_back(monkeypatch):
    monkeypatch.setattr(pipeline, "generate", lambda *a, **k: iter([("content", "Madrid.")]))
    conv = conversations.create()
    first = conversations.add_message(conv, "user", "Quelle est la capitale du Portugal ?")
    conversations.add_message(conv, "assistant", "Lisbonne.")
    conversations.auto_title(conv, "Quelle est la capitale du Portugal ?")
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        status, events = _stream(http, text="Quelle est la capitale de l'Espagne ?",
                                 model="local-test", conversation_id=conv,
                                 replace_from=first,
                                 replace_text="Quelle est la capitale du Portugal ?")
    assert status == 200 and events[0]["title"] == "Quelle est la capitale de l'Espagne ?"
    assert conversations.get(conv)["title"] == "Quelle est la capitale de l'Espagne ?"
    # Un titre choisi par l'utilisateur reste
    conversations.rename(conv, "Géographie")
    first = conversations.get(conv)["messages"][0]["id"]
    assert conversations.rewind(conv, first) is not None
    assert conversations.get(conv)["title"] == "Géographie"


def test_only_the_last_exchange_can_be_replaced(talk, monkeypatch):
    """Une ancienne notice, une autre fenêtre : remplacer une question suivie d'une autre
    effacerait ce qui est venu après. Refusé (412), rien n'est retiré."""
    conv, ids = talk
    monkeypatch.setattr(pipeline, "generate", lambda *a, **k: iter([("content", "Non.")]))
    before = [m["content"] for m in conversations.get(conv)["messages"]]
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        status, _ = _stream(http, text="Quelle est la capitale de la France ?",
                            model="local-test", conversation_id=conv, replace_from=ids[0],
                            replace_text="Quelle est la capitale du Portugal ?")
        refused = http.post(f"/api/conversations/{conv}/rewind", headers=TOKEN, json={
            "message_id": ids[0], "expect": "Quelle est la capitale du Portugal ?"})
    assert status == 412 and refused.status_code == 412
    assert [m["content"] for m in conversations.get(conv)["messages"]] == before


def test_a_request_abandoned_before_it_is_accepted_changes_nothing(talk, monkeypatch):
    """Stop pendant que le nœud prépare la réponse (la question n'est pas encore
    enregistrée) : rien n'est retiré, rien n'est ajouté."""
    from starlette.requests import Request

    async def gone(self):
        return True
    conv, ids = talk
    monkeypatch.setattr(pipeline, "generate", lambda *a, **k: iter([("content", "?")]))
    monkeypatch.setattr(Request, "is_disconnected", gone)
    before = [m["content"] for m in conversations.get(conv)["messages"]]
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        status, _ = _stream(http, text="Et sa population exacte ?", model="local-test",
                            conversation_id=conv, replace_from=ids[2],
                            replace_text="Et sa population ?")
    assert status == 400
    assert [m["content"] for m in conversations.get(conv)["messages"]] == before


def test_reading_a_conversation_waits_for_the_question_being_saved(talk):
    """L'écran qui se resynchronise relit la conversation telle que le nœud la garde : pas
    avant que la question en cours soit enregistrée (ou refusée)."""
    import threading
    import time
    from aiodos.chat import pending
    conv, ids = talk
    marker = pending.begin(conv)

    def saved_later():
        time.sleep(0.6)
        conversations.add_message(conv, "user", "Et son fleuve ?")
        pending.end(conv, marker)
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        began = time.monotonic()                      # une lecture ordinaire n'attend pas
        http.get(f"/api/conversations/{conv}", headers=TOKEN)
        assert time.monotonic() - began < 0.4
        threading.Thread(target=saved_later).start()
        began = time.monotonic()
        read = http.get(f"/api/conversations/{conv}?settled=1", headers=TOKEN).json()
    assert time.monotonic() - began >= 0.5
    assert read["messages"][-1]["content"] == "Et son fleuve ?"


def test_a_first_question_abandoned_leaves_no_empty_conversation(monkeypatch):
    from starlette.requests import Request

    async def gone(self):
        return True
    monkeypatch.setattr(Request, "is_disconnected", gone)
    monkeypatch.setattr(pipeline, "generate", lambda *a, **k: iter([("content", "?")]))
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        status, _ = _stream(http, text="Bonjour", model="local-test")
    assert status == 400 and conversations.list_tree()["conversations"] == []
