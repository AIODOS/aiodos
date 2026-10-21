"""Serveur entre nœuds (/p2p/v1/…), séparé de l'API de l'interface.

Deux routes publiques seulement : « hello » (qui es-tu ?) et le début d'un
appairage (qui n'aboutit qu'après confirmation sur les deux machines). Tout le
reste exige un message scellé par une machine appairée ; les réponses longues
(une conversation) arrivent en flux de morceaux scellés (/p2p/v1/stream/…).

Une requête venue d'Internet passe par le compagnon aiodos-net, qui la relaie depuis
127.0.0.1 avec la clé Internet authentifiée de l'appelant (en-tête X-Aiodos-Peer) :
cette clé doit être celle de la machine qui a scellé l'enveloppe, et l'adresse vue
(127.0.0.1) n'est pas retenue comme adresse de la machine. Cet en-tête n'est cru que d'une
connexion venue de cette machine même (connections.internet_key) : un appareil du réseau
local qui l'enverrait est traité comme ce qu'il est.

La taille des requêtes est bornée avant toute lecture (body_limit.py), et le corps est
analysé ici plutôt que par FastAPI : son erreur 422 recopiait tout ce qui avait été reçu.
Un gros corps n'est analysé que s'il a la forme d'une enveloppe (quelques valeurs simples) :
quelques Mio de [{}, {}, …] deviendraient sinon des centaines de Mio d'objets.

Un « ping » qui le demande (« where », beacons.py) reçoit, dans sa réponse scellée, l'adresse
et le port où il est arrivé ici : une balise ne suffit plus pour qu'une autre machine suive
celle-ci à une nouvelle adresse, il faut que la requête y soit arrivée.
"""
import asyncio
import json
import re
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from aiodos import __version__
from aiodos.cluster import helper as cluster
from aiodos.i18n import t
from aiodos.imaging import share as images_share
from aiodos.network import (activity, body_limit, connections, consult, envelope, group,
                            lending, netinfo, peers, placement, sharing, work)
from aiodos.network.client import PeerError
from aiodos.network.crypto import SealError
from aiodos.node import identity
from aiodos.updates import p2p as updates

# Une enveloppe (envelope.wrap) : un objet de quelques valeurs simples, chaînes ou nombres.
# Expression linéaire, vérifiée avant d'analyser un gros corps
_STRING = r'"[^"\\]*(?:\\.[^"\\]*)*"'
_SIMPLE = rf'(?:{_STRING}|-?[0-9][0-9.eE+-]{{0,40}}|true|false|null)'
_PAIR = rf'\s*{_STRING}\s*:\s*{_SIMPLE}\s*'
_ENVELOPE = re.compile(rf'\s*\{{{_PAIR}(?:,{_PAIR}){{0,7}}\}}\s*'.encode())


def _ping(manager, sender, payload):
    # Charge de cette machine si elle prête ses modèles (mode automatique, placement.py),
    # et l'état de son partage (liste « Machines connectées », activity.py)
    return {"name": manager.name(), "time": time.time(),
            **placement.load_field(manager, sender), **activity.field(manager, sender)}


def _profile(manager, sender, payload):
    # Allégé pour un ami ; avec, pour chacune, le nombre de bibliothèques qui lui sont
    # ouvertes (elle n'envoie ses questions qu'aux machines qui ont quelque chose pour elle),
    # et la charge de cette machine pour mes machines (mode automatique, placement.py)
    return dict(sharing.profile_for(sender, identity.profile()),
                consult={"open": consult.open_count(sender)},
                **placement.load_field(manager, sender), **activity.field(manager, sender))


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
    **consult.HANDLERS,           # recherche dans mes bibliothèques ouvertes : library/search
    **work.HANDLERS,              # travail confié : work/lease, work/blob, work/result…
    **updates.HANDLERS,           # versions signées : update/manifest, update/chunk
    **cluster.HANDLERS,           # carte graphique prêtée : cluster/lend, keep, release
    **images_share.HANDLERS,      # modèles d'images copiés entre mes machines : images/file
    **group.HANDLERS,             # appairage par groupe : group/introduce, propose, key…
}

# Réponses en flux : fonction(manager, émetteur, contenu, stop) -> itérateur de morceaux.
# Elle vérifie la demande tout de suite (PeerError = refus lisible), puis l'itérateur
# produit la réponse ; stop est levé si l'autre machine coupe la connexion.
ALIVE_S = 10                    # signe de vie d'un flux silencieux (client.stream attend 180 s)

STREAM_HANDLERS = {
    "chat": lending.chat,         # conversation servie par un modèle de cette machine
}


def _parse(raw: bytes):
    """Un gros corps n'est analysé que s'il a la forme d'une enveloppe : sinon None."""
    if len(raw) > body_limit.OPEN_BODY and _ENVELOPE.fullmatch(raw) is None:
        return None
    return json.loads(raw)


