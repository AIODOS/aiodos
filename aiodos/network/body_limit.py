"""Taille des requêtes reçues par le serveur entre machines, bornée avant d'être lue.

Faille du 20/10/2026 : ce serveur (0.0.0.0:47701, ouvert par défaut) lisait en mémoire,
puis analysait, tout le corps d'une requête avant de savoir qui l'envoyait. Quelques
centaines de Mio envoyées par un appareil du réseau local, sans appairage, suffisaient à
geler puis arrêter AIODOS tout entier (l'interface tourne dans le même processus). Désormais :

- « hello » et le début d'un appairage : HELLO_BODY et OPEN_BODY, pour tout le monde ;
- un message scellé : OPEN_BODY, sauf d'une machine appairée qui arrive par le chemin
  qu'on lui connaît, son adresse sur le réseau local ou sa clé Internet authentifiée par
  le compagnon (connections.internet_key) : SEALED_BODY. « from », en tête de l'enveloppe,
  ne dit que qui prétend envoyer : les identifiants circulent en clair dans les balises ;
- un résultat de calcul (work/result) : davantage seulement pour une image ou une vidéo
  que cette machine calcule pour celle-ci (work.result_limit, jusqu'à HUGE_BODY), par ce
  même chemin ;
- peu de gros corps lus à la fois : au-delà d'OPEN_BODY, BIG_PER_SENDER par machine et
  BIG_TOTAL en tout ; au-delà de SEALED_BODY, HUGE_PER_SENDER et HUGE_TOTAL. Les suivants
  attendent leur tour (SLOT_WAIT au plus, puis 503), sans être lus ;
- la demande d'un inconnu du réseau public (/public/…, relayée par le compagnon avec sa clé
  jetable, connections.public_key) : PUBLIC_BODY, comme le compagnon l'a déjà bornée ;
  sans cette clé, OPEN_BODY (elle aura un 404, network/public_lending.py).

Une taille annoncée trop grande est refusée sans rien lire ; sans taille annoncée (envoi
en morceaux), la lecture s'arrête à la limite. L'enveloppe est ensuite vérifiée comme
avant (p2p_app.py). Délais et nombre de connexions : connections.py.

Une machine qui vient de changer d'adresse n'envoie de gros messages qu'une fois reconnue
à la nouvelle : ses petits messages scellés (« ping » toutes les 15 s) l'apprennent ici.
"""
import asyncio
import json
import re

from aiodos import i18n
from aiodos.i18n import t
from aiodos.network import connections, peers, work

BASE = "/p2p/v1/"
PUBLIC_BASE = "/public/"             # demandes d'inconnus du réseau public (public_lending.py)
HELLO_BODY = 4 * 1024                # « hello » n'a pas de contenu
PUBLIC_BODY = 256 * 1024             # demande d'un inconnu (le compagnon la borne de même)
OPEN_BODY = 64 * 1024                # début d'appairage ; message d'une machine inconnue
SEALED_BODY = 16 * 1024 ** 2         # conversation prêtée, présentations, recherches…
# Une vidéo calculée par une de mes machines (jobs/kinds/images.MAX_WEBM, 512 Mio), en
# base64 dans la sortie puis dans l'enveloppe scellée (work.result_limit)
HUGE_BODY = 1024 ** 3
PREFIX = 4096                        # « from » est en tête de l'enveloppe (envelope.wrap)
BIG_PER_SENDER = 2                   # corps de plus d'OPEN_BODY lus à la fois, par machine
BIG_TOTAL = 8                        # ... et en tout
HUGE_PER_SENDER = 1                  # corps de plus de SEALED_BODY lus à la fois, par machine
HUGE_TOTAL = 2                       # ... et en tout
SLOT_WAIT = 45                       # s d'attente d'un tour (l'envoyeur attend 60 s au plus)
_FROM = re.compile(rb'"from"\s*:\s*"([^"\\]{1,64})"')


