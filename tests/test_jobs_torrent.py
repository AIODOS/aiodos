"""La file comme un torrent (docs/conception/partage-puissance.md § 2.5 et 2.6, livraison
L2a) : pause « après l'unité en cours » ou « tout de suite », du travail ou de toute la file ;
annulation qui atteint les autres machines ; reprise sans perte après un redémarrage ;
compteurs d'interruptions, de disparitions et de remises ; équité entre applis ; cercles."""
from aiodos import db, i18n
from aiodos.jobs import leases, queue, registry
from aiodos.jobs.models import (MAX_HANDOUTS, MAX_INTERRUPTS, NORMAL, PAUSE_AFTER, PAUSE_NOW,
                                RECOVER_GRACE)
from aiodos.jobs.registry import UnitSpec

OTHER = "machine-portable"
FRIEND = "machine-ami"


def _split(params):
    return f"torrent {params['name']}", [UnitSpec("torrent_task", "cpu", {"n": i})
                                         for i in range(params["count"])], {}


registry.register_job_kind("torrent_kind", _split)
registry.register_portable("torrent_task", compute=lambda payload, file, options, stop: {},
                           assimilate=lambda payload, output: output)


def _job(name="lot", count=2, owner_app=None, reach=None):
    return queue.create_job("torrent_kind", {"name": name, "count": count}, NORMAL,
                            owner_app=owner_app, reach=reach)


def _unit(unit_id) -> dict:
    return dict(db.query_one("SELECT * FROM units WHERE id=?", (unit_id,)))


def _job_row(job_id) -> dict:
    return dict(db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,)))


def _lend(node=OTHER, circle="mine"):
    return leases.claim_remote({"cpu"}, node, lambda unit: True, circle=circle)


# ------------------------------------------------------------------- pauses
def test_pause_after_the_current_unit_lets_it_finish():
    job = _job(count=2)
    first = queue.claim_next({"cpu"}, "ici")
    queue.pause_job(job)                                   # « après » par défaut
    assert queue.claim_next({"cpu"}, "ici") is None        # plus rien ne part
    assert queue.halted_now({job}) == set()                # l'unité en cours continue
    queue.finish_unit(first["id"], {}, 1.0)
    queue.resume_job(job)
    assert queue.claim_next({"cpu"}, "ici")["job_id"] == job
    assert _job_row(job)["pause_mode"] is None


def test_pause_now_stops_here_and_elsewhere_without_costing_an_attempt():
    job = _job(count=2)
    here = queue.claim_next({"cpu"}, "ici")
    away = _lend()
    queue.pause_job(job, PAUSE_NOW)
    assert queue.halted_now({job}) == {job}                # l'exécuteur lève son arrêt
    queue.release_unit(here["id"])                         # Interrupted → rendue
    assert _unit(here["id"])["status"] == "queued" and _unit(here["id"])["attempts"] == 0
    assert leases.renew(away["id"], OTHER) == "paused"     # l'autre machine arrête…
    assert leases.release(away["id"], OTHER) == job        # … et rend l'unité, sans essai
    assert _unit(away["id"])["attempts"] == 0
    queue.resume_job(job)
    assert queue.claim_next({"cpu"}, "ici") is not None


def test_the_whole_queue_paused_and_resumed():
    job = _job(count=2)
    queue.pause_all(PAUSE_AFTER)
    assert queue.claim_next({"cpu"}, "ici") is None and _lend() is None
    assert leases.local_demand({"cpu"}) == set()           # ma file en pause : je prête
    queue.pause_all(PAUSE_NOW)
    assert queue.halted_now({job}) == {job}
    queue.resume_all()
    assert queue.claim_next({"cpu"}, "ici")["job_id"] == job