async def _envelope(request: Request) -> dict:
    """Le corps de la requête (déjà borné par body_limit.py), un objet JSON ; un gros corps
    est vérifié puis analysé hors de la boucle du nœud, qui sert aussi l'interface.
    Illisible : une réponse courte, sans rien recopier de ce qui a été reçu."""
    raw = await request.body()
    try:
        if len(raw) > body_limit.OPEN_BODY:
            body = await asyncio.to_thread(_parse, raw)
        else:
            body = _parse(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, t("p2p.envelope_expected"))
    return body


def _net_key(request: Request) -> str:
    """Clé Internet de l'appelant, ajoutée par le compagnon ; "" pour une requête venue du
    réseau local, même si elle porte l'en-tête (connections.internet_key)."""
    return connections.internet_key(request.scope)


def _caller_address(request: Request) -> str | None:
    """Adresse de l'appelant à retenir ; aucune si la requête vient du compagnon
    (l'adresse vue est alors 127.0.0.1, pas celle de la machine)."""
    return None if _net_key(request) else request.client.host


def _check_transport(peer: dict, request: Request):
    """Par Internet, la clé authentifiée par le compagnon doit être celle de la machine
    qui a scellé l'enveloppe : une machine appairée ne se fait pas passer pour une autre."""
    key = _net_key(request)
    if key and peer.get("net_id") and key != str(peer["net_id"]).lower():
        raise HTTPException(403, t("p2p.wrong_net_key"))


def _arrived_at(request: Request) -> list | None:
    """[adresse, port] où cette requête est arrivée ici (réponse à un « ping » qui le
    demande, beacons.py) ; None par le compagnon, qui la relaie depuis cette machine."""
    server = request.scope.get("server")
    if _net_key(request) or not server or server[1] is None:
        return None
    return [connections.normal_host(server[0]), int(server[1])]


def create_p2p_app(manager) -> FastAPI:
    # i18n: tel quel (titre de l'application, jamais montré : documentation désactivée)
    app = FastAPI(title="AIODOS — réseau", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(body_limit.BodyLimit,
                       actions={"hello", "pair/start", *HANDLERS,
                                *(f"stream/{action}" for action in STREAM_HANDLERS)})

    @app.post("/p2p/v1/hello")
    def hello():
        return {"protocol": envelope.PROTOCOL, "node_id": manager.node_id,
                "name": manager.name(), "version": __version__, "port": manager.p2p_port}

    @app.post("/p2p/v1/pair/start")
    async def pair_start(request: Request):
        body = await _envelope(request)
        net_key = _net_key(request)
        try:
            if net_key:               # par le compagnon : seulement sur invitation
                return await asyncio.to_thread(manager.pairing.incoming_remote, body, net_key)
            return await asyncio.to_thread(manager.pairing.incoming, body, request.client.host)
        except PeerError as exc:
            raise HTTPException(403 if net_key else 400, str(exc))

    # Déclarée avant la route générique ci-dessous, qui capterait sinon
    # « stream/… » comme une action inconnue (FastAPI suit l'ordre des routes)
    @app.post("/p2p/v1/stream/{action:path}")
    async def sealed_stream(action: str, request: Request):
        handler = STREAM_HANDLERS.get(action)
        if handler is None:
            raise HTTPException(404, t("p2p.unknown_action"))
        body = await _envelope(request)
        sender = str(body.get("from") or "")
        peer = await asyncio.to_thread(peers.get, sender)
        if not peer or not peer["key"]:
            raise HTTPException(403, t("p2p.not_paired"))
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
            finally:
                # Arrêtée en route : la réponse se ferme tout de suite (sa place dans la file
                # des conversations prêtées est rendue), sans attendre le ramasse-miettes
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()

        async def frames():
            threading.Thread(target=pump, daemon=True, name=f"aiodos-flux-{action}").start()
            seq = 0
            try:
                while True:
                    try:
                        kind, data = await asyncio.wait_for(inbox.get(), timeout=ALIVE_S)
                    except TimeoutError:
                        # Rien depuis ALIVE_S (modèle qui se charge, longue réflexion) : un
                        # signe de vie, que la machine qui attend ignore ; sans lui, elle ne
                        # saurait pas si celle-ci a disparu
                        kind, data = "data", {"alive": True}
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
    async def sealed(action: str, request: Request):
        handler = HANDLERS.get(action)
        if handler is None:
            raise HTTPException(404, t("p2p.unknown_action"))
        body = await _envelope(request)
        sender = str(body.get("from") or "")
        if action == "pair/confirm":
            key = manager.pairing.key_for(sender)
            pending = manager.pairing.get(sender)
            net_key = _net_key(request)
            if net_key and (pending is None or pending.net_id != net_key):
                raise HTTPException(403, t("p2p.wrong_net_key_pairing"))
        else:
            peer = await asyncio.to_thread(peers.get, sender)
            key = peer["key"] if peer else None
            if peer:
                _check_transport(peer, request)
        if not key:
            raise HTTPException(403, t("p2p.not_paired"))
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
        if action == "ping" and isinstance(payload, dict) and payload.get("where") is True:
            result = dict(result, where=_arrived_at(request))
        return envelope.wrap_reply(key, action, manager.node_id, sender, body["ts"], result)

    return app
