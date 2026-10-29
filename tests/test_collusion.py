"""Contre la collusion (29/10/2026 ; network/sightings.py, network/reputation.py,
network/spot_check.py, docs/conception/reseau-public-calcul.md § 8) : une clé de nœud ne
coûte rien, et deux nœuds d'un même menteur annonçant le même modèle se contrôlaient l'un
l'autre (le contrôle passait toujours).

Les premières vues de l'annuaire (apparues ensemble, se ressemblent) ; « établie » ; les
votes des machines de contrôle publiques : deux indépendantes d'accord pour un contrôle
réussi, une seule sans conclusion, une faute flagrante comptée sur l'avis d'une seule machine
établie, une machine seule contre deux prise en défaut, une machine établie jamais mise en
minorité par des machines sans passé ; « la première fois » bornée ; migration 29. Sans
réseau ni moteur (de bout en bout : test_public_controls.py)."""
import random

import pytest

from aiodos import db
from aiodos.network import reputation, sightings, spot_check
from tests.test_spot_check import (HONEST_A, HONEST_B, LENDER, OTHER, QUESTION, THIRD,
                                   UNRELATED, nothing_pending, run)  # noqa: F401 (fixture)

FOURTH, FIFTH = "d" * 52, "e" * 52
HOUR = 3600
NINE_B = ("qwen3.5:9b",)


def seen(key, hours_ago=0.0, models=NINE_B):
    """Cette machine publique vue pour la première fois dans l'annuaire il y a `hours_ago`
    heures, avec ces modèles."""
    now = db.now()
    db.execute("INSERT OR REPLACE INTO public_sightings(key, first_seen, last_seen, models) "
               "VALUES(?, ?, ?, ?)", (key, now - hours_ago * HOUR, now,
                                      sightings.fingerprint(models)))


def establish(key, hours_ago=72.0, models=NINE_B):
    """Une machine publique établie ici : vue il y a longtemps, un contrôle réussi."""
    seen(key, hours_ago, models)
    reputation.note_check(key, True)
    assert reputation.established(key)


def never():
    rng = random.Random(1)
    rng.random = lambda: 0.99                       # le hasard ne choisirait jamais
    return rng


# ------------------------------------------------------------- premières vues
def test_first_sightings_of_the_directory(monkeypatch):
    now = db.now()
    monkeypatch.setattr(db, "now", lambda: now)
    nodes = [{"key": LENDER, "models": ["qwen3.5:9b", "llama3"]},
             {"key": OTHER, "models": ["LLAMA3", "qwen3.5:9b", "llama3"]},
             {"key": "trop-court", "models": []}, "pas un nœud"]
    assert sightings.sighted(nodes) == 2
    assert sightings.first_seen(LENDER) == sightings.first_seen(OTHER) == now
    assert sightings.first_seen("trop-court") is None and sightings.first_seen(THIRD) is None
    # Apparues ensemble, et la même liste de modèles (ni l'ordre, ni la casse, ni les doublons)
    assert sightings.together(LENDER, OTHER) and sightings.alike(LENDER, OTHER)
    # Revue plus tard : la première vue ne bouge pas ; la dernière, une fois par jour au plus
    monkeypatch.setattr(db, "now", lambda: now + 3 * 86400)
    assert sightings.sighted(nodes) == 0                 # retenue en mémoire
    sightings.forget()
    assert sightings.sighted([{"key": LENDER, "models": ["autre"]}]) == 1
    row = db.query_one("SELECT * FROM public_sightings WHERE key = ?", (LENDER,))
    assert row["first_seen"] == now and row["last_seen"] == now + 3 * 86400
    assert row["models"] == sightings.fingerprint(["qwen3.5:9b", "llama3"])   # la première
    # Une clé jamais vue l'est « maintenant » ; une de mes machines (None) n'est jamais liée
    assert sightings.together(THIRD, FOURTH) and not sightings.alike(THIRD, FOURTH)
    assert sightings.together(THIRD, LENDER, now=now + 2 * HOUR)
    assert not sightings.together(None, LENDER) and not sightings.alike(LENDER, None)


