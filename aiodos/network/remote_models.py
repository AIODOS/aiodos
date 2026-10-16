"""Modèles de mes autres machines, utilisables dans mes conversations (étape C).

Chaque machine appairée annonce dans son profil ses modèles et si elle les
prête. Ils apparaissent dans le choix du modèle sous « Sur mes machines » ;
choisir l'un d'eux fait répondre la conversation par cette machine, en flux
chiffré (cf. lending.py chez elle). Identifiant : « peer:<machine>:<modèle> ».
"""
from aiodos.network import client, peers
from aiodos.network.client import PeerError
from aiodos.node import identity

PREFIX = "peer:"


def model_id(node_id: str, name: str) -> str:
    return f"{PREFIX}{node_id}:{name}"


def parse(model) -> tuple | None:
    """« peer:<machine>:<modèle> » → (machine, modèle) ; None pour un autre modèle.
    Le nom du modèle peut lui-même contenir « : » (qwen3.5:9b)."""
    if not str(model or "").startswith(PREFIX):
        return None
    node_id, _, name = model[len(PREFIX):].partition(":")
    return (node_id, name) if node_id and name else None


def available() -> list:
    """Modèles de conversation prêtés par mes machines en ligne."""
    models = []
    for peer in peers.list_peers():
        profile = peer.get("profile") or {}
        if not peer["online"] or not profile.get("lends_models"):
            continue
        for m in (profile.get("ollama") or {}).get("models") or []:
            caps = m.get("caps") or []
            if caps and "completion" not in caps:          # modèle d'embedding seul
                continue
            models.append({"id": model_id(peer["node_id"], m["name"]), "label": m["name"],
                           "group": "remote", "peer": peer["name"], "size": m.get("size", 0),
                           "caps": caps, "fits_vram": m.get("fits_vram"),
                           "loaded": m.get("loaded")})
    return models


def peer_name(model) -> str | None:
    parsed = parse(model)
    peer = peers.get(parsed[0]) if parsed else None
    return peer["name"] if peer else None


def chat_stream(model, messages, think=True, stop=None):
    """Couples (genre, texte), comme ollama.chat_stream, mais calculés par une autre
    machine. Machine injoignable, refus ou réponse altérée : PeerError (message lisible)."""
    node_id, name = parse(model)
    peer = peers.get(node_id)
    if not peer:
        raise PeerError("Cette machine n'est plus appairée : choisis un autre modèle.")
    if not peer.get("address"):
        raise PeerError(f"Adresse de « {peer['name']} » inconnue pour l'instant.")
    for frame in client.stream(identity.node_id(), peer, "chat",
                               {"model": name, "messages": messages, "think": bool(think)},
                               stop=stop):
        for kind in ("thinking", "content"):
            if frame.get(kind):
                yield kind, frame[kind]
