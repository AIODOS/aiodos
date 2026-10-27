"""La fin de partie (jobs/endgame.py, docs/conception/partage-puissance.md § 2.7) : quand un
travail n'a plus rien en file, une machine qui se libère (ou l'exécuteur d'ici) reçoit le
doublon de l'unité qui tourne depuis le plus longtemps ailleurs. Jamais avant ENDGAME_MIN_S,
jamais plus de deux calculs, jamais pour la machine qui la calcule déjà, et selon les règles
d'une remise (cercles, dernier essai jamais chez un ami, bibliothèque partagée…). Le premier
résultat valable gagne ; l'autre calcul l'apprend à son renouvellement (« done »), calculé
ici son arrêt est levé, et son résultat arrivé après est refusé sans reproche. La fin sans
résultat d'un des deux calculs ne remet pas l'unité en file ; les compteurs et les vues ne
voient pas le doublon. Sans réseau : les appels vont directement au propriétaire.

Le temps passe par l'horloge du module (endgame.time), avancée par `later`."""
import asyncio
import threading
import time
import types

import numpy as np
import pytest

from aiodos import db, i18n
from aiodos.jobs import endgame, leases, queue, registry
from aiodos.jobs.executor import Executor
from aiodos.jobs.models import MAX_ATTEMPTS, NORMAL, PAUSE_NOW
from aiodos.jobs.registry import Interrupted, JobKind, TaskSpec, UnitSpec
from aiodos.knowledge import store
from aiodos.network import peers, sharing, wire, work
from tests.test_images import wait_for
from tests.test_work_leases import B, C, _computed, _lease, owner  # noqa: F401

HERE = "noeud-ici"
OTHER, THIRD, FOURTH, FRIEND = "machine-portable", "machine-fixe", "machine-salon", "machine-ami"


def _split(params):
    return "fin de partie", [UnitSpec("fin_task", "cpu", {"n": i})
                             for i in range(params["count"])], {}


def _expand(unit, here, fresh):
    """Comme un travail de moteur : un run_uuid tiré à chaque remise et gardé dans l'unité."""
    if fresh:
        run = f"principal-{unit['id']}"
        db.execute("UPDATE units SET run_uuid=? WHERE id=?", (run, unit["id"]))
    else:
        run = db.query_one("SELECT run_uuid FROM units WHERE id=?", (unit["id"],))["run_uuid"]
    order = {"run_uuid": run}
    if here:
        order["local"] = {"unit_id": unit["id"]}
    return order


registry.register_job_kind("fin_kind", _split)
registry.register_portable("fin_task", compute=lambda payload, file, options, stop: {},
                           assimilate=lambda payload, output: output)
registry.register_job_kind("fin_moteur", lambda params: (
    "moteur", [UnitSpec("fin_moteur_task", "cpu", {})], {}))
registry.register_portable("fin_moteur_task", compute=lambda payload, file, options, stop: {},
                           assimilate=lambda payload, output: output, expand=_expand)


@pytest.fixture
def later(monkeypatch):
    """Avance l'horloge de la fin de partie de `seconds` à chaque appel : les unités en
    calcul ont l'air de tourner depuis d'autant plus longtemps (un doublon qui prend la place
    du principal repart de son propre départ)."""
    shift = [0.0]
    monkeypatch.setattr(endgame, "time",
                        types.SimpleNamespace(time=lambda: time.time() + shift[0]))

    def advance(seconds=endgame.ENDGAME_COLD_S + 5):
        shift[0] += seconds
    return advance


def _job(count=1, reach=None):
    return queue.create_job("fin_kind", {"count": count}, NORMAL, reach=reach)


def _lend(node, circle="mine"):
    return leases.claim_remote({"cpu"}, node, lambda unit: True, circle=circle)


def _double(node, circle="mine"):
    return endgame.claim_remote({"cpu"}, node, lambda unit: True, leases.LEASE_S, circle=circle)


def _unit(unit_id) -> dict:
    return dict(db.query_one("SELECT * FROM units WHERE id=?", (unit_id,)))


def _copies(unit_id) -> list:
    return [dict(r) for r in db.query("SELECT * FROM unit_copies WHERE unit_id=?", (unit_id,))]


def _counts(unit_id) -> tuple:
    unit = _unit(unit_id)
    return unit["attempts"], unit["handouts"], unit["lost"], unit["interrupts"]


