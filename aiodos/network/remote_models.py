"""Modèles de mes autres machines, utilisables dans mes conversations (étape C).

Chaque machine appairée annonce dans son profil ses modèles et si elle les
prête. Ils apparaissent dans le choix du modèle sous « Sur mes machines » ;
choisir l'un d'eux fait répondre la conversation par cette machine, en flux
chiffré (cf. lending.py chez elle). Identifiant : « peer:<machine>:<modèle> ».

Le profil d'une machine appairée est typé à l'enregistrement et à la relecture
(network/profile.py). Ce qui en est tiré ici l'est encore (chat_models, lends), par défense
en profondeur (audit du 20/10/2026, faille 16) : un modèle qui n'est pas un objet, sans nom,
ou avec un nombre non fini faisait tomber GET /api/models et le mode automatique, pour
toutes les machines à la fois.
"""
import json
import math

from aiodos.i18n import t
from aiodos.network import activity, client, peers
from aiodos.network.client import PeerError
from aiodos.node import identity

PREFIX = "peer:"
READ_ALIVE_S = 180        # la machine envoie un signe de vie toutes les 10 s : 3 minutes sans
READ_SILENT_S = 600       # une version précédente : rien pendant qu'un gros modèle se charge
_TEXTS = ("digest", "family", "parameters", "quantization")


def model_id(node_id: str, name: str) -> str:
    return f"{PREFIX}{node_id}:{name}"


def parse(model) -> tuple | None:
    """« peer:<machine>:<modèle> » → (machine, modèle) ; None pour un autre modèle.
    Le nom du modèle peut lui-même contenir « : » (qwen3.5:9b)."""
    if not str(model or "").startswith(PREFIX):
        return None
    node_id, _, name = model[len(PREFIX):].partition(":")
    return (node_id, name) if node_id and name else None


def lends(profile) -> bool:
    """Ce profil annonce-t-il que la machine prête ses modèles ?"""
    return isinstance(profile, dict) and profile.get("lends_models") is True


def _flag(value) -> bool | None:
    return value if isinstance(value, bool) else None


def chat_models(profile) -> list:
    """Modèles de conversation annoncés dans un profil (celui d'une machine appairée, ou de
    celle-ci), chacun réduit à ce qui a la forme attendue : {"name", "caps", "size",
    "fits_vram", "loaded", "digest", "family", "parameters", "quantization"}. Une entrée qui
    n'est pas un objet ou n'a pas de nom est ignorée ; un nombre non fini ou d'un autre type
    devient 0, une valeur douteuse None ou "". Un modèle d'embedding seul n'en est pas un."""
    ollama = profile.get("ollama") if isinstance(profile, dict) else None
    listed = ollama.get("models") if isinstance(ollama, dict) else None
    found = []
    for m in listed if isinstance(listed, list) else []:
        if not isinstance(m, dict) or not isinstance(m.get("name"), str) or not m["name"]:
            continue
        caps = m.get("caps")
        caps = [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else []
        if caps and "completion" not in caps:          # modèle d'embedding seul
            continue
        size = m.get("size")
        finite = (isinstance(size, (int, float)) and not isinstance(size, bool)
                  and math.isfinite(size) and size >= 0)
        found.append({"name": m["name"], "caps": caps, "size": size if finite else 0,
                      "fits_vram": _flag(m.get("fits_vram")), "loaded": _flag(m.get("loaded")),
                      **{key: m[key] if isinstance(m.get(key), str) else "" for key in _TEXTS}})
    return found


def available() -> list:
    """Modèles de conversation prêtés par mes machines en ligne. auto : la machine peut
    aussi répondre d'office en mode automatique (cf. placement.eligible)."""
    from aiodos.network import placement           # placement s'appuie sur ce module
    models = []
    for peer in peers.list_peers():
        profile = peer.get("profile")
        if not peer["online"] or not lends(profile):
            continue
        auto = placement.eligible(peer)
        for m in chat_models(profile):
            models.append({"id": model_id(peer["node_id"], m["name"]), "label": m["name"],
                           "group": "remote", "peer": peer["name"], "size": m["size"],
                           "caps": m["caps"], "fits_vram": m["fits_vram"],
                           "loaded": m["loaded"], "node_id": peer["node_id"],
                           "trust": peer.get("trust") or "mine", "auto": auto,
                           # pour les autres outils (gateway/catalog.py)
                           **{key: m[key] for key in _TEXTS}})
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
        raise PeerError(t("lending.unpaired"))
    if not (peer.get("address") or peer.get("net_id")):
        raise PeerError(t("lending.unreachable", name=peer["name"]))
    payload = {"model": name, "messages": messages, "think": bool(think)}
    if options:
        payload["options"] = options
    if stats:
        payload["stats"] = True
    # Une machine qui envoie des signes de vie se lit avec un délai court ; une version
    # précédente se tait pendant qu'un gros modèle se charge : le délai d'avant
    try:
        stored = json.loads(peer.get("profile") or "null")
    except (ValueError, RecursionError):
        stored = None
    alive = isinstance(stored, dict) and stored.get("stream_alive") is True
    with activity.chat("by", node_id, stop):         # « répond à ta conversation »
        for frame in client.stream(identity.node_id(), peer, "chat", payload, stop=stop,
                                   read_timeout=READ_ALIVE_S if alive else READ_SILENT_S):
            for kind in ("thinking", "content"):
                if frame.get(kind):
                    yield kind, frame[kind]
            if stats and isinstance(frame.get("stats"), dict):
                yield "stats", frame["stats"]
