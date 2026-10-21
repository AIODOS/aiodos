"""Enrichir la description d'une image ou d'une vidéo avant de la créer, comme le font les
services en ligne (Seedream, Midjourney…) : le modèle de conversation de ce PC change la
demande courte (« un samouraï assis sur la carapace d'un escargot ») en une description
détaillée en anglais, la langue que les modèles d'images comprennent le mieux. Le sujet est
précisé (apparence, tenue, posture, ce qu'il fait), avec la composition, le décor, la lumière,
et un style quand la demande n'en donne pas ; rien de ce qu'elle dit n'est changé.

Réglage « images_enrich » (activé par défaut). Sans modèle de conversation à temps, la demande
part telle quelle. La description utilisée est gardée avec la création : on la relit sous
l'image, « Reprendre » et les retouches partent d'elle (chat/retouches.py la réécrit dans sa
langue)."""
import logging
import threading

from aiodos import settings
from aiodos.chat import retouches
from aiodos.services import ollama

log = logging.getLogger("aiodos.chat")

ENRICH_S = 45                  # au-delà, la demande part telle quelle
MAX_PROMPT = 1500
DETAILED_WORDS = 60            # une description déjà détaillée part telle quelle
_ENRICH = ("Tu écris les descriptions que reçoit un modèle qui crée des {what}. On te donne "
           "une demande courte : écris une seule description détaillée, en anglais, de 60 à "
           "130 mots. Garde exactement le sujet demandé et tout ce que dit la demande, sans "
           "rien contredire, puis précise-le : apparence, tenue, matières, posture, action, "
           "objets tenus, place dans l'image ; ajoute le décor et l'arrière-plan, la lumière, "
           "le cadrage, et un style artistique adapté si la demande n'en donne pas "
           "(photographie, illustration, estampe, peinture, 3D…). N'ajoute ni texte écrit "
           "dans l'image, ni personnage ou objet important que la demande n'implique pas.{video}"
           " Réponds uniquement par la description, sans guillemets, titre ni commentaire.")
_VIDEO = " Décris aussi le mouvement dans la scène et celui de la caméra."


def wanted(prompt: str) -> bool:
    """À enrichir ? Le réglage le veut, et la description est courte (une description déjà
    détaillée, ou reprise d'une création, part telle quelle)."""
    return bool(settings.get("images_enrich")) and len(str(prompt or "").split()) < DETAILED_WORDS


def enrich(prompt: str, kind: str = "image", conversation_model: str | None = None) -> str:
    """La description détaillée de la demande ; la demande elle-même si aucun modèle de
    conversation n'a répondu à temps."""
    prompt = " ".join(str(prompt or "").split())
    model = retouches._rewriter(conversation_model)
    if not prompt or model is None:
        return prompt
    video = kind == "video"
    system = _ENRICH.format(what="vidéos" if video else "images", video=_VIDEO if video else "")
    stop = threading.Event()
    timer = threading.Timer(ENRICH_S, stop.set)
    timer.start()
    parts = []
    try:
        for part, piece in ollama.chat_stream(
                model, [{"role": "system", "content": system},
                        {"role": "user", "content": prompt}],
                think=False, stop=stop, options={"temperature": 0.6, "num_predict": 400},
                timeout=(10, ENRICH_S)):         # un modèle long à charger ne bloque pas plus
            if part == "content":
                parts.append(piece)
    except Exception as exc:
        log.info("Description non enrichie (%s) : la demande part telle quelle", exc)
        return prompt
    finally:
        timer.cancel()
    found = " ".join("".join(parts).split()).strip(" \"«»'")
    if stop.is_set() or len(found) < len(prompt) // 2 or len(found) < 12:
        return prompt
    return found[:MAX_PROMPT]
