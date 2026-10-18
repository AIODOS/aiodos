"""Appairage par groupe : un ami joint toutes mes machines (docs/appairage-groupe.md).

Décisions de l'auteur (18/10/2026) : un ami choisi sur une de mes machines est présenté
d'office à mes autres machines ; chez l'ami, l'accord est demandé la première fois, avec
« toujours accepter ce groupe » ; retirer un ami le retire de toutes mes machines (sauf
« seulement celle-ci ») ; une mise de côté vaut pour toutes ; le téléphone est présenté
aussi, mais ne calcule pour un ami qu'en charge et en Wi-Fi (worker.py).

Une machine P présente une de ses machines F (« mine » pour elle) à la machine A d'un ami
(« friend » pour elle), par ses deux paires déjà vérifiées par un humain :
1. P → F « group/introduce » : F (une de mes machines : rien à demander) prépare une clé
   éphémère X25519 et rend sa partie publique ;
2. P → A « group/propose » : A accepte d'office si elle a dit « toujours accepter ce
   groupe » pour P, sinon son utilisateur répond (3 minutes, « group/answer ») ; elle rend
   sa partie publique ;
3. P → F « group/key » : F termine l'échange. F et A tirent la clé de leur paire comme à
   l'appairage (crypto.pair_secrets) ; P ne voit que des clés publiques.
Si une étape échoue, P le dit à celle qui attendait (« group/cancel ») et la réponse de
l'ami attend la fin : aucune ne garde une paire à moitié faite, et une paire déjà connue
n'est jamais remplacée.

Le niveau de confiance ne monte jamais : F prend A pour un ami ; A prend F au niveau
qu'elle donne à P. On ne présente que ses propres machines (pas d'amis d'amis). Par
Internet, P donne à chacune la clé du compagnon de l'autre et de quoi la joindre : chaque
compagnon l'ajoute à sa liste blanche, sans invitation.

De proche en proche : toute machine qui gagne la machine d'un ami (appairée, choisie comme
amie ou présentée) lui présente ses propres machines ; ainsi le téléphone de l'ami
rencontre aussi mes autres machines. Deux présentations de la même paire peuvent alors se
croiser, une de chaque côté : les deux machines de la paire appliquent la même règle
(celle de la machine qui présente au plus petit identifiant continue), donc une seule
aboutit, avec une seule clé.
"""
import json
import logging
import secrets
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from aiodos import db
from aiodos.network import client, crypto, envelope, netinfo, peers, sharing
from aiodos.network.client import PeerError

log = logging.getLogger("aiodos.network")

TTL = 180                  # une présentation non conclue expire au bout de 3 minutes
MARGIN = 30                # … un peu plus tard chez celles qui relaient la réponse
MAX_OFFERS = 5             # présentations en attente de réponse chez l'ami
ANSWER_TIMEOUT = 25        # la réponse de l'ami attend que la machine présentée ait fini


@dataclass
class Waiting:
    """Chez F : une présentation en cours, sa clé éphémère et la machine présentée."""
    presenter: str
    card: dict
    private: object
    created: float = field(default_factory=time.time)


@dataclass
class Offer:
    """Chez A : une présentation qui attend la réponse de l'utilisateur."""
    intro: str
    presenter: str
    card: dict
    pub: bytes
    created: float = field(default_factory=time.time)


def card(peer: dict) -> dict:
    """Ce que la machine présentée doit savoir de l'autre : son nom, où la joindre sur le
    réseau local, et sa clé Internet avec ses indices (s'ils sont connus)."""
    found = {"node_id": peer["node_id"], "name": peer["name"], "address": peer.get("address"),
             "port": peer.get("port")}
    if peer.get("net_id"):
        hints = peer.get("net_addrs")
        try:
            hints = json.loads(hints) if isinstance(hints, str) else (hints or {})
        except ValueError:
            hints = {}
        found["net"] = {"id": peer["net_id"], "relays": hints.get("relays") or [],
                        "addrs": hints.get("addrs") or []}
    return found


