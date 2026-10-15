"""Extraction de texte depuis les médias.

- Images : OCR (texte écrit) + description, via un modèle de vision Ollama.
- Audio / vidéo : transcription de la parole via faster-whisper (local, CPU).

Ces opérations sont lentes (quelques secondes par image, plus pour l'audio) et
nécessitent un modèle téléchargé. Appelées uniquement depuis un worker.
"""
import base64

import requests

from core import config, ollama_client

# ------------------------------------------------------------------ vision/OCR
def vision_model():
    return config.get("vision_model") or "qwen2.5vl:7b"


def vision_ready():
    try:
        return vision_model() in ollama_client.list_models()
    except Exception:
        return False


def vision_extract(image_path, mode="both"):
    """OCR + description d'une image via un modèle de vision local (Ollama)."""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    if mode == "ocr":
        prompt = ("Transcris fidèlement et intégralement TOUT le texte visible "
                  "dans cette image, sans rien ajouter. S'il n'y a aucun texte, "
                  "réponds exactement : (aucun texte).")
    elif mode == "describe":
        prompt = "Décris en détail, en français, le contenu visuel de cette image."
    else:
        prompt = ("En français : 1) transcris tout le texte visible dans l'image ; "
                  "2) puis décris brièvement son contenu visuel.")
    base = config.get("ollama_url").rstrip("/")
    r = requests.post(base + "/api/generate",
                      json={"model": vision_model(), "prompt": prompt,
                            "images": [b64], "stream": False},
                      timeout=300)
    r.raise_for_status()
    return r.json().get("response", "").strip()


# --------------------------------------------------------------- transcription
_WHISPER = None


def whisper_model_name():
    return config.get("whisper_model") or "small"


def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        # int8 sur CPU : bon compromis vitesse/qualité (le i9 est costaud)
        _WHISPER = WhisperModel(whisper_model_name(), device="cpu",
                                compute_type="int8")
    return _WHISPER


def transcribe(media_path):
    """Transcrit la parole d'un fichier audio ou vidéo en texte."""
    model = _get_whisper()
    segments, info = model.transcribe(str(media_path), vad_filter=True)
    parts = [seg.text.strip() for seg in segments]
    text = "\n".join(p for p in parts if p)
    lang = getattr(info, "language", "?")
    return f"[Transcription · langue détectée : {lang}]\n{text}"
