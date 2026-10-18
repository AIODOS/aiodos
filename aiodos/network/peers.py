"""Machines appairées (confiance établie) et compte des échanges de calcul.

Le compte (ledger) mesure le temps de calcul donné à chaque machine et reçu
d'elle : c'est la base de la règle de réciprocité (qui prête plus est servi plus vite).
Chaque machine a un niveau de confiance, « mine » (mes machines) ou « friend » (un ami),
et peut être mise de côté après un contrôle échoué (cf. sharing.py).

La clé de chaque paire est gardée protégée sous Windows (node/vault.py, DPAPI).
"""
import json
import logging

from aiodos import db
from aiodos.node import vault

log = logging.getLogger("aiodos.network")

ONLINE_WINDOW = 45          # vue il y a moins de 45 s = en ligne


def list_peers() -> list:
    rows = db.query("""
        SELECT p.node_id, p.name, p.address, p.port, p.profile, p.paired_at, p.last_seen,
               p.net_id, p.trust, p.set_aside_at, p.set_aside_reason, p.introduced_by,
               p.group_ok,
               COALESCE(l.given_s, 0) AS given_s, COALESCE(l.received_s, 0) AS received_s,
               COALESCE(l.given_units, 0) AS given_units,
               COALESCE(l.received_units, 0) AS received_units,
               COALESCE(l.given_chats, 0) AS given_chats,
               COALESCE(l.received_chats, 0) AS received_chats,
               COALESCE(l.checked_units, 0) AS checked_units,
               COALESCE(l.refused_units, 0) AS refused_units,
               COALESCE(l.failed_units, 0) AS failed_units
        FROM peers p LEFT JOIN ledger l ON l.node_id = p.node_id
        ORDER BY p.name COLLATE NOCASE""")
    now = db.now()
    return [dict(r, profile=json.loads(r["profile"] or "null"),
                 online=bool(r["last_seen"]) and now - r["last_seen"] < ONLINE_WINDOW)
            for r in rows]


def get(node_id) -> dict | None:
    """La machine appairée, avec la clé de la paire en clair (None si elle est illisible
    ici : base venue d'un autre compte ; il faut alors appairer de nouveau)."""
    row = db.query_one("SELECT * FROM peers WHERE node_id=?", (node_id,))
    if row is None:
        return None
    peer = dict(row)
    try:
        peer["key"] = vault.unprotect(peer["key"])
    except vault.Unreadable as exc:
        log.warning("Clé de la paire avec « %s » illisible ici (%s) : à appairer de nouveau",
                    peer["name"], exc)
        peer["key"] = None
    return peer


def save(node_id, name, key: bytes, address, port, trust: str = "mine"):
    """Machine appairée (de nouveau) : un nouvel appairage redonne sa chance à une machine
    mise de côté, et fixe son niveau de confiance."""
    db.execute(
        "INSERT INTO peers(node_id, name, key, address, port, paired_at, last_seen, trust) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET name=excluded.name, "
        "key=excluded.key, address=excluded.address, port=excluded.port, "
        "paired_at=excluded.paired_at, last_seen=excluded.last_seen, trust=excluded.trust, "
        "set_aside_at=NULL, set_aside_reason=NULL",
        (node_id, name, vault.protect(key), address, port, db.now(), db.now(), trust))


def protect_keys() -> int:
    """Protège les clés enregistrées avant DPAPI (vérifiées avant d'être remplacées).
    → nombre de clés protégées."""
    done = 0
    for row in db.query("SELECT node_id, key FROM peers"):
        clear = bytes(row["key"] or b"")
        if not clear or vault.is_protected(clear):
            continue
        sealed = vault.protect(clear)
        if sealed == clear or not vault.verify(sealed, clear):
            continue
        done += db.execute("UPDATE peers SET key=? WHERE node_id=? AND key=?",
                           (sealed, row["node_id"], row["key"])).rowcount
    return done


def remove(node_id):
    db.execute("DELETE FROM peers WHERE node_id=?", (node_id,))