def _read_card(raw) -> dict:
    if not isinstance(raw, dict):
        raise PeerError("Présentation illisible.")
    node_id = str(raw.get("node_id") or "")
    if not node_id or len(node_id) > 64:
        raise PeerError("Présentation illisible.")
    port = raw.get("port")
    return {"node_id": node_id, "name": str(raw.get("name") or "Machine sans nom")[:60],
            "address": str(raw["address"])[:64] if raw.get("address") else None,
            "port": int(port) if isinstance(port, int) and 0 < port < 65536 else None,
            "net": netinfo.clean(raw.get("net"))}


def _intro(payload) -> str:
    intro = str((payload or {}).get("intro") or "")
    if not intro or len(intro) > 64:
        raise PeerError("Présentation illisible.")
    return intro


class Group:
    def __init__(self, manager):
        self.m = manager
        self._lock = threading.Lock()
        self._waiting = {}        # chez F : intro -> Waiting
        self._offers = {}         # chez A : intro -> Offer
        self._kept = {}           # chez A : intro -> paire gardée d'office, à défaire si P échoue
        self._sent = {}           # chez P : intro -> {"friend", "machine", "created"}
        self._presenting = defaultdict(threading.Lock)     # une présentation par ami à la fois

    def _purge(self):
        now = time.time()
        for intro, offer in list(self._offers.items()):
            if now - offer.created > TTL:
                del self._offers[intro]
        for intro, waiting in list(self._waiting.items()):
            if now - waiting.created > TTL + MARGIN:
                del self._waiting[intro]
        for table in (self._sent, self._kept):
            for intro, item in list(table.items()):
                if now - item["created"] > TTL + MARGIN:
                    del table[intro]

    def _crossing(self, machine: str, intro: str, presenter: str) -> str | None:
        """Une autre présentation de la même paire est en cours ici (sous self._lock). Les
        deux machines de la paire appliquent la même règle : celle de la machine qui
        présente au plus petit identifiant continue (puis la plus petite présentation).
        → "busy" (celle-ci s'arrête), "replaced" (l'autre est abandonnée) ou None."""
        replaced = None
        for table in (self._waiting, self._offers):
            for other, item in list(table.items()):
                if other == intro or item.card["node_id"] != machine:
                    continue
                if (presenter, intro) > (item.presenter, other):
                    return "busy"
                del table[other]
                replaced = "replaced"
        return replaced

    def offers(self) -> list:
        """Présentations qui attendent la réponse de l'utilisateur (vue Réseau)."""
        with self._lock:
            self._purge()
            items = list(self._offers.values())
        return [{"intro": o.intro, "machine": o.card["name"],
                 "presenter": _name(o.presenter), "created": o.created} for o in items]

    # ------------------------------------------------------------ chez P
    def present(self, friend_id: str, only=None) -> dict:
        """Présente à la machine d'un ami mes autres machines (only : seulement celles-ci).
        → {"introduced", "asked", "known", "unreachable", "refused"} : des noms."""
        friend = peers.get(friend_id)
        if not friend or friend.get("trust") != sharing.FRIEND or friend.get("set_aside_at"):
            raise PeerError("Seule la machine d'un ami se présente à mes autres machines.")
        if not friend.get("key"):
            raise PeerError(f"« {friend['name']} » est à appairer de nouveau.")
        report = {"introduced": [], "asked": [], "known": [], "unreachable": [], "refused": []}
        with self._presenting[friend_id]:
            for listed in peers.list_peers():
                if (listed["trust"] != sharing.MINE or listed["node_id"] == friend_id
                        or (only is not None and listed["node_id"] not in only)):
                    continue
                mine = peers.get(listed["node_id"])
                if not mine or not mine.get("key"):
                    continue
                report[self._present_one(friend, mine)].append(mine["name"])
        if report["introduced"] or report["asked"]:
            log.info("« %s » présentée à mes machines : %s", friend["name"],
                     ", ".join(report["introduced"] + report["asked"]))
        return report

    def _present_one(self, friend: dict, mine: dict) -> str:
        intro = secrets.token_hex(12)
        try:
            ready = client.call(self.m.node_id, mine, "group/introduce",
                                {"intro": intro, "machine": card(friend)}, timeout=8)
        except PeerError:
            return "unreachable"
        if ready.get("known") or ready.get("busy") or not ready.get("pub"):
            return "known"                 # déjà appairées, ou une autre présentation en cours
        with self._lock:
            self._purge()
            self._sent[intro] = {"friend": friend["node_id"], "machine": mine["node_id"],
                                 "created": time.time()}
        try:
            reply = client.call(self.m.node_id, friend, "group/propose",
                                {"intro": intro, "machine": card(mine), "pub": ready["pub"]})
        except PeerError:
            reply = None
        if reply and reply.get("accept") and reply.get("pub"):
            if self._finish(intro, reply["pub"]):
                return "introduced"
            self._cancel(friend, intro)       # l'ami l'avait gardée d'office : il l'oublie
            return "unreachable"
        if reply and reply.get("pending"):
            return "asked"                    # l'utilisateur de l'ami répondra (group/answer)
        with self._lock:
            self._sent.pop(intro, None)
        self._cancel(mine, intro)             # ma machine n'attend plus
        if reply is None:
            return "unreachable"
        return "known" if reply.get("known") or reply.get("busy") else "refused"

    def _finish(self, intro: str, pub: str) -> bool:
        """La partie publique de l'ami arrive : elle va à ma machine, qui termine (avec
        « toujours accepter ce groupe » si je l'ai dit ici pour cet ami)."""
        with self._lock:
            sent = self._sent.pop(intro, None)
        mine = peers.get(sent["machine"]) if sent else None
        if not mine:
            return False
        try:
            client.call(self.m.node_id, mine, "group/key",
                        {"intro": intro, "pub": pub, "group_ok": group_ok(sent["friend"])},
                        timeout=8)
            return True
        except PeerError as exc:
            log.info("Présentation à « %s » inachevée : %s", mine["name"], exc)
            return False

    def _cancel(self, peer: dict | None, intro: str):
        """Prévient une machine qu'une présentation s'arrête (au mieux)."""
        if not peer:
            return
        try:
            client.call(self.m.node_id, peer, "group/cancel", {"intro": intro}, timeout=4)
        except PeerError:
            pass

    def answered(self, sender: str, payload: dict) -> dict:
        """« group/answer » : l'utilisateur de l'ami a répondu à une présentation.
        → {"ok"} : la machine présentée a-t-elle terminé ? Sinon l'ami ne garde rien."""
        intro = _intro(payload)
        with self._lock:
            self._purge()
            sent = self._sent.get(intro)
            if not sent or sent["friend"] != sender:
                raise PeerError("Présentation inconnue ou expirée.")
        done = False
        if payload.get("accept") and payload.get("pub"):
            done = self._finish(intro, payload["pub"])
        else:
            with self._lock:
                self._sent.pop(intro, None)
            self._cancel(peers.get(sent["machine"]), intro)
        self.m.changed()
        return {"ok": done}

    # ------------------------------------------------------------ chez F
    def introduced(self, sender: str, payload: dict) -> dict:
        """« group/introduce » : une de mes machines me présente la machine d'un ami."""
        if peers.trust_of(sender) != sharing.MINE:
            raise PeerError("Seule une de mes machines me présente d'autres machines.")
        intro = _intro(payload)
        found = _read_card(payload.get("machine"))
        if found["node_id"] == self.m.node_id:
            raise PeerError("Présentation illisible.")
        if peers.trust_of(found["node_id"]):
            return {"known": True}
        private, public = crypto.new_exchange()
        with self._lock:
            self._purge()
            crossing = self._crossing(found["node_id"], intro, sender)
            if crossing != "busy":
                self._waiting[intro] = Waiting(sender, found, private)
        if crossing == "busy":
            return {"busy": True}
        if crossing == "replaced":
            self.m.changed()                  # une question affichée ici a disparu
        return {"pub": envelope.b64e(public)}

    def keyed(self, sender: str, payload: dict) -> dict:
        """« group/key » : la partie publique de l'ami, relayée par la machine qui présente."""
        intro = _intro(payload)
        with self._lock:
            self._purge()
            waiting = self._waiting.get(intro)
            if waiting is None or waiting.presenter != sender:
                raise PeerError("Présentation inconnue ou expirée.")
            del self._waiting[intro]
            found = waiting.card
            try:
                pub = envelope.b64d(payload.get("pub"))
            except (TypeError, ValueError) as exc:
                raise PeerError("Présentation illisible.") from exc
            key, _code = crypto.pair_secrets(waiting.private, pub, self.m.node_id,
                                             found["node_id"])
            # Le niveau ne monte jamais : la machine d'un ami reste celle d'un ami
            self._store(found, key, sharing.FRIEND, sender)
            if payload.get("group_ok"):       # « toujours accepter ce groupe », dit chez P
                set_group_ok(found["node_id"], True)
        self._announce(found, sender, sharing.FRIEND)
        return {"ok": True}

    # ------------------------------------------------------------ chez A
    def proposed(self, sender: str, payload: dict) -> dict:
        """« group/propose » : une machine appairée me présente une autre machine de la
        même personne. Acceptée d'office pour un groupe accepté une fois pour toutes."""
        presenter = peers.get(sender)
        if presenter is None:
            raise PeerError("Machine non appairée.")
        if presenter.get("set_aside_at"):
            raise PeerError("Machine mise de côté ici.")
        intro = _intro(payload)
        found = _read_card(payload.get("machine"))
        if found["node_id"] == self.m.node_id:
            raise PeerError("Présentation illisible.")
        if peers.trust_of(found["node_id"]):
            return {"known": True}
        try:
            pub = envelope.b64d(payload.get("pub"))
        except (TypeError, ValueError) as exc:
            raise PeerError("Présentation illisible.") from exc
        offer = Offer(intro, sender, found, pub)
        auto = group_ok(sender)
        with self._lock:
            self._purge()
            crossing = self._crossing(found["node_id"], intro, sender)
            if crossing == "busy":
                return {"busy": True}
            if auto:
                public, trust = self._take(offer)
                self._kept[intro] = {"machine": found["node_id"], "presenter": sender,
                                     "created": time.time()}
            elif len(self._offers) >= MAX_OFFERS:
                raise PeerError("Trop de présentations en attente, réessaie plus tard.")
            else:
                self._offers[intro] = offer
        if auto:
            self._announce(found, sender, trust)
            return {"accept": True, "pub": public}
        self.m.publish({"type": "group_request", "intro": intro, "machine": found["name"],
                        "presenter": _name(sender)})
        self.m.changed()
        return {"pending": True}

    def answer(self, intro: str, accept: bool, always: bool = False):
        """Réponse de l'utilisateur d'ici à une présentation (toujours : les prochaines
        machines du même groupe seront acceptées d'office, ici et sur mes autres machines).
        La paire n'est gardée que si la machine présentée a terminé de son côté."""
        with self._lock:
            self._purge()
            offer = self._offers.pop(intro, None)
        if offer is None:
            raise PeerError("Cette présentation a expiré.")
        presenter = peers.get(offer.presenter)
        if presenter is None:
            self.m.changed()
            raise PeerError("La machine qui présentait n'est plus appairée.")
        if accept and always:
            set_group_ok(offer.presenter, True)
        taken = None
        if accept:
            try:
                with self._lock:
                    taken = self._take(offer)
            except PeerError:
                pass                          # appairée entre-temps : rien de plus à faire
        reply = {"intro": intro, "accept": taken is not None}
        if taken:
            reply["pub"] = taken[0]
        try:
            done = bool(client.call(self.m.node_id, presenter, "group/answer", reply,
                                    timeout=ANSWER_TIMEOUT).get("ok"))
        except PeerError:
            done = False
        machine = offer.card["node_id"]
        if taken and not done:
            self._drop(machine)
            self.m.changed()
            raise PeerError(f"La présentation de « {offer.card['name']} » n'a pas abouti : "
                            "une des machines ne répond plus. Elle pourra être refaite.")
        self.m.changed()
        if not taken:
            return
        if always:
            set_group_ok(machine, True)
            self._background(self.spread, "group/ok", {"node_ids": [offer.presenter, machine]})
            self._background(self._accept_pending)
        self._announce(offer.card, offer.presenter, taken[1])

    def _accept_pending(self):
        """« Toujours accepter ce groupe » : les autres présentations du même groupe déjà
        affichées sont acceptées aussi."""
        with self._lock:
            ready = [o.intro for o in self._offers.values() if group_ok(o.presenter)]
        for intro in ready:
            try:
                self.answer(intro, True)
            except PeerError as exc:
                log.info("Présentation non acceptée : %s", exc)

    def _take(self, offer: Offer) -> tuple:
        """Termine l'échange chez A et garde la paire (sous self._lock), au niveau donné à
        la machine qui présente, jamais plus haut. → (partie publique, niveau)."""
        private, public = crypto.new_exchange()
        key, _code = crypto.pair_secrets(private, offer.pub, self.m.node_id,
                                         offer.card["node_id"])
        trust = peers.trust_of(offer.presenter) or sharing.FRIEND
        self._store(offer.card, key, trust, offer.presenter)
        return envelope.b64e(public), trust

    def cancelled(self, sender: str, payload: dict) -> dict:
        """« group/cancel » : la machine qui présentait s'arrête (refus, machine injoignable,
        autre présentation de la même paire) : rien n'est gardé à moitié ici."""
        intro = _intro(payload)
        with self._lock:
            waiting = self._waiting.get(intro)
            if waiting is not None and waiting.presenter == sender:
                del self._waiting[intro]
            offer = self._offers.get(intro)
            if offer is not None and offer.presenter == sender:
                del self._offers[intro]
            else:
                offer = None
            kept = self._kept.get(intro)
            if kept is not None and kept["presenter"] == sender:
                del self._kept[intro]
            else:
                kept = None
        if kept and introduced_by(kept["machine"]) == sender:
            self._drop(kept["machine"])
        if offer or kept:
            self.m.changed()
        return {"ok": True}

    # ------------------------------------------------------------ des deux côtés
    def _store(self, found: dict, key: bytes, trust: str, presenter: str):
        """Garde la nouvelle paire (sous self._lock) ; une paire déjà connue n'est jamais
        remplacée : ce serait une clé différente de celle de l'autre côté."""
        if peers.trust_of(found["node_id"]):
            raise PeerError("Machine déjà appairée.")
        peers.save(found["node_id"], found["name"], key, found.get("address"),
                   found.get("port") or 0, trust)
        set_introduced_by(found["node_id"], presenter)
        if found.get("net"):
            peers.set_net(found["node_id"], found["net"]["id"],
                          {"relays": found["net"]["relays"], "addrs": found["net"]["addrs"]})
            self.m.net_changed()              # liste blanche du compagnon

    def _drop(self, machine: str):
        """Défait une paire gardée alors que la présentation n'a pas abouti."""
        peers.remove(machine)
        self.m.net_changed(machine)

    def _announce(self, found: dict, presenter: str, trust: str):
        self.m.publish({"type": "pair_result", "node_id": found["node_id"],
                        "name": found["name"], "outcome": "paired", "via": _name(presenter)})
        self.m.on_paired(found["node_id"])
        self.m.changed()
        if trust == sharing.FRIEND:
            # De proche en proche : la machine d'un ami rencontre aussi mes autres machines
            self.m.present_later(found["node_id"])

    @staticmethod
    def _background(target, *args):
        threading.Thread(target=target, args=args, daemon=True,
                         name="aiodos-presentation").start()

    # ------------------------------------------------ prévenir toutes mes machines
    def spread(self, action: str, payload: dict) -> list:
        """Prévient mes autres machines (« group/remove », « group/set_aside »,
        « group/ok »). → les noms de celles qui n'ont pas pu l'être."""
        missed = []
        for listed in peers.list_peers():
            if listed["trust"] != sharing.MINE:
                continue
            mine = peers.get(listed["node_id"])
            if not mine or not mine.get("key"):
                continue
            try:
                client.call(self.m.node_id, mine, action, payload, timeout=6)
            except PeerError:
                missed.append(mine["name"])
        return missed


