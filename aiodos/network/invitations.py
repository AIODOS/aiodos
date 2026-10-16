"""Invitation à distance (étape 2.4 de docs/phase2-internet.md) : appairer la machine
d'un ami qui n'est pas sur le même réseau, par les compagnons Internet.

Un compagnon n'accepte que les clés de sa liste blanche, dans les deux sens. Les deux
machines se donnent donc d'abord leurs clés, par un canal que leurs utilisateurs
connaissent déjà (un message, un courriel) :

1. l'invitant crée une invitation : un code qui porte sa clé Internet, de quoi le
   joindre (relais, adresses), et un secret à usage unique, valable 24 heures ;
2. l'ami colle ce code dans son AIODOS, qui lui donne une réponse à renvoyer : sa
   propre clé, et le même secret. Son compagnon admet désormais l'invitant ;
3. l'invitant colle la réponse : son compagnon admet l'ami, et l'appairage commence par
   les compagnons. Il se conclut comme sur le réseau local : le même code à 6 chiffres
   des deux côtés, confirmé sur chaque machine, qui dit ce qu'est l'autre pour elle.

Une demande d'appairage arrivée par Internet n'est reçue que si elle vient de la clé
inscrite dans une invitation, avec son secret (admit). Une invitation qui a servi, ou
périmée, ne vaut plus rien, et sa clé quitte la liste blanche.
"""
import base64
import binascii
import json
import re
import secrets
import time

from aiodos import db
from aiodos.network import netinfo

PREFIX = "AIODOS1-"
LIFETIME = 24 * 3600
KEEP_ENDED = 7 * 24 * 3600         # invitations finies gardées une semaine, puis effacées
SENT = "sent"                      # j'ai invité
RECEIVED = "received"              # on m'a invité
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class InvitationError(ValueError):
    """Invitation refusée ou impossible (message lisible)."""


