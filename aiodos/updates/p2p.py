"""Versions transmises entre machines appairées (actions scellées, cf. network/p2p_app.py) :

    update/manifest   manifeste signé d'une version que cette machine a en entier
    update/chunk      un morceau d'un fichier de ce paquet (seulement ceux du manifeste)

Chaque machine annonce dans son profil la version qu'elle peut transmettre
(identity.profile, « updates »). Côté demandeur, fetch() télécharge morceau par
morceau, reprend un transfert interrompu, et vérifie tout (signature, numéro,
empreinte) avant de garder le paquet. La machine qui transmet n'a pas à être de
confiance : seule compte la signature de la clé de publication.
"""
from aiodos.network import client, envelope
from aiodos.network.client import PeerError
from aiodos.updates import manifest as manifests
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

CHUNK = 1024 * 1024
TIMEOUT = 60


def _store() -> Store:
    return Store()


def serve_manifest(manager, sender, payload):
    wanted = str(payload.get("version") or "")
    store = _store()
    try:
        complete = store.complete(wanted)
    except UpdateError:
        complete = False
    if not complete:
        raise PeerError("Version indisponible sur cette machine.")
    return {"signed": store.signed(wanted)}


def serve_chunk(manager, sender, payload):
    try:
        offset = max(0, int(payload.get("offset") or 0))
        size = min(CHUNK, max(1, int(payload.get("size") or CHUNK)))
    except (TypeError, ValueError) as exc:
        raise PeerError("Demande illisible.") from exc
    try:
        data = _store().read_chunk(str(payload.get("version") or ""),
                                   str(payload.get("name") or ""), offset, size)
    except (UpdateError, OSError) as exc:
        raise PeerError(str(exc)) from exc
    return {"data": envelope.b64e(data)}


HANDLERS = {
    "update/manifest": serve_manifest,
    "update/chunk": serve_chunk,
}


def fetch(my_id: str, peer: dict, wanted: str, store: Store, progress=None) -> dict:
    """Télécharge la version `wanted` chez `peer` : son manifeste vérifié, une fois le
    paquet complet dans `store`. PeerError, UpdateError ou OSError sinon."""
    reply = client.call(my_id, peer, "update/manifest", {"version": wanted}, timeout=TIMEOUT)
    found = manifests.verify(reply.get("signed"))
    if found["version"] != wanted:
        raise UpdateError("La machine a envoyé une autre version que celle annoncée.")
    store.add_manifest(reply["signed"])
    for item in manifests.wanted(found):
        if store.has(wanted, item):
            continue
        part = store.partial(wanted, item["name"])
        received = part.stat().st_size if part.exists() else 0
        if received > item["size"]:
            part.unlink()
            received = 0
        with open(part, "ab") as out:
            while received < item["size"]:
                reply = client.call(my_id, peer, "update/chunk",
                                    {"version": wanted, "name": item["name"],
                                     "offset": received, "size": CHUNK}, timeout=TIMEOUT)
                data = envelope.b64d(reply.get("data") or "")[:item["size"] - received]
                if not data:
                    raise UpdateError("Transfert interrompu par l'autre machine.")
                out.write(data)
                received += len(data)
                if progress:
                    progress(received, item["size"])
        store.finish_download(wanted, item)
    return found
