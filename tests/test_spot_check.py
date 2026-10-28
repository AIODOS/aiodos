"""Les contrôles au hasard du réseau public (network/spot_check.py) et la réputation des
machines (network/reputation.py) : tirage au hasard, première fois toujours contrôlée,
comparaison de deux réponses (honnête ou tricheuse, sur des textes fixes), mise de côté 7
jours puis pour de bon, migration 28, contrôle en arrière-plan avec une fausse machine de
contrôle (jamais bloquant, autre identifiant de conversation, seconde machine qui tranche),
et rien du contenu au journal. Sans réseau ni moteur."""
import logging
import math
import random
import threading

import pytest

from aiodos import db
from aiodos.network import reputation, spot_check
from aiodos.network.spot_check import compare

LENDER = "a" * 52
OTHER = "b" * 52
THIRD = "c" * 52
MINE = None                       # une de mes machines : crue seule

QUESTION = "Pourquoi le ciel est-il bleu ?"
HONEST_A = (
    "Le ciel est bleu à cause de la diffusion de Rayleigh. La lumière du Soleil contient "
    "toutes les couleurs ; en traversant l'atmosphère, elle rencontre les molécules d'azote "
    "et d'oxygène, bien plus petites que sa longueur d'onde. Ces molécules diffusent beaucoup "
    "plus les courtes longueurs d'onde (le bleu, le violet) que les longues (le rouge). Le "
    "bleu est donc renvoyé dans toutes les directions et colore tout le ciel. Le violet se "
    "voit moins, car notre œil y est peu sensible et le Soleil en émet moins.")
HONEST_B = (
    "C'est la diffusion de Rayleigh qui explique la couleur bleue du ciel. La lumière blanche "
    "du Soleil mélange toutes les couleurs. Dans l'atmosphère, les molécules d'azote et "
    "d'oxygène diffusent bien davantage les courtes longueurs d'onde, comme le bleu, que les "
    "longues, comme le rouge. Cette lumière bleue diffusée nous arrive de toutes les "
    "directions : le ciel paraît bleu. Au coucher du Soleil, la lumière traverse plus "
    "d'atmosphère, le bleu est dispersé en route et le ciel rougit.")
UNRELATED = (
    "Pour réussir une pâte à crêpes, mélangez 250 g de farine, 4 œufs, une pincée de sel et "
    "50 cl de lait versé petit à petit. Ajoutez 50 g de beurre fondu, laissez reposer une "
    "heure, puis faites cuire chaque crêpe dans une poêle chaude.")


@pytest.fixture(autouse=True)
def nothing_pending():
    spot_check.forget()
    yield
    spot_check.forget()


def fixed_cosine(value):
    """Un faux modèle d'embedding : deux vecteurs dont le cosinus vaut `value`."""
    return lambda texts: [[1.0, 0.0], [value, math.sqrt(1 - value * value)]]


# ------------------------------------------------------------------- quand
def test_the_first_time_a_machine_serves_here_it_is_always_checked():
    never = random.Random(1)
    never.random = lambda: 0.99                       # le hasard ne choisirait jamais
    assert spot_check.should_check(LENDER, never)
    reputation.note_served(LENDER, 200, 5.0)          # servie, mais jamais contrôlée
    assert spot_check.should_check(LENDER, never)
    reputation.note_check(LENDER, True)
    assert not spot_check.should_check(LENDER, never)
    assert not spot_check.should_check("pas une clé !", random.Random(1))


def test_then_five_percent_at_random():
    reputation.note_check(LENDER, True)
    draws = [spot_check.should_check(LENDER, random.Random(2026)) for _ in range(3)]
    assert len(set(draws)) == 1                        # graine fixée : le même tirage
    rng = random.Random(28)
    picked = sum(spot_check.should_check(LENDER, rng) for _ in range(4000))
    assert 140 <= picked <= 260                        # 5 % de 4 000 : 200
    # Sans générateur donné : SystemRandom, que le prêteur ne peut pas deviner
    assert isinstance(spot_check._rng, random.SystemRandom)


