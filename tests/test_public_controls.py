"""Les contrôles au hasard branchés sur mes conversations (network/public_controls.py,
network/spot_check.py, network/reputation.py ; décisions de l'auteur du 27/10/2026, essai
réel du 28/10/2026).

Une graine tirée au hasard dans chaque demande publique, gardée pour le contrôle ; la réponse
complète et affichée, sa vitesse notée sous la clé du prêteur, puis le contrôle lancé en
arrière-plan (la réponse n'attend pas) chez mes machines d'abord, puis chez une autre machine
du réseau public ; jamais chez un inconnu si la demande porte mes documents. Une machine
prise en faute est mise de côté (une ligne au journal, rien dans la conversation) et n'est
plus choisie ; la vitesse mesurée ici passe avant celle qu'une machine annonce.

Le compagnon simulé et l'annuaire public sont ceux de test_public_chat.py ; mes machines de
contrôle sont simulées (remote_models.chat_stream)."""
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.knowledge import store
from aiodos.network import (peers, placement, public_chat, public_controls, public_lending,
                            remote_models, reputation, spot_check)
from aiodos.node import speed
from tests.test_public_chat import (AUTH, BIG, OTHER, PC, SMALL, THIRD, TOKEN, Executor,
                                    _library, ask, at, directory, local_models, node, routes)
from tests.test_public_chat import companion, opened  # noqa: F401 (fixtures)
from tests.test_public_no_model import nothing_here
from tests.test_spot_check import HONEST_A, HONEST_B, QUESTION, UNRELATED

BEST = placement.PUBLIC_BEST
AUTO = placement.auto_id(SMALL)
FOURTH, FIFTH, SIXTH, SEVENTH = "d4" * 32, "e5" * 32, "f6" * 32, "a7" * 32


def my_machine(model=BIG, node_id="fixe", trust="mine"):
    """Une de mes machines (ou d'un ami) en ligne, qui prête `model` : son identifiant."""
    peers.save(node_id, f"PC {node_id}", node_id.encode().ljust(32, b"k")[:32], "127.0.0.1", 1,
               trust=trust)
    peers.set_profile(node_id, {"lends_models": True, "ollama": {"models": [
        {"name": model, "caps": ["completion"], "fits_vram": True, "loaded": True}]}})
    return remote_models.model_id(node_id, model)


class Mine:
    """Mes machines de contrôle (remote_models.chat_stream) : la même réponse, rendue une fois
    `release` levé s'il est donné ; les demandes reçues."""

    def __init__(self, answer, release=None):
        self.answer, self.release, self.asked = answer, release, []

    def __call__(self, model, messages, think=True, stop=None, options=None, stats=False):
        self.asked.append({"model": model, "messages": messages, "think": think,
                           "options": options})
        if self.release is not None:
            self.release.wait(10)
        yield "content", self.answer


@pytest.fixture
def checks(monkeypatch):
    """Les contrôles lancés (spot_check.run_check) : leur fil, pour les attendre."""
    threads = []
    real = spot_check.run_check

    def recorded(*args, **kw):
        thread = real(*args, **kw)
        threads.append(thread)
        return thread
    monkeypatch.setattr(spot_check, "run_check", recorded)
    yield threads
    for thread in threads:
        if thread is not None:
            thread.join(10)


# ------------------------------------------------------------------ la graine
def test_every_public_request_carries_a_random_seed_and_the_lender_limit(
        monkeypatch, companion, opened):
    nothing_here(monkeypatch)
    directory(node(PC, BIG))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        ask(ui, "Bonjour", model=BEST)
        ask(ui, "Encore", model=BEST)
    first, second = (r["body"]["options"] for r in companion.requests)
    assert first.keys() == second.keys() == {"seed", "max_tokens"}
    assert first["max_tokens"] == public_lending.MAX_TOKENS
    assert all(isinstance(o["seed"], int) and 0 <= o["seed"] <= public_controls.SEED_MAX
               for o in (first, second))
    assert first["seed"] != second["seed"]                  # tirée à chaque demande
    assert reputation.get(PC)["served"] == 2                # notée sous la clé du prêteur


