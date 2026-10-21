# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""status() dans tous les états du tableau (docs/api-applications.md § 6), sans jamais lever ;
wait_ready()."""

import os
import subprocess
import sys
import threading

import pytest

from aiodos_client import Keystore, _proof, texts
from aiodos_client import status as status_module
from aiodos_client.fake import FakeAiodos
from support import interaction, load_contract, wait_until


def _dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _discovery_file(fake, name, **changes):
    data = load_contract("discovery.json")["files"][name]
    data["pid"] = os.getpid()
    data["gateway"]["url"] = fake.url
    data.update(changes)
    fake.write_discovery_file(data)
    return data


def test_not_installed(make_client):
    status = make_client().status()
    assert status.state == "not_installed"
    assert status.message == "AIODOS n'est pas installé sur ce PC."
    assert make_client(lang="en").status().message == "AIODOS is not installed on this PC."


def test_not_running_when_installed_and_nothing_listens(make_client, monkeypatch, tmp_path):
    root = tmp_path / "AIODOS"
    (root / "runtime").mkdir(parents=True)
    (root / "runtime" / "python.exe").write_bytes(b"")
    monkeypatch.setenv("AIODOS_HOME", str(root))
    status = make_client().status()
    assert status.state == "not_running"
    assert status.message == "AIODOS n'est pas lancé."


def test_not_running_from_the_discovery_file(fake, make_client):
    _discovery_file(fake, "stopped")
    assert make_client().status().state == "not_running"
    _discovery_file(fake, "open", pid=_dead_pid())
    assert make_client().status().state == "not_running"
    assert fake.log == []                       # rien n'a été demandé au port


def test_not_running_when_the_open_gateway_does_not_answer(fake, make_client):
    fake.close_gateway(discovery=False)          # le fichier dit « ouverte », rien n'écoute
    assert make_client().status().state == "not_running"


def test_contract_discovery_files(fake, make_client):
    expected = {"open": "needs_access", "closed": "gateway_closed", "error": "gateway_error",
                "stopped": "not_running"}
    for name, state in expected.items():
        data = _discovery_file(fake, name)
        status = make_client().status()
        assert status.state == state, name
        if name == "error":
            assert status.message == data["gateway"]["error"]
    fake.approval_ui = False
    _discovery_file(fake, "headless")
    status = make_client().status()
    assert status.state == "headless"
    assert status.message == "AIODOS tourne sans fenêtre : ouvre-le pour autoriser l'appli."


def test_gateway_closed_and_error(fake, make_client):
    fake.write_discovery(state="closed")
    assert make_client().status().state == "gateway_closed"
    fake.write_discovery(state="error", error="Le port 11435 est déjà pris.")
    status = make_client().status()
    assert (status.state, status.message) == ("gateway_error", "Le port 11435 est déjà pris.")


def test_apps_closed(fake, make_client):
    fake.apps = False
    assert make_client().status().state == "apps_closed"
    key = fake.add_key()
    assert make_client(keystore=Keystore.memory(key)).status().state == "apps_closed"


def test_not_aiodos_on_a_404(fake, make_client):
    fake.override("/aiodos/v1/info", interaction("info.json",
                                                 "Ollama sur le port : la route est inconnue")
                  ["response"])
    status = make_client(url=fake.url).status()
    assert status.state == "not_aiodos"
    assert status.message == "Le port est pris par un autre programme."
    with FakeAiodos(mode="ollama") as ollama:
        assert make_client(url=ollama.url).status().state == "not_aiodos"


def test_too_old_on_a_401_without_the_header(fake, make_client):
    fake.override("/aiodos/v1/info", interaction("info.json",
                                                 "ancienne version d'AIODOS : la route est "
                                                 "inconnue")["response"])
    assert make_client(url=fake.url).status().state == "too_old"
    with FakeAiodos(mode="old") as old:
        assert make_client(url=old.url).status().state == "too_old"


def test_untrusted_never_sends_the_key(make_client, monkeypatch):
    with FakeAiodos(impostor=True) as impostor:
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", impostor.discovery_file)
        key = impostor.add_key()                 # il la « connaît », mais ne sait pas prouver
        status = make_client(keystore=Keystore.memory(key)).status()
        assert status.state == "untrusted"
        assert "n'a pas été envoyée" in status.message
        assert impostor.seen_keys == []
        assert all("Authorization" not in entry["headers"] for entry in impostor.log)


def test_untrusted_on_a_wrong_proof(fake, make_client):
    key = fake.add_key()
    answer = interaction("info.json", "info avec un défi : la preuve")["response"]
    fake.override("/aiodos/v1/info", answer)     # preuve du vecteur d'essai : pas pour cette clé
    assert make_client(keystore=Keystore.memory(key)).status().state == "untrusted"
    assert fake.seen_keys == []


def test_headless_and_requests_closed(fake, make_client):
    fake.approval_ui = False
    assert make_client().status().state == "headless"
    fake.approval_ui = True
    fake.requests = False
    status = make_client().status()
    assert status.state == "requests_closed"
    assert status.message == "AIODOS n'accepte pas de nouvelle application en ce moment."


def test_needs_access(fake, make_client):
    status = make_client().status()
    assert status.state == "needs_access"
    assert status.message == "L'appli n'a pas encore accès à AIODOS."
    assert status.version == "2.1.0" and "chat" in status.features
    assert make_client(lang="en").status().message == "The app does not have access to AIODOS yet."
    unusual = make_client(keystore=Keystore.memory("pas-une-cle"))
    assert unusual.status().state == "needs_access"


def _replay_info(fake, monkeypatch, keyed_title):
    """Rejoue info.json : la preuve du vecteur d'essai, puis la réponse avec la clé."""
    vector = load_contract("proof.json")["vectors"][0]
    monkeypatch.setattr(_proof, "challenge", lambda: vector["challenge"])
    challenge = interaction("info.json", "info avec un défi : la preuve")
    fake.override(f"/aiodos/v1/info?key_id={vector['key_id']}&challenge={vector['challenge']}",
                  challenge["response"])
    fake.override("/aiodos/v1/info", interaction("info.json", keyed_title)["response"])
    return vector["key"]


