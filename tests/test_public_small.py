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

from aiodos import db, paths, settings
from aiodos.api.server import create_app
from aiodos.network import peers, placement, remote_models, reputation
from aiodos.node import speed
from tests import i18n_js
from tests.test_collusion import establish, seen
from tests.test_public_chat import (AUTH, BIG, OTHER, PC, SMALL, THIRD, TOKEN, AUTO, Executor,
                                    ask, at, checkable, directory, local_models, measured, node,
                                    notices, routes)
from tests.test_public_chat import companion, opened  # noqa: F401 (fixtures)

NODE = shutil.which("node")
UNVERIFIED = {"type": "unverified"}
LONG = ["mot, "] * 30                   # une réponse assez longue pour être mesurée (16 jetons)
LIAR, HUGE, LARGE = "qwen3.5:999b", "gpt-oss:120b", "qwen2.5:72b"


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


# ------------------------------------------------- avec un modèle plus gros, accepté
def test_the_author_s_phone_and_his_pc_with_a_bigger_model(monkeypatch, companion, opened):
    """Le cas de l'auteur (décision du 29/10/2026 après-midi) : le S23 converse avec
    qwen3.5:0.8b, son PC, public et non établi, ne prête que qwen3.5:9b. L'accord « modèle plus
    gros » pas encore demandé : la carte le propose, le téléphone répond lui-même (l'amorce
    n'est pas prise) ; refusé : le téléphone, sans carte ; donné : le PC répond avec le 9b,
    d'abord par l'amorce, puis par sa vitesse mesurée, marqué « non vérifiable pour
    l'instant »."""
    phone(monkeypatch)
    local_models(monkeypatch, SMALL)
    directory(node(PC, BIG, tps=60))
    companion.script[PC] = ("answer", LONG)
    with TestClient(create_app(TOKEN, {"testserver"})) as ui:
        events = ask(ui, "Bonjour")                       # pas encore demandé
        assert notices(events) == ["public_bigger"]
        assert next(e for e in events if e["type"] == "notice")["model"] == BIG
        assert routes(events) == [SMALL] and companion.requests == []
        assert placement._trial["at"] is None
        settings.set_value("public_bigger_model", False)  # « Non »
        events = ask(ui, "Bonjour")
        assert routes(events) == [SMALL] and notices(events) == []
        settings.set_value("public_bigger_model", True)   # « Oui » : l'amorce, avec le 9b
        events = ask(ui, "Bonjour")
        assert routes(events) == [at(PC, BIG)] and route_events(events)[0]["unverified"] is True
        assert companion.requests[-1]["body"]["model"] == BIG
        saved = last_message(ui, events)
        assert saved["model"] == at(PC, BIG) and saved["unverified"] is True
        assert reputation.speed(PC) is not None            # mesuré par sa réponse
        again = ask(ui, "Encore", conversation_id=events[0]["conversation_id"])
        assert routes(again) == [at(PC, BIG)] and route_events(again)[0]["unverified"] is True
    # Le 9b devenu contrôlable (une machine établie le prête) : la règle d'avant, sans marque
    directory(node(PC, BIG), checkable(BIG))
    assert plan(bigger=True) == ([at(PC, BIG), SMALL], set())


def test_a_bigger_model_by_the_small_network_rule_has_bounds(monkeypatch, companion, opened):
    """Jamais par cette règle : un nom au-delà de SMALL_MAX_PARAMS milliards (« qwen3.5:999b »,
    « 120b », même mesurés très rapides ; une telle machine répond avec un modèle plus petit
    qu'elle a, s'il est dans la borne), une machine en faute, une machine qu'on n'a pas
    mesurée sur un PC, un modèle qu'une machine établie prête (même écartée quelques
    minutes), ni sans l'accord. Avec ce modèle aussi, le plus gros accepté d'abord."""
    local_models(monkeypatch, SMALL)
    speed.note(100, 10)                                   # un PC, 10 jetons/s
    bigger = lambda: plan(bigger=True)                    # noqa: E731
    directory(node(PC, LIAR), node(OTHER, HUGE))
    measured(PC, 500, established=False)
    measured(OTHER, 500, established=False)
    assert bigger() == ([SMALL], set()) and placement.bigger_public(SMALL) is None
    directory(node(PC, LIAR, LARGE), node(OTHER, HUGE, BIG))      # 72 milliards : dans la borne
    assert bigger() == ([at(PC, LARGE), at(OTHER, BIG), SMALL], {at(PC, LARGE), at(OTHER, BIG)})
    assert placement.bigger_public(SMALL) == LARGE
    assert plan() == ([SMALL], set())                     # sans l'accord : ce modèle seulement
    # Avec ce modèle aussi : le plus gros accepté, sinon ce modèle ; marqué dans les deux cas
    directory(node(PC, SMALL, BIG))
    assert bigger() == ([at(PC, BIG), SMALL], {at(PC, BIG)})
    assert plan() == ([at(PC, SMALL), SMALL], {at(PC, SMALL)})
    # Jamais mesurée, sur un PC : rien (pas d'amorce)
    directory(node(THIRD, BIG))
    assert bigger() == ([SMALL], set()) and placement.bigger_public(SMALL) is None
    # Une machine établie prête le 9b, écartée quelques minutes : pas par cette règle
    witness = "9f" * 32
    directory(node(PC, BIG), checkable(BIG))
    reputation.bench(witness, "slow")
    assert bigger() == ([SMALL], set()) and placement.bigger_public(SMALL) is None
    # En faute (un contrôle raté, la mise de côté passée)
    directory(node(PC, BIG))
    assert bigger() == ([at(PC, BIG), SMALL], {at(PC, BIG)})
    reputation.note_check(PC, False, "link")
    db.execute("UPDATE public_reputation SET aside_until = ? WHERE key = ?", (db.now() - 1, PC))
    assert bigger() == ([SMALL], set()) and placement.bigger_public(SMALL) is None


def test_the_trial_with_a_bigger_model_on_a_phone(monkeypatch, companion, opened):
    """L'amorce d'un téléphone, l'accord donné : la machine jamais mesurée qui n'a qu'un modèle
    plus gros dans la borne est essayée avec lui ; un nom au-delà de la borne, jamais."""
    phone(monkeypatch)
    local_models(monkeypatch, SMALL)
    directory(node(PC, HUGE, tps=99_999), node(OTHER, LIAR, tps=99_999))
    assert plan(bigger=True) == ([SMALL], set()) and placement._trial["at"] is None
    directory(node(PC, HUGE, tps=99_999), node(OTHER, BIG, tps=60))
    assert placement.bigger_public(SMALL) == BIG
    assert plan(bigger=True) == ([at(OTHER, BIG), SMALL], {at(OTHER, BIG)})
    assert plan(bigger=True) == ([SMALL], set())          # une fois par heure ; rien d'autre


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
