"""Créer de l'audio dans la conversation (docs/conception/audio.md § 3), à côté des images et
des vidéos (chat/creations.py, qui appelle ce module) : une voix qui lit un texte, une musique,
un effet sonore.

La demande se reconnaît à sa tournure (intent) :
- une voix : un verbe de lecture et le texte, dit après deux-points ou entre guillemets
  (« Lis ce texte : … », « Dis à voix haute « … » », « Crée une voix qui dit : … ») ; sans
  eux, « Lis ce texte et résume-le » reste une question ;
- une musique : un verbe de création et un genre de musique (« Crée une musique calme pour
  méditer », « Compose une chanson sur la mer, paroles : … ») ;
- un effet sonore : un verbe de création, un son et ce qui le fait (« Fais le bruit d'une
  porte qui grince », « Génère un effet sonore d'explosion »).
L'interface a aussi le menu « Créer » : « Une voix », « Une musique », « Un effet sonore ».

Le modèle : celui demandé s'il peut se calculer, sinon « Automatique » (audio/ranking.py), ici,
chez mes machines, chez mes amis s'ils le proposent ; pas encore le réseau public. La
description d'une musique ou d'un effet est traduite et détaillée en anglais par le modèle de
conversation (chat/enrich.py) ; un texte à lire, jamais. La réponse garde la demande (media :
genre, modèle, texte ou description, paroles, durée, lot, travail).
"""
import re

from aiodos import settings
from aiodos.audio import catalog, files, lenders, ranking
from aiodos.chat import enrich
from aiodos.i18n import t
from aiodos.jobs.kinds import audio as kind

GENRES = catalog.GENRES
MAX_COUNT = kind.MAX_COUNT

# ------------------------------------------------------------- la demande, dans le texte
# Les motifs reconnaissent les demandes en français comme en anglais : ils ne se traduisent pas
# i18n: tel quel (motif de détection)
_READ = (r"lis|lire|lisez|relis|dis|dire|dites|prononce|prononcer|prononcez|r[ée]cite|"
         r"r[ée]citer|read|say|speak|recite")
# i18n: tel quel (motif de détection)
_WHAT = (r"(?:ce|cet|cette|ces|le|la|les|mon|ma|mes|this|the|my)?\s*(?:texte|phrase|passage|"
         r"paragraphe|message|po[èe]me|text|sentence|poem)?")
# i18n: tel quel (motif de détection)
_ALOUD = r"(?:[àa] voix haute|tout haut|[àa] haute voix|aloud|out loud)?"
_QUOTED = r"[«\"“](?P<quoted>[^«»\"“”]{2,})[»\"”]"   # i18n: tel quel (motif de détection)
_VOICE_READ = re.compile(         # i18n: tel quel (motif de détection)
    rf"^\s*(?:(?:peux|pourrais)[- ]tu\s+|(?:can|could) you\s+|please\s+)?(?P<verb>{_READ})"
    rf"(?:[- ](?:moi|nous|me|us))?\s*{_WHAT}\s*{_ALOUD}\s*"
    rf"(?:[:：]\s*(?P<text>\S.*)|{_QUOTED}\s*[.!]?\s*)$", re.IGNORECASE | re.DOTALL)
# i18n: tel quel (motif de détection)
_CREATE = (r"cr[ée]e|cr[ée]er|cr[ée]ez|g[ée]n[èée]re|g[ée]n[ée]rer|g[ée]n[ée]rez|fais|faites|"
           r"faire|fabrique|enregistre|produis|donne|compose|composer|composez|"
           r"create|generate|make|record|produce|give|compose")
# i18n: tel quel (motif de détection)
_VOICE_NOUN = (r"(?:une?|la|a|an|the)\s+(?:voix(?: off)?|audio|enregistrement|fichier audio|"
               r"lecture|voice(?:[- ]?over)?|recording|narration)")
