"""Médias → texte : images (OCR + description via un modèle de vision Ollama)
et audio/vidéo (transcription locale via faster-whisper)."""
import base64
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from aiodos.node import children, priority
from aiodos.services import ollama

_PROMPTS = {
    "ocr": ("Transcris fidèlement et intégralement TOUT le texte visible dans "
            "cette image, sans rien ajouter. S'il n'y a aucun texte, réponds "
            "exactement : (aucun texte)."),
    "describe": "Décris en détail, en français, le contenu visuel de cette image.",
    "both": ("En français : 1) transcris tout le texte visible dans l'image ; "
             "2) puis décris brièvement son contenu visuel."),
}


def vision_extract(path, model: str, mode: str = "both") -> str:
    with open(path, "rb") as f:
        image = base64.b64encode(f.read()).decode()
    text = ollama.generate_with_images(model, _PROMPTS.get(mode, _PROMPTS["both"]),
                                       [image])
    return f"[Image : {path.name}]\n{text}" if text else ""


WHISPER_THREADS = 4               # fils de calcul de faster-whisper par défaut (cpu_threads=0)
IDLE_S = 180                      # programme de transcription fermé après ce temps sans demande
REPLY_S = 3600                    # au-delà sans réponse (modèle à télécharger…), il est arrêté
CHILD = Path(__file__).with_name("whisper_child.py")
_whisper_lock = threading.Lock()  # une transcription à la fois


class TranscriptionError(RuntimeError):
    pass


class _Stopped(Exception):
    """Stop levé pendant que le programme calcule."""


class _Child:
    """Le programme de transcription (services/whisper_child.py), gardé d'un fichier au
    suivant ; fermé après IDLE_S sans demande (sa mémoire est rendue), relancé au besoin.
    Ses réponses sont lues par un fil à part : Stop, ou un programme qui ne répond plus,
    se voient sans attendre le segment suivant."""

    def __init__(self):
        self.process = None
        self.replies = None
        self.last = 0.0
        self.requests = 0

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self):
        flags = priority.CREATION_FLAGS | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, encoding="utf-8", bufsize=1, creationflags=flags)
        children.bind(self.process.pid)
        priority.lower_process(self.process.pid)
        self.last = time.monotonic()
        self.replies = queue.Queue()
        threading.Thread(target=self._read_all, args=(self.process, self.replies), daemon=True,
                         name="aiodos-transcription-lecture").start()
        threading.Thread(target=self._idle, args=(self.process,), daemon=True,
                         name="aiodos-transcription-veille").start()

    @staticmethod
    def _read_all(process, replies):
        try:
            for line in process.stdout:
                replies.put(line)
        except (OSError, ValueError):     # fermé pendant la lecture
            pass
        replies.put(None)

    def _idle(self, process):
        """Ferme le programme quand il n'a pas servi depuis IDLE_S (jamais pendant une
        transcription : elle tient le verrou)."""
        while process.poll() is None:
            time.sleep(min(5.0, IDLE_S / 4))
            if time.monotonic() - self.last < IDLE_S or not _whisper_lock.acquire(False):
                continue
            try:
                if self.process is process:
                    self.close()
            finally:
                _whisper_lock.release()
            return

    def send(self, line: str):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def read(self, rid: int, stop=None) -> dict:
        """La réponse suivante à la demande `rid`. _Stopped si `stop` est levé entre-temps ;
        TranscriptionError si le programme s'arrête, ne répond plus, ou répond autre chose
        (l'appelant l'arrête alors : on ne sait plus où il en est)."""
        deadline = time.monotonic() + REPLY_S
        while True:
            if stop is not None and stop.is_set():
                raise _Stopped()
            try:
                line = self.replies.get(timeout=0.5)
                break
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise TranscriptionError("Le programme de transcription ne répond plus.")
        if line is None:
            raise TranscriptionError("Le programme de transcription s'est arrêté en route.")
        try:
            reply = json.loads(line)
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or reply.get("id") != rid:
            raise TranscriptionError("Réponse inattendue du programme de transcription.")
        return reply

    def close(self):
        """Fin de son entrée : il s'arrête de lui-même (sinon, arrêté)."""
        if self.alive():
            try:
                self.process.stdin.close()
                self.process.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.stop()
        self.process = None

    def stop(self):
        """Arrêt immédiat (Stop pendant un calcul, état incertain) : relancé au besoin."""
        if self.alive():
            self.process.kill()
            self.process.wait()
        self.process = None


_child = _Child()


def command() -> list:
    """Python avec console plutôt que pythonw (dont les sorties sont perdues), sans fenêtre
    (CREATE_NO_WINDOW) ; -P : le dossier du programme n'entre pas dans le chemin des
    modules (ses voisins, media.py, web.py…, ne masquent aucune bibliothèque)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        exe = exe.with_name("python.exe")
    return [str(exe), "-P", str(CHILD)]


def transcribe(path, model_name: str = "small", stop=None, threads=None, pace=None) -> str:
    """Transcrit la parole d'un fichier audio ou vidéo, dans un programme à part en
    priorité basse (services/whisper_child.py), une à la fois : Whisper occupe déjà
    plusieurs cœurs du processeur.

    stop    : threading.Event ; s'il est levé, la transcription s'arrête aussitôt (le
              programme est arrêté s'il calcule ; le texte partiel renvoyé doit alors être
              ignoré par l'appelant) ;
    threads : fils de calcul (None : WHISPER_THREADS) ; un autre nombre recharge le modèle ;
    pace    : pace(secondes) après chaque segment, avec le temps qu'il a pris : la cadence
              du travail de fond (jobs/pacing.py : pause sur batterie, attente du secteur)
              passe avant le calcul du segment suivant."""
    lines, language = [], "?"
    with _whisper_lock:
        try:
            for attempt in (1, 2):
                _child.requests += 1
                rid = _child.requests
                try:
                    if not _child.alive():
                        _child.start()
                    _child.send(json.dumps({"id": rid, "path": str(path), "model": model_name,
                                            "threads": threads or 0}, ensure_ascii=False))
                    reply = _child.read(rid, stop)
                    break
                except (OSError, TranscriptionError):
                    # Fermé entre-temps (inactif depuis longtemps), ou réponse hors protocole :
                    # un programme neuf, une fois
                    _child.stop()
                    if attempt == 2:
                        raise
            began = time.monotonic()
            while not reply.get("end") and "error" not in reply:
                if "language" in reply:
                    language = reply["language"]
                elif "segment" in reply:
                    if str(reply["segment"]).strip():
                        lines.append(str(reply["segment"]).strip())
                    if stop is not None and stop.is_set():
                        _child.send("stop")          # il répond par la fin
                    else:
                        if pace is not None:
                            pace(time.monotonic() - began)
                        _child.send("next")
                    began = time.monotonic()
                reply = _child.read(rid, stop)
        except _Stopped:
            _child.stop()                  # Stop pendant un calcul : arrêté sur-le-champ
            reply = {}
        except BaseException:
            _child.stop()                  # état incertain : on repart d'un programme neuf
            raise
        _child.last = time.monotonic()
        if "error" in reply:               # il attend la demande suivante : on le garde
            raise TranscriptionError(f"Transcription impossible : {reply['error']}")
        text = "\n".join(lines)
    return f"[Transcription de {path.name} · langue : {language}]\n{text}" if text else ""

