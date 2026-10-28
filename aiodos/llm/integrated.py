"""Le moteur intégré vu du reste d'AIODOS : les mêmes fonctions que services/ollama.py
(lister, converser en flux, embeddings, images, capacités, décharger), servies par le
routeur de llama.cpp (llm/engine.py) au lieu d'Ollama. services/ollama.py choisit l'un ou
l'autre ; les appels du reste d'AIODOS ne changent pas. La passerelle vers les autres outils
(gateway/engine.py) leur transmet en plus ses demandes telles quelles (forward).

Un modèle inutilisé depuis IDLE_UNLOAD secondes est déchargé, comme chez Ollama : la
mémoire de la carte graphique revient aux autres (images, jeux). Un modèle que llama.cpp
ne sait pas charger est écarté pour de bon (llm/models.py), avec un message clair.
"""
import json
import logging
import math
import threading
import time

import requests

from aiodos.cluster import engine as programs
from aiodos.i18n import t
from aiodos.llm.messages import SAMPLING, for_template
from aiodos.llm import download, engine, models as registry

log = logging.getLogger("aiodos.llm")

IDLE_UNLOAD = 300                  # secondes sans usage avant de décharger un modèle
BATCH = 16                         # textes par demande d'embeddings
AVAILABLE_TTL = 30
NO_PROXY = engine.NO_PROXY
_router = engine.Router()
_last_use = {}                     # identifiant -> instant (monotonic)
_state = {"available_at": 0.0, "available": False, "janitor": None}
_lock = threading.Lock()


def available() -> bool:
    """llama.cpp est-il là ? (relu au plus toutes les 30 s)"""
    now = time.monotonic()
    if now - _state["available_at"] > AVAILABLE_TTL:
        _state.update(available_at=now, available=programs.folder() is not None)
    return _state["available"]


def version():
    info = programs.info()
    return f"llama.cpp {info['version']}" if info and info.get("version") else "llama.cpp"


def _model(name) -> registry.Model:
    model = registry.find(name)
    if model is None:
        raise RuntimeError(t("llm.model_missing", name=name))
    return model


def _used(model_id: str):
    with _lock:
        _last_use[model_id] = time.monotonic()
        if _state["janitor"] is None:
            _state["janitor"] = threading.Thread(target=_janitor, daemon=True,
                                                 name="aiodos-moteur-menage")
            _state["janitor"].start()


def _janitor():
    """Décharge les modèles inutilisés depuis IDLE_UNLOAD secondes."""
    while True:
        time.sleep(30)
        now = time.monotonic()
        with _lock:
            idle = [i for i, at in _last_use.items() if now - at > IDLE_UNLOAD]
        loaded = _router.status()
        for model_id in idle:
            if loaded.get(model_id) == "loaded" and _router.unload(model_id):
                log.info("Moteur intégré : « %s » déchargé (inutilisé)", model_id)
            with _lock:
                _last_use.pop(model_id, None)


class Incompatible(RuntimeError):
    """Un modèle que llama.cpp ne sait pas charger : écarté pour de bon."""


def _url(model: registry.Model) -> str:
    """L'adresse qui sert ce modèle (le routeur, lancé s'il le faut) ; un modèle que
    llama.cpp ne sait pas charger est écarté (Incompatible)."""
    try:
        url = _router.url(model.id)
    except engine.EngineUnavailable as exc:
        # llama-server seul (llm/direct.py) : le modèle se charge au lancement du serveur
        if "failed to load" in str(exc):
            registry.mark_incompatible(model, str(exc))
            raise Incompatible(t("llm.model_incompatible", name=model.name)) from exc
        raise
    _used(model.id)
    return url


def endpoint(name) -> tuple:
    """(adresse, en-têtes, identifiant) qui servent ce modèle de conversation, pour une
    demande que ce module ne fait pas lui-même (llm/reading.py : une lecture imposée) ; à
    signaler finie par touch(identifiant). RuntimeError s'il n'est pas sur cette machine ou
    n'est pas un modèle de conversation, Incompatible s'il ne se charge pas."""
    model = _model(name)
    if model.embedding:
        raise RuntimeError(t("llm.model_missing", name=name))
    return _url(model), dict(_router.headers), model.id


def touch(model_id: str):
    """Ce modèle vient de servir (le ménage ne le décharge pas tout de suite)."""
    _used(model_id)


def _send(model: registry.Model, path: str, body: dict, stream=False, timeout=(10, 900)):
    """Requête au routeur, sa réponse rendue même en erreur ; un modèle que llama.cpp ne
    sait pas charger est écarté (Incompatible)."""
    url = _url(model)
    response = requests.post(url + path, json={**body, "model": model.id}, stream=stream,
                             headers=_router.headers, timeout=timeout, proxies=NO_PROXY)
    if response.status_code >= 400:
        text = _error_text(response)
        if "failed to load" in text:
            response.close()
            registry.mark_incompatible(model, text)
            raise Incompatible(t("llm.model_incompatible", name=model.name))
    return response


def _post(model: registry.Model, path: str, body: dict, stream=False, timeout=(10, 900)):
    """Requête au routeur ; une erreur devient RuntimeError."""
    response = _send(model, path, body, stream=stream, timeout=timeout)
    if response.status_code >= 400:
        text = _error_text(response)
        response.close()
        raise RuntimeError(t("llm.engine_error", error=text))
    return response


# ----------------------------------------------------------------- comme Ollama
def list_models(max_age=registry.CONFIG_TTL) -> list:
    return [{"name": m.name, "digest": m.digest, "size": m.size, "family": m.family,
             "parameters": m.parameters, "quantization": m.quantization}
            for m in registry.available(max_age)]