def test_contract_info_valid_key(fake, make_client, monkeypatch):
    key = _replay_info(fake, monkeypatch, "info avec une clé valide")
    status = make_client(keystore=Keystore.memory(key)).status()
    assert status.state == "ready" and status.message == "Connecté à AIODOS."
    assert status.scopes == ("chat", "embed") and status.where == ("here", "mine")
    assert status.app_id == "k3j7m2qa" and status.priority == "interactive"
    assert status.limits["streams"] == 4
    assert status.features == ("access", "models", "chat", "embed", "open")
    assert fake.seen_keys == [key]               # envoyée une fois, après la preuve juste


def test_contract_info_revoked_and_unknown_key(fake, make_client, monkeypatch):
    key = _replay_info(fake, monkeypatch, "info avec une clé retirée")
    status = make_client(keystore=Keystore.memory(key)).status()
    assert status.state == "revoked"
    assert status.message == "L'accès a été retiré (ou a expiré) dans AIODOS."
    key = _replay_info(fake, monkeypatch, "info avec une clé inconnue")
    assert make_client(keystore=Keystore.memory(key)).status().state == "needs_access"


def test_revoked_and_expired(ready, fake):
    client, key = ready
    assert client.status().state == "ready"
    fake.revoke(key)
    assert client.status().state == "revoked"
    other = fake.add_key()
    fake.expire_key(other)
    client._keystore.set(other)
    assert client.status().state == "revoked"


def test_other_account(make_client, monkeypatch):
    with FakeAiodos(other_account=True) as server:
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", server.discovery_file)
        key = server.add_key()
        status = make_client(keystore=Keystore.memory(key)).status()
        assert status.state == "other_account"
        assert status.message == "AIODOS tourne sous un autre compte Windows."


def test_pending_then_denied(fake, make_client):
    client = make_client()
    outcome = {}

    def connect():
        try:
            client.connect(["chat"])
        except Exception as exc:                # AccessDenied attendu
            outcome["error"] = exc

    worker = threading.Thread(target=connect)
    worker.start()
    fake.wait_request()
    wait_until(lambda: client.status().state == "pending")
    status = client.status()
    assert status.user_code == "482 197"
    assert status.message == "Autorise l'appli dans la fenêtre d'AIODOS. Code : 482 197"
    fake.deny()
    worker.join(5)
    assert type(outcome["error"]).__name__ == "AccessDenied"
    assert client.status().state == "denied"
    assert client.status().message == "L'accès a été refusé dans AIODOS."


def test_ready(ready):
    client, _ = ready
    status = client.status()
    assert status.ready and status.state == "ready"
    assert status.scopes == ("chat", "embed") and status.where == ("here", "mine")
    assert status.url == client._resolve()[0].url
    assert status.serves("chat") and not status.serves("jobs")


def test_status_never_raises(fake, make_client, tmp_path, monkeypatch):
    class Broken:
        def get(self):
            raise RuntimeError("coffre en panne")

        def set(self, key):
            raise RuntimeError("coffre en panne")

        def delete(self):
            raise RuntimeError("coffre en panne")

    assert make_client(keystore=Broken()).status().state == "needs_access"
    garbage = tmp_path / "gateway.json"
    for content in (b"\xff\xfe", b"[]", b'{"product": "autre"}', b"{" * 100000):
        garbage.write_bytes(content)
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", str(garbage))
        assert make_client().status().state in ("not_installed", "not_running")
    garbage.write_text('{"product": "aiodos", "running": true, "pid": %d, '
                       '"gateway": {"state": "open", "url": "http://10.0.0.1:11435"}}'
                       % os.getpid(), encoding="utf-8")
    client = make_client()
    endpoint, disc, applies = client._resolve()
    assert disc.url is None and not applies     # l'adresse non locale est ignorée
    assert client.status().state == "not_running"


def test_wait_ready(fake, make_client):
    fake.write_discovery(running=False)
    client = make_client()
    threading.Timer(0.4, fake.write_discovery).start()
    status = client.wait_ready(timeout=5)
    assert status.state == "needs_access"
    fake.write_discovery(running=False)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert client.wait_ready(timeout=30, cancel=stop).state == "not_running"
    assert client.wait_ready(timeout=0.2).state == "not_running"


@pytest.mark.parametrize("lang", ["fr", "en"])
def test_every_state_has_a_message(lang):
    for state in status_module.STATES:
        text = texts.state(state, lang, app="PANDORA", code="482 197")
        assert text and text != state and "{" not in text
