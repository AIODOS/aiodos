"""Appairage de deux machines par comparaison d'un code à 6 chiffres.

1. Sur la machine A, l'utilisateur choisit B dans la liste et lance l'appairage.
2. A et B échangent des clés éphémères, avec engagement (comme la comparaison numérique
   de l'appairage Bluetooth, cf. crypto.py), en deux appels à « pair/start » :
   a. « commit » : A envoie son engagement sur sa clé et un nonce, pas sa clé ;
   b. B répond avec sa clé et son nonce ;
   c. « reveal » : A révèle sa clé et son nonce ; B vérifie qu'ils sont ceux de
      l'engagement, sinon il refuse.
   Chacune affiche alors le même code, tiré de toute la transcription : un intermédiaire
   ne peut plus choisir sa clé après coup pour faire apparaître le même code des deux côtés.
3. Sur CHAQUE machine, l'utilisateur confirme que le code est identique, et dit si
   l'autre est une de ses machines ou celle d'un ami (cf. sharing.py) : chacune juge
   l'autre. Sa réponse porte l'identifiant de la demande qu'il a vue (ref).
4. Quand les deux ont accepté, la clé de la paire est enregistrée des deux côtés.

Une demande en attente n'est jamais remplacée : une autre pour la même machine est refusée
tant que la première n'a pas abouti, été refusée ou expiré. Une machine déjà appairée
n'est remplacée que sur un geste explicite (« remplacer sa clé », replace). Une machine
d'une version sans engagement reçoit un refus qui dit de mettre AIODOS à jour : jamais
l'ancien échange, plus faible.

Rien n'est automatique : un appairage exige un geste sur les deux machines. Par
Internet (start_remote, incoming_remote), il faut en plus une invitation acceptée des
deux côtés (cf. invitations.py) : les messages passent alors par les compagnons.

Un nœud dont la version d'origine ignore les amis (updates/launcher.friends_refused, par
group.friends_refused : Termux ou Linux installé depuis une 2.0.x) refuse d'accepter
l'autre machine comme celle d'un ami : si cette version reprenait la main, elle la
traiterait comme une de mes machines. Le refus dit quoi faire (réinstaller ce nœud) ; rien
n'est envoyé à l'autre machine, et la demande reste affichée jusqu'à ce qu'on la refuse ou
qu'elle expire.
"""
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from aiodos.i18n import t
from aiodos.network import (client, connections, crypto, envelope, group, invitations,
                            peers, sharing)
from aiodos.network.client import PeerError

log = logging.getLogger("aiodos.network")

PENDING_TTL = 180          # une demande non conclue expire au bout de 3 minutes
MAX_PENDING = 3            # demandes reçues en attente à la fois
MAX_STARTS = 10            # demandes reçues acceptées par période de PENDING_TTL, quelle
                           # qu'en soit l'issue : un intermédiaire n'a que quelques essais
PROTOCOL = crypto.PAIRING

INITIATOR, RESPONDER = "initiator", "responder"
STARTING = "starting"      # chez celle qui lance : l'échange est en cours
COMMITTED = "committed"    # chez celle qui répond : engagement reçu, clé pas encore révélée
SPOILED = "spoiled"        # chez celle qui répond : révélation fausse, gardée jusqu'à expiration
READY = "ready"            # code calculé : affiché, en attente des réponses

# Ce que répond une version sans engagement (2.0.1 et avant) à une demande sans clé
# i18n: tel quel (réponse d'une version d'avant, reconnue mot pour mot)
OLD_INCOMPLETE = "Demande d'appairage incomplète."


class PairingRefused(PeerError):
    """Appairage refusé ici, avec un message à montrer tel quel (pas « injoignable »)."""


def _outdated_there(name: str) -> str:
    return t("pairing.outdated_there", name=name)


