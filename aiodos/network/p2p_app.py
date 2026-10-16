"""Serveur entre nœuds (/p2p/v1/…), séparé de l'API de l'interface.

Deux routes publiques seulement : « hello » (qui es-tu ?) et le début d'un
appairage (qui n'aboutit qu'après confirmation sur les deux machines). Tout le
reste exige un message scellé par une machine appairée ; les réponses longues
(une conversation) arrivent en flux de morceaux scellés (/p2p/v1/stream/…).
"""
import asyncio
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from aiodos import __version__
from aiodos.network import envelope, lending, peers, work
from aiodos.network.client import PeerError
from aiodos.network.crypto import SealError
from aiodos.node import identity


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

# Réponses en flux : fonction(manager, émetteur, contenu, stop) -> itérateur de morceaux.
# Elle vérifie la demande tout de suite (PeerError = refus lisible), puis l'itérateur
# produit la réponse ; stop est levé si l'autre machine coupe la connexion.
STREAM_HANDLERS = {
    "chat": lending.chat,         # conversation servie par un modèle de cette machine
}


def create_p2p_app(manager) -> FastAPI:
    app = FastAPI(title="AIODOS — réseau", docs_url=None, redoc_url=None, openapi_url=None)

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
        try:
            payload = envelope.unwrap(peer["key"], action, manager.node_id, body, manager.guard)
        except SealError as exc:
            raise HTTPException(403, str(exc))
        await asyncio.to_thread(peers.touch, sender, request.client.host)
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
