"""Créer une image ou une vidéo dans la conversation, comme dans les autres assistants : la
demande (« Crée une image d'un phare sous l'orage ») devient un travail d'images de la file
(jobs/kinds/images.py), calculé ici ou par mes machines qui ont le modèle, et la réponse
de l'assistant montre les images à mesure qu'elles arrivent.

La demande se reconnaît à sa tournure (intent) : un verbe de création, un genre d'image ou
de vidéo, puis ce qu'elle doit montrer. Une question (« comment créer une image Docker ? »)
ou un sujet de code n'en est pas une ; l'interface a aussi son bouton « Créer », explicite.

La réponse de l'assistant garde la demande (colonne media : lot, travail, modèle,
description) ; son texte la résume pour le modèle de conversation (historique, mémoire),
qui sait ainsi ce qui a été créé, sans voir les images.
"""
import re

from aiodos import db, settings
from aiodos.chat import conversations, retouches
from aiodos.imaging import catalog, gallery, lenders
from aiodos.jobs import queue
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.models import URGENT

MAX_VIDEOS = 4                    # vidéos demandées d'une phrase, au plus (plusieurs minutes chacune)


class CreationError(Exception):
    """status : code HTTP ; friends : amis qui pourraient calculer, si on les y autorise."""

    def __init__(self, message, status=400, friends=None):
        super().__init__(message)
        self.status = status
        self.friends = friends or []


# ------------------------------------------------------------- la demande, dans le texte
_STRONG = (r"cr[ée]e|cr[ée]é|cr[ée]er|cr[ée]ez|cr[ée]es|g[ée]n[èée]re|g[ée]n[ée]rer|g[ée]n[ée]rez|"
           r"dessine|dessiner|dessinez|peins|peindre|peignez|imagine|imaginer|imaginez|"
           r"create|generate|draw|paint|render")
_WEAK = (r"fais|faites|faire|fabrique|fabriquer|fabriquez|r[ée]alise|r[ée]aliser|r[ée]alisez|"
         r"produis|produire|produisez|donne|donner|donnez|envoie|envoyer|envoyez|montre|"
         r"montrer|montrez|j'aimerais|je voudrais|je veux|il me faut|make|give|show|send|"
         r"i want|i'd like|i would like")
_ARTICLE = (r"une?|des|quelques|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|douze|seize|"
            r"\d{1,2}|l'|la|le|les|ma|mon|mes|an?|some|two|three|four|five|six|eight|ten")
_ADJECTIVE = (r"petite?s?|belles?|beaux?|jolie?s?|nouvelles?|nouveaux?|autres?|courtes?|"
              r"superbes?|magnifiques?|grandes?|grands?|vraie?s?|r[ée]alistes?|simples?|nice|"
              r"beautiful|short|new|quick|little|small|cool|great|realistic")
_IMAGES = (r"images?|photos?|photographies?|dessins?|logos?|fonds? d'[ée]cran|wallpapers?|"
           r"pictures?|drawings?|affiches?|posters?|ic[ôo]nes?|visuels?|croquis|esquisses?|"
           r"aquarelles?|icons?|sketch(?:es)?")
# Noms qui ont aussi un sens figuré (« donne-moi une illustration de ce principe », « un
# portrait de Napoléon ») : seulement avec un verbe de création sans ambiguïté
_IMAGES_STRONG = r"illustrations?|peintures?|portraits?|paintings?"
_VIDEOS = r"vid[ée]os?|clips?|animations?|videos?"