def test_apart_in_time_or_different_models_they_do_not_look_alike():
    seen(LENDER, 0)
    seen(OTHER, 2)                                      # deux heures avant : ensemble
    seen(THIRD, 7)                                      # sept heures avant : à part
    seen(FOURTH, 1, models=("qwen3.5:9b", "mistral:7b"))
    assert sightings.together(LENDER, OTHER) and sightings.alike(LENDER, OTHER)
    assert not sightings.together(LENDER, THIRD) and not sightings.alike(LENDER, THIRD)
    assert sightings.together(LENDER, FOURTH) and not sightings.alike(LENDER, FOURTH)


def test_the_sightings_stay_bounded_and_keep_the_old_ones(monkeypatch):
    monkeypatch.setattr(sightings, "MAX_ROWS", 2)
    sightings.sighted([{"key": LENDER, "models": []}, {"key": OTHER, "models": []}])
    # Pleine : une inondation de clés neuves n'efface pas la première vue des anciennes
    sightings.sighted([{"key": k * 40, "models": []} for k in "xyz"])
    kept = {row["key"] for row in db.query("SELECT key FROM public_sightings")}
    assert kept == {LENDER, OTHER}
    reputation.note_served("w" * 40, 100, 2.0)          # la réputation ne la remplit pas non plus
    assert sightings.first_seen("w" * 40) is None
    # Plus vue depuis KEEP_DAYS jours : oubliée, la place revient
    now = db.now()
    monkeypatch.setattr(db, "now", lambda: now + 181 * 86400)
    sightings.forget()
    sightings.sighted([{"key": THIRD, "models": []}])
    kept = {row["key"] for row in db.query("SELECT key FROM public_sightings")}
    assert kept == {THIRD}


def test_groups_of_machines_that_appeared_together(monkeypatch):
    """sightings.groups (network/placement.py : un modèle contrôlable chez trois groupes) :
    rangées par première vue, un nouveau groupe à 6 h ou plus de la précédente ; de proche en
    proche (4 h, puis 4 h : un seul groupe, même à 8 h d'un bout à l'autre) ; une clé jamais
    vue l'est à l'instant ; deux machines de groupes différents ne sont jamais apparues
    ensemble."""
    now = db.now()
    monkeypatch.setattr(db, "now", lambda: now)
    seen(LENDER, 0)
    seen(OTHER, 7)
    seen(THIRD, 14)
    keys = [LENDER, OTHER, THIRD]
    assert sightings.groups(keys) == [[THIRD], [OTHER], [LENDER]]
    assert sightings.groups(keys + [FOURTH]) == [[THIRD], [OTHER], [LENDER, FOURTH]]  # jamais vue
    seen(FOURTH, 10.5)                                   # 3 h 30 après THIRD, avant OTHER
    assert sightings.groups(keys + [FOURTH]) == [[THIRD, FOURTH, OTHER], [LENDER]]
    seen(FOURTH, 6)                                      # tout juste 6 h après LENDER : à part
    assert len(sightings.groups([LENDER, FOURTH])) == 2 and not sightings.together(LENDER, FOURTH)
    found = sightings.groups(keys + [FOURTH])
    assert found == [[THIRD], [OTHER, FOURTH], [LENDER]]
    for i, group in enumerate(found):                    # d'un groupe à l'autre : jamais ensemble
        for other in found[i + 1:]:
            assert not any(sightings.together(a, b) for a in group for b in other)
    # firsts : par lots, sans les clés jamais vues ni mal formées ; known évite de relire
    monkeypatch.setattr(sightings, "BATCH", 2)
    assert sightings.firsts(keys + ["trop-court", FIFTH, LENDER]) == {
        LENDER: now, OTHER: now - 7 * HOUR, THIRD: now - 14 * HOUR}
    assert sightings.groups(keys, known={}) == [keys]    # rien de connu : tout à l'instant
    assert sightings.groups([]) == []


def test_established_among_reads_the_whole_directory_at_once(monkeypatch):
    establish(OTHER)
    establish(THIRD)
    reputation.note_served(LENDER, 100, 2.0)            # a servi, jamais contrôlée
    seen(FOURTH, 1)
    reputation.note_check(FOURTH, True)                  # contrôlée, mais vue il y a une heure
    reputation.set_aside(THIRD)
    monkeypatch.setattr(reputation, "BATCH", 2)
    keys = [LENDER, OTHER, THIRD, FOURTH, FIFTH, "trop-court", OTHER]
    assert reputation.established_among(keys) == {OTHER}
    assert reputation.established_among(keys) == {k for k in set(keys) if reputation.established(k)}
    assert reputation.established_among([]) == set()


