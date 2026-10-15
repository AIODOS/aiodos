"""Cryptographie et enveloppe des échanges entre nœuds, migration du schéma."""
import sqlite3
import time

import pytest

from coeur import db, paths
from coeur.network import crypto, envelope
from coeur.network.crypto import SealError


def _pair():
    a_priv, a_pub = crypto.new_exchange()
    b_priv, b_pub = crypto.new_exchange()
    return (crypto.pair_secrets(a_priv, b_pub, "noeud-a", "noeud-b"),
            crypto.pair_secrets(b_priv, a_pub, "noeud-b", "noeud-a"))


def test_both_machines_derive_the_same_key_and_code():
    (key_a, code_a), (key_b, code_b) = _pair()
    assert key_a == key_b and len(key_a) == 32
    assert code_a == code_b and len(code_a) == 6 and code_a.isdigit()


def test_an_interceptor_gets_a_different_code():
    a_priv, a_pub = crypto.new_exchange()
    m_priv, m_pub = crypto.new_exchange()          # l'intrus s'interpose
    _, code_a = crypto.pair_secrets(a_priv, m_pub, "noeud-a", "noeud-b")
    b_priv, b_pub = crypto.new_exchange()
    _, code_b = crypto.pair_secrets(b_priv, m_pub, "noeud-b", "noeud-a")
    assert code_a != code_b                        # l'utilisateur voit la différence


def test_sealed_messages_resist_tampering():
    (key, _), _ = _pair()
    box = crypto.seal(key, b"bonjour", b"contexte")
    assert crypto.open_sealed(key, box, b"contexte") == b"bonjour"
    with pytest.raises(SealError):
        crypto.open_sealed(key, box, b"autre contexte")
    altered = box[:-1] + bytes([box[-1] ^ 1])
    with pytest.raises(SealError):
        crypto.open_sealed(key, altered, b"contexte")
    with pytest.raises(SealError):
        crypto.open_sealed(bytes(32), box, b"contexte")


def test_envelope_round_trip_and_replay():
    (key, _), _ = _pair()
    guard = envelope.ReplayGuard()
    env = envelope.wrap(key, "ping", "noeud-a", "noeud-b", {"x": 1})
    assert envelope.unwrap(key, "ping", "noeud-b", env, guard) == {"x": 1}
    with pytest.raises(SealError):                 # même message renvoyé
        envelope.unwrap(key, "ping", "noeud-b", env, guard)
    with pytest.raises(SealError):                 # détourné vers une autre action
        envelope.unwrap(key, "profile", "noeud-b",
                        envelope.wrap(key, "ping", "noeud-a", "noeud-b", {}), guard)
    with pytest.raises(SealError):                 # destiné à un autre nœud
        envelope.unwrap(key, "ping", "noeud-c",
                        envelope.wrap(key, "ping", "noeud-a", "noeud-b", {}), guard)
    stale = envelope.wrap(key, "ping", "noeud-a", "noeud-b", {})
    stale["ts"] = time.time() - 3600
    with pytest.raises(SealError):                 # trop vieux (et horodatage scellé)
        envelope.unwrap(key, "ping", "noeud-b", stale, guard)
    reply = envelope.wrap_reply(key, "ping", "noeud-b", "noeud-a", env["ts"], {"ok": True})
    assert envelope.unwrap_reply(key, "ping", "noeud-b", "noeud-a", env["ts"], reply) == {"ok": True}


def test_v1_database_is_migrated(tmp_path):
    conn = sqlite3.connect(paths.db_path())
    conn.executescript(db.SCHEMA)
    conn.execute("PRAGMA user_version=1")
    conn.close()
    db.close()
    assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION
    columns = {r["name"] for r in db.query("PRAGMA table_info(units)")}
    assert {"local_only", "lease_until"} <= columns
    assert db.query_one("SELECT COUNT(*) AS n FROM peers")["n"] == 0
