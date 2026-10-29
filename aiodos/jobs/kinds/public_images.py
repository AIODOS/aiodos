"""Une image de la conversation créée par une machine du réseau public (docs/conception/
reseau-public-calcul.md § 11 ; décision de l'auteur du 29/10/2026) : l'unité PUBLIC_TASK d'un
travail « images » que chat/creations.py a confié au réseau public, avec l'accord une fois
(chat/public_creations.py), parce que ni cette machine ni mes machines ne pouvaient le faire.
Une vidéo : jobs/kinds/public_videos.py, le même parcours (create).

Une unité de cette machine seulement (registry.register_local : jamais confiée à une autre de
mes machines, qui n'a pas l'accord, ni le compagnon de celle-ci), sur la ressource « net » :
elle attend le réseau, pas la carte graphique d'ici. Avant chaque envoi, l'accord est relu
(still_allowed) : réseau public rejoint, « Faire calculer mes conversations par des inconnus »,
« Créer mes images et vidéos par le réseau public », et pas « Sur mes machines seulement » pour
la conversation de la création ; un accord retiré depuis la demande arrête l'image (en échec,
sans rien envoyer).

Puis la base du filtre des descriptions (imaging/prompt_filter.py ; la conversation a déjà
passé le filtre entier, chat/creations.py) : une description interdite échoue sans rien
envoyer. Puis les machines du réseau public qui prêtent ce modèle, la mieux placée d'abord
(public_image_ask.lenders) ; la première qui rend une image valide la donne, les autres
attendent. Le filtre d'un prêteur qui la refuse (« refused_content ») arrête l'image : elle
ne part chez aucune autre machine. Une clé jetable par création (public_chat.tag, un
identifiant par lot). Pendant l'envoi, la machine compte dans ce que je lui envoie
(placement._occupy) : deux images d'un même lot ne vont pas en même temps chez le même
prêteur, qui n'a qu'une place ; la seconde attend son tour (in_turn, 29/10/2026 : elle
échouait aussitôt, faute de place, et « Crée quatre images » avec un seul prêteur n'en rendait
qu'une partie), comme quand le prêteur sert un inconnu ou fait la pause de sa puissance
prêtée. L'image est gardée comme une création d'ici
(imaging/gallery.py), sa machine marquée « Créée par une machine du réseau public ». Un arrêt
(travail annulé, AIODOS qui s'arrête) coupe l'envoi : l'unité est rendue (Interrupted).
"""
import logging
import threading
import time

from aiodos import settings
from aiodos.chat import conversations, public_consent
from aiodos.i18n import t
from aiodos.imaging import gallery, prompt_filter
from aiodos.jobs import pacing
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.registry import Interrupted, register_local
from aiodos.network import placement, public_chat, public_image_ask, public_refusals, reputation
from aiodos.network.public_chat import PublicError

log = logging.getLogger("aiodos.images")

TASK = kind.PUBLIC_TASK
TURN_S = 3                        # s entre deux regards, quand l'unité attend son tour (in_turn)
_choosing = threading.Lock()      # choisir un prêteur et le réserver, d'un seul geste (in_turn)


def still_allowed(payload) -> bool:
    """L'image (ou la vidéo) peut-elle encore partir chez un inconnu ? (cf. le début du
    module)"""
    conversation = payload.get("conversation_id")
    mine_only = conversations.mine_only(conversation) if conversation is not None else False
    return public_consent.closed(None, mine_only) is None \
        and settings.get("public_images") is True


def _reserve(asker, key: str, name: str):
    """La machine `key` comptée dans ce que je lui envoie (placement._occupy), dès maintenant :
    → de quoi la rendre (__exit__)."""
    held = placement._occupy(public_chat.model_id(key, asker.MARKER + name), None)
    held.__enter__()
    return held


def _release(held):
    held.__exit__(None, None, None)


