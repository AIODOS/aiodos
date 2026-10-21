"""API cloud Mistral et Qwen (endpoints compatibles OpenAI).

⚠ Tout appel ici envoie le prompt hors de la machine : la route de chat refuse
de l'exécuter sans validation explicite de l'utilisateur (réglage cloud_confirm).
"""
import json

import requests

from aiodos import settings
from aiodos.i18n import t

PROVIDERS = {
    "mistral": {
        "label": "Mistral (cloud)",
        "url": "https://api.mistral.ai/v1/chat/completions",
        "host": "api.mistral.ai",
        "key": "mistral_key",
        "model": "mistral_cloud_model",
    },
    "qwen": {
        "label": "Qwen (cloud)",
        "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
        "host": "dashscope-intl.aliyuncs.com",
        "key": "qwen_key",
        "model": "qwen_cloud_model",
    },
}

MODEL_PREFIX = "cloud:"


def provider_of(model: str):
    """'cloud:mistral' → 'mistral' ; modèle local → None."""
    if model and model.startswith(MODEL_PREFIX):
        name = model[len(MODEL_PREFIX):]
        return name if name in PROVIDERS else None
    return None


def has_key(provider: str) -> bool:
    return bool(settings.get(PROVIDERS[provider]["key"]))


def chat_stream(provider, messages, stop=None):
    """Génère des couples ("content", texte), comme services.ollama.chat_stream."""
    spec = PROVIDERS[provider]
    key = settings.get(spec["key"])
    if not key:
        raise RuntimeError(t("cloud.no_key", name=spec["label"]))
    payload = {"model": settings.get(spec["model"]), "messages": messages, "stream": True}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with requests.post(spec["url"], json=payload, headers=headers,
                       stream=True, timeout=(10, 300)) as r:
        if r.status_code >= 400:
            raise RuntimeError(t("cloud.http_error", name=spec["label"], status=r.status_code,
                                 detail=r.text[:200]))
        for line in r.iter_lines(decode_unicode=True):
            if stop is not None and stop.is_set():
                break
            if not line:
                continue
            if line.startswith("data: "):
                line = line[6:]
            if line.strip() == "[DONE]":
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
            if delta:
                yield "content", delta
