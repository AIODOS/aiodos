"""Modèles proposés aux autres outils, et leurs identifiants.

- « qwen3.5:9b », le nom seul : la machine qui répond est choisie seule (mode
  automatique, network/placement.py), parmi cette machine et mes machines qui le prêtent ;
- « peer:<machine>:qwen3.5:9b » : une machine appairée précise, une des miennes ou celle
  d'un ami qui me prête ses modèles (network/remote_models.py) ;
- « local:qwen3.5:9b » : cette machine seulement (accepté, pas listé).
Les modèles du cloud n'y figurent pas : leur envoi se valide dans l'interface, message par
message. Les modèles d'embedding ne servent que sur cette machine.
"""
import threading
import time
from datetime import datetime, timezone

import requests

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
    cette machine, « peer:… »)."""
    name = str(model_id or "").strip()
    if not name:
        raise UnknownModel("Modèle manquant.")
    if name.startswith(cloud.MODEL_PREFIX):
        raise UnknownModel(f"Les modèles du cloud ne passent pas par la passerelle : « {name} ».")
    if remote_models.parse(name) or placement.parse(name):
        return name
    if name.startswith(LOCAL):
        if not name[len(LOCAL):]:
            raise UnknownModel("Modèle manquant après « local: ».")
        return name[len(LOCAL):]
    return placement.auto_id(name)


def local_name(model_id) -> str:
    """Modèle de cette machine désigné par un outil (embeddings : jamais ailleurs)."""
    model = internal(model_id)
    if remote_models.parse(model):
        raise UnknownModel("Les embeddings ne se calculent que sur cette machine : "
                           f"« {model_id} » est le modèle d'une autre machine.")
    return placement.parse(model) or model


def local_tags(max_age=TAGS_TTL) -> list:
    """Modèles de l'Ollama de cette machine, tels qu'il les décrit (/api/tags)."""
    with _lock:
        if _tags["models"] is not None and time.monotonic() - _tags["at"] < max_age:
            return list(_tags["models"])
    response = requests.get(ollama.base() + "/api/tags", timeout=15)
    response.raise_for_status()
    models = [m for m in response.json().get("models") or [] if m.get("name") or m.get("model")]
    with _lock:
        _tags.update(at=time.monotonic(), models=models)
    return list(models)


def _remote_details(m: dict) -> dict:
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
    return out


def _remote_entry(m: dict, model_id: str, machine) -> dict:
    return {"id": model_id, "name": m["label"], "machine": machine, "remote": True,
            "size": m.get("size", 0), "digest": m.get("digest", ""),
            "details": _remote_details(m), "capabilities": m.get("caps") or ["completion"],
            "modified_at": now_iso()}


def find(model_id) -> dict | None:
    for entry in entries():
        if entry["id"] == model_id:
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