def _friend_of(sender: str, payload: dict) -> str:
    """La machine d'ami visée par un retrait ou une mise de côté venus d'une de mes
    machines ; jamais une de mes machines."""
    if peers.trust_of(sender) != sharing.MINE:
        raise PeerError("Seule une de mes machines le demande.")
    node_id = str((payload or {}).get("node_id") or "")
    if peers.trust_of(node_id) != sharing.FRIEND:
        return ""
    return node_id


def removed(manager, sender, payload) -> dict:
    """« group/remove » : un ami retiré d'une de mes machines l'est aussi d'ici."""
    node_id = _friend_of(sender, payload)
    if node_id:
        manager.remove_now(node_id)
    return {"ok": True}


def set_aside(manager, sender, payload) -> dict:
    """« group/set_aside » : un résultat faux ne dépend pas de la machine à qui il a été
    rendu : mise de côté ici aussi."""
    node_id = _friend_of(sender, payload)
    if node_id:
        peers.set_aside(node_id, str(payload.get("reason") or "contrôle échoué")[:300])
        manager.changed()
    return {"ok": True}


def group_accepted(manager, sender, payload) -> dict:
    """« group/ok » : « toujours accepter ce groupe », dit sur une autre de mes machines,
    vaut ici pour les machines d'ami déjà connues."""
    if peers.trust_of(sender) != sharing.MINE:
        raise PeerError("Seule une de mes machines le demande.")
    ids = (payload or {}).get("node_ids")
    for node_id in (ids if isinstance(ids, list) else [])[:8]:
        if peers.trust_of(str(node_id)) == sharing.FRIEND:
            set_group_ok(str(node_id), True)
    manager.group._background(manager.group._accept_pending)   # questions déjà affichées
    return {"ok": True}


