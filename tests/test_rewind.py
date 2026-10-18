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
