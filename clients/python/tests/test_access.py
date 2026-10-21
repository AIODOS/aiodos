# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""L'accès : connect() passerelle ouverte (accord, refus, expiration, slow_down, droits en
plus) et passerelle fermée (un faux programme d'aide), la relève du contrat, le retrait,
le lancement d'AIODOS."""

import os
import subprocess
import sys
import threading
import time

import pytest

from aiodos_client import Keystore, _discovery, errors
from aiodos_client import access as access_module
from support import interaction, load_contract, wait_until

PANDORA_REQUEST = load_contract("discovery.json")["app_request"]["stdin"]
PANDORA_ARGS = {"scopes": PANDORA_REQUEST["scope"].split(), "where": PANDORA_REQUEST["where"],
                "purpose": PANDORA_REQUEST["purpose"]}


def _connect_in_thread(client, **options):
    outcome = {"codes": [], "threads": []}

    def on_code(code):
        outcome["codes"].append(code)
        outcome["threads"].append(threading.get_ident())

    def run():
        outcome["thread"] = threading.get_ident()
        try:
            outcome["access"] = client.connect(on_code=on_code, **options)
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    return worker, outcome


def test_connect_with_the_gateway_open(fake, make_client):
    store = Keystore.memory()
    client = make_client(keystore=store)
    worker, outcome = _connect_in_thread(client, **PANDORA_ARGS)
    request = fake.wait_request()
    assert request["body"] == PANDORA_REQUEST
    key = fake.approve()
    worker.join(5)
    access = outcome["access"]
    assert access.scopes == ("chat", "embed") and access.where == ("here", "mine")
    assert access.app_id == key[11:19] and not access.upgraded
    assert store.get() == key
    assert outcome["codes"] == ["482 197"] and outcome["threads"] == [outcome["thread"]]
    sent = fake.requests_to("/aiodos/v1/access/requests")[0]
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["headers"]["X-Aiodos-Client"] == "python/0.2.0"
    assert "Authorization" not in sent["headers"] and "Origin" not in sent["headers"]
    polls = fake.requests_to("/aiodos/v1/access/token")
    assert polls and all("Authorization" not in poll["headers"] for poll in polls)
    assert all(poll["json"]["grant_type"] == access_module.GRANT_TYPE for poll in polls)
    assert client.status().state == "ready"


def test_connect_denied(fake, make_client):
    store = Keystore.memory()
    client = make_client(keystore=store)
    worker, outcome = _connect_in_thread(client, scopes=["chat"])
    fake.wait_request()
    fake.deny()
    worker.join(5)
    assert isinstance(outcome["error"], errors.AccessDenied)
    assert outcome["error"].message == "L'accès a été refusé dans AIODOS."
    assert store.get() is None
    assert client.status().state == "denied"


def test_connect_expired(fake, make_client):
    client = make_client()
    worker, outcome = _connect_in_thread(client, scopes=["chat"])
    fake.wait_request()
    fake.expire()
    worker.join(5)
    assert type(outcome["error"]) is errors.AccessExpired
    assert outcome["error"].code == "expired_token"
    assert client.status().state == "needs_access"


def test_slow_down_adds_to_the_interval(fake, make_client, monkeypatch):
    monkeypatch.setattr(access_module, "SLOW_DOWN", 0.4)
    fake.slow_down(1)
    client = make_client()
    worker, outcome = _connect_in_thread(client, scopes=["chat"])
    fake.wait_request()
    wait_until(lambda: len(fake.token_polls) >= 3, timeout=5)
    fake.approve()
    worker.join(5)
    assert "access" in outcome
    first, second, third = fake.token_polls[:3]
    assert second - first >= 0.4                    # 0,05 s d'intervalle + 0,4 s
    assert third - second >= 0.4                    # l'intervalle reste allongé


def test_more_rights_for_the_same_key(ready, fake):
    client, key = ready
    worker, outcome = _connect_in_thread(client, scopes=["chat", "libraries"])
    request = fake.wait_request()
    assert request["upgrade_key"] == key
    sent = fake.requests_to("/aiodos/v1/access/requests")[0]
    assert sent["headers"]["Authorization"] == f"Bearer {key}"
    fake.approve()
    worker.join(5)
    access = outcome["access"]
    assert access.upgraded and "libraries" in access.scopes
    assert client._keystore.get() == key
    assert fake.key_record(key)["scopes"] == ["chat", "embed", "libraries"]


def test_connect_when_already_ready(ready, fake):
    client, _ = ready
    access = client.connect(["chat"])
    assert access.scopes == ("chat", "embed") and not access.upgraded
    assert fake.requests_to("/aiodos/v1/access/requests") == []


def _helper(fake, monkeypatch, error=None):
    home = fake.helper_home(error=error)
    monkeypatch.setenv("AIODOS_HOME", home)
    monkeypatch.setenv("AIODOS_PYTHON", sys.executable)
    return home


def test_connect_with_the_gateway_closed(fake, make_client, monkeypatch):
    fake.close_gateway()                            # rien n'écoute : connexion refusée
    home = _helper(fake, monkeypatch)
    store = Keystore.memory()
    client = make_client(keystore=store)
    worker, outcome = _connect_in_thread(client, **PANDORA_ARGS)
    wait_until(lambda: fake.helper_stdin(home) is not None, timeout=20)
    assert fake.helper_stdin(home) == PANDORA_REQUEST
    wait_until(lambda: client.status().state == "pending", timeout=10)
    time.sleep(0.3)                                 # quelques relèves refusées
    fake.open_gateway()
    key = fake.approve()
    worker.join(10)
    assert outcome["access"].scopes == ("chat", "embed")
    assert outcome["codes"] == ["482 197"]
    assert store.get() == key
    assert client.status().state == "ready"


def test_connect_when_the_gateway_is_open_to_other_tools_only(fake, make_client, monkeypatch):
    fake.apps = False
    _helper(fake, monkeypatch)
    client = make_client()
    worker, outcome = _connect_in_thread(client, scopes=["chat"])
    wait_until(lambda: client.status().state == "pending", timeout=20)
    fake.approve()
    worker.join(10)
    assert outcome["access"].scopes == ("chat",)
    assert fake.apps                                # le premier accord ouvre la passerelle
    assert client.status().state == "ready"


def test_helper_error(fake, make_client, monkeypatch):
    fake.close_gateway()
    contract = load_contract("discovery.json")["app_request"]
    _helper(fake, monkeypatch, error=contract["stdout_error"]["error"])
    with pytest.raises(errors.ApprovalUnavailable) as caught:
        make_client().connect(["chat"])
    assert caught.value.message == contract["stdout_error"]["error"]["message"]
    assert make_client().status().state == "gateway_closed"


def test_helper_messages_survive_the_console_encoding(fake, make_client, monkeypatch):
    fake.close_gateway()
    message = "AIODOS n'accepte pas de nouvelle application : Réglages → Applications."
    _helper(fake, monkeypatch, error={"code": "requests_closed", "message": message})
    with pytest.raises(errors.RequestsClosed) as caught:
        make_client().connect(["chat"])
    assert caught.value.message == message


def test_helper_output_is_the_contract(fake, make_client, monkeypatch):
    fake.close_gateway()
    _helper(fake, monkeypatch)
    request = make_client().request_access(**PANDORA_ARGS)
    expected = load_contract("discovery.json")["app_request"]["stdout_ok"]
    assert request.user_code == expected["user_code"]
    assert request.interval == fake.interval and request.expires_in == expected["expires_in"]
    assert request.url == fake.url and request.request_id.startswith("req_")
    assert not request.upgrade


def test_closed_gateway_without_installation(fake, make_client):
    fake.close_gateway()
    with pytest.raises(errors.NotInstalled):
        make_client().connect(["chat"])


def test_development_copy_without_its_interpreter(fake, make_client, monkeypatch):
    fake.close_gateway()
    _helper(fake, monkeypatch)
    monkeypatch.delenv("AIODOS_PYTHON")
    with pytest.raises(errors.NotRunning) as caught:
        make_client().connect(["chat"])
    assert "AIODOS_PYTHON" in caught.value.message


def test_requests_closed_and_headless(fake, make_client):
    fake.requests = False
    with pytest.raises(errors.RequestsClosed):
        make_client().connect(["chat"])
    fake.requests = True
    fake.approval_ui = False
    with pytest.raises(errors.ApprovalUnavailable):
        make_client().connect(["chat"])
    assert fake.requests_to("/aiodos/v1/access/requests") == []


@pytest.mark.parametrize("title, expected", [
    ("demande refusée : AIODOS n'accepte pas de nouvelle application", errors.RequestsClosed),
    ("demande refusée : en-têtes d'une page web", errors.OriginRefused),
])
def test_contract_refused_requests(fake, make_client, title, expected):
    item = interaction("access.json", title)
    fake.override("/aiodos/v1/access/requests", item["response"], method="POST")
    with pytest.raises(expected) as caught:
        make_client().request_access(["chat"])
    error = item["response"]["json"]["error"]
    assert caught.value.message == error["message"]
    assert caught.value.hint == error["aiodos"]["hint"]
    assert "Origin" not in fake.requests_to("/aiodos/v1/access/requests")[0]["headers"]


def test_contract_request_and_collection(fake, make_client, monkeypatch):
    """Rejoue access.json : la demande, puis la relève (attente, trop souvent, accordé)."""
    monkeypatch.setattr(access_module, "SLOW_DOWN", 0.05)
    request_item = interaction("access.json", "demande d'accès")
    fake.override("/aiodos/v1/access/requests", request_item["response"])
    for title in ("relève : pas encore de réponse", "relève : trop souvent", "relève : accordé"):
        fake.override("/aiodos/v1/access/token", interaction("access.json", title)["response"])
    store = Keystore.memory()
    client = make_client(keystore=store)
    request = client.request_access(**PANDORA_ARGS)
    answer = request_item["response"]["json"]
    assert request.user_code == answer["user_code"] and request.interval == answer["interval"]
    assert request.expires_in == answer["expires_in"]
    assert request.request_id == answer["request_id"] and request.message == answer["message"]
    assert answer["device_code"] not in repr(request)
    assert fake.requests_to("/aiodos/v1/access/requests")[0]["json"] == request_item["request"][
        "json"]
    started = time.monotonic()
    access = request.wait(timeout=30)
    assert time.monotonic() - started >= 2 * answer["interval"]     # l'intervalle d'AIODOS
    granted = interaction("access.json", "relève : accordé")["response"]["json"]
    assert store.get() == granted["access_token"]
    assert access.scopes == ("chat", "embed") and access.where == ("here", "mine")
    assert access.app_id == "k3j7m2qa" and access.priority == "interactive"
    polls = fake.requests_to("/aiodos/v1/access/token")
    assert [poll["json"]["device_code"] for poll in polls] == [answer["device_code"]] * 3


@pytest.mark.parametrize("title, expected, code", [
    ("relève : refusé", errors.AccessDenied, "access_denied"),
    ("relève : expirée", errors.AccessExpired, "expired_token"),
    ("relève : code inconnu ou clé déjà relevée", errors.AccessExpired, "invalid_grant"),
])
def test_contract_collection_failures(fake, make_client, title, expected, code):
    fake.override("/aiodos/v1/access/token", interaction("access.json", title)["response"])
    client = make_client()
    request = client.request_access(["chat"])
    with pytest.raises(expected) as caught:
        request.wait(timeout=10)
    assert caught.value.code == code
    assert not request.active
    assert client.status().state == ("denied" if code == "access_denied" else "needs_access")


def test_contract_upgrade(ready, fake):
    client, key = ready
    fake.override("/aiodos/v1/access/token",
                  interaction("access.json", "relève : droits en plus accordés à la même clé")
                  ["response"])
    access = client.request_access(["chat", "embed", "libraries"]).wait(timeout=10)
    assert access.upgraded and access.scopes == ("chat", "embed", "libraries")
    assert client._keystore.get() == key


def test_cancel_while_waiting(fake, make_client):
    client = make_client()
    request = client.request_access(["chat"])
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    with pytest.raises(errors.Cancelled) as caught:
        request.wait(cancel=stop, timeout=30)
    assert time.monotonic() - started < 2
    assert caught.value.message == "Arrêté"
    assert client.status().state == "needs_access"


def test_disconnect(ready, fake):
    client, key = ready
    assert client.disconnect() is True
    revoke = fake.requests_to("/aiodos/v1/access/revoke")[0]
    assert revoke["headers"]["Authorization"] == f"Bearer {key}"
    assert client._keystore.get() is None
    assert fake.key_record(key)["state"] == "revoked"
    assert client.status().state == "needs_access"
    item = interaction("access.json", "l'appli se retire elle-même")
    assert revoke["json"] == item["request"]["json"]


def test_disconnect_while_aiodos_is_away(ready, fake):
    client, _ = ready
    fake.close_gateway(discovery=False)
    assert client.disconnect() is False
    assert client._keystore.get() is None


def test_where_and_scopes_are_checked(make_client):
    client = make_client()
    with pytest.raises(ValueError):
        client.request_access(["chat"], where=["partout"])
    with pytest.raises(ValueError):
        client.request_access([])
    assert access_module.normalize_where("here, mine,here") == ["here", "mine"]
    assert access_module.normalize_scopes("chat embed chat") == ["chat", "embed"]


def test_launch_starts_the_development_copy(fake, make_client, monkeypatch):
    home = _helper(fake, monkeypatch)
    fake.write_discovery(running=False)
    status = make_client().launch(wait=0)
    assert status.state == "not_running"
    wait_until(lambda: fake.launched(home) is not None, timeout=20)
    launched = fake.launched(home)
    assert launched["argv"] == ["--background"]
    assert os.path.normcase(launched["cwd"]) == os.path.normcase(home)


def test_launch_does_nothing_when_aiodos_runs(fake, make_client, monkeypatch):
    home = _helper(fake, monkeypatch)
    assert make_client().launch(wait=0).state == "needs_access"
    time.sleep(0.5)
    assert fake.launched(home) is None


def test_launch_without_installation(make_client):
    with pytest.raises(errors.NotInstalled):
        make_client().launch(wait=0)


def test_commands_of_an_installed_copy(tmp_path):
    root = tmp_path / "AIODOS"
    (root / "runtime").mkdir(parents=True)
    (root / "app").mkdir()
    for name in ("python.exe", "pythonw.exe"):
        (root / "runtime" / name).write_bytes(b"")
    inst = _discovery._from_root(str(root))
    args, cwd = _discovery.launch_command(inst)
    assert args == [str(root / "runtime" / "pythonw.exe"), "-m", "aiodos", "--background"]
    assert cwd == str(root / "app")
    args, cwd = _discovery.helper_command(inst)
    assert args == [str(root / "runtime" / "python.exe"), "-m", "aiodos", "--app-request"]
    assert cwd == str(root / "app")


def test_installation_lookup_order(monkeypatch, tmp_path):
    registered = tmp_path / "registre"
    (registered / "runtime").mkdir(parents=True)
    (registered / "runtime" / "python.exe").write_bytes(b"")
    default = tmp_path / "local" / "Programs" / "AIODOS"
    (default / "runtime").mkdir(parents=True)
    (default / "runtime" / "python.exe").write_bytes(b"")
    monkeypatch.delenv("AIODOS_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(_discovery, "_registry_roots", lambda: [str(registered)])
    assert _discovery.installation().root == str(registered)
    monkeypatch.setattr(_discovery, "_registry_roots", lambda: [str(tmp_path / "absent")])
    if _discovery.WINDOWS:
        assert _discovery.installation().root == str(default)
    monkeypatch.setenv("AIODOS_HOME", str(tmp_path / "ailleurs"))
    assert _discovery.installation() is None        # AIODOS_HOME : la seule racine regardée


def test_child_environment_is_clean(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\PANDORA\lib")
    monkeypatch.setenv("AIODOS_KEY", "aiodos_app_abcdefgh_" + "A" * 43)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\PANDORA")
    monkeypatch.setenv("AIODOS_TEST_KEEP", "oui")
    env = {name.upper(): value for name, value in _discovery.child_env().items()}
    assert "PYTHONPATH" not in env and "AIODOS_KEY" not in env
    assert "_PYI_APPLICATION_HOME_DIR" not in env and env["AIODOS_TEST_KEEP"] == "oui"


def test_pid_alive_never_uses_os_kill_on_windows(monkeypatch):
    if _discovery.WINDOWS:
        def forbidden(*args):
            raise AssertionError("os.kill ne doit jamais servir sous Windows")
        monkeypatch.setattr(os, "kill", forbidden)
    assert _discovery.pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not _discovery.pid_alive(child.pid)
    for wrong in (0, -1, None, True, "123", 2 ** 40):
        assert not _discovery.pid_alive(wrong)
