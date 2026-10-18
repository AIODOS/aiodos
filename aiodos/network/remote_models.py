"""Modèles de mes autres machines, utilisables dans mes conversations (étape C).

Chaque machine appairée annonce dans son profil ses modèles et si elle les
prête. Ils apparaissent dans le choix du modèle sous « Sur mes machines » ;
choisir l'un d'eux fait répondre la conversation par cette machine, en flux
chiffré (cf. lending.py chez elle). Identifiant : « peer:<machine>:<modèle> ».
"""
from aiodos.network import activity, client, peers
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
    """Modèles de conversation prêtés par mes machines en ligne. auto : la machine peut
    aussi répondre d'office en mode automatique (cf. placement.eligible)."""
    from aiodos.network import placement           # placement s'appuie sur ce module
    models = []
    for peer in peers.list_peers():
        profile = peer.get("profile") or {}
        if not peer["online"] or not profile.get("lends_models"):
            continue
        auto = placement.eligible(peer)
        for m in (profile.get("ollama") or {}).get("models") or []:
            caps = m.get("caps") or []
            if caps and "completion" not in caps:          # modèle d'embedding seul
                continue
            models.append({"id": model_id(peer["node_id"], m["name"]), "label": m["name"],
                           "group": "remote", "peer": peer["name"], "size": m.get("size", 0),
                           "caps": caps, "fits_vram": m.get("fits_vram"),
                           "loaded": m.get("loaded"), "node_id": peer["node_id"],
                           "trust": peer.get("trust") or "mine", "auto": auto,
                           # pour les autres outils (gateway/catalog.py)
                           "digest": m.get("digest", ""), "family": m.get("family", ""),
                           "parameters": m.get("parameters", ""),
                           "quantization": m.get("quantization", "")})
    return models


def peer_name(model) -> str | None:
    parsed = parse(model)
    peer = peers.get(parsed[0]) if parsed else None
    return peer["name"] if peer else None


def chat_stream(model, messages, think=True, stop=None, options=None, stats=False):
    """Couples (genre, texte), comme ollama.chat_stream, mais calculés par une autre
    machine, jointe sur le réseau local ou par Internet (une machine appairée sur
    invitation n'a qu'une clé Internet). Machine injoignable, refus ou réponse altérée :
    PeerError (message lisible). options, stats : cf. lending.py (une machine d'une
    version précédente les ignore : pas de couple « stats » alors)."""
    node_id, name = parse(model)
    peer = peers.get(node_id)
    if not peer:
        raise PeerError("Cette machine n'est plus appairée : choisis un autre modèle.")
    if not (peer.get("address") or peer.get("net_id")):
        raise PeerError(f"« {peer['name']} » est injoignable pour l'instant (ni adresse sur "
                        "le réseau local, ni clé Internet).")
    payload = {"model": name, "messages": messages, "think": bool(think)}
    if options:
        payload["options"] = options
    if stats:
        payload["stats"] = True
    with activity.chat("by", node_id, stop):         # « répond à ta conversation »
        for frame in client.stream(identity.node_id(), peer, "chat", payload, stop=stop):
            for kind in ("thinking", "content"):
                if frame.get(kind):
                    yield kind, frame[kind]
            if stats and isinstance(frame.get("stats"), dict):
                yield "stats", frame["stats"]
