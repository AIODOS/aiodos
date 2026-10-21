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
from aiodos.i18n import t
from aiodos.services import ollama

log = logging.getLogger("aiodos.chat")

VARIATION, EDIT = "variation", "edit"
MAX_TEXT = 300
MAX_WORDS = 8                  # une retouche sans pronom (« avec un chien ») est courte
REWRITE_S = 45                 # au-delà, la demande s'ajoute simplement à la description

# Les tournures sont prises au plus juste : après une image, la conversation continue
# aussi en texte (« Encore merci », « Change de sujet », « Plus précisément, explique… »),
# et chaque retouche de trop en ajouterait une autre. « Répondre en texte » rattrape le reste.
# Elles reconnaissent les demandes en français comme en anglais : elles ne se traduisent pas.
# i18n: tel quel (motif de détection)
_POLITE_END = r"(?:\s*,?\s*(?:stp|svp|s'il (?:te|vous) pla[iî]t|please))?\s*[.!]*\s*$"
# i18n: tel quel (motif de détection)
_AGAIN = (r"(?:et\s+)?(?:encore(?:\s+une?)?|une autre|un autre|d'autres|refais[- ](?:la|le|les)|"
          r"refais|recommence|again|another(?:\s+one)?|one more)")
_VARIATION = re.compile(rf"^\s*{_AGAIN}{_POLITE_END}", re.IGNORECASE)
# « Encore une, mais de nuit » : une variante qui change quelque chose est une retouche
# i18n: tel quel (motif de détection)
_VARIATION_EDIT = re.compile(rf"^\s*{_AGAIN}\s*,?\s*(?:mais\s+|but\s+)?"
                             r"(?:avec|sans|en|de|plus|moins|with|without)\s+\S", re.IGNORECASE)
# i18n: tel quel (motif de détection)
_SAME = re.compile(rf"^\s*(?:pareil|la même|le même|les mêmes|même chose|same){_POLITE_END}",
                   re.IGNORECASE)
# i18n: tel quel (motif de détection)
_SAME_EDIT = re.compile(r"^\s*(?:pareil|la même|le même|les mêmes|même chose|same)\s*,?\s*"
                        r"(?:mais|avec|sans|en|but|with|without)\b", re.IGNORECASE)
# Un pronom qui renvoie à la création : « Fais-la de nuit », « Peux-tu la rendre plus claire ? »
_PRONOUN = re.compile(            # i18n: tel quel (motif de détection)
    r"^\s*(?:(?:et\s+)?(?:maintenant|puis|ensuite)\s*,?\s*)?(?:(?:peux-tu|tu peux|pourrais-tu|"
    r"tu pourrais)\s+)?(?:(?:fais|refais|rends|mets)[- ](?:la|le|les)\b|"
    r"(?:la|le|les)\s+(?:faire|refaire|rendre|mettre)\b|make it\b)", re.IGNORECASE)
# Sans pronom, une retouche courte : un verbe à l'impératif, ou ce qui change
_SHORT = re.compile(              # i18n: tel quel (motif de détection)
    r"^\s*(?:(?:ajoute|rajoute|enlève|enleve|retire|remplace|change|transforme|mets|add|remove)\s|"
    r"(?:plus|moins|more|less)\s+\w|(?:avec|sans|with|without)\s+\S|"
    r"en (?:noir et blanc|couleurs?|hiver|été|automne|printemps|pleine nuit|plein jour|"
    r"aquarelle|dessin|pixel art|3d|style\s+\S)|de (?:nuit|jour)\b|"
    r"(?:style|façon|version)\s+\S|à la manière de\s+\S)", re.IGNORECASE)
# i18n: tel quel (motif de détection)
_QUESTION = re.compile(r"^\s*(comment|pourquoi|quel|quelle|quels|quelles|combien|qui|où|"
                       r"est-ce|qu'est-ce|explique|how|why|what|which|who|where)\b", re.IGNORECASE)
# i18n: tel quel (motif de détection)
_POLITE_ASK = re.compile(r"^\s*(?:peux-tu|tu peux|pourrais-tu|tu pourrais)\s+(?:la|le|les)\b",
                         re.IGNORECASE)
# Une demande de texte, pas d'image (« Change de sujet : parle-moi de Rome »)
_TEXT_TASK = re.compile(          # i18n: tel quel (motif de détection)
    r"\b(explique\w*|parle\w*|résume\w*|resume\w*|raconte\w*|dis-moi|écris|ecris|écrire|"
    r"traduis|tradui\w*|réponds|reponds|sujet|question|texte|phrase|paragraphe|conclusion|"
    r"introduction|liste|doublons|mail|e-mail|courriel|lettre|fonction|tableau)\b",
    re.IGNORECASE)
# Des tournures qui commencent comme une retouche sans en être une
# i18n: tel quel (motif de détection)
_NOT_EDIT = re.compile(r"^\s*(sans (rire|blague|doute|problème|souci|vouloir)|avec plaisir|"
                       r"plus tard|plus jamais|plus ou moins|même pas|pareil pour moi|"
                       r"de rien|en effet|en fait|en tout cas|en gros|plus précisément|"
                       r"change de sujet)\b", re.IGNORECASE)
