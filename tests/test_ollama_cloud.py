"""Les modèles « cloud » d'Ollama (calculés sur ses serveurs, « gpt-oss:120b-cloud ») ne sont
jamais proposés : AIODOS ne calcule que sur des machines (le cloud est retiré le 21/10/2026)."""
from aiodos import settings
from aiodos.services import ollama


class _Tags:
    def raise_for_status(self):
        pass

    def json(self):
        return {"models": [
            {"name": "qwen3.6:latest", "size": 5_000_000_000, "digest": "a",
             "details": {"family": "qwen3", "parameter_size": "8B"}},
            {"name": "gpt-oss:120b-cloud", "size": 384, "digest": "b",
             "remote_model": "gpt-oss:120b", "remote_host": "https://ollama.com:443"},
            {"name": "deepseek-v3.1:671b-cloud", "size": 384, "digest": "c"},
            {"name": "kimi-k2:cloud", "size": 384, "digest": "d"},
            {"name": "mistral:7b", "size": 4_000_000_000, "digest": "e"}]}


def test_ollama_cloud_models_are_never_offered(monkeypatch):
    settings.set_value("llm_engine", "ollama")
    monkeypatch.setattr(ollama.requests, "get", lambda *a, **k: _Tags())
    ollama._models_cache.update(at=0.0, models=None)
    try:
        names = [m["name"] for m in ollama.list_models(max_age=0)]
    finally:
        ollama._models_cache.update(at=0.0, models=None)
    assert names == ["qwen3.6:latest", "mistral:7b"]
    assert ollama.is_cloud({"name": "petit:1b", "remote_host": "https://ollama.com"})
    assert not ollama.is_cloud({"name": "cloudy-model:7b"})