# ------------------------------------------------------------- quand doubler
def test_no_copy_while_units_of_the_job_wait_in_the_queue(later):
    _job(count=2)
    first = _lend(OTHER)
    later()
    assert _double(THIRD) is None                          # la deuxième attend en file
    assert endgame.claim_local({"cpu"}, HERE) is None
    _lend(THIRD)                                           # elle part : plus rien en file
    copy = _double(FOURTH)
    assert copy["id"] == first["id"] and copy["copy"] and copy["place"] == "mine"


def test_no_copy_before_the_unit_has_run_long_enough(later):
    """Sans unité faite pour en juger : ENDGAME_COLD_S."""
    _job()
    unit = _lend(OTHER)
    assert _double(THIRD) is None and endgame.claim_local({"cpu"}, HERE) is None
    later(endgame.ENDGAME_COLD_S - 5)
    assert _double(THIRD) is None
    later(10)
    assert _double(THIRD)["id"] == unit["id"]


@pytest.mark.parametrize("usual, wait", [(100.0, 200), (5.0, endgame.ENDGAME_MIN_S)])
def test_a_unit_is_doubled_once_it_lags_behind_those_already_done(later, usual, wait):
    """Deux fois la durée moyenne des unités faites du même travail, ENDGAME_MIN_S au moins :
    une unité longue n'est pas doublée parce qu'elle est longue, mais parce qu'elle traîne."""
    _job(count=2)
    done = _lend(OTHER)
    queue.finish_unit(done["id"], {}, usual)
    unit = _lend(THIRD)
    later(wait - 5)
    assert _double(FOURTH) is None
    later(10)
    assert _double(FOURTH)["id"] == unit["id"]


def test_never_more_than_two_runs_nor_twice_on_the_same_machine(later):
    _job()
    unit = _lend(OTHER)
    later()
    assert _double(OTHER) is None                          # elle la calcule déjà
    assert _double(THIRD)["id"] == unit["id"]
    assert _double(THIRD) is None and _double(FOURTH) is None
    assert endgame.claim_local({"cpu"}, HERE) is None
    assert len(_copies(unit["id"])) == endgame.MAX_RUNS - 1


def test_the_unit_running_for_the_longest_is_doubled(later):
    _job(count=2)
    _lend(OTHER)
    second = _lend(THIRD)
    db.execute("UPDATE units SET started=started-100 WHERE id=?", (second["id"],))
    later()
    assert _double(FOURTH)["id"] == second["id"]


def test_a_unit_kept_for_my_machines_is_never_doubled_at_a_friend(later):
    _job(reach="here,mine")
    unit = _lend(OTHER)
    later()
    assert _double(FRIEND, circle="friends") is None
    assert endgame.claim_local({"cpu"}, HERE)["id"] == unit["id"]


def test_a_paused_job_gets_no_copy(later):
    job = _job()
    _lend(OTHER)
    later()
    queue.pause_job(job)                                   # « après l'unité en cours »
    assert _double(THIRD) is None and endgame.claim_local({"cpu"}, HERE) is None
    queue.resume_job(job)
    assert _double(THIRD) is not None


