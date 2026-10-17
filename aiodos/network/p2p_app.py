"""Serveur entre nœuds (/p2p/v1/…), séparé de l'API de l'interface.

Deux routes publiques seulement : « hello » (qui es-tu ?) et le début d'un
appairage (qui n'aboutit qu'après confirmation sur les deux machines). Tout le
reste exige un message scellé par une machine appairée ; les réponses longues
(une conversation) arrivent en flux de morceaux scellés (/p2p/v1/stream/…).

Une requête venue d'Internet passe par le compagnon aiodos-net, qui la relaie depuis
127.0.0.1 avec la clé Internet authentifiée de l'appelant (en-tête X-Aiodos-Peer) :
cette clé doit être celle de la machine qui a scellé l'enveloppe, et l'adresse vue
(127.0.0.1) n'est pas retenue comme adresse de la machine.
"""
import asyncio
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from aiodos import __version__
from aiodos.cluster import helper as cluster
from aiodos.network import envelope, lending, netinfo, peers, placement, sharing, work
from aiodos.network.client import PeerError
from aiodos.network.crypto import SealError
from aiodos.node import identity
from aiodos.updates import p2p as updates

PEER_HEADER = "x-aiodos-peer"        # clé Internet de l'appelant, ajoutée par le compagnon


def _ping(manager, sender, payload):
    # Charge de cette machine si elle prête ses modèles (mode automatique, placement.py)
    return {"name": manager.name(), "time": time.time(),
            **placement.load_field(manager, sender)}


def _profile(manager, sender, payload):
    profile = sharing.profile_for(sender, identity.profile())    # allégé pour un ami
    return {**profile, **placement.load_field(manager, sender)}


def _unpair(manager, sender, payload):
    peers.remove(sender)
    manager.net_changed(sender)       # retirée aussi de la liste blanche du compagnon
    manager.changed()
    return {"ok": True}


def _pair_confirm(manager, sender, payload):
    return manager.pairing.remote_answer(sender, bool(payload.get("accept")))


# action -> fonction(manager, émetteur, contenu) ; appelées dans un thread
HANDLERS = {
    "ping": _ping,
    "profile": _profile,
    "unpair": _unpair,
    "pair/confirm": _pair_confirm,
    "net/info": netinfo.handle,   # clés Internet (compagnon aiodos-net) échangées
    **work.HANDLERS,              # travail confié : work/lease, work/blob, work/result…
    **updates.HANDLERS,           # versions signées : update/manifest, update/chunk
    **cluster.HANDLERS,           # carte graphique prêtée : cluster/lend, keep, release
}

# Réponses en flux : fonction(manager, émetteur, contenu, stop) -> itérateur de morceaux.
# Elle vérifie la demande tout de suite (PeerError = refus lisible), puis l'itérateur
# produit la réponse ; stop est levé si l'autre machine coupe la connexion.
STREAM_HANDLERS = {
    "chat": lending.chat,         # conversation servie par un modèle de cette machine
}


def _caller_address(request: Request) -> str | None:
    """Adresse de l'appelant à retenir ; aucune si la requête vient du compagnon
    (l'adresse vue est alors 127.0.0.1, pas celle de la machine)."""
    return None if request.headers.get(PEER_HEADER) else request.client.host


def _check_transport(peer: dict, request: Request):
    """Par Internet, la clé authentifiée par le compagnon doit être celle de la machine
    qui a scellé l'enveloppe : une machine appairée ne se fait pas passer pour une autre."""
    key = (request.headers.get(PEER_HEADER) or "").lower()
    if key and peer.get("net_id") and key != peer["net_id"]:
        raise HTTPException(403, "Clé Internet de l'appelant inattendue pour cette machine.")