def test_a_key_the_reputation_notes_is_seen_at_the_latest_then():
    reputation.note_served(LENDER, 100, 2.0)
    assert sightings.first_seen(LENDER) == reputation.get(LENDER)["first_seen"]
    seen(OTHER, 30)
    reputation.note_served(OTHER, 100, 2.0)            # déjà vue : sa première vue reste
    assert reputation.first_seen(OTHER) == pytest.approx(db.now() - 30 * HOUR, abs=5)


# ------------------------------------------------------------------- établie
def test_established(monkeypatch):
    now = db.now()
    monkeypatch.setattr(db, "now", lambda: now)
    assert not reputation.established(LENDER) and not reputation.established("trop-court")
    reputation.note_check(LENDER, True)                 # contrôlée, mais vue à l'instant
    row = reputation.get(LENDER)
    assert row["checks_ok"] == row["checks_sure"] == 1 and not reputation.established(LENDER)
    monkeypatch.setattr(db, "now", lambda: now + reputation.ESTABLISHED_S + 1)
    assert reputation.established(LENDER)               # un jour après
    # Vue depuis longtemps, mais jamais contrôlée ; ou contrôlée avant la règle d'aujourd'hui
    seen(OTHER, 100)
    reputation.note_served(OTHER, 100, 2.0)
    assert not reputation.established(OTHER)
    db.execute("UPDATE public_reputation SET checks_ok = 3 WHERE key = ?", (OTHER,))
    assert not reputation.established(OTHER)            # checks_sure : 0
    # Prise en faute : plus établie, même revenue après sa semaine de côté
    reputation.note_check(LENDER, False, "unrelated")
    assert not reputation.established(LENDER)
    monkeypatch.setattr(db, "now", lambda: now + 30 * 86400)
    assert not reputation.is_set_aside(LENDER) and not reputation.established(LENDER)
    # Mise de côté à la main : pas établie ; un écart de quelques minutes n'y change rien
    establish(THIRD)
    reputation.bench(THIRD, "slow")
    assert reputation.established(THIRD)
    reputation.set_aside(THIRD)
    assert not reputation.established(THIRD)


# --------------------------------------------------------------------- voter
def test_two_fresh_nodes_of_one_liar_do_not_vouch_for_each_other():
    """Le prêteur et son jumeau, apparus ensemble, le même modèle : le jumeau lui donne
    raison (la même réponse). Sans conclusion : rien de compté, rien d'établi."""
    seen(LENDER, 0)
    seen(OTHER, 0)
    seen(THIRD, 0, models=("qwen3.5:9b", "phi4:14b"))   # un troisième, autre liste : pareil
    ask, verdict = run(UNRELATED, {"jumeau": UNRELATED, "triple": UNRELATED},
                       [("jumeau", OTHER), ("triple", THIRD)])
    assert verdict is None and len(ask.asked) == 2
    row = reputation.get(LENDER)
    assert row["checks_ok"] == row["checks_sure"] == row["checks_failed"] == 0
    assert row["checks_open"] == 1 and not reputation.established(LENDER)
    # Apparus plus tard, mais ensemble et identiques entre eux : ils ne font pas une paire
    spot_check.forget()
    seen(LENDER, 30)
    seen(OTHER, 2)
    seen(THIRD, 2)
    _ask, verdict = run(UNRELATED, {"jumeau": UNRELATED, "triple": UNRELATED},
                        [("jumeau", OTHER), ("triple", THIRD)])
    assert verdict is None and reputation.inconclusive(LENDER) == 2


