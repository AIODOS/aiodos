"""Cryptographie et enveloppe des échanges entre nœuds, migration du schéma."""
import sqlite3
import time

import pytest

from aiodos import db, paths
from aiodos.network import crypto, envelope
from aiodos.network.crypto import SealError


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


def _sealed_text(key, kind, action, sender, recipient, ts, text: bytes) -> str:
    """Un message scellé qui contient ce texte-là, tel qu'une autre machine peut l'envoyer
    (envelope.wrap, lui, n'écrit jamais de nombre non fini)."""
    return envelope.b64e(crypto.seal(key, text, envelope._aad(kind, action, sender, recipient,
                                                              ts)))


@pytest.mark.parametrize("number", [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1E400"])
def test_non_finite_numbers_are_refused_in_every_message(number):
    """Faille 16 : json.loads acceptait NaN et Infinity, et « 1e999 » devient l'infini. Une
    seule de ces valeurs, gardée puis servie à l'interface, faisait tomber toute la réponse
    (Starlette sérialise avec allow_nan=False). Requête, réponse ou morceau de flux : refusés."""
    (key, _), _ = _pair()
    text = b'{"name": "PC", "load": ' + number + b"}"
    ts = time.time()
    env = {"from": "noeud-a", "to": "noeud-b", "ts": ts,
           "box": _sealed_text(key, "req", "ping", "noeud-a", "noeud-b", ts, text)}
    with pytest.raises(SealError, match="non fini"):
        envelope.unwrap(key, "ping", "noeud-b", env, envelope.ReplayGuard())
    reply = {"box": _sealed_text(key, "rep", "ping", "noeud-b", "noeud-a", ts, text)}
    with pytest.raises(SealError, match="non fini"):
        envelope.unwrap_reply(key, "ping", "noeud-b", "noeud-a", ts, reply)
    line = envelope.b64e(crypto.seal(key, text,
                                     envelope._frame_aad("chat", "noeud-b", "noeud-a", ts, 0)))
    with pytest.raises(SealError, match="non fini"):
        envelope.unwrap_frame(key, "chat", "noeud-b", "noeud-a", ts, 0, line)
    with pytest.raises(ValueError):
        envelope.parse_json(text)
    assert envelope.parse_json(b'{"x": 1.5, "y": 1e-400, "z": 10}') == {"x": 1.5, "y": 0.0, "z": 10}


def test_a_machine_never_sends_a_non_finite_number():
    """Pour que ce refus n'arrive jamais entre machines honnêtes, un nombre non fini (un
    capteur illisible…) part en null : le reste du message passe."""
    (key, _), _ = _pair()
    payload = {"power": {"temperature": float("nan"), "level": 50.0},
               "loads": [float("inf"), 1, (float("-inf"),)], "name": "PC"}
    expected = {"power": {"temperature": None, "level": 50.0}, "loads": [None, 1, [None]],
                "name": "PC"}
    env = envelope.wrap(key, "ping", "noeud-a", "noeud-b", payload)
    assert envelope.unwrap(key, "ping", "noeud-b", env, envelope.ReplayGuard()) == expected
    reply = envelope.wrap_reply(key, "ping", "noeud-b", "noeud-a", env["ts"], payload)
    assert envelope.unwrap_reply(key, "ping", "noeud-b", "noeud-a", env["ts"], reply) == expected
    line = envelope.wrap_frame(key, "chat", "noeud-b", "noeud-a", env["ts"], 0, payload)
    assert envelope.unwrap_frame(key, "chat", "noeud-b", "noeud-a", env["ts"], 0, line) == expected


def test_malformed_envelopes_and_replies_are_seal_errors():
    """Une enveloppe ou une réponse mal formée est refusée comme un message altéré (SealError :
    un refus lisible chez p2p_app et client.py), et non par une erreur imprévue (500, ou
    exception que l'appelant n'attend pas)."""
    (key, _), _ = _pair()
    for reply in ({}, {"box": 5}, {"box": "é"}, ["box"], None):
        with pytest.raises(SealError):
            envelope.unwrap_reply(key, "ping", "noeud-b", "noeud-a", 1.0, reply)
    good = envelope.wrap(key, "ping", "noeud-a", "noeud-b", {})
    # Un horodatage NaN passait le contrôle d'écart : toute comparaison avec NaN est fausse
    for bad in (dict(good, ts="nan"), dict(good, ts=float("inf")), dict(good, box=7), "texte"):
        with pytest.raises(SealError):
            envelope.unwrap(key, "ping", "noeud-b", bad, envelope.ReplayGuard())
    ts = time.time()
    reply = {"box": _sealed_text(key, "rep", "ping", "noeud-b", "noeud-a", ts, b"{pas du json")}
    with pytest.raises(SealError, match="illisible"):
        envelope.unwrap_reply(key, "ping", "noeud-b", "noeud-a", ts, reply)


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
