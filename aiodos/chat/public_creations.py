"""Créer mes images et mes vidéos par le réseau public, depuis la conversation
(docs/conception/reseau-public-calcul.md § 11 ; décisions de l'auteur du 29/10/2026, pour les
vidéos : « il faut que ça soit pareil »). La création elle-même : chat/creations.py ; l'envoi :
network/public_image_ask.py, network/public_video_ask.py ; l'unité de la file :
jobs/kinds/public_images.py, jobs/kinds/public_videos.py.

Quand ni cette machine ni mes machines (ni mes amis, si je les y autorise) ne peuvent calculer
une image ou une vidéo, une machine du réseau public peut la créer (choose) :

- si la création peut partir chez un inconnu, aux règles des conversations
  (public_consent.closed : « Faire calculer mes conversations par des inconnus », le réseau
  public rejoint, pas « Sur mes machines seulement » pour cette conversation, pas d'image
  jointe : une image de départ ne part jamais chez un inconnu dans cette version) ;
- si une machine du réseau public prête un modèle de ce genre et a une place
  (public_image_ask.offered_models, public_video_ask.offered_models) ;
- avec l'accord public_images, demandé une fois, le même pour les images et les vidéos (comme
  images_from_friends pour mes amis, et comme le dit la charte, version 4 : « faire créer tes
  images et tes vidéos par la machine d'un inconnu », un seul accord) : tant qu'il ne l'est
  pas (None), une carte dans la conversation (ask), « La description de ton image (ta vidéo)
  part chez la machine d'un inconnu… » [Oui] [Non] ; la réponse est gardée, et se change dans
  Réglages → Cette machine → Partage de puissance (« Créer mes images et vidéos par le réseau
  public »). Contrairement aux cartes des conversations, elle précède la création : rien
  d'autre ne pourrait la faire en attendant.

Sinon, le message « Aucun modèle d'images… » (« Aucun modèle de vidéos… ») dit, quand une
machine du réseau public pourrait la créer, ce qui l'en empêche (réglages, accord refusé, « Sur
mes machines seulement », image jointe) ; si je l'ai accepté et que personne n'en prête, que le
réseau public ne le peut pas non plus ; sinon, le message d'avant. Les textes : public_images
pour une image, public_videos pour une vidéo (TEXTS).

La description part telle qu'enrichie ici (chat/enrich.py : le modèle de conversation de
cette machine, jamais celui d'un inconnu ; sans modèle ici, la demande telle quelle).
"""
from dataclasses import dataclass

from aiodos import settings
from aiodos.chat import public_consent
from aiodos.i18n import t
from aiodos.network import public_image_ask, public_video_ask

ASK = "ask"                       # la carte d'accord (CreationError.public)
# Ce qui empêche une machine du réseau public de créer l'image, dit quand elle le pourrait :
# public_images.closed_off, closed_mine_only, closed_attachments (public_videos de même)
TOLD = ("off", "mine_only", "attachments")
# Le catalogue des textes de chaque genre
TEXTS = {"image": "public_images", "video": "public_videos"}


@dataclass(frozen=True)
class Choice:
    """Ce que le réseau public peut faire pour cette création."""
    model: str | None = None      # le modèle à lui demander
    ask: bool = False             # l'accord n'est pas encore demandé : la carte (message)
    message: str | None = None    # sinon, pourquoi pas (None : le message d'avant)


def offered(media_kind: str = "image") -> list:
    """Les modèles de ce genre qu'une machine du réseau public peut créer pour moi."""
    if media_kind == "video":
        return public_video_ask.offered_models()
    return public_image_ask.offered_models()


def choose(preferred=None, mine_only=False, pictures=(), media_kind: str = "image") -> Choice:
    """Le réseau public peut-il créer cette image ou cette vidéo (cf. le début du module) ?
    preferred : le modèle demandé, s'il le prête ; pictures : les images jointes au message."""
    texts = TEXTS.get(media_kind, TEXTS["image"])
    found = offered(media_kind)
    why = public_consent.closed(None, mine_only, pictures)
    if why is not None:
        # public_images.closed_off, public_images.closed_mine_only, public_images.closed_attachments
        # public_videos.closed_off, public_videos.closed_mine_only, public_videos.closed_attachments
        return Choice(message=t(f"{texts}.closed_{why}") if found and why in TOLD else None)
    consent = settings.get("public_images")
    if not found:
        # public_images.nobody, public_videos.nobody
        return Choice(message=t(f"{texts}.nobody") if consent is True else None)
    if consent is None:
        return Choice(ask=True, message=t(f"{texts}.ask"))   # public_images.ask, public_videos.ask
    if consent is not True:
        # public_images.closed_refused, public_videos.closed_refused
        return Choice(message=t(f"{texts}.closed_refused"))
    return Choice(model=preferred if preferred in found else found[0])


def offerable() -> set:
    """Les modèles d'images et de vidéos qu'une machine du réseau public pourrait créer pour
    moi, pour le mode « Créer » (api/routes/images.py) : rien si mes créations ne partent
    jamais chez un inconnu, ou si je l'ai refusé ; « Sur mes machines seulement », propre à
    chaque conversation, est regardé par l'interface."""
    if public_consent.closed(None, False) is not None or settings.get("public_images") is False:
        return set()
    return set(offered("image")) | set(offered("video"))