def _outdated_here(name: str) -> str:
    """Pour une machine plus ancienne qui lance l'appairage : c'est elle qui lit ce refus."""
    return t("pairing.outdated_here", name=name)


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
    trust: str = sharing.MINE          # ce que l'autre machine est pour celle-ci
    net_id: str | None = None          # par Internet : clé du compagnon de l'autre machine,
    hints: dict | None = None          # de quoi la joindre,
    secret: str | None = None          # et le secret de l'invitation qui le permet
    stage: str = READY
    # Identifiant de la demande montrée à l'utilisateur : sa réponse le porte
    ref: str = field(default_factory=lambda: secrets.token_hex(8))
    known: str | None = None           # déjà appairée ici : son nom enregistré
    replace: bool = False              # l'utilisateur a choisi de remplacer sa clé
    # Chez celle qui répond, entre l'engagement et la révélation
    commit: bytes | None = None
    private: object = None
    pub: bytes | None = None
    nonce: bytes | None = None

    def target(self) -> dict:
        """L'autre machine, telle que client.call la joint pendant l'appairage."""
        return {"node_id": self.node_id, "address": self.address, "port": self.port,
                "name": self.name, "net_id": self.net_id}


def _known(node_id) -> str | None:
    """Le nom d'une machine déjà appairée ici, sinon None."""
    peer = peers.get(node_id)
    return (peer["name"] or node_id) if peer else None


