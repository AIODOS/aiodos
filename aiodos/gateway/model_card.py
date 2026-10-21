"""Les fiches des modèles, communes à tous les catalogues de la passerelle (/v1/models,
/aiodos/v1/models) : les deux ne peuvent pas diverger (docs/api-applications.md § 5.2, 5.4).

Chaque entrée de catalog.entries() a un cercle : here (ce PC), mine (une de mes machines, ou
un gros modèle réparti entre elles), friends (un ami qui prête ses modèles). Une appli ne voit
que les cercles de son lieu effectif (scopes.effective) ; la clé générale voit tout, comme
avant. Le contexte est celui, réel, du moteur intégré de ce PC (llm/context.py) ; celui d'une
autre machine n'est pas transmis (null).
"""
from aiodos import settings
from aiodos.cluster import group as cluster
from aiodos.gateway import catalog
from aiodos.i18n import t
from aiodos.llm import catalog as llm_catalog
from aiodos.llm import context as llm_context
from aiodos.llm import models as llm_models
from aiodos.llm.engine import EMBED_CONTEXT
from aiodos.network import placement, remote_models
from aiodos.node import identity
from aiodos.services import ollama

SUPPORTED = ["tools", "reasoning", "response_format", "max_tokens", "temperature", "top_p",
             "stop", "seed"]


def circle(entry: dict) -> str:
    if not entry.get("remote"):
        return "here"
    return "friends" if entry.get("trust") == "friend" else "mine"


def place(entry: dict) -> str:
    """Pour l'en-tête X-Aiodos-Place et les fiches : here, mine ou friend."""
    c = circle(entry)
    return "friend" if c == "friends" else c


def kind(entry: dict) -> str:
    caps = entry.get("capabilities") or []
    return "embed" if caps and "embedding" in caps and "completion" not in caps else "chat"


def allowed(entries: list, where) -> list:
    """Les entrées des cercles permis (where : None, pas de filtre)."""
    if where is None:
        return list(entries)
    return [e for e in entries if circle(e) in where]


def _loaded() -> set:
    try:
        return {m["name"] for m in ollama.loaded_models()}
    except Exception:
        return set()


def _context(entry: dict) -> int | None:
    if entry.get("remote") or not ollama.use_integrated():
        return None
    return EMBED_CONTEXT if kind(entry) == "embed" else llm_context.chat_context(entry["name"])


def _license(name: str):
    spec = llm_catalog.entry(name)
    if spec and spec.get("license"):
        return {"id": str(spec["license"]).lower(), "restrictive": False}
    return None


def default_of(wanted_kind: str) -> str | None:
    """Le modèle qu'AIODOS prend lui-même, en nom complet : celui de ses conversations
    (default_model, « auto:qwen3.5:9b », « peer:… »…) ou de ses bibliothèques (embed_model).
    Une appli qui n'a pas de préférence prend celui-là (« default » dans les fiches) : pour
    les embeddings, des vecteurs d'un même modèle d'un jour à l'autre."""
    wanted = settings.get("embed_model" if wanted_kind == "embed" else "default_model")
    try:
        model = catalog.internal(wanted)
    except catalog.UnknownModel:
        return None
    peer = remote_models.parse(model)
    name = peer[1] if peer else placement.parse(model) or cluster.parse(model) or model
    return llm_models.full_name(name)


def openai_entry(entry: dict, loaded: set) -> dict:
    """Une entrée de /v1/models, enrichie à la façon d'OpenRouter."""
    caps = entry.get("capabilities") or []
    is_embed = kind(entry) == "embed"
    name = f"{entry['name']} · {entry['machine']}" if entry.get("machine") else entry["name"]
    out = {"id": entry["id"], "object": "model", "created": catalog.epoch(entry["modified_at"]),
           "owned_by": entry.get("machine") or "aiodos", "name": name,
           "context_length": _context(entry),
           "architecture": {"input_modalities": ["text", "image"] if "vision" in caps
                            and not entry.get("remote") else ["text"],
                            "output_modalities": ["embedding"] if is_embed else ["text"]},
           "aiodos": {"kind": kind(entry), "machine": entry.get("machine"), "place": place(entry),
                      "loaded": entry["name"] in loaded if not entry.get("remote")
                      else bool(entry.get("loaded"))}}
    if not is_embed:
        supported = list(SUPPORTED)
        if entry.get("remote") or "tools" not in caps:
            supported.remove("tools")
        if "thinking" not in caps:
            supported.remove("reasoning")
        out["supported_parameters"] = supported
    return out


def logical(entries: list, wanted_kind: str) -> list:
    """/aiodos/v1/models : un modèle logique par nom, avec où il se trouve. Le nom seul
    laisse AIODOS choisir la machine ; where[].id en désigne une."""
    loaded = _loaded()
    here_name = _machine_name()
    default = default_of(wanted_kind)
    groups = {}
    for e in entries:
        if kind(e) != wanted_kind:
            continue
        if e.get("remote") and e.get("machine") is None:
            continue                      # entrée du choix automatique : déjà par machine
        caps = e.get("capabilities") or []
        local = not e.get("remote")
        group = groups.setdefault(e["name"], {
            "id": e["name"], "kind": wanted_kind, "label": e["name"], "installed": True,
            "context": None, "size_bytes": e.get("size") or 0, "loaded": False, "here": False,
            "default": llm_models.full_name(e["name"]) == default, "where": [],
            "license": _license(e["name"])})
        if wanted_kind == "embed":
            group.setdefault("dimensions", None)           # inconnu tant qu'on n'a pas calculé
        item = {"id": f"{catalog.LOCAL}{e['name']}" if local else e["id"],
                "machine": here_name if local else e.get("machine"),
                "place": place(e),
                "loaded": (e["name"] in loaded) if local else bool(e.get("loaded")),
                "context": _context(e)}
        if wanted_kind == "chat":
            item["tools"] = local and "tools" in caps
            item["vision"] = local and "vision" in caps
        group["where"].append(item)
        if local:
            group["here"] = True
            group["context"] = item["context"]
            group["loaded"] = item["loaded"]
            group["size_bytes"] = e.get("size") or group["size_bytes"]
            if wanted_kind == "chat":
                group.update(vision="vision" in caps, tools="tools" in caps,
                             thinking="thinking" in caps)
    out = []
    for group in groups.values():
        if wanted_kind == "chat":
            for flag in ("vision", "tools", "thinking"):
                group.setdefault(flag, False)
        group["where"].sort(key=lambda w: ("here", "mine", "friend").index(w["place"])
                            if w["place"] in ("here", "mine", "friend") else 3)
        out.append(group)
    return sorted(out, key=lambda g: (not g["here"], g["id"]))


def _machine_name() -> str:
    try:
        return identity.profile().get("name") or t("gateway_api.this_machine")
    except Exception:
        return t("gateway_api.this_machine")