def loaded_models() -> list:
    """Modèles chargés en ce moment (llama.cpp place lui-même les couches : la part en
    mémoire de la carte n'est pas connue ici, on suppose le modèle sur la carte)."""
    states = _router.status()
    return [{"name": m.name, "size": m.size, "size_vram": m.size}
            for m in registry.available() if states.get(m.id) == "loaded"]


def capabilities(model: str) -> list:
    found = registry.find(model)
    if found is None:
        return []
    caps = ["embedding"] if found.embedding else ["completion"]
    if found.mmproj:
        caps.append("vision")
    if not found.embedding and "think" in found.template:
        caps.append("thinking")
    if not found.embedding and "tool" in found.template:
        caps.append("tools")
    return caps


def capabilities_many(names) -> dict:
    return {name: capabilities(name) for name in names}


def chat_stream(model, messages, think=None, stop=None, options=None, stats=False,
                timeout=(10, 900)):
    """Couples (genre, texte) comme services/ollama.chat_stream : « thinking », « content »,
    et à la fin (« stats », {...}) si demandé."""
    found = _model(model)
    body = {"messages": for_template(messages), "stream": True}
    if think is False:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    for key, value in (options or {}).items():
        if key in SAMPLING:
            body[SAMPLING[key]] = value
    if stats:
        body["stream_options"] = {"include_usage": True}
    finish = "stop"
    with _post(found, "/v1/chat/completions", body, stream=True, timeout=timeout) as response:
        # llama-server ne dit pas l'encodage de son flux : sans cela, requests le lirait en
        # Latin-1 (« entrÃ©e »)
        response.encoding = "utf-8"
        for line in response.iter_lines(decode_unicode=True):
            if stop is not None and stop.is_set():
                break
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("error"):
                raise RuntimeError(t("llm.engine_error", error=chunk["error"]))
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            usage = chunk.get("usage")
            if stats and isinstance(usage, dict):
                yield "stats", {"prompt_tokens": usage.get("prompt_tokens") or 0,
                                "completion_tokens": usage.get("completion_tokens") or 0,
                                "done_reason": finish}
            if delta.get("reasoning_content"):
                yield "thinking", delta["reasoning_content"]
            if delta.get("content"):
                yield "content", delta["content"]
    _used(found.id)


def embed(texts, model=None, timeout=180) -> list:
    """Vecteurs normés (longueur 1, comme ceux d'Ollama), par lots."""
    from aiodos import settings
    found = _model(model or settings.get("embed_model"))
    if not found.embedding:
        raise RuntimeError(t("llm.not_embedding", name=found.name))
    vectors = []
    for start in range(0, len(texts), BATCH):
        batch = list(texts[start:start + BATCH])
        with _post(found, "/v1/embeddings", {"input": batch}, timeout=timeout) as response:
            data = sorted(response.json().get("data") or [], key=lambda d: d.get("index", 0))
        vectors.extend(_unit(d.get("embedding") or []) for d in data)
    if len(vectors) != len(texts):
        raise RuntimeError(t("llm.vectors_mismatch"))
    return vectors


def _unit(vector) -> list:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def vision_model(wanted: str | None) -> registry.Model:
    """Le modèle qui lit les images : celui du réglage s'il les voit, sinon le plus
    capable des modèles installés qui les voient et tiennent dans 8 Go (qwen2.5vl d'Ollama,
    par exemple, ne se charge pas : qwen3.5 le remplace)."""
    found = registry.find(wanted) if wanted else None
    if found is not None and found.mmproj:
        return found
    seeing = sorted((m for m in registry.available() if m.mmproj and not m.embedding),
                    key=lambda m: m.size)
    if not seeing:
        raise RuntimeError(t("llm.no_vision_model"))
    fitting = [m for m in seeing if m.size <= 8e9]
    return fitting[-1] if fitting else seeing[0]


def generate_with_images(model, prompt, images_b64, timeout=300) -> str:
    """Un modèle qui voit les images (projecteur) : lecture, description."""
    found = vision_model(model)
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        for b64 in images_b64]
    body = {"messages": [{"role": "user", "content": content}], "stream": False,
            "chat_template_kwargs": {"enable_thinking": False}}
    with _post(found, "/v1/chat/completions", body, timeout=(10, timeout)) as response:
        message = ((response.json().get("choices") or [{}])[0].get("message") or {})
    return (message.get("content") or "").strip()


def unload_chat_models() -> list:
    """Décharge les modèles de conversation (la carte graphique va aux images)."""
    states, names = _router.status(), []
    for m in registry.available():
        if not m.embedding and states.get(m.id) == "loaded" and _router.unload(m.id):
            names.append(m.name)
    return names


def forward(name, path: str, body: dict, stream=False, timeout=(10, 900)):
    """Demande d'un autre outil transmise au routeur pour ce modèle (gateway/engine.py) : son
    API est déjà celle d'OpenAI. « model » devient l'identifiant du routeur ; la réponse de
    llama-server est rendue telle quelle, erreurs comprises. RuntimeError si le modèle n'est
    pas sur cette machine, Incompatible s'il ne se charge pas."""
    return _send(_model(name), path, body, stream=stream, timeout=timeout)


def unload(name) -> bool:
    """Décharge ce modèle s'il est chargé (un autre outil le demande : keep_alive 0)."""
    found = registry.find(name)
    return found is not None and _router.unload(found.id)


def pull(name, progress=None, stop=None) -> bool:
    """Comme ollama.pull, depuis Hugging Face (llm/download.py) ; le routeur repartira
    avec le nouveau modèle à la prochaine demande."""
    return download.pull(name, progress=progress, stop=stop)


def stop():
    _router.stop()


def _error_text(response) -> str:
    try:
        error = response.json().get("error")
        return (error.get("message") if isinstance(error, dict) else error) or response.text
    except ValueError:
        return f"HTTP {response.status_code}"
