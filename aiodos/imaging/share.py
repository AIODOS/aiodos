"""Modèles d'images copiés d'une de mes machines, plutôt que depuis Hugging Face : par le
réseau local en quelques minutes, ou par Internet (action scellée, cf. network/p2p_app.py).

    images/file   un morceau d'un fichier de modèle : seulement un fichier du catalogue,
                  désigné par son empreinte, complet et vérifié ici, et seulement pour mes
                  machines (jamais un ami)

Côté demandeur, download.py prend la machine comme source : morceau par morceau, reprise
après une coupure, et l'empreinte vérifiée avant de garder le fichier, comme depuis
Hugging Face. La machine qui transmet n'a pas à être de confiance : seule compte
l'empreinte épinglée dans le catalogue.
"""
from aiodos.imaging import catalog
from aiodos.network import client, envelope, peers, route, sharing
from aiodos.network.client import PeerError
from aiodos.node import identity

CHUNK = 8 * 1024 ** 2             # réseau local
INTERNET_CHUNK = 512 * 1024       # par Internet (4G, relais) : chaque morceau tient dans le délai
TIMEOUT = 60


def _held(sha256: str):
    """Le fichier du catalogue de cette empreinte, s'il est complet et vérifié ici."""
    for spec in (f for m in catalog.MODELS.values() for f in m["files"].values()):
        if spec["sha256"] == sha256 and catalog.file_state(spec) == "ok":
            return catalog.folder() / spec["name"], spec
    return None, None


def serve_file(manager, sender, payload):
    if sharing.level(sender) != sharing.MINE:
        raise PeerError("Les modèles ne se copient qu'entre mes machines.")
    path, spec = _held(str(payload.get("sha256") or ""))
    if path is None:
        raise PeerError("Fichier de modèle indisponible sur cette machine.")
    try:
        offset = max(0, int(payload.get("offset") or 0))
        size = min(CHUNK, max(1, int(payload.get("size") or CHUNK)))
    except (TypeError, ValueError) as exc:
        raise PeerError("Demande illisible.") from exc
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(size)
    return {"data": envelope.b64e(data)}


HANDLERS = {"images/file": serve_file}


def sources(model_id: str) -> list:
    """Mes machines en ligne qui ont ce modèle complet : [{"id", "name", "near"}]."""
    found = []
    for peer in peers.list_peers():
        if not peer["online"] or sharing.level(peer["node_id"]) != sharing.MINE:
            continue
        if model_id in (((peer.get("profile") or {}).get("images") or {}).get("models") or []):
            found.append({"id": peer["node_id"], "name": peer["name"],
                          "near": route.lan_first(peer)})
    return found


class Source:
    """Une de mes machines, vue comme source de fichiers de modèles (download.py)."""

    def __init__(self, node_id: str):
        self.peer = peers.get(node_id)
        if self.peer is None or sharing.level(node_id) != sharing.MINE:
            raise PeerError("Cette machine n'est pas une des miennes.")
        self.name = self.peer["name"]
        self.chunk = CHUNK if route.lan_first(self.peer) else INTERNET_CHUNK

    def read(self, spec: dict, offset: int) -> bytes:
        reply = client.call(identity.node_id(), self.peer, "images/file",
                            {"sha256": spec["sha256"], "offset": offset, "size": self.chunk},
                            timeout=TIMEOUT)
        return envelope.b64d(reply.get("data") or "")