# --------------------------------------------------------- le premier gagne
def test_the_first_result_wins_and_the_late_one_is_refused_politely(owner, later):
    """Le doublon de B finit le premier : son résultat entre, C l'apprend à son
    renouvellement, et son résultat arrivé après est refusé sans échec ni mise de côté.
    Seul B est compté pour la réciprocité ; rien n'entre deux fois."""
    m = owner["manager"]
    orders = [_lease(owner, node=C)["unit"] for _ in range(3)]      # C calcule tout
    assert _lease(owner)["unit"] is None                             # trop tôt
    later()
    copy = _lease(owner)["unit"]                                     # B : un doublon
    assert copy["id"] == orders[0]["id"] and copy["blob"] == orders[0]["blob"]
    assert work.blob(m, B, {"unit": copy["id"], "offset": 0, "size": 1024})["data"]
    assert work.result_limit(B) == work.MINE_RESULT                  # son index sera reçu
    assert work.renew(m, B, {"unit": copy["id"]}) == {"ok": True, "state": "running"}
    reply = work.result(m, B, {"unit": copy["id"], "compute_s": 3.0,
                               "output": wire.pack(_computed(copy))})
    assert reply == {"accepted": True}
    unit = _unit(copy["id"])
    assert unit["status"] == "done" and unit["node"] == B
    assert _counts(copy["id"]) == (1, 1, 0, 0)
    assert work.renew(m, C, {"unit": copy["id"]}) == {"ok": False, "state": "done"}
    late = work.result(m, C, {"unit": copy["id"], "compute_s": 9.0,
                              "output": wire.pack(_computed(orders[0]))})
    assert late == {"accepted": False, "reason": i18n.t("work.done_elsewhere")}
    assert work.release(m, C, {"unit": copy["id"]}) == {"ok": False}
    ledger = {p["node_id"]: p for p in peers.list_peers()}
    assert ledger[B]["received_units"] == 1 and ledger[B]["received_s"] == pytest.approx(3.0)
    assert (ledger[C]["received_units"], ledger[C]["failed_units"],
            ledger[C]["refused_units"]) == (0, 0, 0)
    assert sharing.set_aside_reason(C) is None
    assert [e["source"] for e in store.list_entries(owner["collection"])] == ["docs/a.txt"]
    assert db.query_one("SELECT COUNT(*) AS n FROM unit_copies")["n"] == 0


def test_when_the_original_wins_the_copy_stops_at_its_next_renewal(owner, later):
    m = owner["manager"]
    orders = [_lease(owner, node=C)["unit"] for _ in range(3)]
    later()
    copy = _lease(owner)["unit"]
    reply = work.result(m, C, {"unit": orders[0]["id"], "compute_s": 2.0,
                               "output": wire.pack(_computed(orders[0]))})
    assert reply == {"accepted": True}
    assert work.renew(m, B, {"unit": copy["id"]}) == {"ok": False, "state": "done"}
    late = work.result(m, B, {"unit": copy["id"], "output": wire.pack(_computed(copy))})
    assert late["accepted"] is False and late["reason"] == i18n.t("work.done_elsewhere")
    ledger = {p["node_id"]: p for p in peers.list_peers()}
    assert ledger[C]["received_units"] == 1
    assert (ledger[B]["received_units"], ledger[B]["failed_units"]) == (0, 0)
    assert _unit(orders[0]["id"])["node"] == C and _counts(orders[0]["id"]) == (1, 1, 0, 0)


def test_a_refused_first_result_lets_the_other_run_go_on(owner, later):
    """Le premier résultat valable gagne : celui du doublon, refusé, n'arrête pas le calcul
    principal, qui finit et entre."""
    m = owner["manager"]
    orders = [_lease(owner, node=C)["unit"] for _ in range(3)]
    later()
    copy = _lease(owner)["unit"]
    bad = {"pieces": ["a", "b"], "vectors": np.ones((1, 8), dtype=np.float32)}
    assert work.result(m, B, {"unit": copy["id"], "output": wire.pack(bad)})["accepted"] is False
    unit = _unit(copy["id"])
    assert unit["status"] == "running" and unit["node"] == C and unit["lease_until"]
    assert _counts(copy["id"]) == (1, 1, 0, 0) and _copies(copy["id"]) == []
    assert work.renew(m, C, {"unit": copy["id"]}) == {"ok": True, "state": "running"}
    assert work.result(m, C, {"unit": copy["id"],
                              "output": wire.pack(_computed(orders[0]))}) == {"accepted": True}


def test_a_unit_computed_here_can_be_doubled_and_beaten(later):
    """L'unité d'ici est doublée par une autre machine qui finit la première : le calcul
    d'ici n'entre plus, et son arrêt est levé une fois l'unité faite."""
    _job()
    here = queue.claim_next({"cpu"}, HERE)
    later()
    assert _double(OTHER)["id"] == here["id"]
    assert leases.take_back(here["id"], OTHER)["node"] == OTHER
    assert endgame.win(here["id"], HERE) is False          # son résultat n'entrera pas
    assert endgame.beaten({here["id"]}, HERE) == set()     # il continue, au cas où
    queue.finish_unit(here["id"], {}, 1.0)
    assert endgame.beaten({here["id"]}, HERE) == {here["id"]}
    assert queue.release_unit(here["id"], False, HERE) is None   # rendu : rien ne change
    assert _unit(here["id"])["status"] == "done" and _counts(here["id"]) == (1, 1, 0, 0)


