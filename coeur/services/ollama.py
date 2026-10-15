"""Client de l'API Ollama locale. Fonctions bloquantes (réseau) : à appeler hors
de la boucle asyncio (thread de la file de tâches ou asyncio.to_thread)."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from coeur import settings

_caps_cache = {}
_caps_lock = threading.Lock()
# La liste des modèles change rarement : gardée quelques secondes plutôt que
# redemandée par chaque vue qui s'ouvre en même temps.
_models_cache = {"at": 0.0, "models": None}
MODELS_TTL = 10


def base() -> str:
    url = (settings.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    # Sous Windows, « localhost » se résout d'abord en IPv6 (::1) alors qu'Ollama
    # n'écoute qu'en IPv4 : chaque requête perdait ~2 s à attendre l'échec IPv6.
    return url.replace("://localhost", "://127.0.0.1")


def version():
    try:
        return requests.get(base() + "/api/version", timeout=2).json().get("version")
    except Exception:
        return None


def list_models(max_age=MODELS_TTL) -> list:
    """Modèles installés (copies : l'appelant peut les modifier sans toucher au cache)."""
    with _caps_lock:
        cached = _models_cache["models"]
        if cached is not None and time.monotonic() - _models_cache["at"] < max_age:
            return [dict(m) for m in cached]
    r = requests.get(base() + "/api/tags", timeout=15)
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        details = m.get("details") or {}
        out.append({
            "name": m["name"],
            "digest": m.get("digest", ""),   # identifie la version exacte du modèle
            "size": m.get("size", 0),
            "family": details.get("family", ""),
            "parameters": details.get("parameter_size", ""),
            "quantization": details.get("quantization_level", ""),
        })
    with _caps_lock:
        _models_cache.update(at=time.monotonic(), models=out)
    return [dict(m) for m in out]


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


def capabilities_many(models) -> dict:
    """Capacités de plusieurs modèles, demandées en parallèle (/api/show peut
    prendre 2 s sur un gros modèle la première fois)."""
    with _caps_lock:
        missing = [m for m in models if m not in _caps_cache]
    if missing:
        with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
            list(pool.map(capabilities, missing))
    return {m: capabilities(m) for m in models}


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