_REQUEST = re.compile(
    rf"(?:^|[\s,.;:!?«\"(])(?:(?:me|nous|lui|leur)\s+|m')?(?P<verb>{_STRONG}|{_WEAK})"
    rf"(?:[- ](?:moi|nous|lui|leur|me|us))?\s+(?P<n>{_ARTICLE})\s*"
    rf"(?:(?:{_ADJECTIVE})\s+){{0,3}}(?P<noun>{_IMAGES}|{_IMAGES_STRONG}|{_VIDEOS})\b"
    rf"(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
_DRAWING = re.compile(
    rf"(?:^|[\s,.;:!?«\"(])(?:(?:me|nous|lui)\s+|m')?(?P<verb>dessine\w*|peins|peindre|"
    rf"peignez|draw|paint)(?:[- ](?:moi|nous|lui|me|us))?\s+(?P<rest>(?:{_ARTICLE})\s*\S.*)$",
    re.IGNORECASE | re.DOTALL)

# Une question ou un sujet de code : la réponse est un texte, pas une image
_QUESTION = re.compile(r"^\s*(comment|pourquoi|quels?|quelles?|combien|explique\w*|how|why|"
                       r"what|which)\b", re.IGNORECASE)
_TECHNICAL = re.compile(
    r"\b(docker\w*|iso|kubernetes|k8s|vm|machines? virtuelles?|bootable|base ?64|svg|ascii|"
    r"latex|tikz|matplotlib|pillow|pil|opencv|canvas|html|css|python|javascript|typescript|"
    r"js|rust|golang|code|script|fonction|commande|terminal|powershell|bash|api|sql|regex|"
    r"excel|qemu|vmware|virtualbox|wim|vhdx?|qcow2?|sch[ée]mas?|diagrammes?|"
    r"organigrammes?|graphes?|mermaid|uml|"
    r"images? (?:de |d')?(?:disques?|syst[èe]mes?|conteneurs?|containers?))\b",
    re.IGNORECASE)
_NOT_A_DRAWING = re.compile(r"^(?:\S+\s+)?(tableaux?|graphiques?|plans?|arbres?|courbes?|"
                            r"cartes? mentales?)\b", re.IGNORECASE)
_LINK = re.compile(r"^\s*(?:[:—–-]\s*|de\s+|d'|d\s+(?=une?\b)|du\s+|des\s+|of\s+|avec\s+|with\s+|"
                   r"qui\s+(?:montre|repr[ée]sente)\w*\s+|montrant\s+|repr[ée]sentant\s+|"
                   r"showing\s+|o[ùu]\s+|where\s+|sur\s+laquelle\s+|sur\s+lequel\s+)",
                   re.IGNORECASE)
# Le format dit dans la phrase sert à choisir la taille (shape_of), pas à décrire l'image
_FORMAT = re.compile(r"\s*\b(?:en|au|format)\s+(?:format\s+)?(?:16[:/]9|9[:/]16|1[:/]1|4[:/]3|"
                     r"3[:/]4|portrait|paysage|carr[ée]e?|vertical\w*|horizontal\w*)\b",
                     re.IGNORECASE)
# « J'ai créé une image hier » : un récit, pas une demande (l'auxiliaire juste avant le verbe)
_PAST = re.compile(r"\b(?:ai|as|a|avons|avez|ont)\s+(?:d[ée]j[àa]\s+|bien\s+|aussi\s+)?$",
                   re.IGNORECASE)
_POLITE = re.compile(r"[\s,]*(s'il (?:te|vous) pla[iî]t|stp|svp|please|merci)\s*[.!?]*\s*$",
                     re.IGNORECASE)
_MEDIUM = re.compile(r"^(images?|pictures?|vid[ée]os?|videos?|clips?)$", re.IGNORECASE)
# Noms qui servent aussi au figuré : avec « donne-moi », « je voudrais »…, il faut dire ce
# qu'ils montrent (« une image de… ») ; une affiche ou un logo se demandent sans détour
_LOOSE = re.compile(r"^(images?|photos?|photographies?|pictures?|vid[ée]os?|videos?|clips?|"
                    r"animations?)$", re.IGNORECASE)
# Des expressions où « image » ou « photo » ne demandent rien à dessiner
_FIGURATIVE = re.compile(r"\b(?:images? de marque|photos? d'identit[ée]|images? publique)",
                         re.IGNORECASE)
_EMPTY = re.compile(r"^(ça|ca|cela|ceci|it|this|that|quelque chose|something)$", re.IGNORECASE)
_NUMBERS = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6,
            "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "douze": 12, "seize": 16, "two": 2,
            "three": 3, "four": 4, "five": 5, "eight": 8, "ten": 10}
