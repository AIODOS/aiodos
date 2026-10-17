"""Bibliothèques consultables, côté propriétaire, sans réseau ni Ollama : ce qui est ouvert
et à qui, parties fermées, ce qui sort d'un passage, limites, journal, action scellée."""
import json

import pytest
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.jobs.kinds import reindex
from aiodos.knowledge import chunking, store, visibility
from aiodos.network import consult, envelope, p2p_app, peers, sharing
from aiodos.network.client import PeerError
from aiodos.network.envelope import ReplayGuard
from aiodos.services import ollama

MINE_PC, FRIEND_PC, STRANGER = "portable", "pc-de-lea", "intrus"
KEY = b"k" * 32
QUESTION = "fusée lunaire"


class FakeManager:
    node_id = "moi"

    def __init__(self):
        self.guard = ReplayGuard()

    def name(self):
        return "PC de Marc"

    def changed(self):
        pass


def _add(collection_id, source, text, embed, kind="folder", origin=None):
    pieces = chunking.chunk_text(text)
    return store.upsert_entry(collection_id, source, kind, origin or f"C:/Users/Marc/{source}",
                              list(zip(pieces, embed(pieces))))


@pytest.fixture
def library(fake_embed, monkeypatch):
    """« Cours » (un dossier dont un sous-dossier secret, un document à part, une page
    web), « Général », une bibliothèque gardée sur ce PC et la mémoire des conversations,
    qui parlent toutes de la fusée lunaire. Une de mes machines et un ami appairés."""
    store.ensure_defaults()
    reindex.indexed_model()          # vecteurs du modèle actuel : rien à réindexer au démarrage
    settings.set_value("rag_min_score", 0.2)            # échelle de l'embedding factice
    courses = store.create_collection("Cours")
    _add(courses, "cours/maths/fusee.txt", "La fusée lunaire décolle à l'aube.", fake_embed)
    _add(courses, "cours/secret/journal.txt", "Mon journal intime : la fusée lunaire.", fake_embed)
    _add(courses, "notes.txt", "Notes sur la fusée lunaire et son carburant.", fake_embed,
         kind="file")
    _add(courses, "https://exemple.org/fusee?jeton=abc123#haut", "Page sur la fusée lunaire.",
         fake_embed, kind="url", origin="https://exemple.org/fusee?jeton=abc123#haut")
    general = next(c["id"] for c in store.list_collections()
                   if c["name"] == store.DEFAULT_COLLECTION)
    _add(general, "general.txt", "La fusée lunaire du Général.", fake_embed, kind="file")
    private = store.create_collection("Privé")
    _add(private, "prive.txt", "La fusée lunaire, en privé.", fake_embed, kind="file")
    store.set_scope(private, "local")
    memory = store.conversation_collection_id()
    store.append_chunks(memory, "Conv · #1", "conversation", "1",
                        [(1000, "Utilisateur : la fusée lunaire", fake_embed(["la fusée lunaire"])[0])])
    peers.save(MINE_PC, "Portable", KEY, "127.0.0.1", 1)
    peers.save(FRIEND_PC, "PC de Léa", KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    limiter = consult.Limiter()
    monkeypatch.setattr(consult, "LIMITER", limiter)
    return {"courses": courses, "general": general, "private": private, "memory": memory,
            "manager": FakeManager(), "limiter": limiter}


def _search(lib, node=FRIEND_PC, question=QUESTION, k=8):
    return consult.search(lib["manager"], node, {"question": question, "k": k})


def _documents(reply) -> set:
    return {p["document"] for p in reply["passages"]}


# ----------------------------------------------------------- ce qui est ouvert
def test_nothing_is_consultable_by_default_and_the_question_is_not_even_read(library,
                                                                            monkeypatch):
    assert {c["open_to"] for c in store.list_collections()} == {"none"}
    monkeypatch.setattr(ollama, "embed", lambda *a, **k: pytest.fail("question lue"))
    assert _search(library) == {"passages": [], "open": 0}
    assert _search(library, MINE_PC) == {"passages": [], "open": 0}
    assert consult.open_count(FRIEND_PC) == consult.open_count(MINE_PC) == 0


def test_each_library_says_who_may_search_it(library):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    visibility.set_open_to(library["general"], visibility.MINE)
    assert consult.open_count(FRIEND_PC) == 1 and consult.open_count(MINE_PC) == 2
    seen_by_friend = _search(library)
    assert {p["library"] for p in seen_by_friend["passages"]} == {"Cours"}
    assert seen_by_friend["open"] == 1
    seen_by_mine = _search(library, MINE_PC)
    assert {p["library"] for p in seen_by_mine["passages"]} == {"Cours", "Général"}
    visibility.set_open_to(library["courses"], visibility.NOBODY)
    assert _search(library)["passages"] == []


def test_everyone_waits_for_phase_3_and_odd_levels_are_refused(library):
    with pytest.raises(ValueError, match="phase 3"):
        visibility.set_open_to(library["courses"], visibility.EVERYONE)
    with pytest.raises(ValueError):
        visibility.set_open_to(library["courses"], "partout")
    with pytest.raises(ValueError):
        visibility.set_open_to(library["memory"], visibility.MINE)


def test_a_library_kept_on_this_pc_and_the_memory_never_leave(library):
    with pytest.raises(ValueError, match="gardée sur ce PC"):
        visibility.set_open_to(library["private"], visibility.FRIENDS)
    # Même si la base disait autre chose (ancienne version, base modifiée à la main)
    db.execute("UPDATE collections SET open_to='friends' WHERE id IN (?, ?)",
               (library["private"], library["memory"]))
    reply = _search(library, MINE_PC)
    assert reply == {"passages": [], "open": 0}
    assert {c["name"]: c["open_to"] for c in store.list_collections()}["Privé"] == "none"


def test_the_padlock_closes_consultation_and_removing_it_does_not_reopen(library):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    store.set_scope(library["courses"], "local")
    assert _search(library)["open"] == 0
    store.set_scope(library["courses"], "friends")                   # cadenas retiré
    assert _search(library)["open"] == 0                             # toujours fermée
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    store.set_local_only(library["courses"], True)
    assert _search(library)["open"] == 0


# ---------------------------------------------------------------- parties fermées
def test_closed_folders_and_documents_never_leave(library, fake_embed):
    courses = library["courses"]
    visibility.set_open_to(courses, visibility.FRIENDS)
    assert "journal.txt" in _documents(_search(library))
    visibility.close(courses, "cours/secret", folder=True)
    visibility.close(courses, "notes.txt", folder=False)
    assert _documents(_search(library)) == {"fusee.txt", "https://exemple.org/fusee"}
    # Réindexé, ou ajouté plus tard dans le dossier fermé : toujours fermé
    _add(courses, "cours/secret/journal.txt", "Journal réécrit : la fusée lunaire.", fake_embed)
    _add(courses, "cours/secret/nouveau.txt", "Nouveau secret : la fusée lunaire.", fake_embed)
    assert _documents(_search(library)) == {"fusee.txt", "https://exemple.org/fusee"}
    assert visibility.closed_parts()[courses] == [
        {"path": "cours/secret", "folder": True}, {"path": "notes.txt", "folder": False}]
    visibility.reopen(courses, "cours/secret", folder=True)
    assert {"journal.txt", "nouveau.txt"} <= _documents(_search(library))


def test_a_closed_part_stays_closed_even_if_the_first_filter_failed(library, monkeypatch):
    """Second contrôle : chaque passage trouvé est revérifié avant de sortir."""
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    visibility.close(library["courses"], "cours/secret", folder=True)
    monkeypatch.setattr(visibility, "closed_chunk_ids", lambda collection_id: [])
    assert "journal.txt" not in _documents(_search(library))


def test_a_folder_closes_what_is_below_it_and_nothing_else(library, fake_embed):
    courses = library["courses"]
    visibility.set_open_to(courses, visibility.FRIENDS)
    _add(courses, "cours/secret_bis/autre.txt", "Autre : la fusée lunaire.", fake_embed)
    _add(courses, "cours/100%_sûr/vrai.txt", "Vrai : la fusée lunaire.", fake_embed)
    visibility.close(courses, "cours/secret", folder=True)         # pas « secret_bis »
    visibility.close(courses, "cours/1", folder=True)              # pas « 100%_sûr »
    visibility.close(courses, "cours/100%_sûr/", folder=True)      # « / » final ignoré
    found = _documents(_search(library))
    assert "autre.txt" in found and "journal.txt" not in found and "vrai.txt" not in found
    with pytest.raises(ValueError):
        visibility.close(courses, "  ", folder=True)


# --------------------------------------------------------------- ce qui sort
def test_only_text_document_name_library_and_score_leave(library):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    reply = _search(library)
    assert reply["passages"] and all(set(p) == {"text", "document", "library", "score"}
                                     for p in reply["passages"])
    shown = json.dumps(reply, ensure_ascii=False)
    assert "Users" not in shown and "cours/" not in shown          # ni emplacement, ni dossiers
    assert "jeton" not in shown and "#haut" not in shown           # ni paramètres d'adresse
    assert _documents(reply) == {"fusee.txt", "journal.txt", "notes.txt",
                                 "https://exemple.org/fusee"}


def test_names_shown_to_the_other_machine():
    assert consult.shown_name("cours/maths/fusee.txt", "folder") == "fusee.txt"
    assert consult.shown_name("rapport.pdf", "file") == "rapport.pdf"
    assert consult.shown_name("https://moi:secret@exemple.org:8080/a/b?x=1#y", "url") \
        == "https://exemple.org:8080/a/b"


def test_passages_are_bounded_and_must_be_relevant(library, fake_embed):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    for i in range(20):
        _add(library["courses"], f"cours/serie/{i}.txt", f"Copie {i} : la fusée lunaire.",
             fake_embed)
    assert len(_search(library, k=100)["passages"]) == consult.MAX_PASSAGES
    assert len(_search(library, k=2)["passages"]) == 2
    settings.set_value("rag_min_score", 0.99)                       # rien d'assez proche
    assert _search(library)["passages"] == []


def test_questions_are_checked(library, monkeypatch):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    for bad in ("", "   ", None, 12, "x" * (consult.MAX_QUESTION + 1)):
        with pytest.raises(PeerError):
            _search(library, question=bad)

    def down(texts, model=None):
        raise ConnectionError("Ollama arrêté")
    monkeypatch.setattr(ollama, "embed", down)
    with pytest.raises(PeerError, match="Ollama"):
        _search(library)


# ----------------------------------------------------------------- limites
def test_searches_per_minute_are_limited_per_machine(library, monkeypatch):
    now = [1000.0]
    limiter = consult.Limiter(limit=2, window=60, clock=lambda: now[0])
    monkeypatch.setattr(consult, "LIMITER", limiter)
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    _search(library), _search(library)
    for _ in range(3):
        with pytest.raises(PeerError, match="2 par minute"):
            _search(library)
    assert _search(library, MINE_PC)["passages"]                     # l'autre machine : servie
    now[0] += 60
    assert _search(library)["passages"]
    refusals = [e for e in consult.entries(FRIEND_PC) if e["refused"]]
    assert len(refusals) == 1                                        # un refus noté, pas trois


def test_a_machine_set_aside_gets_nothing(library, monkeypatch):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    peers.set_aside(FRIEND_PC, "un résultat ne correspond pas")
    assert consult.open_count(FRIEND_PC) == 0
    with pytest.raises(PeerError, match="ne te laisse plus chercher"):
        _search(library)
    assert consult.entries(FRIEND_PC)[0]["refused"] == "machine mise de côté"
    # Si elle insiste, la limite borne aussi ce qui est noté
    monkeypatch.setattr(consult, "LIMITER", consult.Limiter(limit=2, clock=lambda: 1000.0))
    for _ in range(6):
        with pytest.raises(PeerError):
            _search(library)
    assert len(consult.entries(FRIEND_PC)) == 1 + 2 + 1


# ------------------------------------------------------------------ journal
def test_each_search_is_logged_without_the_question(library):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    visibility.close(library["courses"], "cours/secret", folder=True)
    reply = _search(library, question="la fusée lunaire de mon voisin")
    entry = consult.entries(FRIEND_PC)[0]
    assert entry["passages"] == len(reply["passages"]) == 3 and entry["refused"] is None
    assert {d["document"] for d in entry["documents"]} == {
        "cours/maths/fusee.txt", "notes.txt", "https://exemple.org/fusee?jeton=abc123#haut"}
    assert all(d["library"] == "Cours" for d in entry["documents"])
    logged = json.dumps([dict(r) for r in db.query("SELECT * FROM consult_log")])
    assert "voisin" not in logged                                    # la question n'est pas gardée
    summary = consult.summary()[FRIEND_PC]
    assert summary["count"] == 1 and summary["passages"] == 3 and summary["refused"] == 0
    assert MINE_PC not in consult.summary()


# ------------------------------------------------------- par le serveur réseau
@pytest.fixture
def server(library):
    return TestClient(p2p_app.create_p2p_app(library["manager"]))


def _sealed(http, sender, payload, key=KEY):
    env = envelope.wrap(key, "library/search", sender, "moi", payload)
    return env, http.post("/p2p/v1/library/search", json=env)


def test_the_search_is_a_sealed_action_for_paired_machines_only(library, server):
    visibility.set_open_to(library["courses"], visibility.FRIENDS)
    env, response = _sealed(server, FRIEND_PC, {"question": QUESTION, "k": 3})
    assert response.status_code == 200
    reply = envelope.unwrap_reply(KEY, "library/search", "moi", FRIEND_PC, env["ts"],
                                  response.json())
    assert len(reply["passages"]) == 3 and reply["open"] == 1
    assert _sealed(server, STRANGER, {"question": QUESTION}, key=b"x" * 32)[1].status_code == 403
    refused = _sealed(server, FRIEND_PC, {"question": ""})[1]
    assert refused.status_code == 400 and "vide" in refused.json()["detail"]
    profile = p2p_app._profile(library["manager"], FRIEND_PC, {})
    assert profile["consult"] == {"open": 1}


# ----------------------------------------------------------- vue par l'interface
TOKEN = {"X-Aiodos-Token": "jeton"}


def test_the_interface_opens_closes_and_shows_the_journal(library):
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as http:
        courses = library["courses"]
        assert http.patch(f"/api/collections/{courses}", json={"open_to": "friends"},
                          headers=TOKEN).status_code == 200
        assert http.patch(f"/api/collections/{library['private']}", json={"open_to": "mine"},
                          headers=TOKEN).status_code == 400
        assert http.patch(f"/api/collections/{courses}", json={"open_to": "everyone"},
                          headers=TOKEN).status_code == 400
        assert http.post(f"/api/collections/{courses}/closed",
                         json={"path": "cours/secret", "folder": True},
                         headers=TOKEN).status_code == 200
        shown = {c["name"]: c for c in http.get("/api/collections", headers=TOKEN).json()
                 ["collections"]}
        assert shown["Cours"]["open_to"] == "friends"
        assert shown["Cours"]["closed"] == [{"path": "cours/secret", "folder": True}]
        assert shown["Général"]["open_to"] == "none" and shown["Général"]["closed"] == []

        _search(library)
        snap = http.get("/api/network", headers=TOKEN).json()
        lea = next(p for p in snap["peers"] if p["node_id"] == FRIEND_PC)
        assert lea["consulted"]["count"] == 1 and lea["consulted"]["passages"] == 3
        entries = http.get(f"/api/network/peers/{FRIEND_PC}/consulted",
                           headers=TOKEN).json()["entries"]
        assert entries[0]["passages"] == 3
        assert http.post(f"/api/collections/{courses}/closed",
                         json={"path": "cours/secret", "folder": True, "closed": False},
                         headers=TOKEN).status_code == 200
        shown = {c["name"]: c for c in http.get("/api/collections", headers=TOKEN).json()
                 ["collections"]}
        assert shown["Cours"]["closed"] == []
