"""Client minimal pour l'API Ollama locale (http://localhost:11434).

Toutes ces fonctions font du réseau : elles doivent être appelées depuis un
QThread (voir api/workers.py), jamais depuis le thread UI.
"""
import json
import requests

from core import config


def _base():
    return config.get("ollama_url").rstrip("/")


def is_up():
    """Vrai si le serveur Ollama répond."""
    try:
        requests.get(_base() + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


def list_models():
    """Liste les modèles installés (noms tels que 'mistral:7b')."""
    r = requests.get(_base() + "/api/tags", timeout=10)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


def chat_stream(model, messages, options=None):
    """Génère la réponse morceau par morceau (streaming JSONL d'Ollama).

    messages : liste de {"role": "system"|"user"|"assistant", "content": str}
    """
    payload = {"model": model, "messages": messages, "stream": True}
    if options:
        payload["options"] = options
    with requests.post(_base() + "/api/chat", json=payload,
                       stream=True, timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            chunk = obj.get("message", {}).get("content", "")
            if chunk:
                yield chunk
            if obj.get("done"):
                break


def embed(text, model=None):
    """Retourne le vecteur d'embedding d'un texte (pour la base de connaissance)."""
    model = model or config.get("embed_model")
    # API récente : /api/embed (champ "input")
    try:
        r = requests.post(_base() + "/api/embed",
                          json={"model": model, "input": text}, timeout=60)
        if r.status_code == 200:
            embs = r.json().get("embeddings")
            if embs:
                return embs[0]
    except Exception:
        pass
    # Fallback ancienne API : /api/embeddings (champ "prompt")
    r = requests.post(_base() + "/api/embeddings",
                      json={"model": model, "prompt": text}, timeout=60)
    r.raise_for_status()
    return r.json().get("embedding", [])
