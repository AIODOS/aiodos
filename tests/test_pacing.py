"""Cadence du travail de fond (jobs/pacing.py) : sur batterie, moins d'unités, moins de
fils de calcul et des pauses, ou rien du tout ; pendant une conversation, les lots
attendent ; à l'arrêt, plus rien n'attend. Batterie simulée (AIODOS_SIMULATE_POWER)."""
import threading
import time

import pytest

from aiodos import settings
from aiodos.jobs import pacing, registry
from aiodos.jobs.executor import Executor
from aiodos.services import media, ollama

SLOTS = {"gpu": 1, "embed": 2, "cpu": 2, "net": 3}


@pytest.fixture
def battery(monkeypatch):
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "battery")
    pacing.refresh(force=True)


def _mode(value):
    settings.set_value("battery_mode", value)
    return pacing.refresh(force=True)


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(pacing, "_sleep", lambda seconds, stop: slept.append(seconds))
    return slept


# --------------------------------------------------------------- régimes
def test_on_mains_nothing_is_limited():
    assert pacing.refresh(force=True)["mode"] == "ac"
    assert pacing.slots(SLOTS) == SLOTS
    assert pacing.threads(4) is None and ollama._threads["limit"] is None


def test_on_battery_eco_by_default(battery):
    assert pacing.current()["mode"] == "eco"
    assert pacing.slots(SLOTS) == {"gpu": 1, "embed": 1, "cpu": 1, "net": 1}
    assert pacing.slots({"embed": 0}) == {"embed": 0}          # --slots plus strict : gardé
    assert pacing.threads(4) == 2
    assert ollama._threads["limit"] == pacing.eco_threads() >= 1


def test_on_battery_pause_or_full_speed_by_choice(battery):
    assert _mode("pause")["mode"] == "pause"
    assert pacing.slots(SLOTS) == dict.fromkeys(SLOTS, 0)
    regime = _mode("normal")
    assert regime["mode"] == "normal" and pacing.slots(SLOTS) == SLOTS
    assert regime["threads"] is None and pacing.threads(4) is None
    assert _mode("turbo")["mode"] == "eco"                      # valeur inconnue : économe


def test_eco_rests_as_long_as_the_batch(battery, no_sleep):
    pacing.rest(0.8)
    pacing.rest(0)
    pacing.rest(500)
    assert no_sleep == [pytest.approx(0.8), pacing.MAX_REST]


def test_no_rest_on_mains_or_at_full_speed(no_sleep, monkeypatch):
    pacing.refresh(force=True)
    pacing.rest(0.8)
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "battery")
    _mode("normal")
    pacing.rest(0.8)
    assert no_sleep == []


# ---------------------------------------------------------- conversations
def test_a_batch_waits_for_the_end_of_a_conversation(fake_embed):
    pacing.set_interactive(1)
    done = threading.Event()
    threading.Thread(target=lambda: (pacing.embed(["a", "b"]), done.set()), daemon=True).start()
    assert not done.wait(0.6)                     # réponse en cours : le lot attend
    pacing.set_interactive(0)
    assert done.wait(5)


def test_the_executor_drives_the_wait():
    executor = Executor("n", lambda event: None)
    executor.interactive_begin()
    executor.interactive_begin()
    executor.interactive_end()
    assert pacing.chatting() and executor.status()["pacing"]["chatting"]
    executor.interactive_end()
    assert not pacing.chatting()


def test_a_unit_for_another_machine_is_released_when_stopped():
    stop = threading.Event()
    pacing.set_interactive(1)
    threading.Timer(0.3, stop.set).start()
    with pytest.raises(registry.Interrupted):
        pacing.wait_turn(stop)


# ------------------------------------------------------------- batterie
def test_in_pause_a_local_unit_waits_for_mains(battery, monkeypatch):
    _mode("pause")
    monkeypatch.setattr(pacing, "PAUSE_POLL", 0.05)
    finished = threading.Event()
    threading.Thread(target=lambda: (pacing.wait_turn(), finished.set()), daemon=True).start()
    assert not finished.wait(0.4)
    monkeypatch.setattr(pacing, "REGIME_TTL", 0)
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "ac")
    assert finished.wait(5)


def test_in_pause_a_unit_for_another_machine_is_given_back(battery):
    _mode("pause")
    with pytest.raises(registry.Interrupted):
        pacing.wait_turn(threading.Event())


def test_the_executor_caps_its_slots_on_battery(battery):
    executor = Executor("n", lambda event: None)
    executor.busy["embed"] = 1
    assert "embed" not in executor.free_resources() and "cpu" in executor.free_resources()
    status = executor.status()
    assert status["slots"]["embed"] == 1 and status["configured"]["embed"] == 2
    assert status["pacing"]["mode"] == "eco" and status["pacing"]["on_battery"]


