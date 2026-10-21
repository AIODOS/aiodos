"""Modèles proposés aux autres outils, et leurs identifiants.

- « qwen3.5:9b », le nom seul : la machine qui répond est choisie seule (mode
  automatique, network/placement.py), parmi cette machine et mes machines qui le prêtent ;
- « peer:<machine>:qwen3.5:9b » : une machine appairée précise, une des miennes ou celle
  d'un ami qui me prête ses modèles (network/remote_models.py) ;
- « local:qwen3.5:9b » : cette machine seulement (accepté, pas listé) ;
- « reparti:qwen3.6 » : un gros modèle réparti entre mes machines (cluster/group.py).
Les modèles du cloud n'y figurent pas : leur envoi se valide dans l'interface, message par
message. Les modèles d'embedding ne servent que sur cette machine.
"""
import threading
import time
from datetime import datetime, timezone

import requests

from aiodos.cluster import group as cluster
from aiodos.network import placement, remote_models
from aiodos.services import cloud, ollama

LOCAL = "local:"
TAGS_TTL = 10               # la liste des modèles d'Ollama change rarement

_lock = threading.Lock()
_tags = {"at": 0.0, "models": None}


class UnknownModel(Exception):
    """Identifiant de modèle que la passerelle ne connaît pas (message lisible)."""


def internal(model_id) -> str:
    """Identifiant donné par un outil → identifiant d'AIODOS (« auto:… », nom seul pour
    cette machine, « peer:… », « reparti:… »). Jamais un modèle du cloud, même déguisé
    (« local:cloud:… ») : son envoi se valide dans l'interface, message par message."""
    name = str(model_id or "").strip()
    if not name:
        raise UnknownModel("Modèle manquant.")
    if name.startswith(cloud.MODEL_PREFIX):
        raise UnknownModel(f"Les modèles du cloud ne passent pas par la passerelle : « {name} ».")
    if remote_models.parse(name) or placement.parse(name) or cluster.parse(name):
        return name
    if name.startswith(LOCAL):
        rest = name[len(LOCAL):]
        if not rest:
            raise UnknownModel("Modèle manquant après « local: ».")
        if placement.reserved(rest):
            raise UnknownModel(f"« {name} » n'est pas un modèle de cette machine.")
        return rest
    return placement.auto_id(name)


def local_name(model_id) -> str:
    """Modèle de cette machine désigné par un outil (embeddings : jamais ailleurs)."""
    model = internal(model_id)
    if remote_models.parse(model) or cluster.parse(model):
        raise UnknownModel("Les embeddings ne se calculent que sur cette machine : "
                           f"« {model_id} » n'est pas un de ses modèles.")
    return placement.parse(model) or model


def local_tags(max_age=TAGS_TTL) -> list:
    """Modèles de cette machine, tels qu'Ollama les décrit (/api/tags) : ceux de son Ollama,
    ou ceux du moteur intégré quand il le remplace (services/ollama.use_integrated)."""
    if ollama.use_integrated():
        return _integrated_tags(max_age)
    with _lock:
        if _tags["models"] is not None and time.monotonic() - _tags["at"] < max_age:
            return list(_tags["models"])
    response = requests.get(ollama.base() + "/api/tags", timeout=15)
    response.raise_for_status()
    models = [m for m in response.json().get("models") or [] if m.get("name") or m.get("model")]
    with _lock:
        _tags.update(at=time.monotonic(), models=models)
    return list(models)


def _integrated_tags(max_age) -> list:
    """Les modèles du moteur intégré (llm/), par la façade ; elle les garde déjà en cache."""
    models = ollama.list_models(max_age)
    caps = ollama.capabilities_many([m["name"] for m in models])
    return [{"name": m["name"], "model": m["name"], "modified_at": now_iso(),
             "size": m.get("size", 0), "digest": m.get("digest", ""), "details": details(m),
             "capabilities": caps.get(m["name"]) or []} for m in models]


def details(m: dict) -> dict:
    """« details » d'Ollama pour un modèle décrit comme ollama.list_models le fait (family,
    parameters, quantization) : prêté, ou du moteur intégré."""
    family = m.get("family") or ""
    return {"parent_model": "", "format": "gguf", "family": family,
            "families": [family] if family else None,
            "parameter_size": m.get("parameters") or "",
            "quantization_level": m.get("quantization") or ""}


def entries() -> list:
    """Modèles proposés, pour les listes des deux API :
    [{"id", "name", "machine" (None : cette machine ou automatique), "remote", "size",
      "digest", "details", "capabilities", "modified_at"}]"""
    out, seen = [], set()
    try:
        tags = local_tags()
    except (requests.RequestException, ValueError):
        tags = []                         # Ollama arrêté ici : restent les autres machines
    for t in tags:
        name = t.get("name") or t.get("model")
        caps = t.get("capabilities") or ollama.capabilities(name)
        out.append({"id": name, "name": name, "machine": None, "remote": False,
                    "size": t.get("size", 0), "digest": t.get("digest", ""),
                    "details": t.get("details") or {}, "capabilities": caps,
                    "modified_at": t.get("modified_at") or now_iso()})
        seen.add(name)
    remote = remote_models.available()
    for m in remote:                      # seulement chez mes machines : mode automatique
        if m.get("auto") and m["label"] not in seen:
            seen.add(m["label"])
            out.append(_remote_entry(m, m["label"], None))
    for m in remote:                      # chaque machine qui le prête, nommément
        out.append(_remote_entry(m, m["id"], m["peer"]))
    try:
        offers = cluster.available()      # gros modèles répartis entre mes machines
    except Exception:
        offers = []
    for m in offers:
        out.append({"id": m["id"], "name": m["label"], "machine": "réparti", "remote": True,
                    "size": m.get("size", 0), "digest": "", "details": details({}),
                    "capabilities": ["completion"], "modified_at": now_iso()})
    return out


def _remote_entry(m: dict, model_id: str, machine) -> dict:
    return {"id": model_id, "name": m["label"], "machine": machine, "remote": True,
            "size": m.get("size", 0), "digest": m.get("digest", ""),
            "details": details(m), "capabilities": m.get("caps") or ["completion"],
            "modified_at": now_iso()}


def find(model_id) -> dict | None:
    """Le modèle proposé sous cet identifiant (« llama3 » trouve « llama3:latest »)."""
    listed = entries()
    for entry in listed:
        if entry["id"] == model_id:
            return entry
    for entry in listed:
        if not entry["machine"] and placement.same_model(entry["id"], str(model_id)):
            return entry
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(iso) -> int:
    """« 2026-06-09T11:57:36.857877+02:00 » → secondes (création, pour l'API d'OpenAI)."""
    try:
        text = str(iso).replace("Z", "+00:00")
        head, dot, rest = text.partition(".")
        if dot:                           # Ollama écrit parfois 7 chiffres après la virgule
            digits = "".join(c for c in rest if c.isdigit())
            text = f"{head}.{digits[:6]}{rest[len(digits):]}"
        return int(datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError):
        return int(time.time())