# i18n: tel quel (motif de détection)
_ONE = re.compile(r"^\s*(?:et\s+)?(?:encore une?|une autre|un autre|another|one more)\b",
                  re.IGNORECASE)
# i18n: tel quel (motif de détection)
_POLITE = re.compile(r"[\s,]*(s'il (?:te|vous) pla[iî]t|stp|svp|please|merci)\s*[.!?]*\s*$",
                     re.IGNORECASE)
# i18n: tel quel (motif de détection)
_LEAD = re.compile(r"^\s*(?:(?:et\s+)?(?:maintenant|puis|ensuite)\s*,?\s*)?(?:(?:peux-tu|tu peux|"
                   r"pourrais-tu|tu pourrais)\s+)?(?:(?:fais|refais|rends|mets)[- ](?:la|le|les)|"
                   r"(?:la|le|les)\s+(?:faire|refaire|rendre|mettre))\s*", re.IGNORECASE)


def detect(text: str) -> str | None:
    """VARIATION, EDIT, ou None si le message n'est pas une retouche de la dernière création."""
    text = " ".join((text or "").replace("’", "'").split())   # i18n: tel quel (apostrophe)
    if not text or len(text) > MAX_TEXT or _QUESTION.match(text):
        return None
    if text.endswith("?") and not _POLITE_ASK.match(text):
        return None
    core = _POLITE.sub("", text).strip()          # « Encore une, merci ! » → « Encore une »
    from aiodos.chat.creations import technical      # même liste de sujets de code
    if not core or technical(text) or _NOT_EDIT.match(core) or _TEXT_TASK.search(core):
        return None
    if _VARIATION.match(core) or _SAME.match(core):
        return VARIATION
    if _VARIATION_EDIT.match(core) or _SAME_EDIT.match(core) or _PRONOUN.match(core):
        return EDIT
    if _SHORT.match(core) and len(core.split()) <= MAX_WORDS:
        return EDIT
    return None


# Retouche d'une image (image vers image) : la part de l'image redessinée. Avec les 8 pas de
# Z-Image Turbo, elle avance par huitièmes (mesuré le 18/10) : 6/8 garde la scène et sa
# lumière (ajouter, retirer, changer un détail) ; 7/8 change l'ambiance (nuit, saison,
# style), au prix d'une scène en partie recomposée.
KEEP, AMBIANCE = 0.75, 0.875
# Une image jointe (une référence) : sa composition et ses couleurs restent, la demande change
# le reste (à 7/8, essayé le 21/10/2026, la référence ne se voyait presque plus)
REFERENCE = 0.65
_AMBIANCE = re.compile(           # i18n: tel quel (motif de détection)
    r"\b(nuit|jour|soir|matin|aube|crépuscule|coucher|lever|hiver|été|automne|printemps|"
    r"neige|pluie|orage|brouillard|style|façon|manière|noir et blanc|couleurs?|aquarelle|"
    r"dessin|peinture|pixel|3d|réaliste|cartoon|manga|anime|vintage|rétro|futuriste|"
    r"night|day|winter|summer|autumn|spring|sunset|sunrise)\b", re.IGNORECASE)


def strength_for(text: str) -> float:
    """La force de retouche selon la demande : une ambiance, ou un détail."""
    return AMBIANCE if _AMBIANCE.search(text or "") else KEEP


def variation_count(text: str, last_count: int) -> int:
    """« Encore une », « Une autre » : une seule ; « Encore », « Refais-les » : autant qu'avant."""
    return 1 if _ONE.match(text or "") else max(1, int(last_count or 1))


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


def _appended(prompt: str, text: str, limit: int = 2000) -> str:
    """Sans modèle de conversation : la demande, sans « fais-la », après la description ;
    au-delà de la longueur permise, le début de la description cède la place, jamais la
    demande."""
    extra = _POLITE.sub("", _LEAD.sub("", text)).strip(" ,.!")
    if not extra:
        return prompt[:limit]
    room = max(0, limit - len(extra) - 2)
    return f"{prompt[:room].rstrip(' ,')}, {extra}"[:limit]


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
                model, [{"role": "system", "content": t("chat_prompts.rewrite_system")},
                        {"role": "user", "content": t("chat_prompts.rewrite_request",
                                                      prompt=prompt, change=text)}],
                think=False, stop=stop, options={"temperature": 0.3, "num_predict": 300},
                timeout=(10, REWRITE_S)):     # un modèle long à charger ne bloque pas plus
            if kind == "content":
                parts.append(piece)
    except Exception as exc:
        log.info("Réécriture de la description impossible (%s) : demande ajoutée telle quelle", exc)
        return _appended(prompt, text)
    finally:
        timer.cancel()
    found = " ".join("".join(parts).split()).strip(" \"«»'")   # i18n: tel quel (guillemets)
    if stop.is_set() or len(found) < 3:
        return _appended(prompt, text)
    return found[:2000]