_VOICE_MADE = re.compile(         # i18n: tel quel (motif de détection)
    rf"^\s*(?P<verb>{_CREATE})(?:[- ](?:moi|nous|me|us))?\s+{_VOICE_NOUN}\s*"
    rf"(?:qui (?:dit|lit|prononce)|disant|lisant|de ce texte|du texte|saying|reading|of this text|"
    rf"of)?\s*(?:[:：]\s*(?P<text>\S.*)|{_QUOTED}\s*[.!]?\s*)$", re.IGNORECASE | re.DOTALL)
# i18n: tel quel (motif de détection)
_ARTICLE = (r"une?|des|quelques|deux|trois|quatre|\d|la|le|les|ma|mon|l'|an?|some|the|two|"
            r"three|four")
# i18n: tel quel (motif de détection)
_ADJECTIVE = (r"petite?s?|belles?|beaux?|jolie?s?|nouvelles?|nouveaux?|courtes?|longues?|"
              r"douces?|calmes?|joyeuses?|tristes?|nice|beautiful|short|new|little|small|"
              r"cool|great|calm|happy|sad|soft|epic|[ée]piques?")
# i18n: tel quel (motif de détection)
_MUSIC = (r"musiques?|chansons?|morceaux?|m[ée]lodies?|airs?|jingles?|berceuses?|"
          r"bandes? originales?|bandes?[- ]sons?|instrumentales?|songs?|music|tracks?|tunes?|"
          r"melod(?:y|ies)|jingles?|lullab(?:y|ies)|soundtracks?|beats?")
# Un verbe sans ambiguïté : « Donne-moi des chansons de Brel » demande des titres, pas une
# création ; « joue » ou « play », une lecture
# i18n: tel quel (motif de détection)
_STRONG = (r"cr[ée]e|cr[ée]er|cr[ée]ez|g[ée]n[èée]re|g[ée]n[ée]rer|g[ée]n[ée]rez|compose|"
           r"composer|composez|create|generate")
# i18n: tel quel (motif de détection)
_MUSIC_VERB = rf"{_STRONG}|fais|faites|faire|fabrique|produis|make|produce"
_MUSIC_ASK = re.compile(          # i18n: tel quel (motif de détection)
    rf"(?:^|[\s,.;:!?«\"(])(?:(?:me|nous|lui)\s+|m')?(?P<verb>{_MUSIC_VERB})"
    rf"(?:[- ](?:moi|nous|me|us))?\s+(?P<n>{_ARTICLE})\s*(?:(?:{_ADJECTIVE})\s+){{0,3}}"
    rf"(?P<noun>{_MUSIC})\b(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
# i18n: tel quel (motif de détection)
_SOUND_NOUN = (r"bruits?|sons?|bruitages?|effets? sonores?|ambiances? sonores?|"
               r"sound effects?|sfx|sounds?|noises?")
_SOUND_ASK = re.compile(          # i18n: tel quel (motif de détection)
    rf"(?:^|[\s,.;:!?«\"(])(?:(?:me|nous|lui)\s+|m')?(?P<verb>{_CREATE})"
    rf"(?:[- ](?:moi|nous|me|us))?\s+(?P<n>{_ARTICLE})\s*(?P<noun>{_SOUND_NOUN})\s+"
    rf"(?P<link>de\s+|d'|du\s+|des\s+|of\s+|d'une?\s+)(?P<rest>\S.*)$",
    re.IGNORECASE | re.DOTALL)
# Les paroles d'une chanson, après « paroles : » (« lyrics: »)
_LYRICS = re.compile(r"[,.;]?\s*(?:avec les |avec des |with (?:the )?)?(?:paroles|lyrics)\s*[:：]\s*"
                     r"(?P<lyrics>\S.*)$", re.IGNORECASE | re.DOTALL)   # i18n: tel quel (motif)
_POLITE = re.compile(r"[\s,]*(s'il (?:te|vous) pla[iî]t|stp|svp|please|merci)\s*[.!?]*\s*$",
                     re.IGNORECASE)   # i18n: tel quel (motif de détection)
_NUMBERS = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "two": 2, "three": 3,
            "four": 4}
_PLURAL = {"des", "quelques", "les", "some"}


def _clean(text: str) -> str:
    # i18n: tel quel (apostrophes ramenées)
    return " ".join((text or "").replace("’", "'").replace("‘", "'").split())


