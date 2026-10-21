"""Les secrets à l'abri sur le disque (node/vault.py) : clés des paires et clés d'API
chiffrées par DPAPI sous Windows, lues en clair par AIODOS, et celles d'avant protégées
ensuite sans rien casser."""
import json
import sys

import pytest

from aiodos import db, settings
from aiodos.network import peers
from aiodos.node import vault

windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI : Windows seulement")
KEY = bytes(range(32))


@windows
def test_a_secret_is_protected_and_read_back():
    sealed = vault.protect(KEY)
    assert sealed.startswith(vault.MARK) and KEY not in sealed
    vault._opened.clear()                       # relu par DPAPI, pas de mémoire
    assert vault.unprotect(sealed) == KEY and vault.verify(sealed, KEY)
    assert vault.protect(sealed) == sealed      # déjà protégé : inchangé
    assert vault.unprotect(KEY) == KEY          # sans marque : tel quel (base d'avant)
    with pytest.raises(vault.Unreadable):
        vault.unprotect(vault.MARK + b"abime" * 10)


@windows
def test_pair_keys_are_stored_protected_and_read_in_clear():
    peers.save("machine-b", "PC fixe", KEY, "127.0.0.1", 1)
    stored = db.query_one("SELECT key FROM peers WHERE node_id='machine-b'")["key"]
    assert bytes(stored).startswith(vault.MARK) and KEY not in bytes(stored)
    assert peers.get("machine-b")["key"] == KEY


@windows
def test_keys_saved_before_are_protected_later_without_breaking_anything():
    db.execute("INSERT INTO peers(node_id, name, key, address, port, paired_at) "
               "VALUES('ancienne', 'Ancienne', ?, '127.0.0.1', 1, 0)", (KEY,))
    assert peers.get("ancienne")["key"] == KEY            # lue telle quelle avant
    assert peers.protect_keys() == 1 and peers.protect_keys() == 0
    stored = db.query_one("SELECT key FROM peers WHERE node_id='ancienne'")["key"]
    assert vault.is_protected(stored)
    vault._opened.clear()
    assert peers.get("ancienne")["key"] == KEY


def test_an_unreadable_pair_key_asks_for_a_new_pairing(monkeypatch):
    db.execute("INSERT INTO peers(node_id, name, key, address, port, paired_at) "
               "VALUES('venue-dailleurs', 'Venue d''ailleurs', ?, '127.0.0.1', 1, 0)",
               (vault.MARK + b"autre compte" * 4,))
    monkeypatch.setattr(vault, "available", lambda: False)   # comme sur une autre machine
    assert peers.get("venue-dailleurs")["key"] is None
    from aiodos.network import client
    with pytest.raises(client.PeerError, match="appaire-la de nouveau"):
        client.call("moi", peers.get("venue-dailleurs"), "ping", {})


@windows
def test_api_keys_are_protected_and_still_masked(monkeypatch):
    """Plus aucune clé d'API depuis le retrait du cloud : la mécanique reste, essayée ici avec
    deux clés d'essai."""
    for key in ("essai_key", "autre_key"):
        monkeypatch.setitem(settings.DEFAULTS, key, "")
    monkeypatch.setattr(settings, "SECRET_KEYS", {"essai_key", "autre_key"})
    settings.set_value("essai_key", "sk-test-12345678")
    raw = json.loads(db.query_one("SELECT value FROM settings WHERE key='essai_key'")["value"])
    assert raw.startswith(vault.TEXT_MARK) and "12345678" not in raw
    assert settings.get("essai_key") == "sk-test-12345678"
    view = settings.public_view()
    assert view["essai_key"] == "" and view["essai_key_hint"] == "••••5678"
    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('autre_key', ?)",
               (json.dumps("sk-ancienne-0000"),))
    assert settings.protect_secrets() == 1 and settings.get("autre_key") == "sk-ancienne-0000"


def test_the_cloud_settings_are_erased_by_the_update():
    """Migration 23 : les réglages du cloud, clés d'API comprises, quittent la base ; un modèle
    par défaut du cloud revient à celui d'AIODOS ; le reste ne bouge pas."""
    db.execute("INSERT INTO settings(key, value) VALUES('mistral_key', ?), ('cloud_confirm', "
               "'true'), ('default_model', ?), ('web_results', '5')",
               (json.dumps("sk-ancienne-0000"), json.dumps("cloud:mistral")))
    path = db.connect().execute("PRAGMA database_list").fetchone()[2]
    db.execute("PRAGMA user_version=22")
    db.close()
    db._initialized.discard(path)
    keys = {r["key"] for r in db.query("SELECT key FROM settings")}
    assert keys.isdisjoint({"mistral_key", "cloud_confirm", "default_model"}) and "web_results" in keys
    assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION
