"""Prêter ses modèles pour les conversations de ses autres machines (étape C).

Une machine sans modèle adapté (pas de GPU, ou un modèle trop gros pour sa
mémoire) peut faire répondre une conversation par un modèle installé ici. Le
texte de la conversation, avec les extraits de bibliothèque que l'autre machine
a choisis, passe par ce PC le temps de la réponse, chiffré, et n'y est pas
enregistré. Désactivé tant que l'utilisateur de ce PC ne l'a pas activé.

Pendant la réponse, ce PC suspend ses propres unités GPU de fond, comme pour
une conversation locale : l'interactif d'abord.
"""
from aiodos import settings
from aiodos.network.client import PeerError
from aiodos.services import ollama

ROLES = {"system", "user", "assistant"}
MAX_MESSAGES = 400


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

    def answer():
        manager.executor.interactive_begin()
        try:
            for kind, text in ollama.chat_stream(model, messages, think=think, stop=stop):
                yield {kind: text}
        finally:
            manager.executor.interactive_end()
    return answer()