# ---------------------------------------------------- le contrôle, en arrière-plan
def test_a_complete_answer_is_checked_in_the_background_by_my_machine(
        monkeypatch, companion, opened, checks):
    """La première réponse d'une machine publique est toujours contrôlée : la réponse
    s'affiche sans l'attendre ; ma machine reçoit exactement la même demande (messages,
    graine, longueur), et la donne pour bonne."""
    nothing_here(monkeypatch)
    directory(node(PC, BIG))
    fixe = my_machine(BIG)
    release = threading.Event()
    mine = Mine(HONEST_B, release)
    monkeypatch.setattr(remote_models, "chat_stream", mine)
    companion.script = {PC: ("answer", [HONEST_A[:200], HONEST_A[200:]])}
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, QUESTION, model=BEST)
    assert routes(events) == [at(PC, BIG)] and events[-1]["error"] is None
    (thread,) = checks
    assert thread.is_alive() and reputation.checked(PC) == 0     # affichée, le contrôle attend
    assert reputation.get(PC)["served"] == 1
    release.set()
    thread.join(10)
    assert reputation.get(PC)["checks_ok"] == 1 and not reputation.is_set_aside(PC)
    (control,) = mine.asked
    sent = companion.requests[0]["body"]
    assert control["model"] == fixe and control["think"] is False
    assert control["messages"] == sent["messages"]          # prompt système compris
    assert control["messages"][-1] == {"role": "user", "content": QUESTION}
    assert control["options"] == {"seed": sent["options"]["seed"],
                                  "num_predict": public_lending.MAX_TOKENS}
    assert len(companion.requests) == 1                     # rien d'autre chez un inconnu


def test_a_cheater_is_set_aside_said_once_and_no_longer_chosen(
        monkeypatch, companion, opened, checks, caplog):
    """Une réponse sans rapport : la conversation déjà affichée ne change pas ; une ligne au
    journal, sans contenu ni clé entière ; la machine n'est plus choisie."""
    nothing_here(monkeypatch)
    directory(node(PC, BIG, tps=90), node(OTHER, BIG, tps=20))
    my_machine(BIG)
    monkeypatch.setattr(remote_models, "chat_stream", Mine(HONEST_B))
    companion.script = {PC: ("answer", [UNRELATED]), OTHER: ("answer", [HONEST_B])}
    with caplog.at_level(logging.DEBUG), TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, QUESTION, model=BEST)
        assert routes(events) == [at(PC, BIG)] and "crêpe" in events[-1]["html"]
        checks[0].join(10)
        assert reputation.is_set_aside(PC) and reputation.get(PC)["checks_failed"] == 1
        conv_id = events[0]["conversation_id"]
        shown = ui.get(f"/api/conversations/{conv_id}", headers=AUTH).json()["messages"][-1]
        assert shown["content"] == UNRELATED                # rien n'y est changé
        events = ask(ui, "Et la mer ?", model=BEST, conversation_id=conv_id)
        assert routes(events) == [at(OTHER, BIG)]          # plus choisie
        assert placement.best_public()[0].model == at(OTHER, BIG)
    said = [r.getMessage() for r in caplog.records if r.name.startswith("aiodos")]
    assert [m for m in said if "mise de côté" in m] == [
        f"Machine du réseau public mise de côté : contrôle raté (unrelated), {PC[:8]}…, 7 jours"]
    for message in said:
        assert PC[:9] not in message and OTHER[:9] not in message
        assert "crêpes" not in message and "Rayleigh" not in message and "ciel" not in message


def test_with_my_documents_only_my_machines_check(monkeypatch, companion, opened, checks,
                                                 fake_embed):
    """Une demande qui porte mes documents (accord donné) : contrôlée par mes machines
    seulement, jamais par un second inconnu ; sans aucune de mes machines, pas de contrôle
    (il attend une prochaine fois)."""
    nothing_here(monkeypatch)
    settings.set_many({"rag_min_score": 0.1, "public_documents": True})
    directory(node(PC, BIG, tps=90), node(OTHER, BIG, tps=20))
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        store.ensure_defaults()
        _library("maison", "fusée lunaire notes maison", fake_embed)
        events = ask(ui, "fusée lunaire", model=BEST, libraries=True)
        assert routes(events) == [at(PC, BIG)]
        assert "notes maison" in " ".join(
            m["content"] for m in companion.requests[-1]["body"]["messages"])
        assert checks == [] and len(companion.requests) == 1
        assert reputation.checked(PC) == 0 and reputation.get(PC)["served"] == 1
        fixe = my_machine(BIG)
        mine = Mine("Bonjour")
        monkeypatch.setattr(remote_models, "chat_stream", mine)
        ask(ui, "fusée lunaire", model=BEST, libraries=True)
        checks[0].join(10)
    assert [c["model"] for c in mine.asked] == [fixe] and len(companion.requests) == 2
    assert "notes maison" in " ".join(m["content"] for m in mine.asked[0]["messages"])
    assert reputation.get(PC)["checks_ok"] == 1
    # Sans mes documents, une machine du réseau public peut aussi contrôler
    assert public_controls.controls(BIG, PC, documents=False) == [
        (fixe, None), (at(OTHER, BIG), OTHER)]
    assert public_controls.controls(BIG, PC, documents=True) == [(fixe, None)]
    assert public_controls.documents_in([{"type": "web"}], {}, {}) is False
    assert public_controls.documents_in([{"type": "knowledge"}], {}, {}) is True
    assert public_controls.documents_in([], {3: ["notes.txt"]}, {}) is True
    assert public_controls.documents_in([], {}, {"collection_ids": [3]}) is True


