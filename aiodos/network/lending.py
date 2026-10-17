"""Prêter ses modèles pour les conversations de ses autres machines (étape C).

Une machine sans modèle adapté (pas de GPU, ou un modèle trop gros pour sa
mémoire) peut faire répondre une conversation par un modèle installé ici. Le
texte de la conversation, avec les extraits de bibliothèque que l'autre machine
a choisis, passe par ce PC le temps de la réponse, chiffré, et n'y est pas
enregistré. Désactivé tant que l'utilisateur de ce PC ne l'a pas activé.

Pendant la réponse, ce PC suspend ses propres unités GPU de fond, comme pour
une conversation locale : l'interactif d'abord.

La demande peut aussi porter (passerelle vers les autres outils, gateway/) des réglages
d'échantillonnage (« options », seulement ceux de SAMPLING : un réglage de chargement
comme num_ctx rechargerait le modèle ici) et « stats » : un dernier morceau donne alors le
nombre de jetons. Une machine d'une version précédente ignore les deux.
"""
from aiodos import settings
from aiodos.network.client import PeerError
from aiodos.services import ollama

ROLES = {"system", "user", "assistant"}
MAX_MESSAGES = 400
SAMPLING = {"temperature", "top_p", "top_k", "min_p", "seed", "num_predict", "stop",
            "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty"}
MAX_STOPS = 8


def _clean(messages) -> list:
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
        raise PeerError("Conversation vide ou trop longue.")
    cleaned = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ROLES \
                or not isinstance(m.get("content"), str):
            raise PeerError("Message de conversation invalide.")
        cleaned.append({"role": m["role"], "content": m["content"]})
    return cleaned


def _options(raw) -> dict:
    """Réglages d'échantillonnage acceptés : des nombres, et quelques fins de texte."""
    if not isinstance(raw, dict):
        return {}
    options = {}
    for key, value in raw.items():
        if key not in SAMPLING:
            continue
        if key == "stop":
            stops = [value] if isinstance(value, str) else value
            if isinstance(stops, list):
                stops = [s[:200] for s in stops if isinstance(s, str) and s][:MAX_STOPS]
                if stops:
                    options["stop"] = stops
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            options[key] = value
    return options


def chat(manager, sender, payload, stop):
    """Vérifie la demande, puis renvoie le générateur de la réponse."""
    if not settings.get("lend_models"):
        raise PeerError(f"« {manager.name()} » ne prête pas ses modèles pour les conversations "
                        "(réglage « Prêter mes modèles » sur cette machine).")
    model = str(payload.get("model") or "")
    messages = _clean(payload.get("messages"))
    try:
        installed = {m["name"] for m in ollama.list_models()}
    except Exception as exc:
        raise PeerError(f"Ollama ne répond pas sur « {manager.name()} ».") from exc
    if model not in installed:
        raise PeerError(f"Le modèle « {model} » n'est plus installé sur « {manager.name()} ».")
    think = None if payload.get("think", True) else False
    extra = {}                            # seulement si demandés (versions précédentes)
    options = _options(payload.get("options"))
    if options:
        extra["options"] = options
    if payload.get("stats"):
        extra["stats"] = True

    def answer():
        manager.executor.interactive_begin()
        try:
            for kind, text in ollama.chat_stream(model, messages, think=think, stop=stop,
                                                 **extra):
                yield {kind: text}
        finally:
            manager.executor.interactive_end()
    return answer()
