# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Les montages des essais. Aucun essai ne voit le vrai AIODOS : pas de fichier de
découverte, pas d'installation, et l'adresse par défaut pointe sur un port fermé."""

import pytest

import aiodos_client.client as client_module
from aiodos_client import Aiodos, Keystore
from aiodos_client.fake import FakeAiodos
from support import APP, closed_port


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("AIODOS_URL", "AIODOS_KEY", "AIODOS_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AIODOS_DISCOVERY_FILE", str(tmp_path / "pas-de-decouverte.json"))
    monkeypatch.setenv("AIODOS_HOME", str(tmp_path / "pas-installe"))
    monkeypatch.setattr(client_module, "DEFAULT_URL", f"http://127.0.0.1:{closed_port()}")


@pytest.fixture
def fake(monkeypatch):
    with FakeAiodos(interval=0.05) as server:          # relève toutes les 0,05 s (AIODOS : 2 s)
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", server.discovery_file)
        yield server


@pytest.fixture
def make_client():
    def make(**options):
        values = dict(APP)
        values.update(options)
        values.setdefault("keystore", Keystore.memory())
        values.setdefault("timeout", 5)
        values.setdefault("load_timeout", 5)
        return Aiodos(**values)
    return make


@pytest.fixture
def ready(fake, make_client):
    """Un client déjà autorisé : (client, clé)."""
    key = fake.add_key()
    return make_client(keystore=Keystore.memory(key)), key