def _mib(size: int) -> str:
    if size >= 1024 ** 2:
        return t("p2p.size_mib", n=i18n.number(size / 1024 ** 2))
    return t("p2p.size_kib", n=size // 1024)


def _declared(scope) -> int | None:
    """Content-Length annoncé, ou None (envoi en morceaux, ou en-tête illisible)."""
    for name, value in scope.get("headers") or []:
        if name == b"content-length":
            try:
                return max(0, int(value))
            except ValueError:
                return None
    return None


def _same_path(scope, known: dict) -> bool:
    """La requête arrive-t-elle par le chemin connu de cette machine ? Par le compagnon
    (depuis cette machine même) : sa clé Internet authentifiée ; sinon : son adresse."""
    net_key = connections.internet_key(scope)
    if net_key:
        return bool(known.get("net_id")) and net_key == str(known["net_id"]).lower()
    client = scope.get("client") or (None, None)
    return bool(known.get("address")) and client[0] is not None \
        and connections.normal_host(client[0]) == connections.normal_host(known["address"])


async def _answer(send, status: int, detail: str, payload: dict | None = None):
    data = json.dumps(payload or {"detail": detail}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": data})


async def _refuse(send, detail: str):
    await _answer(send, 413, detail)


async def _refuse_public(send, detail: str):
    """Un inconnu du réseau public lit un code (public_lending.py), pas une phrase."""
    await _answer(send, 413, detail, {"error": "too_big"})


class _Slots:
    """Gros corps lus en même temps : `per_sender` par machine, `total` en tout. Les
    suivants attendent qu'un tour se libère (tout se passe dans la boucle du nœud)."""

    def __init__(self, per_sender: int, total: int):
        self.per_sender, self.total = per_sender, total
        self.held = {}                   # machine -> corps en cours
        self.count = 0
        self._waiters = []

    def _free(self, sender) -> bool:
        return self.count < self.total and self.held.get(sender, 0) < self.per_sender

    async def acquire(self, sender: str, wait: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while not self._free(sender):
            left = deadline - loop.time()
            if left <= 0:
                return False
            waiter = loop.create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, left)
            except TimeoutError:
                pass
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
        self.held[sender] = self.held.get(sender, 0) + 1
        self.count += 1
        return True

    def release(self, sender: str):
        self.count -= 1
        left = self.held.get(sender, 1) - 1
        if left > 0:
            self.held[sender] = left
        else:
            self.held.pop(sender, None)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                try:
                    waiter.set_result(None)
                except RuntimeError:     # boucle d'un essai précédent, fermée
                    pass


class BodyLimit:
    """Middleware ASGI du serveur entre machines (p2p_app.create_p2p_app). `actions` : les
    actions connues ; une autre n'a droit qu'à OPEN_BODY (elle aura un 404)."""

    def __init__(self, app, actions):
        self.app = app
        self.actions = frozenset(actions)
        self.big = _Slots(BIG_PER_SENDER, BIG_TOTAL)
        self.huge = _Slots(HUGE_PER_SENDER, HUGE_TOTAL)

    def _ceiling(self, action) -> int:
        """Le plus que cette route peut recevoir, de la machine la mieux placée."""
        if action == "hello":
            return HELLO_BODY
        if action is None or action == "pair/start" or action not in self.actions:
            return OPEN_BODY
        return HUGE_BODY if action == "work/result" else SEALED_BODY

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        action = path[len(BASE):] if path.startswith(BASE) else None
        public = path.startswith(PUBLIC_BASE)
        if public:                        # un inconnu : sa limite, décidée tout de suite
            ceiling = PUBLIC_BODY if connections.public_key(scope) else OPEN_BODY
        else:
            ceiling = self._ceiling(action)
        refuse = _refuse_public if public else _refuse
        declared = _declared(scope)
        if declared is not None and declared > ceiling:
            return await refuse(send, t("p2p.request_too_big", limit=_mib(ceiling)))
        # Au-delà d'OPEN_BODY, la limite dépend de la machine qui envoie : décidée dès que
        # le début de l'enveloppe est lu (ou dès qu'on dépasse OPEN_BODY sans taille annoncée)
        decided = public or ceiling <= OPEN_BODY
        limit = ceiling if decided else OPEN_BODY
        refusal = t("p2p.request_too_big", limit=_mib(limit))
        slot = None                      # (tours, machine) : un gros corps en cours de lecture
        try:
            chunks, total = [], 0
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return                     # l'autre machine a coupé : rien à répondre
                part = message.get("body") or b""
                if part:
                    chunks.append(part)
                    total += len(part)
                if not decided and (total > OPEN_BODY
                                    or ((declared or 0) > OPEN_BODY and total >= PREFIX)):
                    decided = True
                    prefix = b"".join(chunks)[:PREFIX]
                    limit, refusal, sender = await self._for_sender(scope, prefix, ceiling)
                    if declared is not None and declared > limit:
                        return await _refuse(send, refusal)
                    if limit > OPEN_BODY:
                        huge = limit > SEALED_BODY and (declared is None or declared > SEALED_BODY)
                        pool = self.huge if huge else self.big
                        if not await pool.acquire(sender, SLOT_WAIT):
                            return await _answer(send, 503, t("p2p.busy_receiving"))
                        slot = (pool, sender)
                if total > limit:
                    return await refuse(send, refusal)
                if not message.get("more_body"):
                    break
            body = b"".join(chunks)
            chunks = None
            given = False

            async def replay():
                nonlocal body, given
                if not given:
                    given, data, body = True, body, None
                    return {"type": "http.request", "body": data, "more_body": False}
                return await receive()         # fin de connexion (flux de conversation)

            async def sending(message):
                # Le tour est rendu dès que la réponse commence (une conversation prêtée
                # dure) ; sinon, quand la requête a été traitée
                nonlocal slot
                if slot is not None and message["type"] == "http.response.start":
                    slot[0].release(slot[1])
                    slot = None
                await send(message)
            await self.app(scope, replay, sending if slot is not None else send)
        finally:
            if slot is not None:
                slot[0].release(slot[1])

    async def _for_sender(self, scope, prefix: bytes, ceiling: int) -> tuple:
        """(limite, refus à donner au-delà, machine) selon la machine qui dit envoyer ce
        message et le chemin par lequel il arrive."""
        found = _FROM.search(prefix)
        sender = found.group(1).decode("utf-8", "replace") if found else ""
        known = await asyncio.to_thread(peers.transport, sender) if sender else None
        if known is None:
            return OPEN_BODY, t("p2p.unpaired_too_big", limit=_mib(OPEN_BODY)), sender
        if not _same_path(scope, known):
            # L'identifiant d'une de mes machines, depuis une autre adresse : un appareil
            # qui le lit dans ses balises, ou cette machine qui vient de changer d'adresse
            return OPEN_BODY, t("p2p.unknown_address_too_big", limit=_mib(OPEN_BODY)), sender
        limit = min(ceiling, SEALED_BODY)
        if ceiling > SEALED_BODY:
            limit = min(ceiling, max(limit, await asyncio.to_thread(work.result_limit, sender)))
        return limit, t("p2p.request_too_big", limit=_mib(limit)), sender
