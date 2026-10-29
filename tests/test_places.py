"""Les places de cette machine (node/places.py) : 60 % de sa puissance par défaut, réglable,
jamais toute (décision de l'auteur, 27/10/2026, docs/conception/essaim.md § 3)."""
import json

from aiodos import db, settings
from aiodos.jobs.executor import Executor
from aiodos.node import places


def test_places_follow_the_power_lent_and_the_processors():
    assert places.count(60, cpus=32) == {"gpu": 1, "embed": 2, "cpu": 4, "net": 3}
    assert places.count(100, cpus=32)["cpu"] == 8               # au plus MAX_CPU
    assert places.count(20, cpus=32) == {"gpu": 1, "embed": 1, "cpu": 1, "net": 3}
    assert places.count(60, cpus=8)["cpu"] == 1                 # un téléphone : une place
    assert places.count(60, cpus=2)["cpu"] == 1                 # jamais aucune


def test_the_power_lent_is_60_percent_by_default_and_bounded():
    assert places.share() == 60
    settings.set_value("lend_power", 5)
    places.forget()
    assert places.share() == places.MIN_SHARE
    settings.set_value("lend_power", "n'importe quoi")
    places.forget()
    assert places.share() == places.DEFAULT_SHARE


def test_the_setting_is_read_again_only_after_a_moment_or_when_forgotten(monkeypatch):
    assert places.share() == 60
    settings.set_value("lend_power", 80)
    assert places.share() == 60                                 # relu au plus toutes les 2 s
    places.forget()
    assert places.share() == 80


def test_no_new_unit_starts_when_memory_runs_short(monkeypatch):
    """Rien de nouveau quand il reste moins de la moitié de la part gardée (20 % à 60 %),
    sauf la lecture de pages ; mémoire illisible : comme avant."""
    executor = Executor("n", lambda event: None)
    monkeypatch.setenv(places.SIMULATE_MEMORY_ENV, "25")
    assert executor.free_resources() == {"gpu", "embed", "cpu", "net"}
    monkeypatch.setenv(places.SIMULATE_MEMORY_ENV, "15")
    assert executor.free_resources() == {"net"}
    settings.set_value("lend_power", 100)                       # tout prêté : plus de seuil
    places.forget()
    assert executor.free_resources() == {"gpu", "embed", "cpu", "net"}
    monkeypatch.delenv(places.SIMULATE_MEMORY_ENV)
    monkeypatch.setattr(places.identity, "memory", lambda: {"total": 0, "available": 0})
    places.forget()
    assert places.memory_free() is None and places.memory_room()


def test_the_executor_takes_its_places_from_the_power_lent(monkeypatch):
    monkeypatch.setattr(places.os, "cpu_count", lambda: 32)
    executor = Executor("n", lambda event: None, {"net": 1})
    assert executor.slots == {"gpu": 1, "embed": 2, "cpu": 4, "net": 1}   # --slots d'abord
    settings.set_value("lend_power", 20)
    places.forget()
    assert executor.status()["configured"] == {"gpu": 1, "embed": 1, "cpu": 1, "net": 1}
    executor.busy["cpu"] = 1
    assert "cpu" not in executor.free_resources()               # machine pleine : rien


def test_a_lending_machine_tells_its_places_and_how_many_are_taken(monkeypatch):
    """Dites à chaque demande de travail : de quoi suivre le chantier, et choisir la plus
    rapide qui a une place libre (essaim, § 3)."""
    from aiodos.network import worker
    executor = Executor("n", lambda event: None)
    executor.busy["embed"] = 1
    lender = worker.Worker(type("Manager", (), {"executor": executor})())
    monkeypatch.setattr(lender, "_caps", (worker.time.monotonic(), {"work_api": 1}))
    caps = lender.capabilities()
    assert caps["places"] == executor.slots and caps["busy"]["embed"] == 1
    executor.busy["embed"] = 2
    assert lender.capabilities()["busy"]["embed"] == 2          # relu à chaque demande


def test_a_lower_gpu_share_chosen_before_is_kept():
    """Migration 25 : « GPU prêté au maximum » plus bas que 100 % devient la puissance
    prêtée ; à 100 % (l'ancien défaut), les 60 % s'appliquent."""
    conn = db.connect()
    for value, expected in ((25, 25), (100, 60)):
        db.execute("DELETE FROM settings WHERE key IN ('share_max_gpu', 'lend_power')")
        db.execute("INSERT INTO settings(key, value) VALUES('share_max_gpu', ?)",
                   (json.dumps(value),))
        db.execute("PRAGMA user_version=24")
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        db.close()
        db._initialized.discard(path)
        conn = db.connect()
        assert db.query_one("SELECT 1 FROM settings WHERE key='share_max_gpu'") is None
        places.forget()
        assert places.share() == expected


def test_the_memory_of_aiodos_engines_counts_as_available(monkeypatch):
    """Le modèle chargé par AIODOS pour servir ne rend pas la machine « pleine » : la mémoire
    de ses moteurs (node/children.resident) compte comme disponible (PC de l'auteur,
    29/10/2026 : 8,7 Go chargés sur 32, 19 % disponibles, plus aucune place annoncée)."""
    gib = 2 ** 30
    monkeypatch.delenv(places.SIMULATE_MEMORY_ENV)
    monkeypatch.setattr(places.identity, "memory",
                        lambda: {"total": 32 * gib, "available": 6 * gib})
    monkeypatch.setattr(places.children, "resident", lambda: 0)
    places.forget()
    assert not places.memory_room()                     # 19 % : sous le seuil de 20 %
    monkeypatch.setattr(places.children, "resident", lambda: int(8.7 * gib))
    places.forget()
    assert places.memory_room() and round(places.memory_free(), 2) == 0.46
    monkeypatch.setattr(places.children, "resident", lambda: 40 * gib)    # jamais plus que tout
    places.forget()
    assert places.memory_free() == 1.0