_PLURAL = {"des", "quelques", "les", "mes", "some"}

# Format : un mot explicite d'abord, puis l'écran visé, puis le sujet lui-même
_SHAPES = (
    ("square", re.compile(r"\b(carr[ée]e?s?|square|1[:/]1)\b", re.IGNORECASE)),
    ("portrait", re.compile(r"\b(vertical\w*|9[:/]16|format portrait|en portrait|"
                            r"t[ée]l[ée]phones?|smartphones?|stor(?:y|ies))\b", re.IGNORECASE)),
    ("landscape", re.compile(r"\b(horizontal\w*|16[:/]9|format paysage|en paysage|"
                             r"panoram\w*|banni[èe]res?|banners?|wide)\b", re.IGNORECASE)),
    ("portrait", re.compile(r"\bportraits?\b", re.IGNORECASE)),
    ("landscape", re.compile(r"\b(paysages?|fonds? d'[ée]cran|wallpapers?|landscapes?)\b",
                             re.IGNORECASE)),
)
_SECONDS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:s|sec|secs|secondes?|seconds?)\b", re.IGNORECASE)
# La durée d'une vidéo n'a rien à faire dans sa description (« de 5 secondes d'un renard »)
_DURATION = re.compile(r"(?:^|,?\s+(?:de|d'une durée de|pendant|durant|of)?\s*)"
                       r"\d+(?:[.,]\d+)?\s*(?:s|sec|secs|secondes?|seconds?)\b\s*(?:de\s+|d')?",
                       re.IGNORECASE)


def technical(text: str) -> bool:
    """Un sujet de code ou de système (Docker, ISO, Python…) : la réponse est un texte."""
    return _TECHNICAL.search(text or "") is not None


