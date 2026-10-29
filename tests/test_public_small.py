"""La règle des petits réseaux (décision de l'auteur du 29/10/2026 ; network/placement.py) : sur
un réseau encore petit (son PC et son téléphone), aucune machine n'est établie, et le PC ne
répondait jamais aux conversations du téléphone. Tant qu'aucune machine publique établie ne
prête ce modèle, une machine publique mesurée ici FASTER fois plus rapide que cette machine passe
devant, même non établie, et sa réponse est « non vérifiable pour l'instant » ; dès qu'une
machine établie le prête, la règle stricte reprend. Jamais une machine en faute. Sur un
téléphone, l'amorce de la mesure : une machine jamais mesurée essayée avant lui, une fois par
heure au plus, la toute première conversation comprise. La marque suit la réponse : événement
« route », message gardé, interface.

Le compagnon simulé et l'annuaire public sont ceux de test_public_chat.py."""
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from aiodos import db, paths
from aiodos.api.server import create_app
from aiodos.network import peers, placement, remote_models, reputation
from aiodos.node import speed
from tests import i18n_js
from tests.test_collusion import establish, seen
from tests.test_public_chat import (AUTH, BIG, OTHER, PC, SMALL, THIRD, TOKEN, AUTO, Executor,
                                    ask, at, checkable, directory, local_models, measured, node,
                                    routes)
from tests.test_public_chat import companion, opened  # noqa: F401 (fixtures)

NODE = shutil.which("node")
UNVERIFIED = {"type": "unverified"}
LONG = ["mot, "] * 30                   # une réponse assez longue pour être mesurée (16 jetons)


def plan(ex=None, bigger=False):
    """(machines à essayer, celles dont la réponse est « non vérifiable pour l'instant »)."""
    marked = set()
    found = placement.plan(AUTO, ex or Executor(), True, bigger, marked)
    return found, marked


def phone(monkeypatch, yes=True):
    monkeypatch.setattr(placement.android, "is_android", lambda: yes)


def route_events(events):
    return [e for e in events if e["type"] == "route"]


def last_message(ui, events):
    conv = ui.get(f"/api/conversations/{events[0]['conversation_id']}", headers=AUTH).json()
    return conv["messages"][-1]


# ------------------------------------------------------------------------ la règle
def test_without_an_established_machine_a_faster_one_goes_ahead_marked(monkeypatch, companion,
                                                                       opened):
    """Aucune machine établie ne prête ce modèle : une machine publique mesurée ici deux fois
    plus rapide passe devant, même non établie, marquée ; pas assez rapide, non ; un PC dont la
    vitesse n'est pas connue garde la règle (personne devant lui sans mesure de lui-même), un
    téléphone non (toute vitesse mesurée). Un modèle plus gros accepté passe devant comme avant,
    sans marque (il est contrôlable)."""
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL, tps=50))
    speed.note(100, 10)                                   # 10 jetons/s ici
    assert plan() == ([SMALL, at(PC, SMALL)], set())      # jamais mesurée : pas sur un PC
    measured(PC, 15, established=False)                   # 15 < 2 × 10
    assert plan() == ([SMALL, at(PC, SMALL)], set())
    measured(PC, 50, established=False)                   # moyenne glissante : 25,5
    assert plan() == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})
    first = placement.candidates(SMALL, Executor(), public=True)[0]
    assert first.unverified and not first.established
    # Cette machine occupée : devant de toute façon, et toujours marquée
    assert plan(Executor(1)) == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})
    speed.forget()                                        # un PC dont la vitesse est inconnue
    assert plan() == ([SMALL, at(PC, SMALL)], set())
    phone(monkeypatch)                                    # un téléphone : toute vitesse mesurée
    assert plan() == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})
    # Un modèle plus gros, accepté et contrôlable (une machine établie qui n'a que lui)
    directory(node(PC, SMALL, BIG), checkable(BIG))
    assert plan(bigger=True) == ([at(PC, BIG), SMALL], set())


def test_once_an_established_machine_lends_the_model_the_strict_rule_returns(
        monkeypatch, companion, opened):
    """Une machine établie prête ce modèle (place libre ou non, même écartée quelques minutes) :
    seules les établies passent devant ; elle ne compte pas si elle ne prête que d'autres
    modèles, ou plus du tout."""
    local_models(monkeypatch, SMALL)
    speed.note(100, 10)
    measured(PC, 50, established=False)
    witness = "9f" * 32                                    # la machine établie de checkable
    directory(node(PC, SMALL), checkable(SMALL))           # établie, sans place
    assert plan() == ([SMALL, at(PC, SMALL)], set())
    reputation.bench(witness, "slow")                      # écartée 10 min : établie quand même
    assert plan() == ([SMALL, at(PC, SMALL)], set())
    directory(node(PC, SMALL), checkable(BIG))             # elle n'a pas ce modèle
    assert plan() == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})
    directory(node(PC, SMALL), dict(checkable(SMALL), lends=False))   # elle ne prête plus
    assert plan() == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})
    # L'établie, mesurée, passe devant ; la non établie retourne derrière, sans marque
    directory(node(PC, SMALL), node(OTHER, SMALL))
    measured(OTHER, 50)
    assert plan() == ([at(OTHER, SMALL), SMALL, at(PC, SMALL)], set())
    phone(monkeypatch)                                     # un téléphone pareil, sans amorce
    speed.forget()
    assert plan() == ([at(OTHER, SMALL), SMALL, at(PC, SMALL)], set())