# -------------------------------------------------------------- annulation
def test_cancel_reaches_the_other_machine_and_refuses_its_result():
    job = _job(count=3)
    here = queue.claim_next({"cpu"}, "ici")
    away = _lend()
    queue.cancel_job(job, by="app")
    assert leases.renew(away["id"], OTHER) == "cancelled"
    assert leases.take_back(away["id"], OTHER) is None     # son résultat est refusé
    assert _unit(away["id"])["status"] == "cancelled"
    assert queue.halted_now({job}) == {job}
    queue.release_unit(here["id"])                         # l'unité d'ici, interrompue…
    assert _unit(here["id"])["status"] == "cancelled"      # … ne revient pas en file
    assert _job_row(job)["cancelled_by"] == "app"


# ------------------------------------------------------------ redémarrage
def test_a_restart_costs_no_attempt_and_keeps_units_given_away():
    job = _job(count=2)
    here = queue.claim_next({"cpu"}, "ici")
    away = _lend()
    queue.recover()                                        # après un arrêt brutal
    mine = _unit(here["id"])
    assert mine["status"] == "queued" and mine["attempts"] == 0 and mine["interrupts"] == 1
    kept = _unit(away["id"])
    assert kept["status"] == "running" and kept["node"] == OTHER
    assert kept["lease_until"] >= db.now() + RECOVER_GRACE - 5   # bail de grâce
    assert leases.renew(away["id"], OTHER) == "running"    # elle se raccroche
    assert queue.job_summary(job)["status"] == "running"


def test_a_unit_that_keeps_crashing_aiodos_ends_up_counting():
    _job(count=1)
    unit = queue.claim_next({"cpu"}, "ici")
    db.execute("UPDATE units SET interrupts=? WHERE id=?", (MAX_INTERRUPTS, unit["id"]))
    queue.interrupt_units([unit["id"]])
    assert _unit(unit["id"])["attempts"] == 1              # cette fois, l'essai compte


def test_a_unit_nobody_finishes_fails():
    job = _job(count=1)
    unit = queue.claim_next({"cpu"}, "ici")
    db.execute("UPDATE units SET handouts=? WHERE id=?", (MAX_HANDOUTS, unit["id"]))
    assert queue.release_unit(unit["id"]) == (job, True)
    assert _unit(unit["id"])["status"] == "failed"
    assert _unit(unit["id"])["error"] == i18n.t("jobs.no_progress")


def test_each_handout_is_counted():
    _job(count=1)
    unit = queue.claim_next({"cpu"}, "ici")
    queue.release_unit(unit["id"])
    _lend()
    assert _unit(unit["id"])["handouts"] == 2


# --------------------------------------------------------- équité, cercles
def test_an_app_with_units_running_waits_for_the_others():
    big = _job("gros lot", count=5, owner_app="pandora")
    queue.claim_next({"cpu"}, "ici")                       # PANDORA a une unité en cours
    small = _job("petit", count=1, owner_app="arklight")
    assert queue.claim_next({"cpu"}, "ici")["job_id"] == small
    assert queue.claim_next({"cpu"}, "ici")["job_id"] == big


def test_units_only_go_where_they_may():
    mine_only = _job("chez moi", count=1, reach="mine")
    assert queue.claim_next({"cpu"}, "ici") is None        # pas ce PC (« mine » sans « here »)
    assert _lend(FRIEND, circle="friends") is None         # ni un ami
    assert _lend(OTHER, circle="mine")["job_id"] == mine_only


def test_a_friend_keeps_the_audited_rules():
    """Chez un ami, rien ne change (audit du 20/10) : un bail échu compte l'essai, et l'unité
    ne se raccroche pas à lui."""
    _job(count=1)
    unit = _lend(FRIEND, circle="friends")
    db.execute("UPDATE units SET lease_until=1 WHERE id=?", (unit["id"],))
    leases.expire()
    assert _unit(unit["id"])["attempts"] == 1 and _unit(unit["id"])["status"] == "queued"
    assert leases.renew(unit["id"], FRIEND) == "reassigned"
