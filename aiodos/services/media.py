"""Médias → texte : images (OCR + description via un modèle de vision Ollama)
et audio/vidéo (transcription locale via faster-whisper)."""
import base64
import threading
import time

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


_whisper = {}
_whisper_lock = threading.Lock()
WHISPER_THREADS = 4               # fils de calcul de faster-whisper par défaut (cpu_threads=0)


def transcribe(path, model_name: str = "small", stop=None, threads=None, rest=None) -> str:
    """Transcrit la parole d'un fichier audio ou vidéo (une à la fois : le
    modèle Whisper occupe déjà plusieurs cœurs du processeur).

    stop    : threading.Event ; s'il est levé, la transcription s'arrête au segment
              suivant (le texte partiel renvoyé doit alors être ignoré par l'appelant) ;
    threads : fils de calcul (None : WHISPER_THREADS) ; un autre nombre recharge le modèle ;
    rest    : rest(secondes) après chaque segment, avec le temps qu'il a pris (cadence du
              travail de fond, cf. jobs/pacing.py)."""
    with _whisper_lock:
        key = (model_name, threads or 0)
        model = _whisper.get(key)
        if model is None:
            from faster_whisper import WhisperModel
            _whisper.clear()              # un seul modèle en mémoire (~500 Mo pour « small »)
            model = WhisperModel(model_name, device="cpu", compute_type="int8",
                                 cpu_threads=threads or 0)
            _whisper[key] = model
        segments, info = model.transcribe(str(path), vad_filter=True)
        lines = []
        began = time.monotonic()
        for segment in segments:          # générateur : le calcul avance segment par segment
            if stop is not None and stop.is_set():
                break
            if segment.text.strip():
                lines.append(segment.text.strip())
            if rest is not None:
                rest(time.monotonic() - began)
                began = time.monotonic()
        text = "\n".join(lines)
    language = getattr(info, "language", "?")
    return f"[Transcription de {path.name} · langue : {language}]\n{text}" if text else ""