def test_a_copy_computes_under_its_own_run_uuid(later):
    """Un doublon ne tire pas de run_uuid pour l'unité : il calcule sous le sien, que
    l'unité reprend s'il gagne ; refusé, le principal reprend le sien."""
    queue.create_job("fin_moteur", {}, NORMAL)
    unit = _lend(OTHER)
    assert registry.payload_for(unit, here=False)["run_uuid"] == f"principal-{unit['id']}"
    later()
    copy = _double(THIRD)
    order = endgame.payload_for(copy, here=False)
    assert order["run_uuid"] == copy["run_uuid"] != f"principal-{unit['id']}"
    assert _unit(unit["id"])["run_uuid"] == f"principal-{unit['id']}"
    taken = leases.take_back(unit["id"], THIRD)
    assert taken["run_uuid"] == copy["run_uuid"]
    assert registry.payload_for(taken, here=True, fresh=False)["run_uuid"] == copy["run_uuid"]
    queue.fail_unit(unit["id"], "résultat refusé", 0.0)
    assert _unit(unit["id"])["node"] == OTHER
    assert _unit(unit["id"])["run_uuid"] == f"principal-{unit['id']}"
    endgame.drop(unit["id"], THIRD)
    local = endgame.claim_local({"cpu"}, HERE)
    assert endgame.payload_for(local)["local"] == {"unit_id": None}   # il ne dit pas l'étape


# ------------------------------------------------- un calcul tombe sans résultat
def test_the_failure_of_one_run_keeps_the_unit_running_with_the_other(later):
    job = _job()
    unit = _lend(OTHER)
    later()
    _double(THIRD)
    # Le doublon échoue : seule sa ligne s'en va
    assert leases.fail(unit["id"], THIRD, "panne") == (job, False)
    assert _unit(unit["id"])["node"] == OTHER and _copies(unit["id"]) == []
    assert leases.renew(unit["id"], THIRD) == "reassigned"
    # Le principal échoue : le doublon prend sa place, rien n'est compté
    _double(THIRD)
    assert leases.fail(unit["id"], OTHER, "panne") == (job, False)
    promoted = _unit(unit["id"])
    assert promoted["status"] == "running" and promoted["node"] == THIRD
    assert promoted["lease_until"] and _counts(unit["id"]) == (1, 1, 0, 0)
    assert leases.renew(unit["id"], THIRD) == "running"
    assert leases.renew(unit["id"], OTHER) == "reassigned"
    # Les deux échoués : les règles d'avant (un nouvel essai)
    assert leases.fail(unit["id"], THIRD, "panne") == (job, False)
    assert _unit(unit["id"])["status"] == "queued" and _counts(unit["id"]) == (1, 1, 0, 0)


def test_a_lost_lease_or_a_release_keeps_the_unit_running_with_the_other(later):
    job = _job()
    unit = _lend(OTHER)
    later()
    _double(THIRD)
    # Le doublon disparaît : sa ligne s'en va à l'échéance
    db.execute("UPDATE unit_copies SET lease_until=1")
    assert leases.expire() == []
    assert _copies(unit["id"]) == [] and _unit(unit["id"])["node"] == OTHER
    # Le principal disparaît : le doublon prend sa place, sans disparition comptée
    _double(THIRD)
    db.execute("UPDATE units SET lease_until=1 WHERE id=?", (unit["id"],))
    assert leases.expire() == [(job, False)]
    assert _unit(unit["id"])["node"] == THIRD and _counts(unit["id"]) == (1, 1, 0, 0)
    assert _double(FOURTH) is None                         # son calcul vient de commencer
    # Le principal rendu (son utilisateur est revenu) : de même
    later()
    _double(FOURTH)
    assert leases.release(unit["id"], THIRD) == job
    assert _unit(unit["id"])["node"] == FOURTH and _unit(unit["id"])["status"] == "running"
    # Un doublon rendu : l'unité continue
    later()
    _double(OTHER)
    assert leases.release(unit["id"], OTHER) == job and _unit(unit["id"])["node"] == FOURTH
    # Le dernier calcul rendu : en file, sans essai, comme avant
    assert leases.release(unit["id"], FOURTH) == job
    assert _unit(unit["id"])["status"] == "queued" and _counts(unit["id"]) == (0, 1, 0, 0)


