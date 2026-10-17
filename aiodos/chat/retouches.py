"""Retoucher la dernière création de la conversation, comme dans les autres assistants :
après une image, « Fais-la de nuit », « Ajoute un chat », « Encore une », « Plus
lumineuse »… La demande n'a pas besoin de redire « image » : elle se rapporte à la
dernière création de la conversation (chat/creations.py).

- Une variante (« encore une », « une autre », « refais-la ») : même description, même
  modèle et même format, une autre graine.
- Une retouche : une nouvelle description, écrite par le modèle de conversation à partir
  de l'ancienne et de la demande (« un phare sous l'orage » + « de nuit » → « un phare
  sous l'orage, la nuit… ») ; même graine, pour garder la composition. Sans modèle de
  conversation à temps (Ollama arrêté, modèle trop lent), la demande s'ajoute à la
  description.

Une question (« Pourquoi est-elle floue ? ») ou un sujet de code n'est pas une retouche.
"""
import logging
import re
import threading

from aiodos import settings
from aiodos.services import ollama

log = logging.getLogger("aiodos.chat")

VARIATION, EDIT = "variation", "edit"
MAX_TEXT = 300
REWRITE_S = 45                 # au-delà, la demande s'ajoute simplement à la description

_VARIATION = re.compile(
    r"^\s*(?:et\s+)?(?:encore(?:\s+une?)?|une autre|un autre|d'autres|refais[- ](?:la|le|les)|"
    r"refais|recommence|again|another|one more)\b(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
_EDIT = re.compile(
    r"^\s*(?:(?:et\s+)?(?:maintenant|puis|ensuite)\s*,?\s*)?(?:(?:peux-tu|tu peux|pourrais-tu|"
    r"tu pourrais)\s+)?(?:(?:fais|faire|rends|rendre|mets|mettre)[- ](?:la|le|les|en)\b|"
    r"(?:la|le|les)\s+(?:faire|rendre|mettre|refaire)\b|ajoute|rajoute|enlève|retire|"
    r"supprime|change|remplace|transforme|plus\s|moins\s|avec\s|sans\s|en noir et blanc|"
    r"en couleurs?\b|en style\b|façon\s|style\s|de nuit\b|de jour\b|la même|le même|"
    r"les mêmes|pareil|même (?:chose|image|photo|vidéo|scène)|make it|add\s|remove\s|"
    r"without\s|with\s|same\s)", re.IGNORECASE)
_QUESTION = re.compile(r"^\s*(comment|pourquoi|quel|quelle|quels|quelles|combien|qui|où|"
                       r"est-ce|qu'est-ce|explique|how|why|what|which|who|where)\b", re.IGNORECASE)
# Des tournures qui commencent comme une retouche sans en être une
_NOT_EDIT = re.compile(r"^\s*(sans (rire|blague|doute|problème|souci)|avec plaisir|plus tard|"
                       r"plus jamais|même pas|pareil pour moi)\b", re.IGNORECASE)
_SAME = re.compile(r"^\s*(pareil|la même|le même|les mêmes|même chose|same)\s*[.!]*\s*$",
                   re.IGNORECASE)
_POLITE = re.compile(r"[\s,]*(s'il (?:te|vous) pla[iî]t|stp|svp|please|merci)\s*[.!?]*\s*$",
                     re.IGNORECASE)
_LEAD = re.compile(r"^\s*(?:(?:et\s+)?(?:maintenant|puis|ensuite)\s*,?\s*)?(?:(?:peux-tu|tu peux|"
                   r"pourrais-tu|tu pourrais)\s+)?(?:(?:fais|faire|rends|rendre|mets|mettre)"
                   r"[- ](?:la|le|les|en)|(?:la|le|les)\s+(?:faire|rendre|mettre|refaire))\s*",
                   re.IGNORECASE)

_REWRITE = ("Tu réécris des descriptions d'images et de vidéos pour un modèle qui les crée. "
            "On te donne la description actuelle et une modification demandée : écris la "
            "nouvelle description complète, qui garde tout ce qui n'est pas modifié. Réponds "
            "uniquement par la description, en une à trois phrases, dans la langue de la "
            "description actuelle, sans guillemets ni commentaire.")


def detect(text: str) -> str | None:
    """VARIATION, EDIT, ou None si le message n'est pas une retouche de la dernière création."""
    text = " ".join((text or "").replace("’", "'").split())
    if not text or len(text) > MAX_TEXT or _QUESTION.match(text):
        return None
    if text.endswith("?") and not re.match(r"^\s*(peux-tu|tu peux|pourrais-tu|tu pourrais)\b",
                                           text, re.IGNORECASE):
        return None
    from aiodos.chat.creations import technical      # même liste de sujets de code
    if technical(text) or _NOT_EDIT.match(text):
        return None
    if _SAME.match(text):
        return VARIATION
    found = _VARIATION.match(text)
    if found:
        # « Encore une, mais de nuit » : une retouche ; « Encore une ! » : une variante
        rest = _POLITE.sub("", found["rest"]).strip(" ,.!")
        return EDIT if re.match(r"(mais|avec|sans|en|de|plus|moins|but|with|without)\b",
                                rest, re.IGNORECASE) else VARIATION
    return EDIT if _EDIT.match(text) else None


def _rewriter(conversation_model: str | None) -> str | None:
    """Un modèle de conversation installé ici pour réécrire : celui de la conversation,
    sinon celui par défaut, sinon le premier qui sait converser."""
    try:
        installed = [m["name"] for m in ollama.list_models()]
    except Exception:
        return None
    wanted = str(conversation_model or "")
    if wanted.startswith("auto:"):
        wanted = wanted[len("auto:"):]
    for name in (wanted, str(settings.get("default_model") or "")):
        if name in installed:
            return name
    caps = ollama.capabilities_many(installed)
    return next((n for n in installed if "completion" in (caps.get(n) or ["completion"])), None)


def _appended(prompt: str, text: str) -> str:
    """Sans modèle de conversation : la demande, sans « fais-la », après la description."""
    extra = _POLITE.sub("", _LEAD.sub("", text)).strip(" ,.!")
    return f"{prompt}, {extra}" if extra else prompt


def rewrite(prompt: str, text: str, conversation_model: str | None = None) -> str:
    """La description retouchée selon la demande (EDIT)."""
    model = _rewriter(conversation_model)
    if model is None:
        return _appended(prompt, text)
    stop = threading.Event()
    timer = threading.Timer(REWRITE_S, stop.set)
    timer.start()
    parts = []
    try:
        for kind, piece in ollama.chat_stream(
                model, [{"role": "system", "content": _REWRITE},
                        {"role": "user", "content": f"Description actuelle : {prompt}\n"
                                                    f"Modification demandée : {text}"}],
                think=False, stop=stop, options={"temperature": 0.3, "num_predict": 300}):
            if kind == "content":
                parts.append(piece)
    except Exception as exc:
        log.info("Réécriture de la description impossible (%s) : demande ajoutée telle quelle", exc)
        return _appended(prompt, text)
    finally:
        timer.cancel()
    found = " ".join("".join(parts).split()).strip(" \"«»'")
    if stop.is_set() or len(found) < 3:
        return _appended(prompt, text)
    return found[:2000]