def test_a_machine_at_fault_never_benefits(monkeypatch, companion, opened):
    """Mise de côté (contrôle raté), écartée (un échec), ou en faute (un contrôle raté, la mise
    de côté passée) : jamais devant par cette règle."""
    local_models(monkeypatch, SMALL)
    speed.note(100, 10)
    directory(node(PC, SMALL), node(OTHER, SMALL))
    measured(PC, 50, established=False)
    measured(OTHER, 50, established=False)
    assert plan() == ([at(PC, SMALL), at(OTHER, SMALL), SMALL],
                      {at(PC, SMALL), at(OTHER, SMALL)})
    reputation.note_check(OTHER, False, "link")           # de côté 7 jours : plus candidate
    assert plan() == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})
    db.execute("UPDATE public_reputation SET aside_until = ? WHERE key = ?", (db.now() - 1, OTHER))
    assert plan() == ([at(PC, SMALL), SMALL, at(OTHER, SMALL)], {at(PC, SMALL)})   # en faute
    reputation.bench(PC, "cut")                           # écartée quelques minutes
    assert plan() == ([SMALL, at(OTHER, SMALL)], set())


# ---------------------------------------------------------------- l'amorce de la mesure
def test_the_first_conversation_of_a_phone_goes_to_the_faster_public_machine(
        monkeypatch, companion, opened):
    """Le S23 et le PC de l'auteur, sur un réseau public encore petit : la toute première
    conversation part vers le PC (jamais mesuré, essayé une fois), marquée ; mesuré par sa
    réponse, il passe ensuite devant par sa vitesse, marqué ; établi, sans marque. La marque est
    dite par l'événement « route » et gardée avec la réponse."""
    phone(monkeypatch)
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL, tps=60))
    companion.script[PC] = ("answer", LONG)
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        first = ask(ui, "Bonjour")
        assert routes(first) == [at(PC, SMALL)]
        assert route_events(first)[0]["unverified"] is True
        assert first[-1]["error"] is None and "mot" in first[-1]["html"]
        saved = last_message(ui, first)
        assert saved["model"] == at(PC, SMALL) and saved["unverified"] is True
        assert UNVERIFIED in saved["sources"]
        assert reputation.speed(PC) is not None            # mesuré par sa réponse
        conv = first[0]["conversation_id"]
        again = ask(ui, "Encore", conversation_id=conv)     # l'amorce prise : par sa vitesse
        assert routes(again) == [at(PC, SMALL)] and route_events(again)[0]["unverified"] is True
        establish(PC)                                      # établi : la règle stricte, sans marque
        third = ask(ui, "Et encore", conversation_id=conv)
        assert routes(third) == [at(PC, SMALL)] and "unverified" not in route_events(third)[0]
        saved = last_message(ui, third)
        assert saved["unverified"] is False and UNVERIFIED not in saved["sources"]
        # Rouverte : chaque réponse garde sa marque
        messages = ui.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
        assert [m["unverified"] for m in messages if m["role"] == "assistant"] == [
            True, True, False]


def test_a_trial_that_fails_hands_over_unmarked(monkeypatch, companion, opened):
    """La machine essayée refuse : ce téléphone répond lui-même, et sa réponse n'est pas
    marquée ; l'amorce est prise pour l'heure."""
    phone(monkeypatch)
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL))
    companion.script[PC] = ("refuse", 503, "busy")
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")
        assert routes(events) == [at(PC, SMALL), SMALL]
        assert [e.get("unverified") for e in route_events(events)] == [True, None]
        saved = last_message(ui, events)
        assert saved["model"] == SMALL and saved["unverified"] is False
        assert routes(ask(ui, "Encore")) == [SMALL]