def test_pause_now_and_cancel_reach_the_copy(later):
    job = _job()
    unit = _lend(OTHER)
    later()
    _double(THIRD)
    queue.pause_job(job, PAUSE_NOW)
    assert leases.renew(unit["id"], THIRD) == "paused"
    assert leases.release(unit["id"], THIRD) == job and _copies(unit["id"]) == []
    assert _unit(unit["id"])["node"] == OTHER and _unit(unit["id"])["attempts"] == 1
    queue.resume_job(job)
    _double(FOURTH)
    queue.cancel_job(job)
    assert leases.renew(unit["id"], FOURTH) == "cancelled"
    assert leases.take_back(unit["id"], FOURTH) is None


def test_a_restart_keeps_copies_given_away_and_forgets_those_computed_here(later):
    _job(count=2)
    here = queue.claim_next({"cpu"}, HERE)                 # calculée ici
    away = _lend(OTHER)                                    # et ailleurs
    later()
    assert _double(THIRD)["id"] == here["id"]
    assert endgame.claim_local({"cpu"}, HERE)["id"] == away["id"]
    queue.recover()                                        # après un arrêt brutal
    # Celle d'ici : son doublon d'ailleurs prend sa place, sans interruption comptée
    mine = _unit(here["id"])
    assert mine["status"] == "running" and mine["node"] == THIRD
    assert _counts(here["id"]) == (1, 1, 0, 0)
    assert leases.renew(here["id"], THIRD) == "running"
    # Le doublon d'ici est oublié ; celle d'ailleurs garde sa machine
    assert _copies(away["id"]) == [] and _unit(away["id"])["node"] == OTHER


# ------------------------------------------------------------- chez un ami
def test_a_friend_gets_a_copy_only_under_the_rules_of_a_handout(owner, later):
    """Une bibliothèque qui ne lui est pas ouverte, une unité réservée à mes machines, le
    dernier essai : rien pour un ami. Son échec ne fait tomber que son doublon, qui compte
    dans ce qu'il détient."""
    peers.set_trust(B, sharing.FRIEND)
    m = owner["manager"]
    orders = [_lease(owner, node=C)["unit"] for _ in range(3)]
    later()
    assert _lease(owner)["unit"] is None                   # bibliothèque non partagée
    store.set_scope(owner["collection"], "friends")
    db.execute("UPDATE units SET reach='here,mine'")
    assert _lease(owner)["unit"] is None                   # réservée à mes machines
    db.execute("UPDATE units SET reach=NULL")
    db.execute("UPDATE units SET attempts=? WHERE id=?", (MAX_ATTEMPTS, orders[0]["id"]))
    copy = _lease(owner)["unit"]
    assert copy is not None and copy["id"] != orders[0]["id"]   # pas son dernier essai
    assert work._held_by(B) == 1
    assert work.fail(m, B, {"unit": copy["id"], "error": "panne"}) == {"ok": True}
    assert _copies(copy["id"]) == [] and _unit(copy["id"])["node"] == C
    assert _counts(copy["id"]) == (1, 1, 0, 0)
    again = _lease(owner)["unit"]
    assert again is None or again["id"] != copy["id"]      # rendue en échec : pas de nouveau


# ------------------------------------------------------- compteurs et vues
def test_the_interface_does_not_count_a_copy_as_one_more_unit(later):
    job = _job()
    unit = _lend(OTHER)
    later()
    _double(THIRD)
    summary = queue.job_summary(job)
    assert (summary["total"], summary["remote"], summary["local"]) == (1, {OTHER: 1}, 0)
    endgame.drop(unit["id"], THIRD)
    assert endgame.claim_local({"cpu"}, HERE)["id"] == unit["id"]   # doublon ici
    assert queue.job_summary(job)["local"] == 0 and queue.running_here() == 0
    assert [j["remote"] for j in queue.list_jobs() if j["id"] == job] == [{OTHER: 1}]
    assert _counts(unit["id"]) == (1, 1, 0, 0)


