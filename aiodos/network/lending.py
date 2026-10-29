"""Prêter ses modèles pour les conversations de ses autres machines (étape C).

Une machine sans modèle adapté (pas de GPU, ou un modèle trop gros pour sa
mémoire) peut faire répondre une conversation par un modèle installé ici. Le
texte de la conversation, avec les extraits de bibliothèque que l'autre machine
a choisis, passe par ce PC le temps de la réponse, chiffré, et n'y est pas
enregistré. Désactivé tant que l'utilisateur de ce PC ne l'a pas activé.

Pendant la réponse, ce PC suspend ses propres unités GPU de fond, comme pour
une conversation locale : l'interactif d'abord. L'historique de la vue Tâches
(network/lent_history.py) en garde une ligne sans le texte : pour quelle machine, le
modèle, la durée, les jetons rendus, l'issue.

La demande peut aussi porter (passerelle vers les autres outils, gateway/) des réglages
d'échantillonnage (« options », seulement ceux de SAMPLING : un réglage de chargement
comme num_ctx rechargerait le modèle ici) et « stats » : un dernier morceau donne alors le
nombre de jetons. Une machine d'une version précédente ignore les deux.
"""
import time

from aiodos import settings
from aiodos.i18n import t
from aiodos.network import activity, lend_queue, lent_history
from aiodos.network.client import PeerError
from aiodos.services import ollama

ROLES = {"system", "user", "assistant"}
MAX_MESSAGES = 400
SAMPLING = {"temperature", "top_p", "top_k", "min_p", "seed", "num_predict", "stop",
            "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty"}
MAX_STOPS = 8
KEEPALIVE_S = 5                   # signe de vie à la machine qui attend son tour (lend_queue)


def _clean(messages) -> list:
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
        raise PeerError(t("lending.chat_invalid"))
    cleaned = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ROLES \
                or not isinstance(m.get("content"), str):
            raise PeerError(t("lending.message_invalid"))
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
        raise PeerError(t("lending.not_lending", name=manager.name()))
    from aiodos.network import worker
    if not worker.welcomes(sender):
        raise PeerError(t("lending.phone_for_friends", name=manager.name()))
    model = str(payload.get("model") or "")
    messages = _clean(payload.get("messages"))
    try:
        installed = {m["name"] for m in ollama.list_models()}
    except Exception as exc:
        raise PeerError(t("lending.ollama_down", name=manager.name())) from exc
    if model not in installed:
        raise PeerError(t("lending.model_gone", model=model, name=manager.name()))
    think = None if payload.get("think", True) else False
    extra = {}                            # seulement si demandés (versions précédentes)
    options = _options(payload.get("options"))
    if options:
        extra["options"] = options
    if payload.get("stats"):
        extra["stats"] = True

    circle = lent_history.circle_of(sender)

    def stopped() -> bool:                # stop peut manquer (appel direct, sans arrêt)
        return stop is not None and stop.is_set()

    def answer():
        # Une à la fois ; mes machines, puis qui m'a le plus donné, passent d'abord
        # (lend_queue.py). En attendant, un signe de vie toutes les KEEPALIVE_S secondes :
        # la machine qui attend peut partir, ce qui libère sa place ici
        ticket = lend_queue.enter(sender)
        try:
            while not lend_queue.wait(ticket, stop, tick=KEEPALIVE_S):
                yield {"queued": lend_queue.position(ticket)}
            manager.executor.interactive_begin()
            # L'historique de la vue Tâches : pour qui, le modèle, la durée, les jetons rendus
            lent = lent_history.begin(lent_history.CHAT, circle, peer_id=sender, model=model)
            began, tokens, counted = time.monotonic(), 0, None
            try:
                with activity.chat("for", sender, stop):    # « répond à sa conversation »
                    for kind, text in ollama.chat_stream(model, messages, think=think,
                                                         stop=stop, **extra):
                        if kind in ("content", "thinking"):
                            tokens += 1
                        elif kind == "stats" and isinstance(text, dict):
                            counted = text.get("completion_tokens")
                        yield {kind: text}
                if not stopped():
                    produced = counted if isinstance(counted, int) and counted > 0 else tokens
                    lent.end(lent_history.DONE, None, time.monotonic() - began,
                             tokens=produced)
            except Exception:
                if not stopped():
                    lent.end(lent_history.FAILED, "engine", time.monotonic() - began,
                             tokens=tokens)
                raise
            finally:
                manager.executor.interactive_end()
                # Sans fin dite plus haut : arrêtée en route (Stop, machine partie)
                lent.end(lent_history.STOPPED, "left", time.monotonic() - began, tokens=tokens)
        except lend_queue.Busy:
            lent_history.refused(lent_history.CHAT, circle, "busy", peer_id=sender, model=model)
            raise PeerError(t("lending.busy", name=manager.name())) from None
        except lend_queue.Cancelled:
            return                                       # elle est partie avant son tour
        finally:
            lend_queue.leave(ticket)
    return answer()