def test_the_control_machines(monkeypatch, companion, opened):
    """Mes machines d'abord (pas celles de mes amis, pas un autre modèle), puis les machines
    publiques qui ont ce modèle et une place, la plus rapide d'abord (mesurée ici, sinon
    annoncée) ; ni le prêteur, ni une machine mise de côté ; quatre au plus."""
    directory(node(PC, BIG, tps=90), node(OTHER, BIG, tps=20), node(THIRD, BIG, tps=50),
              node(FOURTH, BIG, tps=70), node(FIFTH, BIG, tps=80), node(SIXTH, SMALL, tps=99),
              node(SEVENTH, BIG, free=0))
    fixe = my_machine(BIG)
    my_machine(BIG, node_id="ami", trust="friend")
    my_machine(SMALL, node_id="portable")
    reputation.set_aside(FIFTH)
    reputation.note_served(OTHER, 1000, 10.0)              # 100 jetons/s mesurés ici
    assert public_controls.controls(BIG, PC, documents=False) == [
        (fixe, None), (at(OTHER, BIG), OTHER), (at(FOURTH, BIG), FOURTH),
        (at(THIRD, BIG), THIRD)]
    assert public_controls.controls(BIG, PC) == [(fixe, None)]     # dans le doute : les miennes
    db.execute("DELETE FROM peers")
    assert public_controls.controls(BIG, PC) == []


def test_a_cut_or_stopped_answer_is_neither_noted_nor_checked(monkeypatch, companion, opened,
                                                               checks):
    directory(node(PC, BIG))
    my_machine(BIG)
    monkeypatch.setattr(remote_models, "chat_stream", Mine("Bonjour"))
    question = [{"role": "user", "content": QUESTION}]
    companion.script = {PC: ("error_after", "Bon", "busy")}
    with pytest.raises(public_chat.PublicError):
        list(public_controls.stream(at(PC, BIG), question))
    companion.script = {PC: ("answer", ["Bon", "jour"])}
    stop = threading.Event()
    pieces = public_controls.stream(at(PC, BIG), question, stop=stop)
    assert next(pieces) == ("content", "Bon")
    stop.set()                                              # Stop, ou le client parti
    assert list(pieces) == []
    assert reputation.get(PC) is None and checks == []
    assert list(public_controls.stream(at(PC, BIG), question)) == [
        ("content", "Bon"), ("content", "jour")]
    assert reputation.get(PC)["served"] == 1 and len(checks) == 1


def test_a_lender_cannot_inflate_its_speed():
    """Les jetons qu'il dit, jamais plus qu'une estimation prudente du texte reçu."""
    assert public_controls._tokens(10, "mot " * 100) == 10
    assert public_controls._tokens(100000, "Bonjour") == public_lending.estimate_tokens("Bonjour")
    assert public_controls._tokens(None, "abc") == 1


# ---------------------------------------------------------------- le choix
def test_placement_skips_set_aside_machines_and_prefers_the_speed_measured_here(
        monkeypatch, companion, opened):
    local_models(monkeypatch, SMALL)
    monkeypatch.setattr(placement.android, "is_android", lambda: False)
    directory(node(PC, SMALL, tps=100), node(OTHER, SMALL, tps=10))
    speed.note(300, 10)                                     # 30 jetons/s ici
    plan = lambda: placement.plan(AUTO, Executor(), public=True)  # noqa: E731
    assert plan() == [at(PC, SMALL), SMALL, at(OTHER, SMALL)]
    reputation.note_served(PC, 200, 10.0)                  # mesurée ici : 20, pas 100
    reputation.note_served(OTHER, 1000, 10.0)              # 100, pas 10
    assert plan() == [at(OTHER, SMALL), SMALL, at(PC, SMALL)]
    reputation.set_aside(OTHER)                             # contrôle raté : plus candidate
    assert plan() == [SMALL, at(PC, SMALL)]
    assert [c.model for c in placement.best_public()] == [at(PC, SMALL)]
    assert placement.best_public()[0].tps == 20.0
