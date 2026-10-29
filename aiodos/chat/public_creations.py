"""Créer mes images par le réseau public, depuis la conversation (docs/conception/
reseau-public-calcul.md § 11 ; décision de l'auteur du 29/10/2026). La création elle-même :
chat/creations.py ; l'envoi : network/public_image_ask.py ; l'unité de la file :
jobs/kinds/public_images.py.

Quand ni cette machine ni mes machines (ni mes amis, si je les y autorise) ne peuvent calculer
une image, une machine du réseau public peut la créer (choose) :

- si la création peut partir chez un inconnu, aux règles des conversations
  (public_consent.closed : « Faire calculer mes conversations par des inconnus », le réseau
  public rejoint, pas « Sur mes machines seulement » pour cette conversation, pas d'image
  jointe : une image de départ ne part jamais chez un inconnu dans cette version) ;
- si une machine du réseau public prête un modèle d'images et a une place
  (public_image_ask.offered_models) ;
- avec l'accord public_images, demandé une fois : tant qu'il ne l'est pas (None), une carte
  dans la conversation (ask), « La description de ton image part chez la machine d'un
  inconnu… » [Oui] [Non] ; la réponse est gardée, et se change dans Réglages → Cette
  machine → Partage de puissance. Contrairement aux cartes des conversations, elle précède la
  création : rien d'autre ne pourrait la faire en attendant.

Sinon, le message « Aucun modèle d'images… » dit, quand une machine du réseau public pourrait
la créer, ce qui l'en empêche (réglages, accord refusé, « Sur mes machines seulement », image
jointe) ; sans machine du réseau public, le message d'avant. Les vidéos ne passent pas par le
réseau public dans cette version.

La description part telle qu'enrichie ici (chat/enrich.py : le modèle de conversation de
cette machine, jamais celui d'un inconnu ; sans modèle ici, la demande telle quelle).
"""
from dataclasses import dataclass

from aiodos import settings
from aiodos.chat import public_consent
from aiodos.i18n import t
from aiodos.network import public_image_ask

ASK = "ask"                       # la carte d'accord (CreationError.public)
# Ce qui empêche une machine du réseau public de créer l'image, dit quand elle le pourrait :
# public_images.closed_off, closed_mine_only, closed_attachments
TOLD = ("off", "mine_only", "attachments")


@dataclass(frozen=True)
class Choice:
    """Ce que le réseau public peut faire pour cette création."""
    model: str | None = None      # le modèle d'images à lui demander
    ask: bool = False             # l'accord n'est pas encore demandé : la carte (message)
    message: str | None = None    # sinon, pourquoi pas (None : le message d'avant)


def choose(preferred=None, mine_only=False, pictures=()) -> Choice:
    """Le réseau public peut-il créer cette image (cf. le début du module) ? preferred : le
    modèle demandé, s'il le prête ; pictures : les images jointes au message."""
    offered = public_image_ask.offered_models()
    why = public_consent.closed(None, mine_only, pictures)
    if why is not None:
        # public_images.closed_off, public_images.closed_mine_only, public_images.closed_attachments
        return Choice(message=t(f"public_images.closed_{why}")
                      if offered and why in TOLD else None)
    consent = settings.get("public_images")
    if not offered:
        return Choice(message=t("public_images.nobody") if consent is True else None)
    if consent is None:
        return Choice(ask=True, message=t("public_images.ask"))
    if consent is not True:
        return Choice(message=t("public_images.closed_refused"))
    return Choice(model=preferred if preferred in offered else offered[0])


def offerable() -> set:
    """Les modèles d'images qu'une machine du réseau public pourrait créer pour moi, pour le
    mode « Créer » (api/routes/images.py) : rien si mes créations ne partent jamais chez un
    inconnu, ou si je l'ai refusé ; « Sur mes machines seulement », propre à chaque
    conversation, est regardé par l'interface."""
    if public_consent.closed(None, False) is not None or settings.get("public_images") is False:
        return set()
    return set(public_image_ask.offered_models())
