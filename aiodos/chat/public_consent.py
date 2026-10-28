"""Ce qu'une conversation peut confier au réseau public, et ce que l'interface en montre
(docs/conception/reseau-public-calcul.md § 6 et 7 ; décisions de l'auteur du 27/10/2026). La
conversation elle-même part par network/public_chat.py, vers la machine choisie par
network/placement.py.

- reach : la conversation peut-elle partir chez un inconnu ? Le réglage use_public (« Faire
  calculer mes conversations par des inconnus », activé par défaut), le réseau public rejoint
  (public_network), pas « Sur mes machines seulement » pour cette conversation, et aucune
  pièce jointe, ni dans le message ni dans la conversation (elles ne partent jamais, ni ce
  qu'on en a lu). En mode automatique, un modèle plus gros d'une machine publique seulement
  avec l'accord public_bigger_model ; tant qu'il n'est pas demandé (None) et qu'un tel modèle
  pourrait répondre, une carte d'accord (« notice » public_bigger).
- admit, juste avant d'envoyer la conversation à une machine publique : une réponse qui
  s'appuierait sur mes documents (pipeline.documents_found) n'y part, avec leurs passages,
  qu'avec l'accord public_documents ; sans lui, elle n'y part pas (PublicError « documents » :
  la machine suivante, une des miennes ou celle-ci, répond), et tant qu'il n'est pas demandé,
  une carte d'accord (« notice » public_documents). Puis, la première fois qu'une conversation
  part chez un inconnu, une note (« notice » public_first), une seule fois (public_warned).

Les cartes ne bloquent pas l'envoi (le plus simple qui reste honnête) : la réponse en cours
part tout de suite sur ce qui est permis sans accord (le même modèle, sans mes documents), la
carte le dit, et le choix vaut pour les messages suivants ; il se change dans les Réglages.

Sans aucun modèle de conversation, ni ici ni sur mes machines (un téléphone neuf) : entry
propose « Automatique · réseau public » (placement.PUBLIC_BEST), qui prend déjà le meilleur
modèle du réseau public, donc sans carte d'un modèle plus gros ; la carte des documents et la
note de la première fois, comme ailleurs. Refusé à une conversation (closed : réglages,
« Sur mes machines seulement », pièces jointes), refusal dit pourquoi, au lieu d'un « aucune
machine » sans explication.
"""
import threading
from dataclasses import dataclass

from aiodos import settings
from aiodos.chat import attachments, pipeline
from aiodos.i18n import t
from aiodos.network import placement, public_chat

BIGGER = "public_bigger"         # genres des cartes et de la note (événement « notice »)
DOCUMENTS = "public_documents"
FIRST = "public_first"

_lock = threading.Lock()


@dataclass(frozen=True)
class Reach:
    """Ce que le réseau public peut faire pour ce message."""
    public: bool = False          # une machine publique peut répondre
    bigger: bool = False          # avec un modèle plus gros (accord donné)
    ask_bigger: str | None = None  # le modèle plus gros à proposer (carte), accord non demandé
    closed: str | None = None     # sinon, pourquoi (closed)


def closed(conv_id, mine_only, joined=()) -> str | None:
    """Pourquoi la conversation ne peut pas partir chez un inconnu (cf. le début du module) :
    « off » (use_public), « unjoined » (public_network), « mine_only », « attachments » (dans
    ce message), « conversation_attachments » (dans un message d'avant) ; None si elle le peut.
    Chacun a son message (public_chat.closed_<code>)."""
    if not settings.get("use_public"):
        return "off"
    if not settings.get("public_network"):
        return "unjoined"
    if mine_only:
        return "mine_only"
    if joined:
        return "attachments"
    if conv_id is not None and attachments.in_conversation(conv_id):
        return "conversation_attachments"
    return None


def allowed(conv_id, mine_only, joined=()) -> bool:
    """La conversation peut-elle partir chez un inconnu ? (cf. le début du module)"""
    return closed(conv_id, mine_only, joined) is None


def reach(model, conv_id, mine_only, joined=()) -> Reach:
    """Pour le choix des machines (placement.plan) et la carte d'un modèle plus gros. Un
    modèle précis (cette machine, une machine appairée) passe tel quel : pas de carte ;
    « Automatique · réseau public » prend déjà le meilleur modèle : pas de carte non plus."""
    why = closed(conv_id, mine_only, joined)
    if why is not None:
        return Reach(closed=why)
    name = placement.parse(model)
    if name is None or model == placement.PUBLIC_BEST:
        return Reach(public=True)
    consent = settings.get("public_bigger_model")
    ask = placement.bigger_public(name) if consent is None else None
    return Reach(public=True, bigger=consent is True, ask_bigger=ask)


def bigger_notice(reach_: Reach) -> dict | None:
    return {"kind": BIGGER, "model": reach_.ask_bigger} if reach_.ask_bigger else None


def refusal(model, reach_: Reach) -> str | None:
    """« Automatique · réseau public » refusé à ce message (rien ici ni sur mes machines ne
    répondrait à la place) : pourquoi, lisible ; None pour un autre modèle, ou si la
    conversation pouvait partir (placement.plan dit alors qu'aucune machine ne répond)."""
    if model != placement.PUBLIC_BEST or reach_.closed is None:
        return None
    return t(f"public_chat.closed_{reach_.closed}")


def entry() -> dict | None:
    """« Automatique · réseau public », proposé à l'interface (pipeline.available_models) quand
    ni cette machine ni mes machines n'ont de modèle de conversation ; None si mes
    conversations ne partent jamais chez un inconnu (use_public, public_network). ready : une
    machine du réseau public peut répondre en ce moment ; models : les meilleurs modèles
    qu'elles donneraient, le premier d'abord. « Sur mes machines seulement », propre à chaque
    conversation, est regardé par l'interface (et refusé ici, refusal)."""
    if closed(None, False) is not None:
        return None
    best = placement.best_public()
    names = list(dict.fromkeys(public_chat.parse(c.model)[1] for c in best))
    return {"id": placement.PUBLIC_BEST, "label": t("placement.public_auto"),
            "group": "public", "ready": bool(best), "models": names}


def first_time() -> bool:
    """Vrai la première fois seulement qu'une conversation part chez un inconnu."""
    with _lock:
        if settings.get("public_warned"):
            return False
        settings.set_value("public_warned", True)
        return True


def admit(question, libraries, conversation_id, memo: dict):
    """Juste avant d'envoyer la conversation à une machine publique : couples ("notice", {…})
    à montrer, puis PublicError « documents » si la réponse s'appuierait sur mes documents
    sans accord. memo : ce qui est déjà su ou montré pour ce message (la recherche dans mes
    documents est faite une fois, chaque carte montrée une fois)."""
    consent = settings.get("public_documents")
    if consent is not True:
        if "documents" not in memo:
            memo["documents"] = pipeline.documents_found(question, libraries, conversation_id)
        if memo["documents"]:
            if consent is None and DOCUMENTS not in memo:
                memo[DOCUMENTS] = True
                yield "notice", {"kind": DOCUMENTS}
            raise public_chat.PublicError("documents")
    if FIRST not in memo and first_time():
        memo[FIRST] = True
        yield "notice", {"kind": FIRST}