def _clean(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


def _subject(rest: str) -> str:
    rest = _POLITE.sub("", rest).strip()
    return rest.rstrip(" .!?;").strip()


def intent(text: str) -> dict | None:
    """{"kind": "image" | "video", "prompt", "count", "shape", "seconds"} si le message
    demande de créer une image ou une vidéo ; None sinon (question, code, simple mention)."""
    text = _clean(text or "")
    if not text or len(text) > 2000 or _QUESTION.match(text) or technical(text) \
            or _FIGURATIVE.search(text):
        return None
    found = _REQUEST.search(text)
    if found is not None and _PAST.search(text[:found.start("verb")]):
        return None
    found = found or _elliptic(text)
    if found:
        verb, noun = found["verb"].lower(), found["noun"].lower()
        strong = re.fullmatch(_STRONG, verb, re.IGNORECASE) is not None
        if not strong and text.rstrip().endswith("?"):
            return None               # « Je veux une vidéo de mon mariage, qui contacter ? »
        if not strong and re.fullmatch(_IMAGES_STRONG, noun, re.IGNORECASE):
            return None               # « donne-moi une illustration de ce principe » : un exemple
        rest = _FORMAT.sub("", found["rest"], count=1)
        link = _LINK.match(rest)
        if not _subject(rest):
            return None               # « Envoie-moi une photo », « Tu sais créer des images ? »
        if not strong and not link and _LOOSE.match(noun):
            return None               # « donne-moi une image claire de la situation »
        video = re.fullmatch(_VIDEOS, noun, re.IGNORECASE) is not None
        if video:
            rest = _DURATION.sub(" ", rest, count=1)
            link = _LINK.match(rest)
        if _MEDIUM.match(noun):
            subject = _subject(rest[link.end():] if link else rest)
        else:
            subject = _subject(f"{noun}{rest}")
        plural = noun.endswith(("s", "x")) and not noun.endswith("croquis")
        count = _count(found["n"].lower(), plural)
    else:
        found = _DRAWING.search(text)
        if not found or _NOT_A_DRAWING.match(found["rest"]):
            return None
        verb = found["verb"].lower()
        word = {"draw": "drawing", "paint": "painting"}.get(verb) or (
            "peinture" if verb.startswith("pei") else "dessin")
        subject = _subject(found["rest"])
        subject = f"{subject}, {word}" if subject else ""
        video, count = False, 1
    if len(re.sub(r"[^\w]", "", subject)) < 3 or _EMPTY.match(subject):
        return None
    seconds = _SECONDS.search(text) if video else None
    return {"kind": "video" if video else "image", "prompt": subject[:kind.MAX_PROMPT],
            "count": min(count, MAX_VIDEOS if video else kind.MAX_COUNT),
            "shape": shape_of(text),
            "seconds": float(seconds[1].replace(",", ".")) if seconds else None}


_ELLIPTIC = re.compile(
    rf"^\s*(?P<n>une?|des|deux|trois|quatre|cinq|six|\d{{1,2}})\s+(?:(?:{_ADJECTIVE})\s+){{0,2}}"
    rf"(?P<noun>{_IMAGES}|{_VIDEOS})\b(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)


def _elliptic(text: str):
    """« Une image de chat, stp » : une demande sans verbe, au début du message, qui dit ce
    qu'il faut montrer (« Une image vaut mille mots » n'en est pas une, ni une question)."""
    found = _ELLIPTIC.match(text)
    if found is None or text.rstrip().endswith("?") or not _LINK.match(found["rest"]):
        return None
    return {"verb": "crée", "n": found["n"], "noun": found["noun"], "rest": found["rest"]}


def _count(word: str, plural: bool) -> int:
    if word.isdigit():
        return max(1, int(word))
    if word in _NUMBERS:
        return _NUMBERS[word]
    return 2 if plural and word in _PLURAL else 1


def shape_of(text: str) -> str | None:
    for shape, pattern in _SHAPES:
        if pattern.search(text):
            return shape
    return None


# ----------------------------------------------------------------- modèle et réglages
def usable(media_kind: str | None = None) -> list:
    """Modèles qui peuvent se calculer en ce moment (ici, ou chez mes machines en ligne),
    dans l'ordre du catalogue, ceux à licence restrictive en dernier."""
    others = lenders.helpers()
    found = []
    for model_id, spec in catalog.MODELS.items():
        if media_kind and spec.get("kind", "image") != media_kind:
            continue
        here, away = lenders.can_compute(model_id, others)
        if here or away:
            found.append(model_id)
    return sorted(found, key=lambda m: bool(catalog.MODELS[m].get("notice")))


def pick(media_kind: str, preferred: str | None = None) -> str | None:
    """Le modèle préféré s'il peut se calculer, sinon le premier qui le peut."""
    models = usable(media_kind)
    if preferred in models:
        return preferred
    return models[0] if models else None


def size_for(model_id: str, shape: str | None) -> tuple | None:
    """(largeur, hauteur) du premier format du modèle qui a cette forme."""
    test = {"portrait": lambda w, h: h > w, "landscape": lambda w, h: w > h,
            "square": lambda w, h: w == h}.get(shape or "")
    if test is None:
        return None
    return next(((w, h) for w, h, *_ in catalog.model(model_id)["sizes"] if test(w, h)), None)


def frames_for(model_id: str, seconds: float | None) -> int | None:
    """Le nombre d'images de la durée proposée la plus proche (vidéos)."""
    spec = catalog.model(model_id)
    if not seconds or not spec.get("durations"):
        return None
    fps = spec.get("fps") or 16
    return min((n for n, _ in spec["durations"]), key=lambda n: abs(n / fps - seconds))


def describe(media: dict) -> str:
    """Ce que le modèle de conversation lit de cette réponse (historique, mémoire)."""
    n, video = int(media.get("count") or 1), media.get("kind") == "video"
    what = ("vidéo" if video else "image") + ("s" if n > 1 else "")
    many = f"de {n}" if n > 1 else "d'une"
    return (f"(Création {many} {what} avec {media.get('label') or media.get('model')}, "
            f"d'après la description : « {media.get('prompt')} ».)")


def capability_note() -> str | None:
    """Pour le modèle de conversation : AIODOS sait créer des images (et des vidéos) quand
    un modèle peut les calculer ; sinon rien."""
    kinds = {catalog.MODELS[m].get("kind", "image") for m in usable()}
    if not kinds:
        return None
    what = "des images et des vidéos" if kinds == {"image", "video"} else (
        "des vidéos" if kinds == {"video"} else "des images")
    return (f"AIODOS, l'application où tu réponds, sait aussi créer {what} : l'utilisateur "
            "les demande dans la conversation (par exemple « Crée une image d'un phare sous "
            "l'orage ») et AIODOS les crée lui-même. Si on te demande si tu sais en créer, "
            "réponds que oui et invite à décrire ce qu'il faut créer. Tu ne vois pas les "
            "images créées : tu n'en connais que la description.")


def preflight(text: str, conversation_id=None, before=None) -> dict | None:
    """La demande reconnue dans le message, et le modèle qui la calculerait (None : aucun
    ne le peut en ce moment). Après une création, une retouche (« Fais-la de nuit ») ou
    une variante (« Encore une ») de celle-ci : followup, d'après chat/retouches.py.
    before : le message remplacé (« Modifier ») ; la création d'avant lui compte."""
    found = intent(text)
    if found is None and conversation_id is not None:
        last = conversations.last_media(conversation_id, before)
        followup = retouches.detect(text) if last else None
        if followup:
            count = last.get("count") or 1
            if followup == retouches.VARIATION:      # « Encore une » après quatre : une
                count = retouches.variation_count(text, count)
            found = {"kind": last.get("kind") or "image", "prompt": None,
                     "count": count, "shape": None, "seconds": None,
                     "followup": followup, "model_wanted": last.get("model")}
    if found is None:
        return None
    model_id = pick(found["kind"], found.pop("model_wanted", None))
    return {**found, "model": model_id,
            "label": catalog.MODELS[model_id]["label"] if model_id else None}


# ------------------------------------------------------------------- dans la conversation
def _nothing_can(media_kind: str) -> CreationError:
    """Aucun modèle de ce genre ne peut calculer : la façon d'en avoir un. Des amis qui
    le proposeraient, si on les y autorise, sont nommés (friends)."""
    what = "vidéos" if media_kind == "video" else "images"
    offered = sorted({name for model_id, names in lenders.helpers(friends=True).items()
                      if catalog.MODELS.get(model_id, {}).get("kind", "image") == media_kind
                      for name in names})
    if offered and not settings.get("images_from_friends"):
        who = ", ".join(offered)
        return CreationError(
            f"Aucun modèle de {what} n'est prêt ici ni chez tes machines, mais {who} "
            f"{'peuvent' if len(offered) > 1 else 'peut'} les calculer pour toi : ta "
            f"description et le résultat passeront chez {'eux' if len(offered) > 1 else 'lui'} "
            f"le temps du calcul, sans y être gardés.", status=409, friends=offered)
    return CreationError(f"Aucun modèle de {what} ne peut calculer en ce moment, ni ici ni "
                         f"chez tes machines en ligne : installe-en un dans « Images et "
                         f"vidéos ».", status=409)


def _followed(request: dict, text: str, conv_id, chat_model, before=None) -> dict:
    """Une retouche ou une variante de la dernière création de la conversation (avant le
    message remplacé, s'il y en a un) : ses réglages (modèle, format, durée, nombre), et
    sa description retouchée ou reprise."""
    last = conversations.last_media(conv_id, before) if conv_id is not None else None
    if last is None:
        return request
    edit = request.get("followup") == retouches.EDIT
    prompt = retouches.rewrite(last["prompt"], text, chat_model) if edit else last["prompt"]
    count = request.get("count") or (last.get("count") if edit
                                     else retouches.variation_count(text, last.get("count")))
    followed = {**request, "kind": last.get("kind") or "image", "model": last.get("model"),
                "prompt": prompt, "width": last.get("width"), "height": last.get("height"),
                "frames": last.get("frames"), "count": count,
                "shape": None, "seconds": None,
                # Retouche : même graine, la composition reste ; variante : une autre
                "seed": last.get("seed") if edit else None}
    single = int(last.get("count") or 1) == 1 and int(count or 1) == 1
    start_from = _first_image(last) if edit and single and last.get("kind") != "video" else None
    if start_from is not None:        # une image retouchée part de la précédente
        followed["init_path"] = str(start_from)
        followed["strength"] = retouches.strength_for(text)
    return followed


def _first_image(media: dict):
    """Le fichier de la première image d'une création, s'il est encore là."""
    for row in gallery.batch(str(media.get("batch") or "")):
        path = gallery.path_of(row)
        if path.suffix == ".png" and path.is_file():
            return path
    return None


def start(text: str, request: dict, conversation_id=None, chat_model=None,
          replace_from=None, replace_text=None) -> dict:
    """Ajoute la demande à la conversation (créée au besoin) et lance son travail.
    request : kind, model, prompt, count, width, height, shape, frames, seconds, seed,
    negative (vides : tirés du message, du modèle et de ses réglages par défaut), et
    followup (retouche ou variante de la dernière création, chat/retouches.py).
    replace_from, replace_text : « Modifier » ; ce message de l'utilisateur et la suite
    sont retirés (replace), une fois la demande acceptée.
    → {"conversation_id", "title", "user_message_id", "message_id", "job_id", "media"} ;
    CreationError si rien ne peut la calculer, ou si elle est invalide."""
    text = (text or "").strip()
    if not text:
        raise CreationError("Message vide.")
    conv_id = conversation_id
    if conv_id is not None and not conversations.get(conv_id):
        conv_id = None
    if replace_from is not None and conv_id is None:
        raise CreationError(CHANGED, status=CHANGED_STATUS)
    if request.get("followup") and not request.get("prompt"):
        request = _followed(request, text, conv_id, chat_model, replace_from)
    found = intent(text) or {}
    media_kind = request.get("kind") or found.get("kind") or "image"
    if media_kind not in ("image", "video"):
        raise CreationError("Genre inconnu : image ou vidéo.")
    model_id = pick(media_kind, request.get("model"))
    if model_id is None:
        raise _nothing_can(media_kind)
    # Un format choisi (largeur, hauteur) l'emporte sur celui que la phrase suggère ; il ne
    # vaut que pour le modèle demandé (un autre a ses propres formats et durées)
    same = model_id == request.get("model")
    chosen = (request["width"], request["height"]) \
        if same and request.get("width") and request.get("height") else None
    shape = request.get("shape") or (None if chosen else found.get("shape"))
    size = size_for(model_id, shape) or chosen
    frames = request.get("frames") if same and catalog.is_video(model_id) else None
    if catalog.is_video(model_id) and not frames:
        frames = frames_for(model_id, request.get("seconds") or found.get("seconds"))
    try:
        params = kind.normalize({
            "model": model_id, "prompt": request.get("prompt") or found.get("prompt") or text,
            "negative": request.get("negative") or "",
            "width": size[0] if size else None, "height": size[1] if size else None,
            "count": request.get("count") or found.get("count") or 1,
            "seed": request.get("seed"), "steps": request.get("steps"), "frames": frames,
            # retouche : l'image de départ, posée ici seulement (_followed), jamais par l'API
            "init_path": request.get("init_path") if same else None,
            "strength": request.get("strength") if same else None})
    except ValueError as exc:
        raise CreationError(str(exc)) from exc
    # D'un seul geste : l'échange remplacé, la question, le travail et la réponse
    with db.transaction():
        stopped = replace(conv_id, replace_from, replace_text) if replace_from is not None else []
        if conv_id is None:
            conv_id = conversations.create(model=chat_model or None)
        user_message_id = conversations.add_message(conv_id, "user", text)
        title = conversations.auto_title(conv_id, text)
        job_id = queue.create_job("generate_images", {**params, "conversation_id": conv_id},
                                  URGENT)
        media = {"kind": "video" if catalog.is_video(model_id) else "image", "model": model_id,
                 "label": catalog.MODELS[model_id]["label"], "prompt": params["prompt"],
                 "count": params["count"], "batch": params["batch"], "job_id": job_id,
                 "width": params["width"], "height": params["height"], "seed": params["seed"]}
        if "frames" in params:
            media["frames"] = params["frames"]
        if request.get("auto"):
            media["auto"] = True      # reconnue dans le message : « Répondre en texte » possible
        if params.get("init_path"):
            media["retouch"] = True   # partie de l'image précédente (dit sous l'image)
        message_id = conversations.add_message(conv_id, "assistant", describe(media),
                                               media=media)
    return {"conversation_id": conv_id, "title": title, "user_message_id": user_message_id,
            "message_id": message_id, "job_id": job_id, "media": view(media),
            "stopped": stopped}


def undo(message_id: int) -> dict:
    """« Répondre en texte » : une création reconnue au vol qui n'en était pas une. Elle
    quitte la conversation avec le message qui l'a demandée (que le modèle de conversation
    reçoit à la place), et son travail s'arrête. Seulement la dernière réponse de la
    conversation. → {"conversation_id", "text", "job_id"} ; CreationError sinon."""
    found = conversations.creation_message(message_id)
    if found is None:
        raise CreationError("Création introuvable.", status=404)
    if not found["last"]:
        raise CreationError("La conversation a continué depuis : seule la dernière création "
                            "peut céder la place à une réponse en texte.", status=409)
    media = found["media"]
    job = queue.job_summary(media["job_id"]) if media.get("job_id") else None
    if same_job(job, media) and job["status"] in ("queued", "running", "paused"):
        queue.cancel_job(job["id"])
    conversations.remove_messages(found["conversation_id"],
                                  [message_id] + ([found["asked_id"]] if found["asked_id"] else []))
    return {"conversation_id": found["conversation_id"], "text": found["asked"] or "",
            "job_id": job["id"] if same_job(job, media) else None}


CHANGED = "La conversation a changé entre-temps (autre fenêtre ?) : elle est rechargée."
CHANGED_STATUS = 412              # pas 409 : celui-là dit qu'aucun modèle ne peut calculer


def replace(conv_id, message_id, expect=None) -> list:
    """« Régénérer », « Modifier » : la conversation reprend à ce message de l'utilisateur,
    retiré avec la suite (conversations.rewind) ; les créations retirées s'arrêtent.
    Seulement le dernier échange : une question venue après (autre fenêtre, ancienne
    notice) le protège. CreationError (412) si ce n'est pas le message attendu (expect :
    son texte). → les travaux de création arrêtés."""
    found = conversations.user_message(conv_id, message_id)
    if (found is None or (expect is not None and found != expect)
            or conversations.later_question(conv_id, message_id)):
        raise CreationError(CHANGED, status=CHANGED_STATUS)
    stopped = []
    for media in conversations.media_from(conv_id, message_id):
        job = queue.job_summary(media["job_id"]) if media.get("job_id") else None
        if same_job(job, media) and job["status"] in ("queued", "running", "paused"):
            queue.cancel_job(job["id"])
            stopped.append(job["id"])
    if conversations.rewind(conv_id, message_id, expect) is None:
        raise CreationError(CHANGED, status=CHANGED_STATUS)
    return stopped


def same_job(job: dict | None, media: dict) -> bool:
    """Ce travail est-il bien celui de cette création ? Les numéros des travaux retirés de
    la liste (« Retirer les terminés ») sont repris par les suivants."""
    return bool(job) and job.get("kind") == "generate_images" \
        and (job.get("params") or {}).get("batch") == media.get("batch")


def view(media: dict | None) -> dict | None:
    """La demande telle que l'interface la montre : avec ses images déjà là (fichier
    compris) et son travail, s'il est encore dans la file."""
    if not media:
        return None
    images = [dict(row, path=str(gallery.path_of(row)))
              for row in gallery.batch(str(media.get("batch") or ""))]
    job = queue.job_summary(media["job_id"]) if media.get("job_id") else None
    if not same_job(job, media):
        job = None                    # travail retiré de la liste, numéro repris depuis
    expected = gallery.typical_seconds(media.get("model"), media.get("width"),
                                       media.get("height"), media.get("frames"))
    return {**media, "images": images, "job": job, "expected_s": expected}
