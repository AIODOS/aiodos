"""La transcription dans un programme à part (services/whisper_child.py) : priorité basse,
modèle gardé d'un fichier au suivant, cadence du travail de fond entre deux segments, arrêt
au segment suivant, erreur lisible, fermeture quand il ne sert plus. Avec un faux
faster_whisper (le vrai n'est ni chargé ni téléchargé)."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from aiodos.jobs.registry import Interrupted
from aiodos.node import priority
from aiodos.services import media

FAKE = '''
import json, os, subprocess, sys
from types import SimpleNamespace

# Comme de vraies bibliothèques à leur chargement : un programme lancé, qui hérite de
# l'entrée (sous Windows, bloqué si un autre fil attendait sur ce tuyau), et du texte
# écrit sur la sortie standard, par Python et directement (code natif)
subprocess.run([sys.executable, "-c", "pass"], timeout=30)
print("chargement du modèle…")
os.write(1, b"bruit du code natif" + bytes([10]))

def _log(entry):
    with open(os.environ["FAKE_WHISPER_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\\n")

class WhisperModel:
    def __init__(self, name, device, compute_type, cpu_threads):
        _log({"load": name, "threads": cpu_threads, "device": device})

    def transcribe(self, path, vad_filter):
        if "illisible" in path:
            raise ValueError("fichier illisible")
        if "long" in path:
            import time
            time.sleep(60)                       # un long fichier : la détection de voix

        def segments():
            for text in (" un ", "deux", "trois"):
                _log({"segment": text})
                yield SimpleNamespace(text=text)
        return segments(), SimpleNamespace(language="fr")
'''


@pytest.fixture
def whisper(tmp_path, monkeypatch):
    """Le programme de transcription, avec un faux faster_whisper ; ce qu'il fait est noté."""
    fake = tmp_path / "faux"
    fake.mkdir()
    (fake / "faster_whisper.py").write_text(FAKE, encoding="utf-8")
    log = tmp_path / "whisper.log"
    monkeypatch.setenv("PYTHONPATH", str(fake))
    monkeypatch.setenv("FAKE_WHISPER_LOG", str(log))
    media._child.stop()
    yield lambda: [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []
    media._child.stop()


def test_the_transcription_runs_apart_at_low_priority_and_is_paced(whisper):
    paces = []
    text = media.transcribe(Path("note.wav"), "small", threads=2, pace=paces.append)
    assert text == "[Transcription de note.wav · langue : fr]\nun\ndeux\ntrois"
    assert len(paces) == 3 and all(p >= 0 for p in paces)
    if sys.platform == "win32":
        state = priority.process_state(media._child.process.pid)
        assert state["priority_class"] == priority.BELOW_NORMAL_PRIORITY_CLASS
    first = media._child.process.pid
    media.transcribe(Path("note.wav"), "small", threads=2)      # même modèle, gardé
    media.transcribe(Path("note.wav"), "small")                 # secteur : fils par défaut
    assert media._child.process.pid == first
    assert [e for e in whisper() if "load" in e] == [
        {"load": "small", "threads": 2, "device": "cpu"},
        {"load": "small", "threads": 0, "device": "cpu"}]


def test_a_stop_ends_it_at_once(whisper):
    stop = threading.Event()
    text = media.transcribe(Path("note.wav"), "small", stop=stop,
                            pace=lambda seconds: stop.set())
    assert "trois" not in text and "trois" not in [e.get("segment") for e in whisper()]
    assert media.transcribe(Path("note.wav"), "small").endswith("trois")   # il sert encore


def test_a_stop_while_it_computes_does_not_wait_for_it(whisper):
    stop = threading.Event()
    threading.Timer(1.0, stop.set).start()
    began = time.monotonic()
    assert media.transcribe(Path("long.wav"), "small", stop=stop) == ""
    assert time.monotonic() - began < 15 and not media._child.alive()


def test_what_libraries_print_never_mixes_with_the_replies(whisper):
    """Le faux faster_whisper écrit sur la sortie standard en se chargeant : les
    transcriptions de deux fichiers restent chacune la sienne."""
    first = media.transcribe(Path("a.wav"), "small")
    second = media.transcribe(Path("b.wav"), "small")
    assert first.startswith("[Transcription de a.wav") and first.endswith("un\ndeux\ntrois")
    assert second.startswith("[Transcription de b.wav") and second.endswith("un\ndeux\ntrois")


def test_an_unreadable_file_is_reported_and_the_program_kept(whisper):
    with pytest.raises(media.TranscriptionError, match="fichier illisible"):
        media.transcribe(Path("illisible.wav"), "small")
    pid = media._child.process.pid
    assert media.transcribe(Path("note.wav"), "small").endswith("trois")
    assert media._child.process.pid == pid


def test_waiting_for_mains_stops_the_program(whisper):
    def interrupted(seconds):
        raise Interrupted()
    with pytest.raises(Interrupted):
        media.transcribe(Path("note.wav"), "small", pace=interrupted)
    assert not media._child.alive()
    assert [e["segment"] for e in whisper() if "segment" in e] == [" un "]
    assert media.transcribe(Path("note.wav"), "small").endswith("trois")    # relancé


def test_the_program_closes_when_idle_and_comes_back(whisper, monkeypatch):
    monkeypatch.setattr(media, "IDLE_S", 0.3)
    media.transcribe(Path("note.wav"), "small")
    first = media._child.process
    first.wait(10)                                   # inactif : sa mémoire est rendue
    deadline = time.monotonic() + 10
    while media._child.process is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert media._child.process is None
    assert media.transcribe(Path("note.wav"), "small").endswith("trois")
    assert media._child.process.pid != first.pid
    assert len([e for e in whisper() if "load" in e]) == 2


def test_a_closed_program_is_restarted_once(whisper):
    media.transcribe(Path("note.wav"), "small")
    media._child.process.kill()                       # fermé entre deux fichiers
    media._child.process.wait()
    began = time.monotonic()
    assert media.transcribe(Path("note.wav"), "small").endswith("trois")
    assert time.monotonic() - began < 30