# ---------------------------------------------------------------- comparer
def test_two_honest_answers_agree():
    found = compare(QUESTION, HONEST_A, HONEST_B)
    assert found.ok and found.reason == "agree"
    assert compare(QUESTION, HONEST_A, HONEST_B, embed=fixed_cosine(0.93),
                   embed_model="qwen3-embedding:0.6b").ok
    # Une réponse courte mais finie n'est pas une réponse tronquée
    short = compare(QUESTION, "Le ciel est bleu à cause de la diffusion de Rayleigh.", HONEST_B)
    assert short.ok and short.measures["len_ratio"] < spot_check.TRUNCATED_RATIO


def test_an_answer_to_another_question_is_caught():
    found = compare(QUESTION, UNRELATED, HONEST_B)
    assert not found.ok and found.reason == "unrelated" and "contained" in found.measures
    found = compare(QUESTION, UNRELATED, HONEST_B, embed=fixed_cosine(0.35),
                    embed_model="bge-m3")
    assert not found.ok and found.reason == "unrelated" and found.measures["cos"] == 0.35


def test_each_embedding_model_has_its_own_scale():
    between = fixed_cosine(0.62)
    assert compare(QUESTION, HONEST_A, HONEST_B, embed=between,
                   embed_model="qwen3-embedding:0.6b").ok                 # seuil 0,58
    assert not compare(QUESTION, HONEST_A, HONEST_B, embed=between, embed_model="bge-m3").ok
    assert compare(QUESTION, HONEST_A, HONEST_B, embed=between,
                   embed_model="embeddinggemma").ok                       # non mesuré : 0,6


def test_without_embeddings_the_common_words_decide():
    def broken(texts):
        raise RuntimeError("moteur arrêté")
    assert compare(QUESTION, HONEST_A, HONEST_B, embed=broken).ok
    assert compare(QUESTION, UNRELATED, HONEST_B, embed=broken).reason == "unrelated"


def test_an_added_link_mail_or_phone_number_is_caught():
    for ad in ("Offre spéciale : https://promo-express.example/offre?ref=ia !",
               "Bons plans sur www.meilleurs-prix.example, profitez-en.",
               "Pour nos astuces, écrivez à contact@bonplan-malin.example.",
               "Un crédit rapide ? Appelez le 06 12 34 56 78."):
        for text in (HONEST_A + "\n\n" + ad, ad + "\n\n" + HONEST_A):
            found = compare(QUESTION, text, HONEST_B)
            assert not found.ok and found.reason == "link", ad


def test_a_link_the_other_answer_or_the_question_gives_is_not_an_ad():
    a = HONEST_A + " Source : https://en.wikipedia.org/wiki/Rayleigh_scattering"
    b = HONEST_B + " Voir https://fr.wikipedia.org/wiki/Diffusion_Rayleigh."
    assert compare(QUESTION, a, b).ok                  # le même site, une autre page
    asked = "Que dit www.meteofrance.fr sur la couleur du ciel ?"
    assert compare(asked, HONEST_A + " D'après meteofrance.fr, c'est la diffusion.",
                   HONEST_B).ok
    # « mot.De plus », sans espace, n'est pas un domaine ; des dates non plus
    assert compare(QUESTION, HONEST_A + " Il diffuse le bleu.De plus, en 1871 et 1899.",
                   HONEST_B).ok


def test_a_truncated_answer_is_caught():
    cut = HONEST_A[:int(len(HONEST_A) * 0.35)]
    cut = cut[:cut.rfind(" ")]
    found = compare(QUESTION, cut, HONEST_B)
    assert not found.ok and found.reason == "truncated" and found.measures["unfinished"]
    assert compare(QUESTION, "Voici pourquoi :", HONEST_B).reason == "truncated"
    assert not compare(QUESTION, "   ", HONEST_B).ok            # rien : vide
    assert compare(QUESTION, "", "").ok


