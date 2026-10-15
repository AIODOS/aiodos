"""Médias → texte : images (OCR + description via un modèle de vision Ollama)
et audio/vidéo (transcription locale via faster-whisper)."""
import base64
import threading

from coeur.services import ollama

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


def transcribe(path, model_name: str = "small") -> str:
    """Transcrit la parole d'un fichier audio ou vidéo (une à la fois : le
    modèle Whisper occupe déjà tous les cœurs du processeur)."""
    with _whisper_lock:
        model = _whisper.get(model_name)
        if model is None:
            from faster_whisper import WhisperModel
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _whisper[model_name] = model
        segments, info = model.transcribe(str(path), vad_filter=True)
        text = "\n".join(s.text.strip() for s in segments if s.text.strip())
    language = getattr(info, "language", "?")
    return f"[Transcription de {path.name} · langue : {language}]\n{text}" if text else ""
