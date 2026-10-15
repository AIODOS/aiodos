"""Serveur entre nœuds (/p2p/v1/…), séparé de l'API de l'interface.

Deux routes publiques seulement : « hello » (qui es-tu ?) et le début d'un
appairage (qui n'aboutit qu'après confirmation sur les deux machines). Tout le
reste exige un message scellé par une machine appairée.
"""
import asyncio
import time

from fastapi import FastAPI, HTTPException, Request

from coeur import __version__
from coeur.network import envelope, peers, work
from coeur.network.client import PeerError
from coeur.network.crypto import SealError
from coeur.node import identity


def _ping(manager, sender, payload):
    return {"name": manager.name(), "time": time.time()}


def _profile(manager, sender, payload):
    return identity.profile()


def _unpair(manager, sender, payload):
    peers.remove(sender)
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
    **work.HANDLERS,              # travail confié : work/lease, work/blob, work/result…
}


def create_p2p_app(manager) -> FastAPI:
    app = FastAPI(title="Cœur IA — réseau", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/p2p/v1/hello")
    def hello():
        return {"protocol": envelope.PROTOCOL, "node_id": manager.node_id,
                "name": manager.name(), "version": __version__, "port": manager.p2p_port}

    @app.post("/p2p/v1/pair/start")
    def pair_start(body: dict, request: Request):
        try:
            return manager.pairing.incoming(body, request.client.host)
        except PeerError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/p2p/v1/{action:path}")
    async def sealed(action: str, body: dict, request: Request):
        handler = HANDLERS.get(action)
        if handler is None:
            raise HTTPException(404, "Action inconnue.")
        sender = str(body.get("from") or "")
        if action == "pair/confirm":
            key = manager.pairing.key_for(sender)
        else:
            peer = await asyncio.to_thread(peers.get, sender)
            key = peer["key"] if peer else None
        if not key:
            raise HTTPException(403, "Machine non appairée.")
        try:
            payload = envelope.unwrap(key, action, manager.node_id, body, manager.guard)
        except SealError as exc:
            raise HTTPException(403, str(exc))
        if action != "pair/confirm":
            await asyncio.to_thread(peers.touch, sender, request.client.host)
        try:
            result = await asyncio.to_thread(handler, manager, sender, payload)
        except PeerError as exc:
            raise HTTPException(400, str(exc))
        return envelope.wrap_reply(key, action, manager.node_id, sender, body["ts"], result)

    return app