def test_the_trial_once_an_hour_and_never_on_a_claim_alone(monkeypatch, companion, opened):
    """L'amorce : sur un téléphone seulement, une machine jamais mesurée, une fois par TRIAL_S
    toutes machines confondues ; la plus ancienne d'abord, même si une identité neuve se dit
    bien plus rapide ; une machine qui se dit elle-même trop lente (moins de deux fois ce
    téléphone, s'il est mesuré), en faute, ou une machine établie qui a ce modèle : pas
    d'amorce ; ni quand une autre de mes machines, libre, répondrait d'abord (l'amorce n'est
    alors pas prise), ni quand ce téléphone est occupé."""
    phone(monkeypatch)
    local_models(monkeypatch, SMALL)
    directory(node(PC, SMALL, tps=99_999), node(OTHER, SMALL, tps=60))
    seen(OTHER, 48)                                       # connue depuis deux jours
    assert plan() == ([at(OTHER, SMALL), SMALL, at(PC, SMALL)], {at(OTHER, SMALL)})
    assert plan() == ([SMALL, at(PC, SMALL), at(OTHER, SMALL)], set())   # une fois par heure
    monkeypatch.setattr(placement, "TRIAL_S", 0)
    assert plan() == ([at(OTHER, SMALL), SMALL, at(PC, SMALL)], {at(OTHER, SMALL)})
    # Ce téléphone mesuré à 50 jetons/s : l'autre se dit trop lente (60 < 100)
    speed.note(500, 10)
    assert plan() == ([at(PC, SMALL), SMALL, at(OTHER, SMALL)], {at(PC, SMALL)})
    speed.forget()
    # Un PC : jamais d'amorce
    phone(monkeypatch, False)
    assert plan() == ([SMALL, at(PC, SMALL), at(OTHER, SMALL)], set())
    phone(monkeypatch)
    # En faute (un contrôle raté, la mise de côté passée) : pas d'amorce
    directory(node(THIRD, SMALL))
    reputation.note_check(THIRD, False, "link")
    db.execute("UPDATE public_reputation SET aside_until = ? WHERE key = ?", (db.now() - 1, THIRD))
    assert plan() == ([SMALL, at(THIRD, SMALL)], set())
    # Une machine établie prête ce modèle : la règle stricte, pas d'amorce
    directory(node(PC, SMALL), checkable(SMALL))
    assert plan() == ([SMALL, at(PC, SMALL)], set())
    # Cette machine occupée : les machines publiques devant, sans amorce ni marque
    directory(node(PC, SMALL))
    monkeypatch.setattr(placement, "TRIAL_S", 3600)
    placement.forget()
    assert plan(Executor(1)) == ([at(PC, SMALL), SMALL], set())
    assert placement._trial["at"] is None
    # Une autre de mes machines, libre, avec ce modèle : elle d'abord, l'amorce pas prise
    peers.save("fixe", "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.set_profile("fixe", {"lends_models": True, "ollama": {"models": [
        {"name": SMALL, "caps": ["completion"], "fits_vram": True, "loaded": False}]}})
    fixe = remote_models.model_id("fixe", SMALL)
    assert plan() == ([SMALL, fixe, at(PC, SMALL)], set())
    assert placement._trial["at"] is None
    db.execute("DELETE FROM peers")
    assert plan() == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})


# ----------------------------------------------------------------------- l'interface
@pytest.mark.skipif(NODE is None, reason="Node.js absent")
@pytest.mark.parametrize("lang, text", [("fr", "non vérifiable pour l'instant"),
                                        ("en", "not verifiable yet")])
def test_the_interface_shows_the_mark(tmp_path, lang, text):
    """models.js : la marque à côté de « Répondu par … », cachée sauf si la réponse la porte,
    avec son infobulle (aucune autre machine ne peut encore contrôler cette réponse) ; chat.js
    la montre selon l'événement « route » et la réponse gardée (unverified)."""
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    for name in ("state.js", "dom.js"):
        (js / name).write_bytes((paths.STATIC_DIR / "js" / name).read_bytes())
    (js / "views" / "models.js").write_bytes(
        (paths.STATIC_DIR / "js" / "views" / "models.js").read_bytes())
    prelude = i18n_js.install(js, lang=lang, module="./js/i18n.js")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "essai.mjs").write_text(prelude + """
import * as models from './js/views/models.js';
console.log(JSON.stringify({ shown: models.unverifiedTag(true), hidden: models.unverifiedTag() }));
""", encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "essai.mjs")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr[:400]
    tags = json.loads(done.stdout)
    title = {"fr": "Aucune autre machine ne peut encore contrôler cette réponse",
             "en": "No other machine can check this answer yet"}[lang]
    escaped = text.replace("'", "&#39;")
    assert tags["shown"].startswith('<span class="unverified-tag" title="')
    assert f">{escaped}</span>" in tags["shown"] and title in tags["shown"]
    assert " hidden " in tags["hidden"] and "hidden" not in tags["shown"]
    view = (paths.STATIC_DIR / "js" / "views" / "chat.js").read_text(encoding="utf-8")
    assert "unverified(!!ev.unverified)" in view and "view.unverified(!!m.unverified)" in view
