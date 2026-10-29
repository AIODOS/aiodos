"""Une image de la conversation créée par une machine du réseau public (docs/conception/
reseau-public-calcul.md § 11 ; décision de l'auteur du 29/10/2026) : l'unité PUBLIC_TASK d'un
travail « images » que chat/creations.py a confié au réseau public, avec l'accord une fois
(chat/public_creations.py), parce que ni cette machine ni mes machines ne pouvaient le faire.

Une unité de cette machine seulement (registry.register_local : jamais confiée à une autre de
mes machines, qui n'a pas l'accord, ni le compagnon de celle-ci), sur la ressource « net » :
elle attend le réseau, pas la carte graphique d'ici. Avant chaque envoi, l'accord est relu
(still_allowed) : réseau public rejoint, « Faire calculer mes conversations par des inconnus »,
« Créer mes images par le réseau public », et pas « Sur mes machines seulement » pour la
conversation de la création ; un accord retiré depuis la demande arrête l'image (en échec,
sans rien envoyer).

Puis les machines du réseau public qui prêtent ce modèle, la mieux placée d'abord
(public_image_ask.lenders) ; la première qui rend une image valide la donne, les autres
attendent. Une clé jetable par création (public_chat.tag, un identifiant par lot). Pendant
l'envoi, la machine compte dans ce que je lui envoie (placement._occupy) : deux images d'un
même lot ne vont pas en même temps chez le même prêteur, qui n'a qu'une place. L'image est
gardée comme une création d'ici (imaging/gallery.py), sa machine marquée « Créée par une
machine du réseau public ». Un arrêt (travail annulé, AIODOS qui s'arrête) coupe l'envoi :
l'unité est rendue (Interrupted).
"""
import logging

from aiodos import settings
from aiodos.chat import conversations, public_consent
from aiodos.i18n import t
from aiodos.imaging import gallery
from aiodos.jobs import pacing
from aiodos.jobs.kinds import images as kind
from aiodos.jobs.registry import Interrupted, register_local
from aiodos.network import placement, public_chat, public_image_ask, reputation
from aiodos.network.public_chat import PublicError

log = logging.getLogger("aiodos.images")

TASK = kind.PUBLIC_TASK


def still_allowed(payload) -> bool:
    """L'image peut-elle encore partir chez un inconnu ? (cf. le début du module)"""
    conversation = payload.get("conversation_id")
    mine_only = conversations.mine_only(conversation) if conversation is not None else False
    return public_consent.closed(None, mine_only) is None \
        and settings.get("public_images") is True


def compute(payload, file, options, stop=None):
    halt = stop if stop is not None else pacing.stopping()
    if halt.is_set():
        raise Interrupted()
    if payload.get("public") is not True or not still_allowed(payload):
        raise ValueError(t("public_images.withdrawn"))
    params = kind.checked({k: v for k, v in payload.items()
                           if k not in ("public", "conversation_id", "init_path", "strength")})
    model = params["model"]
    tried = public_image_ask.lenders(model)
    if not tried:
        raise ValueError(t("public_images.nobody_now"))
    conversation = public_chat.tag(("creation", str(params.get("batch") or "")))
    failure = None
    for key, name in tried:
        if halt.is_set():
            raise Interrupted()
        try:
            with placement._occupy(public_chat.model_id(key, public_image_ask.MARKER + name),
                                   None):
                made = public_image_ask.image(key, params, stop=halt, conversation=conversation)
        except PublicError as exc:
            failure = exc
            # i18n: tel quel (journal) ; la clé abrégée et le code seulement
            log.info("Image par le réseau public : %s n'a pas pu la créer (%s)",
                     reputation.short(key), exc.code)
            continue
        if made is None:
            raise Interrupted()                  # arrêtée en route : l'unité est rendue
        machine = t("public_images.machine")
        row = gallery.add(params, made["png"], machine=machine, seconds=made["seconds"])
        return {"image_id": row["id"], "seed": row["seed"], "machine": row["machine"],
                "public": True}
    if halt.is_set():
        raise Interrupted()
    raise ValueError(t("public_images.failed", error=str(failure)))


register_local(TASK, compute)