def _bytes(value, size: int) -> bytes:
    """Un champ binaire reçu (base64), de la taille attendue."""
    try:
        raw = envelope.b64d(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise PeerError(t("pairing.unreadable")) from exc
    if len(raw) != size:
        raise PeerError(t("pairing.unreadable"))
    return raw


def _name(payload: dict) -> str:
    return str(payload.get("name") or t("pairing.unnamed"))[:60]


class Pairing:
    def __init__(self, manager):
        self.m = manager
        self._pending = {}
        self._starts = []           # heures des demandes reçues acceptées (MAX_STARTS)
        self._lock = threading.Lock()

    def _purge(self):
        now = time.time()
        for node_id, p in list(self._pending.items()):
            if now - p.created > PENDING_TTL:
                del self._pending[node_id]
        self._starts = [t for t in self._starts if now - t <= PENDING_TTL]

    def get(self, node_id) -> Pending | None:
        with self._lock:
            self._purge()
            return self._pending.get(node_id)

    def key_for(self, node_id) -> bytes | None:
        """La clé de l'appairage en cours, une fois le code calculé (sinon rien à sceller)."""
        p = self.get(node_id)
        return p.key if p and p.stage == READY and p.key else None

    def snapshot(self) -> list:
        with self._lock:
            self._purge()
            return [self._shown(p) for p in self._pending.values() if p.stage == READY]

    @staticmethod
    def _shown(p: Pending) -> dict:
        """Une demande telle que l'interface la montre (jamais la clé)."""
        return {"node_id": p.node_id, "name": p.name, "role": p.role, "code": p.code,
                "ref": p.ref, "address": p.address, "internet": bool(p.net_id),
                "known": p.known, "local_ok": p.local_ok, "remote_ok": p.remote_ok}

    # ------------------------------------------------------- côté initiateur
    def start(self, address, port, node_id: str | None = None) -> Pending:
        """Appairage sur le réseau local avec la machine à cette adresse ; node_id : celle
        que l'utilisateur a choisie dans la liste (une autre à cette adresse est refusée)."""
        info = client.hello(address, port)
        found = info.get("node_id")
        if found == self.m.node_id:
            raise PeerError(t("pairing.this_machine"))
        if not peers.valid_id(found):
            raise PeerError(t("pairing.invalid_id"))
        if node_id is not None and found != node_id:
            raise PairingRefused(t("pairing.not_the_listed_one"))
        return self._initiate(found, str(info.get("name") or t("pairing.unnamed"))[:60],
                              lambda payload: client.pair_start(address, port, payload),
                              address, int(port))

    def start_remote(self, target: dict) -> Pending:
        """Appairage par Internet avec la machine d'une invitation acceptée des deux côtés
        (invitations.target : node_id, name, net_id, hints, secret)."""
        way = {"node_id": target["node_id"], "net_id": target["net_id"]}
        return self._initiate(
            target["node_id"], str(target.get("name") or t("pairing.unnamed"))[:60],
            lambda payload: client.pair_start_via(way, dict(payload,
                                                            invitation=target["secret"])),
            None, 0, net_id=target["net_id"], hints=target["hints"], secret=target["secret"])

    def _initiate(self, node_id, name, send, address, port, **remote) -> Pending:
        """Engagement, puis révélation : la clé de cette machine ne part qu'après celle de
        l'autre. La place est réservée dès le début : aucune demande reçue pour la même
        machine ne s'y glisse pendant l'échange."""
        me = self.m.node_id
        private, public = crypto.new_exchange()
        nonce = crypto.new_nonce()
        pending = Pending(node_id, name, address, port, INITIATOR, b"", "", stage=STARTING,
                          known=_known(node_id), **remote)
        with self._lock:
            self._purge()
            if node_id in self._pending:
                raise PairingRefused(t("pairing.already_running", name=name))
            self._pending[node_id] = pending
        done = False
        try:
            try:
                reply = send({"pairing": PROTOCOL, "step": "commit", "node_id": me,
                              "name": self.m.name(), "port": self.m.p2p_port,
                              "commit": envelope.b64e(crypto.commitment(me, node_id, public,
                                                                        nonce))})
            except PeerError as exc:
                if str(exc) == OLD_INCOMPLETE:
                    raise PairingRefused(_outdated_there(name)) from exc
                raise
            if not isinstance(reply, dict) or reply.get("node_id") != node_id:
                raise PairingRefused(t("pairing.not_the_chosen_one"))
            if reply.get("pairing") != PROTOCOL:
                raise PairingRefused(_outdated_there(name))
            try:
                their_pub = _bytes(reply.get("pub"), 32)
                their_nonce = _bytes(reply.get("nonce"), crypto.PAIR_NONCE)
                key, code = crypto.transcript_secrets(private, (me, public, nonce),
                                                      (node_id, their_pub, their_nonce))
            except (PeerError, ValueError) as exc:
                raise PeerError(t("pairing.reply_unreadable")) from exc
            send({"pairing": PROTOCOL, "step": "reveal", "node_id": me,
                  "pub": envelope.b64e(public), "nonce": envelope.b64e(nonce)})
            with self._lock:
                if self._pending.get(node_id) is not pending:
                    raise PeerError(t("pairing.interrupted"))
                pending.name = str(reply.get("name") or name)[:60]
                pending.key, pending.code, pending.stage = key, code, READY
            done = True
        finally:
            if not done:
                self._drop(pending)
        self.m.changed()
        return pending

    def _drop(self, pending: Pending):
        with self._lock:
            if self._pending.get(pending.node_id) is pending:
                del self._pending[pending.node_id]

    # -------------------------------------------- côté répondant (route réseau)
    def incoming_remote(self, payload: dict, net_key: str) -> dict:
        """Demande d'appairage arrivée par le compagnon : reçue seulement de la clé d'une
        invitation en cours, avec son secret (aux deux étapes)."""
        invitation = invitations.admit(net_key, payload.get("invitation"))
        if invitation is None or invitation["node_id"] != str(payload.get("node_id")):
            raise PeerError(t("pairing.no_invitation"))
        return self.incoming(payload, None, invitation)

    def incoming(self, payload: dict, address: str | None, invitation: dict | None = None) -> dict:
        if not isinstance(payload, dict):
            raise PeerError(t("pairing.unreadable"))
        if payload.get("pairing") != PROTOCOL:
            # Version sans engagement (ou d'une autre génération) : refus clair, jamais
            # l'ancien échange
            newer = isinstance(payload.get("pairing"), int) and payload["pairing"] > PROTOCOL
            raise PairingRefused(_outdated_there(self.m.name()) if newer
                                 else _outdated_here(self.m.name()))
        if payload.get("step") not in ("commit", "reveal"):
            raise PeerError(t("pairing.unreadable"))
        node_id = str(payload.get("node_id") or "")
        if not peers.valid_id(node_id):
            raise PeerError(t("pairing.id_unreadable"))
        if node_id == self.m.node_id:
            raise PeerError(t("pairing.with_itself"))
        if payload["step"] == "commit":
            return self._committed(node_id, payload, address, invitation)
        return self._revealed(node_id, payload, address, invitation)

    def _committed(self, node_id, payload, address, invitation) -> dict:
        """Étape 1 chez celle qui répond : l'engagement est gardé, sa clé et son nonce
        renvoyés. Rien n'est encore montré à l'utilisateur."""
        commit = _bytes(payload.get("commit"), crypto.COMMIT)
        try:
            port = int(payload.get("port"))
        except (TypeError, ValueError) as exc:
            raise PeerError(t("pairing.unreadable")) from exc
        if not 0 < port < 65536:
            raise PeerError(t("pairing.unreadable"))
        private, public = crypto.new_exchange()
        nonce = crypto.new_nonce()
        pending = Pending(node_id, _name(payload), address, port, RESPONDER, b"", "",
                          stage=COMMITTED, known=_known(node_id), commit=commit,
                          private=private, pub=public, nonce=nonce)
        if invitation:
            pending.net_id, pending.hints = invitation["net_id"], invitation["hints"]
            pending.secret = invitation["secret"]
        here = self.m.name()
        with self._lock:
            self._purge()
            if node_id in self._pending:      # jamais remplacée, quelle qu'en soit l'étape
                raise PeerError(t("pairing.already_running_there", name=here))
            waiting = sum(1 for p in self._pending.values() if p.role == RESPONDER)
            if waiting >= MAX_PENDING or len(self._starts) >= MAX_STARTS:
                raise PeerError(t("pairing.too_many", name=here))
            self._pending[node_id] = pending
            self._starts.append(time.time())
        return {"pairing": PROTOCOL, "node_id": self.m.node_id, "name": here,
                "pub": envelope.b64e(public), "nonce": envelope.b64e(nonce)}

    def _revealed(self, node_id, payload, address, invitation) -> dict:
        """Étape 2 chez celle qui répond : la clé révélée doit être celle de l'engagement,
        et venir du même appelant. Alors seulement le code est calculé et montré."""
        their_pub = _bytes(payload.get("pub"), 32)
        their_nonce = _bytes(payload.get("nonce"), crypto.PAIR_NONCE)
        with self._lock:
            self._purge()
            pending = self._pending.get(node_id)
            if (pending is None or pending.role != RESPONDER or pending.stage != COMMITTED
                    or not self._same_caller(pending, address, invitation)):
                raise PeerError(t("pairing.no_commitment"))
            ok = crypto.commitment_ok(pending.commit, node_id, self.m.node_id, their_pub,
                                      their_nonce)
            secrets_ = None
            if ok:
                try:
                    secrets_ = crypto.transcript_secrets(
                        pending.private, (node_id, their_pub, their_nonce),
                        (self.m.node_id, pending.pub, pending.nonce))
                except ValueError:
                    secrets_ = None
            pending.private = None            # la clé éphémère ne sert qu'une fois
            if secrets_ is None:
                # Gardée jusqu'à expiration : une révélation fausse ne libère pas la place
                # (sinon un intermédiaire essaierait sans fin de nouvelles clés)
                pending.stage = SPOILED
                raise PeerError(t("pairing.commitment_mismatch"))
            pending.key, pending.code = secrets_
            pending.stage = READY
            shown = self._shown(pending)
        self.m.publish({"type": "pair_request", **shown})
        self.m.changed()
        return {"pairing": PROTOCOL, "ok": True}

    @staticmethod
    def _same_caller(pending: Pending, address, invitation) -> bool:
        """La révélation vient-elle de celui qui s'est engagé ? Même adresse sur le réseau
        local, même invitation (clé du compagnon et secret) par Internet."""
        if pending.secret or invitation:
            return bool(pending.secret and invitation
                        and invitation.get("net_id") == pending.net_id
                        and secrets.compare_digest(str(invitation.get("secret") or "").encode(),
                                                   pending.secret.encode()))
        # Sous une forme comparable : en double pile, « ::ffff:192.168.1.20 » est
        # « 192.168.1.20 » (relecture de l'appairage, 21/10/2026)
        return address is not None and pending.address is not None \
            and connections.normal_host(address) == connections.normal_host(pending.address)

    # ------------------------------------- réponse de l'utilisateur d'ici
    def answer(self, node_id, accept: bool, trust: str = sharing.MINE, ref: str | None = None,
               replace: bool = False):
        """ref : l'identifiant de la demande que l'utilisateur a vue (l'interface le donne
        toujours) ; replace : il a choisi de remplacer la clé d'une machine déjà appairée."""
        if trust not in sharing.LEVELS:
            raise PeerError(t("sharing.unknown_trust", trust=repr(trust)))
        pending = self.get(node_id)
        if pending is None or pending.stage != READY:
            raise PeerError(t("pairing.expired"))
        if ref is not None and not secrets.compare_digest(str(ref).encode(),
                                                          pending.ref.encode()):
            raise PeerError(t("pairing.changed"))
        if accept:
            refused = group.friends_refused(trust)    # un ami, sur une origine qui les ignore
            if refused:
                raise PairingRefused(refused)
            # Relue maintenant : elle a pu être appairée depuis que la demande est arrivée
            # (présentation de groupe), et accepter remplacerait alors sa clé
            pending.known = _known(node_id) or pending.known
        if accept and pending.known and not replace:
            raise PeerError(t("pairing.already_paired", name=pending.known))
        pending.trust = trust
        pending.replace = bool(replace)
        pending.local_ok = bool(accept)
        try:
            reply = client.call(self.m.node_id, pending.target(), "pair/confirm",
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
        if pending is None or pending.stage != READY:
            raise PeerError(t("pairing.none_running"))
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
            elif pending.local_ok and pending.remote_ok and not pending.replace \
                    and _known(pending.node_id):
                # Appairée entre l'acceptation d'ici et celle de l'autre machine : sa clé
                # n'est pas remplacée sans que l'utilisateur l'ait choisi (relecture finale du
                # 21/10/2026) ; un nouvel appairage le lui demandera
                del self._pending[pending.node_id]
                log.warning("Appairage avec « %s » abandonné : déjà appairée entre-temps, sa "
                            "clé n'est pas remplacée", pending.name)
                outcome = "refused"
            elif pending.local_ok and pending.remote_ok:
                del self._pending[pending.node_id]
                peers.save(pending.node_id, pending.name, pending.key,
                           pending.address, pending.port, pending.trust)
                if pending.net_id:                # par Internet : sa clé, déjà vérifiée
                    peers.set_net(pending.node_id, pending.net_id, pending.hints or {})
                    invitations.use(pending.secret)
                outcome = "paired"
            else:
                outcome = None
        if outcome:
            self.m.publish({"type": "pair_result", "node_id": pending.node_id,
                            "name": pending.name, "outcome": outcome})
        if outcome == "paired":
            group.undecline(pending.node_id)     # appairée par code : plus refusée ici
            self.m.on_paired(pending.node_id)
            # Appairage par groupe : un ami rencontre mes autres machines, une nouvelle
            # machine à moi rencontre mes amis (network/group.py)
            if pending.trust == sharing.FRIEND:
                self.m.present_later(pending.node_id)
            else:
                self.m.present_new_machine(pending.node_id)
        if outcome and pending.net_id:
            self.m.net_changed()              # liste blanche : l'invitation a servi
        self.m.changed()
