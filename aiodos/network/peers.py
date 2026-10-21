"""Machines appairées (confiance établie) et compte des échanges de calcul.

Le compte (ledger) mesure le temps de calcul donné à chaque machine et reçu
d'elle : c'est la base de la règle de réciprocité (qui prête plus est servi plus vite).
Chaque machine a un niveau de confiance, « mine » (mes machines) ou « friend » (un ami),
et peut être mise de côté après un contrôle échoué (cf. sharing.py).

La clé de chaque paire est gardée protégée sous Windows (node/vault.py, DPAPI). Celle d'un
ami ou d'une machine mise de côté est en plus hors de portée d'une version d'origine 2.0.x
qui reprendrait la main (vault.hold : scellée, sinon marquée) : elle ignore les niveaux de
confiance et traiterait l'ami comme une de mes machines. La clé suit le niveau et la mise de
côté (_kept), et la migration 18 (db.py) l'a fait pour les paires d'avant. Le profil et le
nom reçus d'une machine sont typés et bornés (network/profile.py) à l'écriture comme à la
lecture.
"""
import json
import logging
import re

from aiodos import db
from aiodos.network import profile as schema
from aiodos.node import vault

log = logging.getLogger("aiodos.network")

ONLINE_WINDOW = 45          # vue il y a moins de 45 s = en ligne
MINE = "mine"               # sharing.MINE (sharing importe ce module-ci)


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
    return [dict(r, profile=_stored_profile(r["profile"]),
                 online=bool(r["last_seen"]) and now - r["last_seen"] < ONLINE_WINDOW)
            for r in rows]


def _stored_profile(text):
    """Profil gardé, relu typé : une base écrite avant la correction du 20/10/2026 (NaN,
    objet à la place d'une liste) ne fait tomber ni GET /api/network ni la vue Réseau."""
    if not text:
        return None
    try:
        return schema.clean(json.loads(text))
    except (ValueError, RecursionError):
        return None


def transport(node_id) -> dict | None:
    """Adresse, port et clé Internet d'une machine appairée, sans déchiffrer la clé de la
    paire (appelé pour chaque balise et chaque gros message reçus). None : pas appairée."""
    if not isinstance(node_id, str) or not node_id:
        return None
    row = db.query_one("SELECT address, port, net_id FROM peers WHERE node_id=?", (node_id,))
    return dict(row) if row else None


def addresses() -> list:
    """Adresses gardées des machines appairées sur le réseau local (network/connections.py :
    leurs connexions ne comptent pas parmi celles des appareils inconnus)."""
    return [r["address"] for r in db.query(
        "SELECT DISTINCT address FROM peers WHERE address IS NOT NULL AND address <> ''")]


def get(node_id) -> dict | None:
    """La machine appairée, avec la clé de la paire en clair, sans marque (None si elle est
    illisible ici : base venue d'un autre compte ; il faut alors appairer de nouveau)."""
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


def _kept(key, trust, aside: bool) -> bytes:
    """La clé d'une paire (en clair, ou telle qu'enregistrée) comme on la garde : celle d'un
    ami ou d'une machine mise de côté hors de portée d'une 2.0.x qui reprendrait la main
    (vault.hold) ; celle d'une de mes machines comme avant, sans marque (scellée si DPAPI le
    permet) : hors DPAPI, une 2.0.x relancée la lit encore et sert mes machines."""
    if trust != MINE or aside:
        return vault.hold(key)
    return vault.protect(vault.release(key))


def save(node_id, name, key: bytes, address, port, trust: str = MINE):
    """Machine appairée (de nouveau) : un nouvel appairage redonne sa chance à une machine
    mise de côté, et fixe son niveau de confiance."""
    db.execute(
        "INSERT INTO peers(node_id, name, key, address, port, paired_at, last_seen, trust) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET name=excluded.name, "
        "key=excluded.key, address=excluded.address, port=excluded.port, "
        "paired_at=excluded.paired_at, last_seen=excluded.last_seen, trust=excluded.trust, "
        "set_aside_at=NULL, set_aside_reason=NULL",
        (node_id, name, _kept(key, trust, False), address, port, db.now(), db.now(), trust))


