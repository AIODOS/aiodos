"""Client de l'API Ollama locale. Fonctions bloquantes (réseau) : à appeler hors
de la boucle asyncio (thread de la file de tâches ou asyncio.to_thread)."""
import json
import threading

import requests

from coeur import settings

_caps_cache = {}
_caps_lock = threading.Lock()


def base() -> str:
    return (settings.get("ollama_url") or "http://localhost:11434").rstrip("/")


def version():
    try:
        return requests.get(base() + "/api/version", timeout=2).json().get("version")
    except Exception:
        return None


def list_models() -> list:
    r = requests.get(base() + "/api/tags", timeout=10)
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        details = m.get("details") or {}
        out.append({
            "name": m["name"],
            "size": m.get("size", 0),
            "family": details.get("family", ""),
            "parameters": details.get("parameter_size", ""),
            "quantization": details.get("quantization_level", ""),
        })
    return out


def loaded_models() -> list:
    """Modèles actuellement chargés en mémoire (et part en VRAM)."""
    try:
        r = requests.get(base() + "/api/ps", timeout=3)
        return [{"name": m["name"], "size": m.get("size", 0),
                 "size_vram": m.get("size_vram", 0)}
                for m in r.json().get("models", [])]
    except Exception:
        return []


def capabilities(model: str) -> list:
    """Capacités déclarées par Ollama : completion, vision, thinking, tools, embedding…"""
    with _caps_lock:
        if model in _caps_cache:
            return _caps_cache[model]
    try:
        r = requests.post(base() + "/api/show", json={"model": model}, timeout=10)
        caps = r.json().get("capabilities") or []
    except Exception:
        caps = []
    with _caps_lock:
        _caps_cache[model] = caps
    return caps


def chat_stream(model, messages, think=None, stop=None):
    """Génère des couples (genre, texte) : genre = "thinking" ou "content".

    think : None = comportement par défaut du modèle, False = pas de réflexion.
    stop  : threading.Event ; s'il est levé, on coupe le flux (bouton Stop).
    """
    payload = {"model": model, "messages": messages, "stream": True}
    if think is False and "thinking" in capabilities(model):
        payload["think"] = False
    with requests.post(base() + "/api/chat", json=payload,
                       stream=True, timeout=(10, 900)) as r:
        if r.status_code >= 400:
            raise RuntimeError(_error_text(r))
        for line in r.iter_lines(decode_unicode=True):
            if stop is not None and stop.is_set():
                break
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                raise RuntimeError(obj["error"])
            msg = obj.get("message") or {}
            if msg.get("thinking"):
                yield "thinking", msg["thinking"]
            if msg.get("content"):
                yield "content", msg["content"]
            if obj.get("done"):
                break


def embed(texts, model=None) -> list:
    """Vecteurs d'embedding d'une liste de textes (envoyés par lots)."""
    model = model or settings.get("embed_model")
    vectors = []
    for start in range(0, len(texts), 16):
        batch = texts[start:start + 16]
        r = requests.post(base() + "/api/embed",
                          json={"model": model, "input": batch}, timeout=180)
        if r.status_code >= 400:
            raise RuntimeError(_error_text(r))
        vectors.extend(r.json().get("embeddings") or [])
    if len(vectors) != len(texts):
        raise RuntimeError("Ollama n'a pas renvoyé un vecteur par texte.")
    return vectors


def generate_with_images(model, prompt, images_b64, timeout=300) -> str:
    """Appel ponctuel à un modèle de vision (OCR, description d'image)."""
    r = requests.post(base() + "/api/generate",
                      json={"model": model, "prompt": prompt,
                            "images": images_b64, "stream": False},
                      timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(_error_text(r))
    return (r.json().get("response") or "").strip()


def _error_text(response) -> str:
    try:
        return response.json().get("error") or response.text
    except ValueError:
        return f"HTTP {response.status_code}"