def test_the_reading_catches_a_cut_another_model_and_an_inserted_passage():
    verdict = spot_check.reading_verdict
    honest = {"lp": [-0.4, -0.1, -1.2, -0.05] * 40, "eos": -0.2, "max_tokens": 400}
    assert verdict(honest).ok and verdict(honest).measures["ll_n"] == 160
    # Coupée : le modèle ne se serait pas arrêté là… sauf à la longueur maximale
    cut = {**honest, "eos": -15.0}
    assert verdict(cut).reason == "truncated"
    assert verdict({**cut, "lp": [-0.3] * 380}).ok                 # 95 % de 400 : la limite
    assert verdict({**cut, "max_tokens": None}).ok                 # limite inconnue
    # Un autre modèle : bien moins probable que la réponse de contrôle lue de même
    other = {"lp": [-1.0] * 200, "eos": -0.3, "max_tokens": 400}
    control = {"lp": [-0.4] * 180}
    assert verdict(other, control).reason == "unlikely"
    assert verdict(other, control).measures["ll_gap"] == pytest.approx(-0.6)
    assert verdict(other).ok                                       # seule : -1,0 passe
    assert verdict({**other, "lp": [-1.6] * 200}).reason == "unlikely"
    # Une publicité sans lien : une fenêtre très improbable
    inserted = {"lp": [-0.3] * 100 + [-9.0] * 20 + [-0.3] * 60, "eos": -0.1}
    assert verdict(inserted).reason == "inserted"
    assert verdict({"lp": []}).ok and verdict({"lp": [float("nan")]}).ok
    found = compare(QUESTION, HONEST_A, HONEST_B, reading=other, reading_b=control)
    assert not found.ok and found.reason == "unlikely" and "ll_mean" in found.measures


# --------------------------------------------------------------- réputation
def test_a_failed_check_sets_aside_seven_days_then_two_for_good(monkeypatch):
    now = db.now()
    monkeypatch.setattr(db, "now", lambda: now)
    assert reputation.get(LENDER) is None and not reputation.is_set_aside(LENDER)
    row = reputation.note_check(LENDER, False)
    assert row["checks_failed"] == 1 and not row["forever"]
    assert row["aside_until"] == pytest.approx(now + 7 * 86400)
    assert reputation.is_set_aside(LENDER)
    monkeypatch.setattr(db, "now", lambda: now + 7 * 86400 + 1)
    assert not reputation.is_set_aside(LENDER)        # une semaine après : de retour
    reputation.note_check(LENDER, True)
    assert not reputation.is_set_aside(LENDER)
    row = reputation.note_check(LENDER, False)        # deuxième contrôle raté
    assert row["forever"] and row["checks_failed"] == 2 and row["checks_ok"] == 1
    monkeypatch.setattr(db, "now", lambda: now + 400 * 86400)
    assert reputation.is_set_aside(LENDER)            # pour de bon
    assert reputation.checked(LENDER) == 3


def test_set_aside_by_hand_and_bad_keys():
    reputation.set_aside(OTHER)
    assert reputation.is_set_aside(OTHER) and not reputation.get(OTHER)["forever"]
    reputation.set_aside(OTHER, forever=True)
    assert reputation.get(OTHER)["forever"]
    assert reputation.is_set_aside("trop-court")     # une clé mal formée l'est toujours
    with pytest.raises(ValueError):
        reputation.note_served("pas une clé !", 10, 1.0)


def test_served_answers_and_speed_measured_here():
    reputation.note_served(LENDER, 400, 10.0)          # 40 jetons/s
    reputation.note_served(LENDER, 8, 0.01)            # trop court : ne dit rien
    reputation.note_served(LENDER, 200, 10.0)          # 20 jetons/s
    row = reputation.get(LENDER)
    assert row["served"] == 3 and reputation.checked(LENDER) == 0
    assert reputation.speed(LENDER) == pytest.approx(0.7 * 40 + 0.3 * 20)
    assert reputation.speed(OTHER) is None