def test_two_independent_controls_that_agree_vouch_for_the_lender(monkeypatch):
    real_now = db.now
    seen(LENDER, 0)
    seen(OTHER, 72)
    seen(THIRD, 100)
    ask, verdict = run(HONEST_A, {"o": HONEST_B, "t": HONEST_B}, [("o", OTHER), ("t", THIRD)])
    assert verdict.ok and [machine for machine, _sent in ask.asked] == ["o", "t"]
    row = reputation.get(LENDER)
    assert row["checks_ok"] == row["checks_sure"] == 1 and row["checks_open"] == 0
    assert not reputation.established(LENDER)           # vue à l'instant
    later = db.now() + reputation.ESTABLISHED_S + 60
    monkeypatch.setattr(db, "now", lambda: later)
    assert reputation.established(LENDER)
    # Deux machines d'accord qui se ressemblent : une troisième, différente, est demandée
    # d'abord (la diversité), et c'est elle qui fait la paire
    monkeypatch.setattr(db, "now", real_now)
    spot_check.forget()
    seen(FOURTH, 50)
    seen(FIFTH, 50)                                     # apparue avec FOURTH, mêmes modèles
    seen(THIRD, 10, models=("qwen3.5:9b", "mistral:7b"))
    ask, verdict = run(HONEST_A, {"q": HONEST_B, "c": HONEST_B, "t": HONEST_B},
                       [("q", FOURTH), ("c", FIFTH), ("t", THIRD)])
    assert verdict.ok and [machine for machine, _sent in ask.asked] == ["q", "t"]


def test_one_public_control_alone_is_inconclusive():
    establish(OTHER)
    seen(LENDER, 0)
    _ask, verdict = run(HONEST_A, {"o": HONEST_B}, [("o", OTHER)])
    assert verdict is None
    assert reputation.checked(LENDER) == 0 and reputation.inconclusive(LENDER) == 1
    # « La première fois » dure jusqu'à une conclusion, FIRST_TRIES contrôles au plus
    assert spot_check.should_check(LENDER, never())
    for _ in range(spot_check.FIRST_TRIES - 1):
        reputation.note_open(LENDER)
    assert not spot_check.should_check(LENDER, never())


def test_a_flagrant_fault_counts_even_with_one_established_control():
    seen(LENDER, 0)
    establish(OTHER)
    _ask, verdict = run(UNRELATED, {"o": HONEST_B}, [("o", OTHER)])
    assert not verdict.ok and verdict.reason == "unrelated"
    assert reputation.is_set_aside(LENDER) and reputation.get(LENDER)["checks_failed"] == 1


def test_one_control_without_a_past_never_condemns_alone():
    """Elle pourrait accuser un honnête : une faute, même flagrante, vue par une seule
    machine sans passé ici ne compte pas ; ni une faute qui n'est pas flagrante (un passage
    ajouté, vu à la lecture), même par une machine établie ; ni celle d'une machine établie
    apparue avec le prêteur."""
    seen(LENDER, 0)
    seen(OTHER, 72)
    _ask, verdict = run(UNRELATED, {"o": HONEST_B}, [("o", OTHER)])
    assert verdict is None and not reputation.is_set_aside(LENDER)
    # Un passage ajouté (sans lien), vu par la lecture d'une machine établie
    spot_check.forget()
    establish(THIRD)
    inserted = {"lp": [-0.3] * 200 + [-5.0] * 16 + [-0.3] * 184, "eos": -0.1}
    plain = {"lp": [-0.3] * 400, "eos": -0.1}

    def read(machine, sent, texts):
        return [inserted if text == HONEST_A else plain for text in texts]
    _ask, verdict = run(HONEST_A, {"t": HONEST_B}, [("t", THIRD)], read=read)
    assert verdict is None and not reputation.is_set_aside(LENDER)
    assert spot_check.compare(QUESTION, HONEST_A, HONEST_B, reading=inserted,
                              reading_b=plain).reason == "inserted"
    # Établie, mais apparue avec le prêteur
    spot_check.forget()
    establish(FOURTH, hours_ago=72)
    seen(LENDER, 70)
    _ask, verdict = run(UNRELATED, {"q": HONEST_B}, [("q", FOURTH)])
    assert verdict is None and not reputation.is_set_aside(LENDER)
    assert reputation.inconclusive(LENDER) == 3