def _name(node_id: str) -> str:
    row = db.query_one("SELECT name FROM peers WHERE node_id=?", (node_id,))
    return row["name"] if row else node_id[:8]


def group_ok(node_id: str) -> bool:
    row = db.query_one("SELECT group_ok FROM peers WHERE node_id=?", (node_id,))
    return bool(row and row["group_ok"])


def set_group_ok(node_id: str, value: bool):
    db.execute("UPDATE peers SET group_ok=? WHERE node_id=?", (1 if value else 0, node_id))


def introduced_by(node_id: str) -> str | None:
    row = db.query_one("SELECT introduced_by FROM peers WHERE node_id=?", (node_id,))
    return row["introduced_by"] if row else None


def set_introduced_by(node_id: str, presenter: str | None):
    db.execute("UPDATE peers SET introduced_by=? WHERE node_id=?", (presenter, node_id))


HANDLERS = {
    "group/introduce": lambda m, sender, payload: m.group.introduced(sender, payload),
    "group/key": lambda m, sender, payload: m.group.keyed(sender, payload),
    "group/propose": lambda m, sender, payload: m.group.proposed(sender, payload),
    "group/answer": lambda m, sender, payload: m.group.answered(sender, payload),
    "group/cancel": lambda m, sender, payload: m.group.cancelled(sender, payload),
    "group/remove": removed,
    "group/set_aside": set_aside,
    "group/ok": group_accepted,
}
