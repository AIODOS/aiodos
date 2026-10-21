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

Elle ne peut pas non plus retenir le transfert : chaque appel a une échéance (CALL_LIMIT,
en plus du délai de lecture de client.call, qui repart à chaque octet reçu), chaque
morceau doit faire exactement la taille demandée, et chaque fichier doit arriver à un
débit minimal (MIN_RATE). Un transfert abandonné garde ce qui est reçu : il reprendra là,
chez cette même machine ; une autre le reprend de zéro, sans quoi un morceau faux laissé
par la première ferait accuser la seconde quand l'empreinte échoue. Son profil, enfin, est
lu sans rien supposer de sa forme (offered_by, held_by).
"""
import threading
import time

from aiodos.network import client, envelope
from aiodos.network.client import PeerError
from aiodos.updates import approval
from aiodos.updates import manifest as manifests
from aiodos.updates import version as versions
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

CHUNK = 1024 * 1024
TIMEOUT = 60                     # délai de lecture d'un appel (client.call)
CALL_LIMIT = 150                 # durée totale d'un appel, réponse comprise
MIN_RATE = 32 * 1024             # octets par seconde, en moyenne sur un fichier
FILE_MARGIN = 120                # secondes accordées en plus à chaque fichier
TOO_SLOW = ("L'autre machine transmet trop lentement : transfert abandonné, il reprendra "
            "plus tard.")

_lock = threading.Lock()
_stuck = {}                      # machine -> fil d'un appel resté sans réponse


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
    # Réduite à sa forme canonique : elle a pu être gardée telle quelle par une version
    # plus ancienne, avec ce que la machine qui l'a transmise y avait ajouté
    return {"signed": manifests.canonical(store.signed(wanted))}


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
    **approval.HANDLERS,          # update/pending, update/approve (deuxième appareil)
}


# ------------------------------------------------------------ profil d'une machine
def _updates(peer: dict) -> dict:
    """La partie « updates » du profil de `peer`, ou {} : le profil vient de l'autre
    machine, tel qu'elle l'a envoyé (un ami peut y mettre n'importe quoi)."""
    profile = peer.get("profile") if isinstance(peer, dict) else None
    updates = profile.get("updates") if isinstance(profile, dict) else None
    return updates if isinstance(updates, dict) else {}


def offered_by(peer: dict) -> str | None:
    """La version que `peer` annonce pouvoir transmettre, si son numéro est lisible."""
    offer = _updates(peer).get("offer")
    return offer if versions.valid(offer) else None


def held_by(peer: dict, version: str) -> list:
    """Types de fichiers que `peer` annonce avoir pour cette version (profil lu par
    peers.list_peers). Une machine 2.0.x n'annonce rien : elle n'a que l'archive."""
    files = _updates(peer).get("files")
    kinds = files.get(version) if isinstance(files, dict) else None
    return [k for k in kinds if isinstance(k, str)] if isinstance(kinds, list) else []


# --------------------------------------------------------------------- appels
def _call(my_id: str, peer: dict, action: str, payload: dict,
          limit: float = CALL_LIMIT) -> dict:
    """client.call, dans une échéance de `limit` secondes au total (le délai de client.call
    repart à chaque octet reçu : un filet d'octets le tiendrait indéfiniment). Un appel
    qui la dépasse est abandonné (UpdateError) ; tant qu'il n'a pas fini, on ne demande plus
    rien à cette machine : un seul fil, au plus, reste en attente d'elle."""
    node = peer.get("node_id")
    with _lock:
        waiting = _stuck.get(node)
        if waiting is not None and waiting.is_alive():
            raise UpdateError("L'autre machine n'a toujours pas répondu à la demande "
                              "précédente.")
        _stuck.pop(node, None)
    box = {}

    def run():
        try:
            box["reply"] = client.call(my_id, peer, action, payload, timeout=TIMEOUT)
        except BaseException as exc:          # rendue à l'appelant telle quelle
            box["error"] = exc
    worker = threading.Thread(target=run, daemon=True, name="aiodos-updates-appel")
    worker.start()
    worker.join(max(0.0, limit))
    if worker.is_alive():
        with _lock:
            _stuck[node] = worker
        raise UpdateError(TOO_SLOW)
    if "error" in box:
        raise box["error"]
    reply = box.get("reply")
    if not isinstance(reply, dict):
        raise UpdateError("Réponse illisible de l'autre machine.")
    return reply


def _manifest(my_id: str, peer: dict, wanted: str, store: Store) -> dict:
    reply = _call(my_id, peer, "update/manifest", {"version": wanted})
    found = manifests.verify(reply.get("signed"))
    if found["version"] != wanted:
        raise UpdateError("La machine a envoyé une autre version que celle annoncée.")
    store.add_manifest(reply["signed"])
    return found


def _chunk_bytes(reply: dict) -> bytes:
    data = reply.get("data")
    if not isinstance(data, str):
        return b""
    try:
        return envelope.b64d(data)
    except ValueError:
        return b""


def _download(my_id: str, peer: dict, wanted: str, item: dict, store: Store, progress=None):
    """Un fichier du paquet, gardé seulement une fois son empreinte vérifiée. Déjà reçu
    avec une autre version (même empreinte) : copié sans rien demander au réseau. Chaque
    morceau doit faire exactement la taille demandée, et le fichier arriver avant son
    échéance (MIN_RATE, FILE_MARGIN) ; sinon UpdateError, et la suite reprendra là, chez
    cette même machine : chacune a son propre début de fichier (store.partial_from), pour
    qu'une empreinte fausse accuse toujours la machine qui a tout envoyé."""
    if store.has(wanted, item) or store.reuse(wanted, item):
        return
    source = str(peer.get("node_id") or "")
    part = store.partial_from(wanted, item["name"], source)
    received = part.stat().st_size if part.exists() else 0
    if received > item["size"]:
        part.unlink()
        received = 0
    deadline = time.monotonic() + (item["size"] - received) / MIN_RATE + FILE_MARGIN
    with open(part, "ab") as out:
        while received < item["size"]:
            left = deadline - time.monotonic()
            if left <= 0:
                raise UpdateError(TOO_SLOW)
            expected = min(CHUNK, item["size"] - received)
            data = _chunk_bytes(_call(my_id, peer, "update/chunk",
                                      {"version": wanted, "name": item["name"],
                                       "offset": received, "size": CHUNK},
                                      limit=min(CALL_LIMIT, left)))
            if not data:
                raise UpdateError("Transfert interrompu par l'autre machine.")
            if len(data) != expected:
                raise UpdateError("Morceau de taille inattendue : transfert interrompu.")
            out.write(data)
            received += len(data)
            if progress:
                progress(received, item["size"])
    store.finish_download(wanted, item, source)


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