def protect_keys() -> int:
    """Protège les clés enregistrées avant DPAPI (vérifiées avant d'être remplacées), et
    celles marquées faute de DPAPI (vault.hold). → nombre de clés protégées."""
    done = 0
    for row in db.query("SELECT node_id, key FROM peers"):
        stored = bytes(row["key"] or b"")
        if not stored or vault.is_protected(stored):
            continue
        clear = vault.unprotect(stored)        # la clé seule, sans la marque HELD
        sealed = vault.protect(clear)
        if sealed == clear or not vault.verify(sealed, clear):
            continue
        done += db.execute("UPDATE peers SET key=? WHERE node_id=? AND key=?",
                           (sealed, row["node_id"], row["key"])).rowcount
    return done


def remove(node_id):
    db.execute("DELETE FROM peers WHERE node_id=?", (node_id,))


def touch(node_id, address=None, port=None, name=None):
    """Machine vue à l'instant (son adresse a pu changer : DHCP). Le nom vient d'elle : il
    est nettoyé (une ligne, sans chevrons, NAME caractères au plus)."""
    name = schema.name(name) if name is not None else None
    db.execute("UPDATE peers SET last_seen=?, address=COALESCE(?, address), "
               "port=COALESCE(?, port), name=COALESCE(?, name) WHERE node_id=?",
               (db.now(), address, port, name, node_id))


def set_profile(node_id, profile: dict):
    """Profil reçu d'une machine appairée, réduit à ce qui a la forme attendue (l'enveloppe
    prouve qui répond, pas ce qui est répondu) ; jamais de nombre non fini dans la base.
    Autre chose qu'un objet : l'ancien profil reste."""
    cleaned = schema.clean(profile)
    if cleaned is None:
        return
    db.execute("UPDATE peers SET profile=? WHERE node_id=?",
               (json.dumps(cleaned, ensure_ascii=False, allow_nan=False), node_id))


def update_profile(node_id, key: str, value):
    """Change une seule clé du profil enregistré, dans la base même : un profil plus récent,
    reçu entre-temps, n'est pas écrasé. Sans profil (ou s'il n'est pas un objet) : rien."""
    db.execute("UPDATE peers SET profile=json_set(profile, ?, json(?)) WHERE node_id=? "
               "AND json_valid(profile) AND json_type(profile)='object'",
               (f"$.{key}", json.dumps(value, ensure_ascii=False), node_id))


_ID = re.compile(r"[0-9a-f]{32}")


def valid_id(node_id) -> bool:
    """Un identifiant de machine (uuid4 en hexadécimal) : rien d'autre n'est gardé d'une
    autre machine, car il sert à composer des chemins (API locale, interface)."""
    return isinstance(node_id, str) and _ID.fullmatch(node_id) is not None


def trust_of(node_id) -> str | None:
    """Niveau de confiance de cette machine appairée (None : pas appairée)."""
    row = db.query_one("SELECT trust FROM peers WHERE node_id=?", (node_id,))
    return row["trust"] if row else None


def set_trust(node_id, trust: str) -> bool:
    """Change le niveau de confiance d'une machine appairée (False : pas appairée). Sa clé
    suit (_kept) : devenue celle d'un ami, une 2.0.x qui reprendrait la main ne s'en sert
    plus."""
    with db.transaction() as conn:
        row = conn.execute("SELECT key, set_aside_at FROM peers WHERE node_id=?",
                           (node_id,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE peers SET trust=?, key=? WHERE node_id=?",
                     (trust, _kept(row["key"], trust, row["set_aside_at"] is not None), node_id))
    return True


def set_aside(node_id, reason: str | None):
    """Machine mise de côté (reason) ou rétablie (None) : plus rien ne lui est confié. Sa
    clé suit (_kept) : tant qu'elle est mise de côté, une 2.0.x qui reprendrait la main ne
    s'en sert pas ; rétablie, une de mes machines retrouve sa clé comme avant."""
    with db.transaction() as conn:
        row = conn.execute("SELECT key, trust FROM peers WHERE node_id=?", (node_id,)).fetchone()
        if row is None:
            return
        conn.execute("UPDATE peers SET set_aside_at=?, set_aside_reason=?, key=? WHERE node_id=?",
                     (db.now() if reason else None, reason,
                      _kept(row["key"], row["trust"], bool(reason)), node_id))


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
