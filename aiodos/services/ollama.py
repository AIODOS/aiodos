"""Les modèles de cette machine : le moteur intégré (llama.cpp, aiodos/llm/) quand il est
là, sinon l'API d'Ollama. Décision de l'auteur (20/10/2026) : se passer d'Ollama ; ce module
reste l'unique porte d'entrée, le reste d'AIODOS n'a pas à savoir qui répond. Réglage
« llm_engine » : auto (le moteur intégré dès que llama.cpp est là), integrated, ollama.

Fonctions bloquantes (réseau) : à appeler hors de la boucle asyncio (thread de la file de
tâches ou asyncio.to_thread)."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from aiodos import settings
from aiodos.llm import integrated

_caps_cache = {}
_caps_lock = threading.Lock()
# La liste des modèles change rarement : gardée quelques secondes plutôt que
# redemandée par chaque vue qui s'ouvre en même temps.
_models_cache = {"at": 0.0, "models": None}
MODELS_TTL = 10
# Fils de calcul demandés pour les embeddings sur batterie (cf. jobs/pacing.py), seulement
# si le modèle tourne sur le processeur : sur la carte graphique, num_thread ne change ni
# le débit ni la charge (mesuré), et chaque valeur différente fait recharger le modèle.
# D'où une seule valeur à la fois pour tous les embeddings, conversations comprises.
_threads = {"limit": None, "cpu": set(), "checked": {}}
LOADED_TTL = 30


def use_integrated() -> bool:
    """Le moteur intégré répond-il ? (réglage llm_engine, et llama.cpp présent)"""
    choice = str(settings.get("llm_engine") or "auto")
    if choice == "ollama":
        return False
    return choice == "integrated" or integrated.available()


def base() -> str:
    url = (settings.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    # Sous Windows, « localhost » se résout d'abord en IPv6 (::1) alors qu'Ollama
    # n'écoute qu'en IPv4 : chaque requête perdait ~2 s à attendre l'échec IPv6.
    return url.replace("://localhost", "://127.0.0.1")


def version():
    if use_integrated():
        return integrated.version()
    try:
        return requests.get(base() + "/api/version", timeout=2).json().get("version")
    except Exception:
        return None


def list_models(max_age=MODELS_TTL) -> list:
    """Modèles installés (copies : l'appelant peut les modifier sans toucher au cache)."""
    if use_integrated():
        return integrated.list_models(max_age)
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
    if use_integrated():
        return integrated.loaded_models()
    try:
        r = requests.get(base() + "/api/ps", timeout=3)
        return [{"name": m["name"], "size": m.get("size", 0),
                 "size_vram": m.get("size_vram", 0)}
                for m in r.json().get("models", [])]
    except Exception:
        return []


def pull(model: str, progress=None, stop=None, timeout=(10, 600)) -> bool:
    """Télécharge un modèle (POST /api/pull, en flux). progress(reçu, total, étape) à
    chaque nouvelle : octets de toutes les couches. Vrai une fois installé ; faux si
    `stop` est levé en route ; RuntimeError si Ollama refuse."""
    if use_integrated():
        return integrated.pull(model, progress=progress, stop=stop)
    layers = {}
    with requests.post(base() + "/api/pull", json={"model": model, "stream": True},
                       stream=True, timeout=timeout) as r:
        if r.status_code >= 400:
            raise RuntimeError(_error_text(r))
        for line in r.iter_lines(decode_unicode=True):
            if stop is not None and stop.is_set():
                return False
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                raise RuntimeError(obj["error"])
            if obj.get("digest") and obj.get("total"):
                layers[obj["digest"]] = (obj.get("completed") or 0, obj["total"])
            if progress is not None:
                progress(sum(c for c, _ in layers.values()), sum(t for _, t in layers.values()),
                         str(obj.get("status") or ""))
            if obj.get("status") == "success":
                return True
    return False


def unload_chat_models() -> list:
    """Décharge de la carte graphique les modèles de conversation (keep_alive 0) : sa
    mémoire va à un autre moteur (images). Les modèles d'embedding, petits, restent pour
    l'indexation ; la prochaine conversation recharge son modèle. → noms déchargés."""
    if use_integrated():
        return integrated.unload_chat_models()
    names = []
    for m in loaded_models():
        if not m["size_vram"] or "completion" not in capabilities(m["name"]):
            continue
        try:
            requests.post(base() + "/api/generate", json={"model": m["name"], "keep_alive": 0},
                          timeout=15)
            names.append(m["name"])
        except requests.RequestException:
            pass
    return names


def capabilities(model: str) -> list:
    """Capacités déclarées par Ollama : completion, vision, thinking, tools, embedding…"""
    if use_integrated():
        return integrated.capabilities(model)
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
    if use_integrated():
        return integrated.capabilities_many(models)
    with _caps_lock:
        missing = [m for m in models if m not in _caps_cache]
    if missing:
        with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
            list(pool.map(capabilities, missing))
    return {m: capabilities(m) for m in models}


def chat_stream(model, messages, think=None, stop=None, options=None, stats=False,
                timeout=(10, 900)):
    """Génère des couples (genre, texte) : genre = "thinking" ou "content".

    think   : None = comportement par défaut du modèle, False = pas de réflexion.
    stop    : threading.Event ; s'il est levé, on coupe le flux (bouton Stop).
    options : réglages d'échantillonnage d'Ollama (temperature, num_predict…).
    stats   : à la fin, un couple ("stats", {"prompt_tokens", "completion_tokens",
              "done_reason"}) en plus (passerelle vers les autres outils).
    """
    if use_integrated():
        yield from integrated.chat_stream(model, messages, think=think, stop=stop,
                                          options=options, stats=stats, timeout=timeout)
        return
    payload = {"model": model, "messages": messages, "stream": True}
    if think is False and "thinking" in capabilities(model):
        payload["think"] = False
    if options:
        payload["options"] = options
    with requests.post(base() + "/api/chat", json=payload,
                       stream=True, timeout=timeout) as r:
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
                if stats:
                    yield "stats", {"prompt_tokens": obj.get("prompt_eval_count") or 0,
                                    "completion_tokens": obj.get("eval_count") or 0,
                                    "done_reason": obj.get("done_reason") or "stop"}
                break


def set_thread_limit(limit):
    """Fils de calcul pour les embeddings d'un modèle sur le processeur (None : le choix
    d'Ollama). Appelé par jobs/pacing.py quand l'alimentation change."""
    _threads["limit"] = limit


def _full_name(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


def _on_cpu(model: str) -> bool:
    """Le modèle tourne-t-il (en partie) sur le processeur ? D'après /api/ps, relu au
    plus toutes les LOADED_TTL s. Un modèle vu sur le processeur le reste tant qu'on ne
    l'a pas revu chargé ailleurs : déchargé puis rechargé, il le serait sans num_thread."""
    name, now = _full_name(model), time.monotonic()
    if now - _threads["checked"].get(name, 0.0) > LOADED_TTL:
        _threads["checked"][name] = now
        for m in loaded_models():
            if _full_name(m["name"]) == name:
                if m["size_vram"] < m["size"]:
                    _threads["cpu"].add(name)
                else:
                    _threads["cpu"].discard(name)
    return name in _threads["cpu"]


def embed_options(model: str) -> dict:
    limit = _threads["limit"]
    return {"num_thread": int(limit)} if limit and _on_cpu(model) else {}


def embed(texts, model=None, timeout=180) -> list:
    """Vecteurs d'embedding d'une liste de textes (envoyés par lots). timeout : délai de
    chaque lot (plus court pour une recherche venue d'une autre machine)."""
    if use_integrated():
        return integrated.embed(texts, model, timeout)
    model = model or settings.get("embed_model")
    vectors = []
    for start in range(0, len(texts), 16):
        batch = texts[start:start + 16]
        body = {"model": model, "input": batch}
        options = embed_options(model)
        if options:
            body["options"] = options
        r = requests.post(base() + "/api/embed", json=body, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(_error_text(r))
        vectors.extend(r.json().get("embeddings") or [])
    if len(vectors) != len(texts):
        raise RuntimeError("Ollama n'a pas renvoyé un vecteur par texte.")
    return vectors


def generate_with_images(model, prompt, images_b64, timeout=300) -> str:
    """Appel ponctuel à un modèle de vision (OCR, description d'image)."""
    if use_integrated():
        return integrated.generate_with_images(model, prompt, images_b64, timeout)
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
