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
"""
import threading
from dataclasses import dataclass

from aiodos import settings
from aiodos.chat import attachments, pipeline
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


def allowed(conv_id, mine_only, joined=()) -> bool:
    """La conversation peut-elle partir chez un inconnu ? (cf. le début du module)"""
    if not settings.get("use_public") or not settings.get("public_network"):
        return False
    if mine_only or joined:
        return False
    return conv_id is None or not attachments.in_conversation(conv_id)


def reach(model, conv_id, mine_only, joined=()) -> Reach:
    """Pour le choix des machines (placement.plan) et la carte d'un modèle plus gros. Un
    modèle précis (cette machine, une machine appairée) passe tel quel : pas de carte."""
    if not allowed(conv_id, mine_only, joined):
        return Reach()
    name = placement.parse(model)
    if name is None:
        return Reach(public=True)
    consent = settings.get("public_bigger_model")
    ask = placement.bigger_public(name) if consent is None else None
    return Reach(public=True, bigger=consent is True, ask_bigger=ask)


def bigger_notice(reach_: Reach) -> dict | None:
    return {"kind": BIGGER, "model": reach_.ask_bigger} if reach_.ask_bigger else None


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