def test_a_control_machine_alone_against_two_is_set_aside():
    """La première ment (une réponse sans rapport), deux machines indépendantes donnent raison
    au prêteur : la troisième lit A, sa réponse et les deux d'avant ; la menteuse, seule contre
    deux, est prise en défaut, le prêteur blanchi."""
    seen(LENDER, 0)
    seen(OTHER, 72)
    seen(THIRD, 100)
    seen(FOURTH, 130)
    read_by = {}

    def read(machine, sent, texts):
        read_by[machine] = sorted(texts)
        return [None] * len(texts)
    ask, verdict = run(HONEST_A, {"menteur": UNRELATED, "h1": HONEST_B, "h2": HONEST_B},
                       [("menteur", OTHER), ("h1", THIRD), ("h2", FOURTH)], read=read)
    assert verdict.ok and len(ask.asked) == 3
    assert reputation.get(LENDER)["checks_sure"] == 1
    assert reputation.is_set_aside(OTHER) and reputation.get(OTHER)["checks_failed"] == 1
    assert not reputation.is_set_aside(THIRD) and not reputation.is_set_aside(FOURTH)
    assert len(read_by["menteur"]) == len(read_by["h1"]) == 2
    assert read_by["h2"] == sorted([HONEST_A, HONEST_B, UNRELATED, HONEST_B])
    # Une machine honnête qui a seulement jugé autrement n'est pas accusée : le prêteur cite
    # une source que les deux autres citent aussi, pas elle (« lien ajouté », à ses yeux) ;
    # sa propre réponse ressemble à la leur
    spot_check.forget()
    reputation.forget()
    seen(FIFTH, 160)
    cited = HONEST_A + " Source : https://fr.wikipedia.org/wiki/Diffusion_Rayleigh"
    also = HONEST_B + " Voir https://en.wikipedia.org/wiki/Rayleigh_scattering."
    _ask, verdict = run(cited, {"severe": HONEST_B, "h1": also, "h2": also},
                        [("severe", FIFTH), ("h1", THIRD), ("h2", FOURTH)])
    assert verdict.ok and not reputation.is_set_aside(FIFTH)
    assert reputation.checked(FIFTH) == 0


def test_machines_without_a_past_cannot_outvote_an_established_one():
    """Une machine établie voit une réponse sans rapport ; deux machines sans passé ici
    (indépendantes en apparence : un menteur patient) lui donnent raison. Elles ne la
    mettent pas en minorité : la faute flagrante compte, et personne n'est accusé à tort."""
    seen(LENDER, 0)
    establish(OTHER)
    seen(THIRD, 30)
    seen(FOURTH, 50)
    _ask, verdict = run(UNRELATED, {"etablie": HONEST_B, "j1": UNRELATED, "j2": UNRELATED},
                        [("etablie", OTHER), ("j1", THIRD), ("j2", FOURTH)])
    assert not verdict.ok and verdict.reason == "unrelated"
    assert reputation.is_set_aside(LENDER)
    assert not reputation.is_set_aside(OTHER) and reputation.established(OTHER)
    assert not reputation.is_set_aside(THIRD) and not reputation.is_set_aside(FOURTH)
    # Avec une machine établie de leur côté, la majorité l'emporte
    spot_check.forget()
    reputation.forget()
    establish(FIFTH, hours_ago=90)
    db.execute("DELETE FROM public_reputation WHERE key = ?", (LENDER,))
    _ask, verdict = run(HONEST_A, {"etablie": UNRELATED, "j1": HONEST_B, "e2": HONEST_B},
                        [("etablie", OTHER), ("j1", THIRD), ("e2", FIFTH)])
    assert verdict.ok and reputation.is_set_aside(OTHER)


def test_my_machine_still_decides_alone():
    seen(LENDER, 0)
    _ask, verdict = run(HONEST_A, {"pc": HONEST_B}, [("pc", None)])
    assert verdict.ok and reputation.get(LENDER)["checks_sure"] == 1


# ----------------------------------------------------------------- migration
def test_migration_29_adds_first_sightings_and_sure_checks():
    reputation.note_served(LENDER, 100, 2.0)
    first = reputation.get(LENDER)["first_seen"]
    conn = db.connect()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    db.execute("DROP TABLE public_sightings")
    db.execute("ALTER TABLE public_reputation DROP COLUMN checks_sure")
    db.execute("ALTER TABLE public_reputation DROP COLUMN checks_open")
    db.execute("PRAGMA user_version=28")
    db.close()
    db._initialized.discard(path)
    assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION >= 29
    assert sightings.first_seen(LENDER) == first        # la première vue gardée
    row = reputation.get(LENDER)
    assert row["checks_sure"] == row["checks_open"] == 0 and row["served"] == 1