def _polite(text: str) -> str:
    return _POLITE.sub("", text).strip().rstrip(" .;").strip()


def _count(word: str, plural: bool) -> int:
    word = word.lower()
    if word.isdigit():
        return max(1, min(int(word), MAX_COUNT))
    if word in _NUMBERS:
        return _NUMBERS[word]
    return 2 if plural and word in _PLURAL else 1


def _seconds(text: str) -> float | None:
    from aiodos.chat import creations
    found = creations._SECONDS.search(text)
    return float(found[1].replace(",", ".")) if found else None


def _without_duration(text: str) -> str:
    from aiodos.chat import creations
    return creations._DURATION.sub(" ", text, count=1)


def intent(text: str) -> dict | None:
    """{"kind": "voice" | "music" | "sound", "prompt", "lyrics", "count", "seconds"} si le
    message demande de créer de l'audio ; None sinon. Pour une voix, prompt est le texte à
    lire, tel qu'écrit."""
    from aiodos.chat import creations
    raw = (text or "").strip()
    text = _clean(raw)
    if not text or len(raw) > catalog.MAX_TEXT + 200:
        return None
    for pattern in (_VOICE_READ, _VOICE_MADE):
        found = pattern.match(raw.replace("’", "'"))      # i18n: tel quel (apostrophes)
        if found:
            said = (found["text"] or found["quoted"] or "").strip()
            said = said.strip("«»\"“” ").strip()          # i18n: tel quel (guillemets ôtés)
            if len(re.sub(r"\W", "", said)) >= 2:
                return {"kind": catalog.VOICE, "prompt": said[:catalog.MAX_TEXT],
                        "lyrics": None, "count": 1, "seconds": None}
    if creations._QUESTION.match(text) or creations.technical(text):
        return None
    found = _MUSIC_ASK.search(text)
    genre = catalog.MUSIC
    if found is None:
        found = _SOUND_ASK.search(text)
        genre = catalog.SOUND
    if found is None or creations._PAST.search(text[:found.start("verb")]):
        return None
    if not re.fullmatch(_STRONG, found["verb"], re.IGNORECASE) and text.rstrip().endswith("?"):
        return None                   # « Tu me fais une musique ? » : une question
    rest = found["rest"]
    lyrics = None
    if genre == catalog.MUSIC:
        sung = _LYRICS.search(rest)
        if sung:
            lyrics = sung["lyrics"].strip()[:catalog.MAX_TEXT]
            rest = rest[:sung.start()]
    seconds = _seconds(rest)
    rest = _without_duration(rest)
    if genre == catalog.SOUND:
        subject = _polite(rest)
        noun = found["noun"].lower()
        subject = f"{noun} {found['link'].strip()} {subject}".replace("d' ", "d'") \
            if subject else ""
    else:
        if not _polite(rest) and not lyrics:
            return None               # « Crée une musique » : rien à dire de plus
        subject = _polite(f"{found['noun']}{rest}")
    if len(re.sub(r"\W", "", subject)) < 3:
        return None
    plural = found["noun"].lower().endswith(("s", "x"))
    return {"kind": genre, "prompt": subject[:catalog.MAX_TEXT], "lyrics": lyrics,
            "count": _count(found["n"], plural), "seconds": seconds}


# ----------------------------------------------------------------- modèle et réglages
def is_audio(kind_name) -> bool:
    return kind_name in GENRES


def language(request: dict | None = None) -> str:
    """La langue d'une voix : celle de la demande, sinon celle de l'interface."""
    return ranking.language_of((request or {}).get("language"))


def preflight(found: dict) -> dict:
    """La demande reconnue, et le modèle qui la calculerait (None : aucun en ce moment)."""
    choice = ranking.choose(found["kind"], None, language())
    return {**found, "model": choice.model,
            "label": catalog.label(choice.model) if choice.model else None}


def usable() -> list:
    """Les genres d'audio qu'un modèle peut calculer en ce moment (ici, mes machines, mes
    amis si je l'accepte)."""
    return [genre for genre in GENRES if ranking.choose(genre, None, language()).model]