# ------------------------------------------------------- l'exécuteur d'ici
@pytest.fixture
def slow(monkeypatch):
    """Une tâche portable d'essai (processeur) qui dure tant que son arrêt n'est pas levé ou
    que l'essai ne la libère pas (gate), huit secondes au plus ; elle note son arrêt et ce
    qui entre."""
    seen = types.SimpleNamespace(stops=[], started=threading.Event(), gate=threading.Event(),
                                 assimilated=[])

    def compute(payload, file, options, stop):
        seen.stops.append(stop)
        seen.started.set()
        deadline = time.monotonic() + 8
        while not seen.gate.is_set() and time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise Interrupted()
            time.sleep(0.02)
        return {"fini": True}

    def assimilate(payload, output):
        seen.assimilated.append(output)
        return output
    monkeypatch.setitem(registry.TASKS, "fin_lente",
                        TaskSpec("fin_lente", compute, assimilate=assimilate))
    monkeypatch.setitem(registry.JOB_KINDS, "fin_lente", JobKind(
        "fin_lente", lambda params: ("Lente", [UnitSpec("fin_lente", "cpu", {})], {})))
    return seen


@pytest.fixture
def executor():
    """Un exécuteur d'ici (une place « cpu »), lancé à la demande, sa boucle asyncio dans un
    fil à part, comme dans le nœud."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="essai-fin-de-partie")
    thread.start()

    def run(coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(10)
    made = Executor(HERE, lambda event: None, slots={"cpu": 1})
    started = []

    def start():
        run(made.start())
        started.append(True)
        return made
    yield start
    if started:
        run(made.stop())
        wait_for(lambda: not made._running)
        run(asyncio.sleep(0.05))
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def test_a_copy_computed_here_gets_its_stop_when_the_unit_is_done_elsewhere(slow, executor,
                                                                            later):
    queue.create_job("fin_lente", {})
    unit = _lend(OTHER)
    later()
    here = executor()
    assert slow.started.wait(5)                            # le doublon calcule ici
    assert [(c["node"], c["lease_until"]) for c in _copies(unit["id"])] == [(HERE, None)]
    assert leases.take_back(unit["id"], OTHER) is not None  # l'autre machine finit d'abord
    queue.finish_unit(unit["id"], {"fini": True}, 2.0)
    here.wake()
    wait_for(lambda: slow.stops[0].is_set(), 4)
    wait_for(lambda: here.busy["cpu"] == 0 and not here._halts)
    assert slow.assimilated == []                          # rien n'entre deux fois
    done = _unit(unit["id"])
    assert done["status"] == "done" and done["node"] == OTHER
    assert _counts(unit["id"]) == (1, 1, 0, 0) and _copies(unit["id"]) == []


def test_a_copy_computed_here_and_interrupted_gives_back_only_itself(slow, executor, later):
    """Pause « tout de suite » : le doublon d'ici s'arrête et ne rend que lui-même ; l'unité
    reste à la machine qui la calcule, qui l'apprend à son renouvellement."""
    job = queue.create_job("fin_lente", {})
    unit = _lend(OTHER)
    later()
    here = executor()
    assert slow.started.wait(5)
    queue.pause_job(job, PAUSE_NOW)
    here.wake()
    wait_for(lambda: slow.stops[0].is_set(), 4)
    wait_for(lambda: here.busy["cpu"] == 0 and not here._halts)
    kept = _unit(unit["id"])
    assert kept["status"] == "running" and kept["node"] == OTHER and _copies(unit["id"]) == []
    assert _counts(unit["id"]) == (1, 1, 0, 0) and slow.assimilated == []
    assert leases.renew(unit["id"], OTHER) == "paused"


def test_a_copy_computed_here_that_finishes_first_wins(slow, executor, later):
    job = queue.create_job("fin_lente", {})
    unit = _lend(OTHER)
    later()
    here = executor()
    assert slow.started.wait(5)
    slow.gate.set()                                        # le doublon d'ici finit d'abord
    wait_for(lambda: _unit(unit["id"])["status"] == "done")
    done = _unit(unit["id"])
    assert done["node"] == HERE and done["place"] == "here"
    assert slow.assimilated == [{"fini": True}] and _counts(unit["id"]) == (1, 1, 0, 0)
    assert leases.renew(unit["id"], OTHER) == "done"       # l'autre machine arrête
    assert leases.take_back(unit["id"], OTHER) is None     # et son résultat est refusé
    wait_for(lambda: queue.job_summary(job)["status"] == "done")
    wait_for(lambda: not here._halts)