def create_p2p_app(manager) -> FastAPI:
    app = FastAPI(title="AIODOS — réseau", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/p2p/v1/hello")
    def hello():
        return {"protocol": envelope.PROTOCOL, "node_id": manager.node_id,
                "name": manager.name(), "version": __version__, "port": manager.p2p_port}

    @app.post("/p2p/v1/pair/start")
    def pair_start(body: dict, request: Request):
        net_key = (request.headers.get(PEER_HEADER) or "").lower()
        try:
            if net_key:               # par le compagnon : seulement sur invitation
                return manager.pairing.incoming_remote(body, net_key)
            return manager.pairing.incoming(body, request.client.host)
        except PeerError as exc:
            raise HTTPException(403 if net_key else 400, str(exc))

    # Déclarée avant la route générique ci-dessous, qui capterait sinon
    # « stream/… » comme une action inconnue (FastAPI suit l'ordre des routes)
    @app.post("/p2p/v1/stream/{action:path}")
    async def sealed_stream(action: str, body: dict, request: Request):
        handler = STREAM_HANDLERS.get(action)
        if handler is None:
            raise HTTPException(404, "Action inconnue.")
        sender = str(body.get("from") or "")
        peer = await asyncio.to_thread(peers.get, sender)
        if not peer:
            raise HTTPException(403, "Machine non appairée.")
        _check_transport(peer, request)
        try:
            payload = envelope.unwrap(peer["key"], action, manager.node_id, body, manager.guard)
        except SealError as exc:
            raise HTTPException(403, str(exc))
        await asyncio.to_thread(peers.touch, sender, _caller_address(request))
        stop = threading.Event()
        try:
            chunks = await asyncio.to_thread(handler, manager, sender, payload, stop)
        except PeerError as exc:
            raise HTTPException(400, str(exc))
        loop = asyncio.get_running_loop()
        inbox = asyncio.Queue()

        def pump():                       # le calcul est bloquant : dans un thread
            put = lambda item: loop.call_soon_threadsafe(inbox.put_nowait, item)
            try:
                for chunk in chunks:
                    if stop.is_set():
                        break
                    put(("data", chunk))
                put(("end", None))
            except Exception as exc:
                put(("error", str(exc)[:300]))

        async def frames():
            threading.Thread(target=pump, daemon=True, name=f"aiodos-flux-{action}").start()
            seq = 0
            try:
                while True:
                    kind, data = await inbox.get()
                    item = data if kind == "data" else {"end": True} if kind == "end" \
                        else {"error": data}
                    yield envelope.wrap_frame(peer["key"], action, manager.node_id, sender,
                                              body["ts"], seq, item) + "\n"
                    seq += 1
                    if kind != "data":
                        break
            finally:
                stop.set()                # fin, ou machine partie : on arrête le calcul

        return StreamingResponse(frames(), media_type="text/plain")

    @app.post("/p2p/v1/{action:path}")
    async def sealed(action: str, body: dict, request: Request):
        handler = HANDLERS.get(action)
        if handler is None:
            raise HTTPException(404, "Action inconnue.")
        sender = str(body.get("from") or "")
        if action == "pair/confirm":
            key = manager.pairing.key_for(sender)
            pending = manager.pairing.get(sender)
            net_key = (request.headers.get(PEER_HEADER) or "").lower()
            if net_key and (pending is None or pending.net_id != net_key):
                raise HTTPException(403, "Clé Internet de l'appelant inattendue pour cet appairage.")
        else:
            peer = await asyncio.to_thread(peers.get, sender)
            key = peer["key"] if peer else None
            if peer:
                _check_transport(peer, request)
        if not key:
            raise HTTPException(403, "Machine non appairée.")
        try:
            payload = envelope.unwrap(key, action, manager.node_id, body, manager.guard)
        except SealError as exc:
            raise HTTPException(403, str(exc))
        if action != "pair/confirm":
            await asyncio.to_thread(peers.touch, sender, _caller_address(request))
        try:
            result = await asyncio.to_thread(handler, manager, sender, payload)
        except PeerError as exc:
            raise HTTPException(400, str(exc))
        return envelope.wrap_reply(key, action, manager.node_id, sender, body["ts"], result)

    return app