def touch(node_id, address=None, port=None, name=None):
    """Machine vue à l'instant (son adresse a pu changer : DHCP)."""
    db.execute("UPDATE peers SET last_seen=?, address=COALESCE(?, address), "
               "port=COALESCE(?, port), name=COALESCE(?, name) WHERE node_id=?",
               (db.now(), address, port, name, node_id))


def set_profile(node_id, profile: dict):
    db.execute("UPDATE peers SET profile=? WHERE node_id=?",
               (json.dumps(profile, ensure_ascii=False), node_id))


def update_profile(node_id, key: str, value):
    """Change une seule clé du profil enregistré, dans la base même : un profil plus récent,
    reçu entre-temps, n'est pas écrasé. Sans profil (ou s'il n'est pas un objet) : rien."""
    db.execute("UPDATE peers SET profile=json_set(profile, ?, json(?)) WHERE node_id=? "
               "AND json_valid(profile) AND json_type(profile)='object'",
               (f"$.{key}", json.dumps(value, ensure_ascii=False), node_id))


def trust_of(node_id) -> str | None:
    """Niveau de confiance de cette machine appairée (None : pas appairée)."""
    row = db.query_one("SELECT trust FROM peers WHERE node_id=?", (node_id,))
    return row["trust"] if row else None


def set_trust(node_id, trust: str) -> bool:
    return db.execute("UPDATE peers SET trust=? WHERE node_id=?", (trust, node_id)).rowcount > 0


def set_aside(node_id, reason: str | None):
    """Machine mise de côté (reason) ou rétablie (None) : plus rien ne lui est confié."""
    db.execute("UPDATE peers SET set_aside_at=?, set_aside_reason=? WHERE node_id=?",
               (db.now() if reason else None, reason, node_id))


def set_net(node_id, net_id: str, hints: dict) -> bool:
    """Clé Internet (compagnon aiodos-net) d'une machine appairée, et les indices pour la
    joindre. Vrai si elles ont changé (la liste blanche du compagnon est à revoir)."""
    text = json.dumps(hints, sort_keys=True)
    with db.transaction() as conn:
        row = conn.execute("SELECT net_id, net_addrs FROM peers WHERE node_id=?",
                           (node_id,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE peers SET net_id=?, net_addrs=?, net_seen=? WHERE node_id=?",
                     (net_id, text, db.now(), node_id))
    return (row["net_id"], row["net_addrs"]) != (net_id, text)


def net_list() -> list:
    """Machines appairées dont on connaît la clé Internet."""
    return [dict(r) for r in db.query(
        "SELECT node_id, net_id, net_addrs FROM peers WHERE net_id IS NOT NULL ORDER BY node_id")]


def account(node_id, given_s=0.0, received_s=0.0, given_units=0, received_units=0,
            given_chats=0, received_chats=0, checked_units=0, refused_units=0, failed_units=0):
    """Ce qui a été échangé avec cette machine : temps de calcul et unités, et conversations
    servies par un modèle prêté (given : par cette machine-ci, pour elle). Et sa fiabilité :
    ses résultats contrôlés et conformes (checked), refusés (refused), les unités qu'elle n'a
    pas pu calculer pour moi (failed)."""
    db.execute(
        "INSERT INTO ledger(node_id, given_s, received_s, given_units, received_units, "
        "given_chats, received_chats, checked_units, refused_units, failed_units) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE "
        "SET given_s = given_s + excluded.given_s, received_s = received_s + excluded.received_s, "
        "given_units = given_units + excluded.given_units, "
        "received_units = received_units + excluded.received_units, "
        "given_chats = given_chats + excluded.given_chats, "
        "received_chats = received_chats + excluded.received_chats, "
        "checked_units = checked_units + excluded.checked_units, "
        "refused_units = refused_units + excluded.refused_units, "
        "failed_units = failed_units + excluded.failed_units",
        (node_id, given_s, received_s, given_units, received_units, given_chats, received_chats,
         checked_units, refused_units, failed_units))