def test_the_table_stays_bounded_and_keeps_the_cheaters(monkeypatch):
    monkeypatch.setattr(reputation, "MAX_ROWS", 3)
    now = db.now()
    reputation.note_check("d" * 40, False)                       # prise en défaut
    for n, key in enumerate(("e" * 40, "f" * 40, "g" * 40, "h" * 40)):
        monkeypatch.setattr(db, "now", lambda n=n: now + n)
        reputation.note_served(key, 100, 2.0)
    kept = {row["key"] for row in db.query("SELECT key FROM public_reputation")}
    assert kept == {"d" * 40, "g" * 40, "h" * 40}
    # Plus vue depuis KEEP_DAYS jours et sans faute : oubliée
    monkeypatch.setattr(db, "now", lambda: now + 200 * 86400)
    reputation.note_served("i" * 40, 100, 2.0)
    kept = {row["key"] for row in db.query("SELECT key FROM public_reputation")}
    assert kept == {"d" * 40, "i" * 40}
    # Pleine de machines prises en défaut : la nouvelle venue n'est pas effacée aussitôt
    for key in ("j" * 40, "k" * 40, "l" * 40):
        reputation.note_check(key, False)
    reputation.note_served("m" * 40, 100, 2.0)
    assert reputation.get("m" * 40)["served"] == 1


def test_migration_28_adds_the_reputation_table():
    conn = db.connect()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    db.execute("DROP TABLE public_reputation")
    db.execute("PRAGMA user_version=27")
    db.close()
    db._initialized.discard(path)
    assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION >= 28
    reputation.note_check(LENDER, True)
    assert reputation.checked(LENDER) == 1


# ------------------------------------------------------------------ contrôler
REQUEST = {"model": "qwen3.5:9b", "conversation": "conversation-controlee-0123456789",
           "messages": [{"role": "system", "content": "Sois clair."},
                        {"role": "user", "content": QUESTION}],
           "options": {"temperature": 0.7, "max_tokens": 400, "seed": 1234}}