# ------------------------------------------------------------------- codes
def _encode(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(code) -> dict:
    """Le contenu d'un code d'invitation ou de réponse, vérifié."""
    text = re.sub(r"\s+", "", str(code or ""))          # collé avec des retours à la ligne
    if not text.startswith(PREFIX):
        raise InvitationError("Ce n'est pas un code d'invitation AIODOS.")
    body = text[len(PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvitationError("Code illisible : a-t-il été copié en entier ?") from exc
    if not isinstance(data, dict) or data.get("v") != 1 or data.get("k") not in ("invite", "reply"):
        raise InvitationError("Code d'une autre version d'AIODOS : mettez-vous à jour tous les deux.")
    net = netinfo.clean({"id": data.get("net"), "relays": data.get("r"), "addrs": data.get("a")})
    secret, node_id = str(data.get("s") or ""), str(data.get("id") or "")
    if net is None or not _HEX32.match(secret) or not _HEX32.match(node_id):
        raise InvitationError("Code incomplet : a-t-il été copié en entier ?")
    try:
        expires = float(data.get("x") or 0)
    except (TypeError, ValueError):
        expires = 0.0
    return {"kind": data["k"], "node_id": node_id, "name": str(data.get("n") or "Machine sans nom")[:60],
            "net": net, "secret": secret, "expires": expires}


def _mine() -> dict:
    """Clé Internet de cette machine et de quoi la joindre (compagnon en marche)."""
    net = netinfo.clean(netinfo.mine())
    if net is None:
        raise InvitationError("Active d'abord la connexion par Internet (carte « Internet ») : "
                              "l'invitation passe par le compagnon aiodos-net.")
    return net


def _code(kind: str, manager, net: dict, secret: str, expires: float) -> str:
    return _encode({"v": 1, "k": kind, "id": manager.node_id, "n": manager.name(),
                    "net": net["id"], "r": net["relays"], "a": net["addrs"], "s": secret,
                    "x": int(expires)})


def _hints(net: dict) -> str:
    return json.dumps({"relays": net["relays"], "addrs": net["addrs"]}, sort_keys=True)


# --------------------------------------------------------------- les trois temps
def create(manager) -> dict:
    """1. Nouvelle invitation : {id, code à envoyer à l'ami, expires}."""
    net = _mine()
    secret, now = secrets.token_hex(16), time.time()
    invitation_id = db.execute(
        "INSERT INTO invitations(secret, role, created, expires) VALUES(?, ?, ?, ?)",
        (secret, SENT, now, now + LIFETIME)).lastrowid
    return {"id": invitation_id, "code": _code("invite", manager, net, secret, now + LIFETIME),
            "expires": now + LIFETIME}


def accept(manager, code) -> dict:
    """2. L'ami colle l'invitation : l'invitant est admis par son compagnon, et il reçoit
    la réponse à renvoyer. {reply, name : celui de l'invitant}."""
    found = decode(code)
    if found["kind"] != "invite":
        raise InvitationError("C'est une réponse : elle se colle sur la machine qui a invité.")
    if found["node_id"] == manager.node_id:
        raise InvitationError("Cette invitation vient de cette machine-ci.")
    if found["expires"] < time.time():
        raise InvitationError("Invitation périmée : demandes-en une nouvelle.")
    net = _mine()
    row = db.query_one("SELECT role, used FROM invitations WHERE secret=?", (found["secret"],))
    if row is not None and (row["role"] != RECEIVED or row["used"]):
        raise InvitationError("Cette invitation a déjà servi.")
    if row is None:
        db.execute("INSERT INTO invitations(secret, role, created, expires, node_id, name, "
                   "net_id, net_addrs) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                   (found["secret"], RECEIVED, time.time(), found["expires"], found["node_id"],
                    found["name"], found["net"]["id"], _hints(found["net"])))
    return {"reply": _code("reply", manager, net, found["secret"], found["expires"]),
            "name": found["name"]}


def answered(manager, invitation_id: int, code) -> dict:
    """3. L'invitant colle la réponse : l'ami est admis par son compagnon. La machine à
    appairer (cf. target, Pairing.start_remote)."""
    found = decode(code)
    if found["kind"] != "reply":
        raise InvitationError("C'est une invitation : elle se colle sur la machine de l'ami.")
    row = db.query_one("SELECT * FROM invitations WHERE id=? AND role=?", (invitation_id, SENT))
    if row is None or row["secret"] != found["secret"]:
        raise InvitationError("Cette réponse ne correspond pas à cette invitation.")
    if row["used"]:
        raise InvitationError("Cette invitation a déjà servi.")
    if row["expires"] < time.time():
        raise InvitationError("Invitation périmée : crées-en une nouvelle.")
    if found["node_id"] == manager.node_id:
        raise InvitationError("Cette réponse vient de cette machine-ci.")
    db.execute("UPDATE invitations SET node_id=?, name=?, net_id=?, net_addrs=? WHERE id=?",
               (found["node_id"], found["name"], found["net"]["id"], _hints(found["net"]),
                invitation_id))
    return target(db.query_one("SELECT * FROM invitations WHERE id=?", (invitation_id,)))


def target(row) -> dict:
    """L'autre machine d'une invitation, telle que l'appairage la joint."""
    try:
        hints = json.loads(row["net_addrs"] or "{}")
    except ValueError:
        hints = {}
    return {"invitation_id": row["id"], "node_id": row["node_id"], "name": row["name"],
            "net_id": row["net_id"], "hints": hints, "secret": row["secret"]}


def admit(net_key: str, secret: str) -> dict | None:
    """Demande d'appairage arrivée par Internet : l'invitation qui l'autorise (clé de
    l'appelant inscrite, bon secret, ni périmée ni utilisée), sinon None."""
    row = db.query_one("SELECT * FROM invitations WHERE secret=? AND net_id=? AND used IS NULL "
                       "AND expires>?", (str(secret or ""), str(net_key or ""), time.time()))
    return target(row) if row else None


def use(secret: str):
    """Appairage conclu : l'invitation ne sert plus."""
    db.execute("UPDATE invitations SET used=? WHERE secret=?", (time.time(), secret))


def cancel(invitation_id: int) -> bool:
    return db.execute("DELETE FROM invitations WHERE id=? AND used IS NULL",
                      (invitation_id,)).rowcount > 0


# ------------------------------------------------------------ liste blanche
def allowlist_rows() -> list:
    """Les clés à admettre dans la liste blanche du compagnon le temps des invitations en
    cours (même forme que peers.net_list) ; les finies et périmées n'y sont plus."""
    db.execute("DELETE FROM invitations WHERE expires<?", (time.time() - KEEP_ENDED,))
    return [dict(r) for r in db.query(
        "SELECT node_id, net_id, net_addrs FROM invitations WHERE net_id IS NOT NULL "
        "AND used IS NULL AND expires>?", (time.time(),))]


def snapshot() -> list:
    """Invitations en cours, pour l'interface (jamais leur secret)."""
    return [{"id": r["id"], "role": r["role"], "created": r["created"], "expires": r["expires"],
             "name": r["name"], "answered": bool(r["net_id"])}
            for r in db.query("SELECT * FROM invitations WHERE used IS NULL AND expires>? "
                              "ORDER BY created DESC", (time.time(),))]
