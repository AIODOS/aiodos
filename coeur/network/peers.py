"""Machines appairées (confiance établie) et compte des échanges de calcul.

Le compte (ledger) mesure le temps de calcul donné à chaque machine et reçu
d'elle : c'est la base de la règle de réciprocité (qui prête plus est servi plus vite).
"""
import json

from coeur import db

ONLINE_WINDOW = 45          # vue il y a moins de 45 s = en ligne


def list_peers() -> list:
    rows = db.query("""
        SELECT p.node_id, p.name, p.address, p.port, p.profile, p.paired_at, p.last_seen,
               COALESCE(l.given_s, 0) AS given_s, COALESCE(l.received_s, 0) AS received_s,
               COALESCE(l.given_units, 0) AS given_units,
               COALESCE(l.received_units, 0) AS received_units
        FROM peers p LEFT JOIN ledger l ON l.node_id = p.node_id
        ORDER BY p.name COLLATE NOCASE""")
    now = db.now()
    return [dict(r, profile=json.loads(r["profile"] or "null"),
                 online=bool(r["last_seen"]) and now - r["last_seen"] < ONLINE_WINDOW)
            for r in rows]


def get(node_id) -> dict | None:
    row = db.query_one("SELECT * FROM peers WHERE node_id=?", (node_id,))
    return dict(row) if row else None


def save(node_id, name, key: bytes, address, port):
    db.execute(
        "INSERT INTO peers(node_id, name, key, address, port, paired_at, last_seen) "
        "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET name=excluded.name, "
        "key=excluded.key, address=excluded.address, port=excluded.port, "
        "paired_at=excluded.paired_at, last_seen=excluded.last_seen",
        (node_id, name, key, address, port, db.now(), db.now()))


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


def account(node_id, given_s=0.0, received_s=0.0, given_units=0, received_units=0):
    db.execute(
        "INSERT INTO ledger(node_id, given_s, received_s, given_units, received_units) "
        "VALUES(?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
        "given_s = given_s + excluded.given_s, received_s = received_s + excluded.received_s, "
        "given_units = given_units + excluded.given_units, "
        "received_units = received_units + excluded.received_units",
        (node_id, given_s, received_s, given_units, received_units))
