"""Enrichir la description d'une image ou d'une vidéo avant de la créer, comme le font les
services en ligne (Seedream, Midjourney…) : le modèle de conversation de ce PC change la
demande courte (« un samouraï assis sur la carapace d'un escargot ») en une description
détaillée en anglais, la langue que les modèles d'images comprennent le mieux. Le sujet est
précisé (apparence, tenue, posture, ce qu'il fait), avec la composition, le décor, la lumière,
et un style quand la demande n'en donne pas ; rien de ce qu'elle dit n'est changé.

Réglage « images_enrich » (activé par défaut). Sans modèle de conversation à temps, la demande
part telle quelle. Seul un modèle installé ici réécrit (retouches._rewriter), jamais celui d'un
inconnu du réseau public, même quand c'est une de ses machines qui créera l'image
(chat/public_creations.py) : sans modèle ici, la description part telle quelle. La description utilisée est gardée avec la création : on la relit sous
l'image, « Reprendre » et les retouches partent d'elle (chat/retouches.py la réécrit dans sa
langue)."""
import logging
import threading

from aiodos import settings
from aiodos.chat import retouches
from aiodos.i18n import t
from aiodos.services import ollama

log = logging.getLogger("aiodos.chat")

ENRICH_S = 45                  # au-delà, la demande part telle quelle
MAX_PROMPT = 1500
DETAILED_WORDS = 60            # une description déjà détaillée part telle quelle
# Les consignes (enrich_prompts.json) : une image, une vidéo, une musique, un effet sonore
PROMPTS = ("image", "video", "music", "sound")


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
    # La consigne (enrich_prompts.json) suit la langue de la requête ; la description qu'elle
    # demande est en anglais dans toutes les langues
    # Une musique ou un effet sonore (chat/audio_creations.py) : leur propre consigne
    system = t(f"enrich_prompts.{kind}") if kind in PROMPTS else t("enrich_prompts.image")
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
    found = " ".join("".join(parts).split()).strip(" \"«»'")   # i18n: tel quel (guillemets ôtés)
    if stop.is_set() or len(found) < len(prompt) // 2 or len(found) < 12:
        return prompt
    return found[:MAX_PROMPT]