def in_turn(asker, model: str, halt) -> tuple:
    """(asker.lenders(modèle), la première réservée : _reserve), dès qu'une machine qui prête
    ce modèle a une place ; choisie et réservée d'un seul geste (_choosing), pour que deux
    unités d'un même lot ne la prennent pas toutes deux (la seconde serait refusée, « busy »,
    un refus compté contre un prêteur honnête). Tant qu'elles sont toutes occupées
    (asker.lent : par une autre de mes créations ; par un inconnu ; ou dans la pause de leur
    puissance prêtée), l'unité attend son tour, un regard toutes les TURN_S secondes,
    asker.TOTAL_S secondes au plus (le temps d'une création) ; ([], None) si aucune ne le
    prête plus, ou après ce temps. Interrupted à l'arrêt."""
    deadline = time.monotonic() + asker.TOTAL_S
    while True:
        with _choosing:
            tried = asker.lenders(model)
            if tried:
                return tried, _reserve(asker, *tried[0])
        if time.monotonic() >= deadline or not asker.lent(model):
            return [], None
        if halt.wait(TURN_S):
            raise Interrupted()


def create(payload, stop, asker, send, keep, texts: str) -> dict:
    """Le parcours d'une création par le réseau public (cf. le début du module), pour une
    image ou une vidéo : asker, le module du demandeur (network/public_image_ask.py,
    network/public_video_ask.py : lenders, lent, MARKER, TOTAL_S) ; send(clé, réglages,
    stop=, conversation=) → ce que rend le prêteur, ou None (arrêtée) ; keep(réglages, rendu)
    → la ligne de la galerie ; texts : le catalogue de ses messages (public_images,
    public_videos)."""
    halt = stop if stop is not None else pacing.stopping()
    if halt.is_set():
        raise Interrupted()
    if payload.get("public") is not True or not still_allowed(payload):
        # public_images.withdrawn, public_videos.withdrawn
        raise ValueError(t(f"{texts}.withdrawn"))
    params = kind.checked({k: v for k, v in payload.items()
                           if k not in ("public", "conversation_id", "init_path", "strength")})
    model = params["model"]
    if prompt_filter.base(params["prompt"], params.get("negative") or "") is not None:
        # public_images.refused_filter, public_videos.refused_filter
        raise ValueError(t(f"{texts}.refused_filter"))
    tried, reserved = in_turn(asker, model, halt)
    if not tried:
        raise ValueError(t(f"{texts}.nobody_now"))   # public_images…, public_videos.nobody_now
    held = [reserved]                    # la machine réservée, rendue après son essai
    try:
        if not still_allowed(payload):               # relu après l'attente
            raise ValueError(t(f"{texts}.withdrawn"))
        conversation = public_chat.tag(("creation", str(params.get("batch") or "")))
        failure = None
        for rank, (key, name) in enumerate(tried):
            if halt.is_set():
                raise Interrupted()
            if rank:
                held.append(_reserve(asker, key, name))
            try:
                made = send(key, params, stop=halt, conversation=conversation)
            except PublicError as exc:
                failure = exc
                # i18n: tel quel (journal) ; la clé abrégée et le code seulement
                log.info("Création par le réseau public : %s n'a pas pu la faire (%s)",
                         reputation.short(key), exc.code)
                if exc.code in public_refusals.INNOCENT:     # son filtre : aucune autre
                    # public_images.refused_remote, public_videos.refused_remote
                    raise ValueError(t(f"{texts}.refused_remote")) from None
                continue
            finally:
                _release(held.pop())
            if made is None:
                raise Interrupted()              # arrêtée en route : l'unité est rendue
            row = keep(params, made)
            return {"image_id": row["id"], "seed": row["seed"], "machine": row["machine"],
                    "public": True}
    finally:
        for left in held:                        # pas essayée (arrêt, accord retiré)
            _release(left)
    if halt.is_set():
        raise Interrupted()
    # public_images.failed, public_videos.failed
    raise ValueError(t(f"{texts}.failed", error=str(failure)))


def _keep(params: dict, made: dict) -> dict:
    return gallery.add(params, made["png"], machine=t("public_images.machine"),
                       seconds=made["seconds"])


def compute(payload, file, options, stop=None):
    return create(payload, stop, public_image_ask, public_image_ask.image, _keep,
                  "public_images")


register_local(TASK, compute)