def nothing_ready(genre: str):
    """Aucun modèle de ce genre ne peut calculer : la façon d'en avoir un. Des amis qui le
    proposeraient, si on les y autorise, sont nommés."""
    from aiodos.chat.creations import CreationError
    offered = sorted({name for model_id, names in lenders.helpers(friends=True).items()
                      if model_id in catalog.MODELS and catalog.genre(model_id) == genre
                      for name in names})
    if offered and not settings.get("images_from_friends"):
        return CreationError(t("chat_audio.nothing_ready_friends", n=len(offered),
                               who=", ".join(offered)), status=409, friends=offered)
    # chat_audio.nothing_ready_voice, chat_audio.nothing_ready_music, chat_audio.nothing_ready_sound
    return CreationError(t(f"chat_audio.nothing_ready_{genre}"), status=409)


def prepare(text: str, request: dict, found: dict, conv_id, chat_model, mine_only) -> dict:
    """Le plan qu'accept() enregistre (chat/creations.py) pour une création audio ;
    CreationError si elle ne peut pas aboutir."""
    from aiodos.chat.creations import CreationError
    genre = request.get("kind") or found.get("kind")
    if request.get("attachments"):
        raise CreationError(t("chat_audio.no_attachment"))
    lang = language(request)
    choice = ranking.choose(genre, request.get("model"), lang)
    if choice.model is None:
        raise nothing_ready(genre)
    model_id = choice.model
    asked = request.get("prompt") or found.get("prompt") or text
    params = {"model": model_id, "count": request.get("count") or found.get("count") or 1,
              "seed": request.get("seed")}
    if genre == catalog.VOICE:
        voice = request.get("voice") if model_id == request.get("model") else None
        params.update(text=asked, voice=voice, language=lang)
    else:
        prompt, lyrics = asked, request.get("lyrics") or found.get("lyrics") or ""
        sung = _LYRICS.search(prompt) if genre == catalog.MUSIC and not lyrics else None
        if sung and prompt[:sung.start()].strip():   # « Créer » : « …, paroles : … »
            prompt, lyrics = prompt[:sung.start()].strip(), sung["lyrics"].strip()
        if enrich.wanted(prompt):
            prompt = enrich.enrich(prompt, genre, chat_model)
        seconds = request.get("seconds") or found.get("seconds")
        spec = catalog.model(model_id)
        if seconds:
            seconds = max(1, min(int(round(float(seconds))), spec["max_seconds"]))
        params.update(prompt=prompt, duration=seconds or spec["default_seconds"],
                      lyrics=lyrics)
    try:
        params = kind.normalize(params)
    except ValueError as exc:
        raise CreationError(str(exc)) from exc
    return {"text": text, "conv_id": conv_id, "chat_model": chat_model, "model_id": model_id,
            "params": params, "auto": bool(request.get("auto")), "attachments": [],
            "reference": False, "mine_only": bool(mine_only), "engine": "audiocpp",
            "where": choice.where}


def media(params: dict, job_id) -> dict:
    """Ce que garde la réponse de l'assistant : le genre, le modèle, le texte lu ou la
    description, les paroles, la durée demandée, le lot et le travail."""
    found = {"kind": params["genre"], "media": "audio", "model": params["model"],
             "label": catalog.label(params["model"]),
             "license": catalog.model(params["model"])["license"],
             "prompt": params.get("text") or params.get("prompt"),
             "count": params["count"], "batch": params["batch"], "job_id": job_id,
             "seed": params["seed"], "width": 0, "height": 0}
    for key in ("voice", "language", "lyrics", "duration"):
        if params.get(key):
            found[key] = params[key]
    return found


def describe(media_row: dict) -> str:
    """Ce que le modèle de conversation lit de cette réponse (historique, mémoire)."""
    # chat_prompts.creation_voice, chat_prompts.creation_music, chat_prompts.creation_sound
    return t(f"chat_prompts.creation_{media_row.get('kind')}", n=int(media_row.get("count") or 1),
             model=media_row.get("label") or media_row.get("model"),
             prompt=media_row.get("prompt"))


def ready_here(model_id: str) -> bool:
    return files.ready(model_id)