class FakeAsk:
    """Les machines de contrôle : leur réponse, ou une exception ; les demandes reçues."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, machine, request):
        self.asked.append((machine, request))
        found = self.answers[machine]
        if isinstance(found, BaseException):
            raise found
        return found


def run(answer, answers, controls, **kw):
    ask = FakeAsk(answers)
    done = []
    thread = spot_check.run_check(LENDER, REQUEST, answer, ask, controls, embed=None,
                                  on_done=done.append, **kw)
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    return ask, done[0]


def test_an_honest_lender_passes_and_the_check_uses_another_conversation():
    ask, verdict = run(HONEST_A, {"pc": HONEST_B}, [("pc", MINE)])
    assert verdict.ok
    assert reputation.get(LENDER)["checks_ok"] == 1 and not reputation.is_set_aside(LENDER)
    (machine, sent), = ask.asked
    assert machine == "pc"
    # La même demande (modèle, messages, options, graine), sous un autre identifiant
    assert {k: v for k, v in sent.items() if k != "conversation"} == \
        {k: v for k, v in REQUEST.items() if k != "conversation"}
    assert sent["conversation"] != REQUEST["conversation"]
    assert len(sent["conversation"]) == 32
    assert REQUEST["conversation"] == "conversation-controlee-0123456789"   # pas touchée


def test_a_cheater_caught_by_one_of_my_machines_is_set_aside():
    _ask, verdict = run(UNRELATED, {"pc": HONEST_B}, [("pc", MINE)])
    assert not verdict.ok and verdict.reason == "unrelated"
    assert reputation.is_set_aside(LENDER) and reputation.get(LENDER)["checks_failed"] == 1


def test_a_public_control_alone_does_not_condemn():
    ask, verdict = run(HONEST_A, {"inconnu": UNRELATED}, [("inconnu", OTHER)])
    assert verdict is None                             # sans conclusion
    assert reputation.checked(LENDER) == 0 and reputation.checked(OTHER) == 0


def test_a_second_control_decides_and_unmasks_a_lying_control():
    # La première machine de contrôle ment, la seconde donne raison au prêteur
    ask, verdict = run(HONEST_A, {"menteur": UNRELATED, "honnete": HONEST_B},
                       [("menteur", OTHER), ("honnete", THIRD)])
    assert verdict.ok
    assert reputation.get(LENDER)["checks_ok"] == 1
    assert reputation.is_set_aside(OTHER) and not reputation.is_set_aside(THIRD)
    first, second = (sent["conversation"] for _machine, sent in ask.asked)
    assert first != second                             # une clé jetable par demande
    # Les deux machines de contrôle donnent tort au prêteur : il est pris en défaut
    spot_check.forget()
    _ask, verdict = run(UNRELATED, {"x": HONEST_A, "y": HONEST_B},
                        [("x", "x" * 40), ("y", "y" * 40)])
    assert not verdict.ok and reputation.get(LENDER)["checks_failed"] == 1


def test_the_lender_itself_set_aside_machines_and_failures_are_skipped():
    reputation.set_aside(OTHER)
    ask, verdict = run(HONEST_A, {"lui": HONEST_A, "ecarte": HONEST_B,
                                  "panne": RuntimeError("injoignable"), "pc": HONEST_B},
                       [("lui", LENDER), ("ecarte", OTHER), ("panne", THIRD), ("pc", MINE)])
    assert verdict.ok
    assert [machine for machine, _sent in ask.asked] == ["panne", "pc"]


def test_no_control_machine_no_verdict():
    _ask, verdict = run(HONEST_A, {}, [])
    assert verdict is None and reputation.checked(LENDER) == 0


def test_the_check_never_blocks_the_answer():
    release = threading.Event()

    def slow(machine, request):
        release.wait(5)
        return HONEST_B
    done = []
    thread = spot_check.run_check(LENDER, REQUEST, HONEST_A, slow, [("pc", MINE)],
                                  embed=None, on_done=done.append)
    assert thread.is_alive() and not done             # rendu tout de suite
    # Un seul contrôle à la fois par prêteur, MAX_RUNNING en tout
    assert spot_check.run_check(LENDER, REQUEST, HONEST_A, slow, [("pc", MINE)]) is None
    never = random.Random(5)
    never.random = lambda: 0.99
    assert not spot_check.should_check(LENDER, never)   # déjà en contrôle : plus « première »
    release.set()
    thread.join(5)
    assert done and done[0].ok
    assert spot_check.run_check("pas une clé", REQUEST, HONEST_A, slow, []) is None


def test_nothing_of_the_conversation_reaches_the_log(caplog):
    secret = "Zorglub-Secret-4242"
    request = {**REQUEST, "messages": [{"role": "user", "content": f"Parle de {secret}"}]}
    ask = FakeAsk({"x": f"{secret} n'existe pas. https://promo.example", "y": HONEST_B})
    with caplog.at_level(logging.DEBUG):
        thread = spot_check.run_check(LENDER, request, f"{secret} est {UNRELATED}", ask,
                                      [("x", OTHER), ("y", MINE)], embed=None)
        thread.join(5)
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "Contrôle du réseau public" in said
    assert secret not in said and "crêpes" not in said and "Rayleigh" not in said
    assert LENDER not in said and reputation.short(LENDER) in said     # abrégée
    for _machine, sent in ask.asked:
        assert sent["conversation"] not in said        # mène à la clé jetable
    assert REQUEST["conversation"] not in said
    for row in db.query("SELECT * FROM public_reputation"):
        assert secret not in repr(tuple(row))