def test_shutdown_frees_every_waiting_batch():
    pacing.set_interactive(1)
    outcome = []

    def background():
        try:
            pacing.wait_turn()
        except registry.Interrupted:
            outcome.append("rendue")
    waiting = threading.Thread(target=background)
    waiting.start()
    time.sleep(0.2)
    pacing.shutdown()
    waiting.join(5)
    assert outcome == ["rendue"]


# ------------------------------------------------- fils de calcul : Ollama
@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.setattr(ollama, "loaded_models", lambda: [
        {"name": "embeddinggemma:latest", "size": 1000, "size_vram": 0},       # processeur
        {"name": "bge-m3:latest", "size": 1000, "size_vram": 1000}])          # carte graphique
    ollama._threads["checked"].clear()
    ollama._threads["cpu"].clear()


def test_num_thread_only_for_a_model_on_the_processor(loaded):
    ollama.set_thread_limit(8)
    assert ollama.embed_options("embeddinggemma") == {"num_thread": 8}
    assert ollama.embed_options("bge-m3") == {}
    ollama.set_thread_limit(None)
    assert ollama.embed_options("embeddinggemma") == {}


def test_every_embedding_request_carries_it(loaded, monkeypatch):
    sent = []

    class Reply:
        status_code = 200

        def json(self):
            return {"embeddings": [[0.5]]}
    monkeypatch.setattr(ollama.requests, "post",
                        lambda url, json, timeout: sent.append(json) or Reply())
    ollama.set_thread_limit(8)
    ollama.embed(["question"], model="embeddinggemma")      # une conversation aussi :
    ollama.embed(["question"], model="bge-m3")              # pas de rechargement en alternance
    ollama.set_thread_limit(None)
    ollama.embed(["question"], model="embeddinggemma")
    assert [s.get("options") for s in sent] == [{"num_thread": 8}, None, None]


# ------------------------------------- calcul sur le processeur : pages, segments
def test_waiting_for_mains_stops_a_long_transcription_between_segments(battery, monkeypatch,
                                                                       tmp_path):
    """En « attendre le secteur », une longue vidéo s'arrête au segment suivant (une unité
    pour une autre machine lui est rendue ; une unité d'ici attendrait)."""
    from aiodos.jobs.kinds import index
    _mode("pause")
    done = []

    def transcribe(path, model_name, stop=None, threads=None, pace=None):
        for segment in range(3):
            pace(0.01)                          # après chaque segment : la cadence
            done.append(segment)
        return "texte"
    monkeypatch.setattr(media, "transcribe", transcribe)
    video = tmp_path / "cours.mp4"
    video.write_bytes(b"\x00")
    with pytest.raises(registry.Interrupted):
        index.compute_index_file({"path": str(video), "source": "cours.mp4"}, str(video),
                                 {"embed_model": "x", "vision": None, "whisper": "small"},
                                 threading.Event())
    assert done == []


def test_documents_are_paced_page_by_page(tmp_path):
    from aiodos.services import extract
    durations = []
    assert list(extract.paced(["p1", "p2", "p3"], durations.append)) == ["p1", "p2", "p3"]
    assert len(durations) == 3 and all(d >= 0 for d in durations)
    assert list(extract.paced(["seul"])) == ["seul"]               # sans cadence

    def stopped(seconds):
        raise registry.Interrupted()
    with pytest.raises(registry.Interrupted):
        list(extract.paced(["p1", "p2"], stopped))


def test_step_rests_on_battery_then_waits_for_mains(battery, no_sleep):
    pacing.step(0.4)                                       # « ménager » : une pause
    assert no_sleep == [pytest.approx(0.4)]
    _mode("pause")
    with pytest.raises(registry.Interrupted):              # pour une autre machine : rendue
        pacing.step(0.4, threading.Event())


def test_unknown_power_is_mains_on_a_pc_but_battery_on_a_phone(monkeypatch):
    monkeypatch.setattr(pacing.power, "read", lambda: {"plugged": None, "mobile": False})
    assert pacing.refresh(force=True)["mode"] == "ac"
    monkeypatch.setattr(pacing.power, "read", lambda: {"plugged": None, "mobile": True})
    assert pacing.refresh(force=True)["mode"] == "eco"


# --------------------------------------------- temps de calcul sans les attentes
def test_waiting_is_not_counted_as_computing(monkeypatch):
    """Réciprocité : le temps passé à attendre une conversation ou à se reposer ne compte
    pas comme du calcul donné (ni dans les statistiques d'une unité d'ici)."""
    from aiodos.jobs import executor

    def unit(task, payload):
        pacing._count_wait(time.monotonic() - 0.25)       # 0,25 s d'attente pendant l'unité
        return {"ok": True}
    monkeypatch.setattr(registry, "run_local", unit)
    result, waited = executor._run_unit("index_file", {})
    assert result == {"ok": True} and waited == pytest.approx(0.25, abs=0.05)
