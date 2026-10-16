"""Versions transmises entre machines appairées (actions scellées, cf. network/p2p_app.py) :

    update/manifest   manifeste signé d'une version dont cette machine a des fichiers
    update/chunk      un morceau d'un fichier de ce paquet (seulement ceux du manifeste,
                      présents en entier)

Chaque machine annonce dans son profil la version qu'elle peut transmettre, et les
fichiers qu'elle a de chaque paquet (identity.profile, « updates » : « offer »,
« files »). Côté demandeur, fetch() télécharge une version (son manifeste et l'archive
du code), fetch_file() un fichier de plus, le compagnon de sa plateforme ; morceau par
morceau, en reprenant un transfert interrompu, et en vérifiant tout (signature, numéro,
empreinte) avant de garder un fichier. La machine qui transmet n'a pas à être de
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
        held = store.held(wanted)
    except UpdateError:
        held = []
    if not held:
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


def held_by(peer: dict, version: str) -> list:
    """Types de fichiers que `peer` annonce avoir pour cette version (profil lu par
    peers.list_peers). Une machine 2.0.x n'annonce rien : elle n'a que l'archive."""
    files = (((peer.get("profile") or {}).get("updates") or {}).get("files") or {})
    kinds = files.get(version) if isinstance(files, dict) else None
    return [k for k in kinds if isinstance(k, str)] if isinstance(kinds, list) else []


def _manifest(my_id: str, peer: dict, wanted: str, store: Store) -> dict:
    reply = client.call(my_id, peer, "update/manifest", {"version": wanted}, timeout=TIMEOUT)
    found = manifests.verify(reply.get("signed"))
    if found["version"] != wanted:
        raise UpdateError("La machine a envoyé une autre version que celle annoncée.")
    store.add_manifest(reply["signed"])
    return found


def _download(my_id: str, peer: dict, wanted: str, item: dict, store: Store, progress=None):
    """Un fichier du paquet, gardé seulement une fois son empreinte vérifiée."""
    if store.has(wanted, item):
        return
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


def fetch(my_id: str, peer: dict, wanted: str, store: Store, progress=None) -> dict:
    """Télécharge la version `wanted` chez `peer` : son manifeste vérifié, une fois
    l'archive du code dans `store`. PeerError, UpdateError ou OSError sinon. Le
    compagnon de cette plateforme se demande ensuite (fetch_file) à une machine qui
    l'annonce : celle-ci ne l'a pas forcément."""
    found = _manifest(my_id, peer, wanted, store)
    _download(my_id, peer, wanted, manifests.source(found), store, progress)
    return found


def fetch_file(my_id: str, peer: dict, wanted: str, kind: str, store: Store,
               progress=None) -> dict:
    """Télécharge chez `peer` le fichier de type `kind` de la version `wanted` (le
    compagnon de cette plateforme), et le manifeste s'il manque : version installée
    par une machine 2.0.x, qui ne prend que l'archive, ou depuis une archive. Le fichier
    du manifeste, une fois dans le paquet ; PeerError, UpdateError ou OSError sinon."""
    found = store.manifest(wanted)
    if found is None:
        found = _manifest(my_id, peer, wanted, store)
    item = next((f for f in found["files"] if f.get("kind") == kind), None)
    if item is None:
        raise UpdateError(f"La version {wanted} ne livre pas de fichier « {kind} ».")
    _download(my_id, peer, wanted, item, store, progress)
    return item
