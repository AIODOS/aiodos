"""Connexion aux API cloud Mistral et Qwen (endpoints compatibles OpenAI).

⚠ Sécurité : tout appel ici envoie le prompt dans le cloud. L'appelant DOIT
demander une validation explicite à l'utilisateur avant (voir cloud_confirm).
"""
import json

import requests

from core import config

ENDPOINTS = {
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    # DashScope international (compatible OpenAI) pour Qwen
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
}

HOSTS = {
    "mistral": "api.mistral.ai",
    "qwen": "dashscope-intl.aliyuncs.com",
}


def host(provider):
    return HOSTS.get(provider, "?")


def _key_and_model(provider):
    if provider == "mistral":
        return config.get("mistral_key"), config.get("mistral_cloud_model")
    return config.get("qwen_key"), config.get("qwen_cloud_model")


def has_key(provider):
    key, _ = _key_and_model(provider)
    return bool(key)


def cloud_chat_stream(provider, messages):
    """Streame la réponse d'un modèle cloud (SSE compatible OpenAI)."""
    key, model = _key_and_model(provider)
    if not key:
        raise RuntimeError(
            f"Clé API manquante pour {provider}. "
            "Renseigne-la dans Réglages cloud.")
    url = ENDPOINTS[provider]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "stream": True}
    with requests.post(url, json=payload, headers=headers,
                       stream=True, timeout=180) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data: "):
                line = line[6:]
            if line.strip() == "[DONE]":
                break
            try:
                obj = json.loads(line)
            except Exception:
                continue
            choices = obj.get("choices") or [{}]
            delta = choices[0].get("delta", {}).get("content", "")
            if delta:
                yield delta
