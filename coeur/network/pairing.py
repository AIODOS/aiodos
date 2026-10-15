"""Appairage de deux machines par comparaison d'un code à 6 chiffres.

1. Sur la machine A, l'utilisateur choisit B dans la liste et lance l'appairage.
2. A et B échangent des clés éphémères ; chacune affiche le même code.
3. Sur CHAQUE machine, l'utilisateur confirme que le code est identique.
4. Quand les deux ont accepté, la clé de la paire est enregistrée des deux côtés.

Rien n'est automatique : un appairage exige un geste sur les deux machines.
"""
import threading
import time
from dataclasses import dataclass, field

from coeur.network import client, crypto, envelope, peers
from coeur.network.client import PeerError

PENDING_TTL = 180          # une demande non conclue expire au bout de 3 minutes
MAX_PENDING = 3


@dataclass
class Pending:
    node_id: str
    name: str
    address: str
    port: int
    role: str               # initiator | responder
    key: bytes
    code: str
    created: float = field(default_factory=time.time)
    local_ok: bool | None = None       # réponse de l'utilisateur de cette machine
    remote_ok: bool | None = None      # réponse de l'utilisateur de l'autre machine


class Pairing:
    def __init__(self, manager):
        self.m = manager
        self._pending = {}
        self._lock = threading.Lock()

    def _purge(self):
        now = time.time()
        for node_id, p in list(self._pending.items()):
            if now - p.created > PENDING_TTL:
                del self._pending[node_id]

    def get(self, node_id) -> Pending | None:
        with self._lock:
            self._purge()
            return self._pending.get(node_id)

    def key_for(self, node_id) -> bytes | None:
        p = self.get(node_id)
        return p.key if p else None

    def snapshot(self) -> list:
        with self._lock:
            self._purge()
            return [{"node_id": p.node_id, "name": p.name, "role": p.role, "code": p.code,
                     "local_ok": p.local_ok, "remote_ok": p.remote_ok}
                    for p in self._pending.values()]

    # ------------------------------------------------------- côté initiateur
    def start(self, address, port) -> Pending:
        info = client.hello(address, port)
        if info["node_id"] == self.m.node_id:
            raise PeerError("C'est cette machine-ci.")
        private, public = crypto.new_exchange()
        reply = client.pair_start(address, port, {
            "node_id": self.m.node_id, "name": self.m.name(),
            "port": self.m.p2p_port, "pub": envelope.b64e(public)})
        key, code = crypto.pair_secrets(private, envelope.b64d(reply["pub"]),
                                        self.m.node_id, reply["node_id"])
        pending = Pending(reply["node_id"], reply["name"], address, int(port),
                          "initiator", key, code)
        with self._lock:
            self._purge()
            self._pending[pending.node_id] = pending
        self.m.changed()
        return pending

    # -------------------------------------------- côté répondant (route réseau)
    def incoming(self, payload: dict, address: str) -> dict:
        try:
            node_id = str(payload["node_id"])
            port = int(payload["port"])
            peer_public = envelope.b64d(payload["pub"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerError("Demande d'appairage incomplète.") from exc
        if node_id == self.m.node_id:
            raise PeerError("Une machine ne s'appaire pas avec elle-même.")
        with self._lock:
            self._purge()
            if len(self._pending) >= MAX_PENDING and node_id not in self._pending:
                raise PeerError("Trop de demandes d'appairage en cours, réessaie plus tard.")
        private, public = crypto.new_exchange()
        key, code = crypto.pair_secrets(private, peer_public, self.m.node_id, node_id)
        name = str(payload.get("name") or "Machine sans nom")[:60]
        pending = Pending(node_id, name, address, port, "responder", key, code)
        with self._lock:
            self._pending[node_id] = pending
        self.m.publish({"type": "pair_request", "node_id": node_id, "name": name, "code": code})
        self.m.changed()
        return {"node_id": self.m.node_id, "name": self.m.name(), "pub": envelope.b64e(public)}

    # ------------------------------------- réponse de l'utilisateur d'ici
    def answer(self, node_id, accept: bool):
        pending = self.get(node_id)
        if pending is None:
            raise PeerError("Cette demande d'appairage a expiré.")
        pending.local_ok = bool(accept)
        target = {"node_id": pending.node_id, "address": pending.address,
                  "port": pending.port, "name": pending.name}
        try:
            reply = client.call(self.m.node_id, target, "pair/confirm",
                                {"accept": bool(accept)}, key=pending.key)
            if reply.get("accept") is not None:
                pending.remote_ok = bool(reply["accept"])
        except PeerError:
            if accept:
                raise
        self._settle(pending)

    # -------------------------------- réponse de l'utilisateur de l'autre machine
    def remote_answer(self, node_id, accept: bool) -> dict:
        pending = self.get(node_id)
        if pending is None:
            raise PeerError("Aucune demande d'appairage en cours avec cette machine.")
        pending.remote_ok = bool(accept)
        local = pending.local_ok
        self._settle(pending)
        return {"accept": local}

    def _settle(self, pending: Pending):
        with self._lock:
            if self._pending.get(pending.node_id) is not pending:
                return
            if pending.local_ok is False or pending.remote_ok is False:
                del self._pending[pending.node_id]
                outcome = "refused"
            elif pending.local_ok and pending.remote_ok:
                del self._pending[pending.node_id]
                peers.save(pending.node_id, pending.name, pending.key,
                           pending.address, pending.port)
                outcome = "paired"
            else:
                outcome = None
        if outcome:
            self.m.publish({"type": "pair_result", "node_id": pending.node_id,
                            "name": pending.name, "outcome": outcome})
        self.m.changed()
