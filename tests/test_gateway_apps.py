"""Les applications sur la passerelle (docs/api-applications.md) : demande d'accès au format
de la RFC 8628, carte d'accord, une clé par appli, droits, lieu de calcul, outils jamais
retirés en silence, pages web refusées, preuve d'identité, fichier de découverte, journal
sans contenu, et le contrat partagé avec les bibliothèques clientes (clients/contract/).

Le nœud tourne dans le processus de l'essai (TestClient), la passerelle sur un vrai port de
127.0.0.1 ; un faux Ollama répond. L'accord se donne comme dans l'interface, par ses routes
derrière le jeton (aucune route de la passerelle n'accorde rien)."""
import base64
import hashlib
import hmac
import json
import secrets
import sys
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.gateway import access_flow, app_connect, apps, caller, discovery, info, limits
from aiodos.gateway import server as gateway_server
from aiodos.gateway import usage
from aiodos.knowledge import store
from aiodos.network import client, peers, placement
from tests.nodes import free_port
from tests.ollama_stub import EMBEDDING, OllamaStub

TOKEN = "jeton"
UI = {"X-Aiodos-Token": TOKEN}
SHOW = "cle-de-la-fenetre"
LOCAL = "petit:1b"
LENT = "grand:7b"                    # seulement chez l'ami
FRIEND = "ami01"
CONTRACT = Path(__file__).resolve().parents[1] / "clients" / "contract"
ME = caller.Caller(pid=4242, program=r"C:\Programmes\PANDORA\PANDORA.exe", same_account=True)
REQUEST = {"client_name": "PANDORA", "client_id": "arkane.pandora", "client_version": "3.2.0",
           "scope": "chat embed", "where": ["here", "mine", "friends", "public"],
           "purpose": "Traduire les prompts et décrire les images."}
TOKEN_BODY = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code"}


class Node:
    def __init__(self, ui, port, ollama):
        self.ui, self.port, self.ollama = ui, port, ollama
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method, path, key=None, **kw):
        headers = dict(kw.pop("headers", {}))
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return requests.request(method, self.base + path, headers=headers, timeout=30, **kw)

    def approve(self, request_id, **body):
        reply = self.ui.post(f"/api/gateway/requests/{request_id}/approve", json=body,
                             headers=UI)
        assert reply.status_code == 200, reply.text
        return reply.json()

    def ask(self, body=None, who=ME):
        """Une demande par le canal de l'instance (passerelle fermée), comme le programme
        d'aide : la carte s'affiche."""
        status, reply = app_connect.instance_request({"request": body or REQUEST, "pid": 1},
                                                     self.port)
        assert status == 200, reply
        return reply

    def connect(self, body=None, **grant) -> str:
        """Demande, accord, relève : la clé de l'appli."""
        asked = self.ask(body)
        self.approve(asked["request_id"], **grant)
        got = self.call("POST", "/aiodos/v1/access/token",
                        json={**TOKEN_BODY, "device_code": asked["device_code"]})
        assert got.status_code == 200, got.text
        return got.json()["access_token"]


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setattr(caller, "of_pid", lambda pid: ME)
    monkeypatch.setattr(caller, "identify", lambda scope: ME)
    placement.forget()
    with OllamaStub([LOCAL]) as ollama:
        port = free_port()
        settings.set_many({"ollama_url": ollama.url, "gateway_port": port,
                           "embed_model": EMBEDDING, "llm_engine": "ollama"})
        store.ensure_defaults()
        with TestClient(create_app(TOKEN, {"testserver"}, show_key=SHOW)) as ui:
            access_flow._hooks["approval_ui"] = True      # une fenêtre peut montrer la carte
            yield Node(ui, port, ollama)
    placement.forget()


@pytest.fixture
def friend(monkeypatch):
    """Un ami qui prête LENT (et LOCAL) ; ses réponses sont simulées."""
    peers.save(FRIEND, "Paul", b"k" * 32, "127.0.0.1", 1)
    db.execute("UPDATE peers SET trust='friend' WHERE node_id=?", (FRIEND,))
    peers.set_profile(FRIEND, {"lends_models": True, "ollama": {"models": [
        {"name": m, "caps": ["completion"], "size": 7, "fits_vram": True} for m in (LENT, LOCAL)]}})
    sent = []

    def fake_stream(my_id, peer, action, payload, stop=None, **kw):
        sent.append(payload)
        yield {"content": f"Bonjour de {peer['name']}"}
    monkeypatch.setattr(client, "stream", fake_stream)
    return sent


# ------------------------------------------------------------- l'accès
def test_closed_until_the_first_approval_which_opens_it(node):
    """Fermée par défaut ; la demande passe par le canal de l'instance (programme d'aide) ;
    l'accord ouvre la passerelle aux applications ; la clé est relevée une seule fois."""
    assert not settings.get("gateway_apps")
    with pytest.raises(requests.ConnectionError):
        node.call("GET", "/aiodos/v1/info")
    asked = node.ask()
    assert asked["user_code"].replace(" ", "").isdigit() and asked["interval"] == 2
    assert asked["url"] == node.base
    pending = node.ui.get("/api/gateway/requests", headers=UI).json()
    assert [p["client_name"] for p in pending] == ["PANDORA"]
    assert pending[0]["program"] == ME.program and "device_code" not in pending[0]
    node.approve(asked["request_id"], scopes=["chat"], where=["here", "mine"])
    assert settings.get("gateway_apps")
    body = {**TOKEN_BODY, "device_code": asked["device_code"]}
    got = node.call("POST", "/aiodos/v1/access/token", json=body).json()
    key = got["access_token"]
    assert apps.is_app_key(key) and got["scope"] == "chat"         # décoché : embed
    assert got["aiodos"]["where"] == ["here", "mine"]
    again = node.call("POST", "/aiodos/v1/access/token", json=body)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    row = db.query_one("SELECT * FROM gateway_apps WHERE id=?", (apps.key_id(key),))
    assert row["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert key not in json.dumps(dict(row))                        # seul le haché est gardé
    me = node.call("GET", "/aiodos/v1/info", key=key).json()
    assert me["auth"] == "valid" and me["app"]["scopes"] == ["chat"]


def test_pending_denied_and_muted(node):
    key = node.connect()                                  # ouvre la passerelle aux applis
    assert key
    for _ in range(3):
        asked = node.call("POST", "/aiodos/v1/access/requests", json=REQUEST).json()
        body = {**TOKEN_BODY, "device_code": asked["device_code"]}
        pending = node.call("POST", "/aiodos/v1/access/token", json=body)
        assert pending.json()["error"] == "authorization_pending"
        assert node.call("POST", "/aiodos/v1/access/token", json=body).json()["error"] \
            == "slow_down"                               # trop tôt : 5 s de plus
        node.ui.post(f"/api/gateway/requests/{asked['request_id']}/deny", json={}, headers=UI)
        assert node.call("POST", "/aiodos/v1/access/token", json=body).json()["error"] \
            == "access_denied"
    muted = node.call("POST", "/aiodos/v1/access/requests", json=REQUEST)
    assert muted.status_code == 429 and muted.json()["error"]["code"] == "rate_limited"


def test_granted_rights_are_a_subset_of_those_asked(node):
    asked = node.ask()
    widened = node.ui.post(f"/api/gateway/requests/{asked['request_id']}/approve",
                           json={"scopes": ["chat", "libraries"]}, headers=UI)
    assert widened.status_code == 400
    narrowed = node.ui.post(f"/api/gateway/requests/{asked['request_id']}/approve",
                            json={"where": ["here", "mine", "friends", "public", "cloud"]},
                            headers=UI)
    assert narrowed.status_code == 400


def test_a_request_is_validated_and_names_are_cleaned(node):
    for wrong in ({**REQUEST, "client_name": "AIODOS"}, {**REQUEST, "client_id": "Pas Bon"},
                  {**REQUEST, "scope": "chat admin"}, {**REQUEST, "where": ["cloud"]},
                  {**REQUEST, "purpose": "x" * 201}):
        status, reply = app_connect.instance_request({"request": wrong, "pid": 1}, node.port)
        assert status == 400 and reply["error"]["code"] == "invalid_request", wrong
    tricky = {**REQUEST, "client_name": "PAN‮DORA\u0007 <b>"}
    asked = node.ask(tricky)
    pending = node.ui.get("/api/gateway/requests", headers=UI).json()
    shown = next(p for p in pending if p["id"] == asked["request_id"])
    assert shown["client_name"] == "PANDORA <b>"          # contrôle et sens d'écriture retirés


def test_another_windows_account_and_a_blocked_program_are_refused(node, monkeypatch):
    monkeypatch.setattr(caller, "of_pid", lambda pid: caller.Caller(7, ME.program, False))
    status, reply = app_connect.instance_request({"request": REQUEST, "pid": 7}, node.port)
    assert status == 403 and reply["error"]["code"] == "other_account"
    monkeypatch.setattr(caller, "of_pid", lambda pid: ME)
    asked = node.ask()
    node.ui.post(f"/api/gateway/requests/{asked['request_id']}/deny", json={"block": True},
                 headers=UI)
    status, reply = app_connect.instance_request({"request": REQUEST, "pid": 1}, node.port)
    assert status == 403 and reply["error"]["code"] == "blocked_program"
    blocked = node.ui.get("/api/gateway/blocked", headers=UI).json()
    assert [b["exe_path"] for b in blocked] == [ME.program]


def test_revoked_at_once_and_said_so(node):
    key = node.connect()
    assert node.call("GET", "/v1/models", key=key).status_code == 200
    app_id = apps.key_id(key)
    assert node.ui.delete(f"/api/gateway/apps/{app_id}", headers=UI).json()["revoked"]
    refused = node.call("GET", "/v1/models", key=key)
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "key_revoked"
    assert node.call("GET", "/aiodos/v1/info", key=key).json()["auth"] == "revoked"


def test_the_app_revokes_itself(node):
    key = node.connect()
    assert node.call("POST", "/aiodos/v1/access/revoke", key=key, json={}).json() == \
        {"revoked": True}
    assert node.call("GET", "/v1/models", key=key).json()["error"]["code"] == "key_revoked"


def test_extra_rights_for_the_same_key(node):
    key = node.connect(scopes=["chat"])
    asked = node.call("POST", "/aiodos/v1/access/requests", key=key,
                      json={**REQUEST, "scope": "chat embed"}).json()
    pending = node.ui.get("/api/gateway/requests", headers=UI).json()
    assert pending[0]["upgrade"]["app_id"] == apps.key_id(key)
    node.approve(asked["request_id"])
    got = node.call("POST", "/aiodos/v1/access/token",
                    json={**TOKEN_BODY, "device_code": asked["device_code"]}).json()
    assert "access_token" not in got and got["aiodos"]["upgraded"] and got["scope"] == "chat embed"
    assert node.call("GET", "/aiodos/v1/info", key=key).json()["app"]["scopes"] == ["chat", "embed"]


def test_a_reinstalled_app_replaces_its_old_access(node):
    old = node.connect()
    asked = node.ask()
    pending = node.ui.get("/api/gateway/requests", headers=UI).json()
    assert pending[0]["existing"]["app_id"] == apps.key_id(old)
    node.approve(asked["request_id"])
    new = node.call("POST", "/aiodos/v1/access/token",
                    json={**TOKEN_BODY, "device_code": asked["device_code"]}).json()
    assert new["access_token"] != old
    assert node.call("GET", "/aiodos/v1/info", key=old).json()["auth"] == "revoked"


def test_a_manual_key_for_a_tool(node):
    made = node.ui.post("/api/gateway/apps", json={"name": "Open WebUI", "scopes": ["chat"],
                                                   "where": ["here"]}, headers=UI).json()
    assert apps.is_app_key(made["key"]) and made["app"]["kind"] == "manual"
    settings.set_value("gateway_apps", True)
    node.ui.post("/api/gateway", json={}, headers=UI)              # la passerelle s'ouvre
    assert node.call("GET", "/v1/models", key=made["key"]).status_code == 200
    listed = node.ui.get("/api/gateway/apps", headers=UI).json()
    assert "key" not in listed[0] and listed[0]["name"] == "Open WebUI"


# ---------------------------------------------------------- droits, lieu
def test_a_missing_right_is_refused_with_its_code(node):
    key = node.connect(scopes=["chat"])
    refused = node.call("POST", "/v1/embeddings", key=key,
                        json={"model": EMBEDDING, "input": "un chat noir"})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "scope_denied"
    assert refused.json()["error"]["aiodos"]["detail"] == {"scope": "embed"}
    mcp = node.call("POST", "/mcp", key=key, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "tools/list"}).json()
    assert [t["name"] for t in mcp["result"]["tools"]] == ["converser"]   # pas les bibliothèques


def test_where_narrows_never_widens(node, friend):
    key = node.connect(where=["here", "mine"])
    names = {m["id"] for m in node.call("GET", "/v1/models", key=key).json()["data"]}
    assert LOCAL in names and not any(FRIEND in n for n in names)      # ni l'ami ni ses modèles
    wider = node.call("POST", "/v1/chat/completions", key=key, headers={"X-Aiodos-Where": "friends"},
                      json={"model": LOCAL, "messages": [{"role": "user", "content": "Salut"}]})
    assert wider.status_code == 403 and wider.json()["error"]["code"] == "where_denied"
    explicit = node.call("POST", "/v1/chat/completions", key=key,
                         json={"model": f"peer:{FRIEND}:{LENT}",
                               "messages": [{"role": "user", "content": "Salut"}]})
    assert explicit.status_code == 403 and explicit.json()["error"]["code"] == "where_denied"
    assert friend == []                                             # rien n'est parti chez lui


def test_a_friend_answers_when_allowed(node, friend):
    key = node.connect()
    reply = node.call("POST", "/v1/chat/completions", key=key,
                      json={"model": f"peer:{FRIEND}:{LENT}",
                            "messages": [{"role": "user", "content": "Salut"}]})
    assert reply.status_code == 200, reply.text
    assert reply.json()["choices"][0]["message"]["content"] == "Bonjour de Paul"
    assert reply.headers["X-Aiodos-Place"] == "friend"
    assert reply.json()["aiodos"]["place"] == "friend"
    assert friend and "PANDORA" not in json.dumps(friend) and "aiodos_app" not in json.dumps(friend)


def test_tools_are_never_dropped_silently_for_an_app(node, friend):
    key = node.connect()
    body = {"model": f"peer:{FRIEND}:{LENT}", "messages": [{"role": "user", "content": "Allume"}],
            "tools": [{"type": "function", "function": {"name": "allumer",
                                                         "parameters": {"type": "object"}}}]}
    refused = node.call("POST", "/v1/chat/completions", key=key, json=body)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "tools_unavailable"
    optional = node.call("POST", "/v1/chat/completions", key=key, json=body,
                         headers={"X-Aiodos-Tools": "optional"})
    assert optional.status_code == 200 and "Paul" in optional.text


def test_who_answered_is_said(node):
    key = node.connect()
    reply = node.call("POST", "/v1/chat/completions", key=key,
                      json={"model": LOCAL, "messages": [{"role": "user", "content": "Salut"}]})
    assert reply.status_code == 200, reply.text
    assert reply.headers["X-Aiodos-Model"] == LOCAL and reply.headers["X-Aiodos-Place"] == "here"
    assert reply.headers["X-Aiodos-Api"] == "1"


def test_the_catalogue_names_the_models_of_aiodos(node):
    """« default » : le modèle des conversations d'AIODOS et celui de ses bibliothèques, qu'une
    appli sans préférence prend (des vecteurs d'un même modèle d'un appel à l'autre)."""
    key = node.connect()
    settings.set_value("default_model", LOCAL)
    chat = node.call("GET", "/aiodos/v1/models?kind=chat", key=key).json()["models"]
    assert [m["id"] for m in chat if m["default"]] == [LOCAL]
    for chosen in (f"auto:{LOCAL}", f"peer:ami01:{LOCAL}"):         # le mode automatique…
        settings.set_value("default_model", chosen)
        chat = node.call("GET", "/aiodos/v1/models?kind=chat", key=key).json()["models"]
        assert [m["id"] for m in chat if m["default"]] == [LOCAL], chosen
    for chosen in ("absent:7b", f"cloud:{LOCAL}", ""):
        settings.set_value("default_model", chosen)
        chat = node.call("GET", "/aiodos/v1/models?kind=chat", key=key).json()["models"]
        assert chat and not any(m["default"] for m in chat), chosen
    settings.set_value("embed_model", EMBEDDING.removesuffix(":latest"))      # nom sans étiquette
    embed = node.call("GET", "/aiodos/v1/models?kind=embed", key=key).json()["models"]
    assert [m["id"] for m in embed if m["default"]] == [EMBEDDING]


def test_an_app_never_unloads_a_model(node):
    key = node.connect()
    before = len(node.ollama.requests)
    reply = node.call("POST", "/api/generate", key=key,
                      json={"model": LOCAL, "keep_alive": 0})
    assert reply.status_code == 200 and reply.json()["done"]
    assert len(node.ollama.requests) == before                     # rien n'est parti à Ollama


# ------------------------------------------------------------ pages web
def test_web_pages_cannot_ask_for_access(node):
    node.connect()                                       # passerelle ouverte aux applis
    url = "/aiodos/v1/access/requests"
    refused = node.call("POST", url, json=REQUEST, headers={"Origin": "http://localhost:3000"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "origin_refused"
    plain = node.call("POST", url, data=json.dumps(REQUEST), headers={"Content-Type": "text/plain"})
    assert plain.status_code == 415
    cross = node.call("POST", url, json=REQUEST, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    image = node.call("GET", "/aiodos/v1/info", headers={"Sec-Fetch-Dest": "image"})
    assert image.status_code == 403
    node_fetch = node.call("POST", url, json=REQUEST, headers={"Sec-Fetch-Mode": "cors"})
    assert node_fetch.status_code == 200                 # le fetch de Node passe


def test_an_app_key_when_apps_are_closed(node):
    key = node.connect()
    settings.set_value("gateway_enabled", True)          # la passerelle reste ouverte…
    settings.set_value("gateway_apps", False)            # … mais plus aux applications
    refused = node.call("GET", "/v1/models", key=key)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "apps_closed"
    assert node.call("GET", "/aiodos/v1/info", key=key).status_code == 200


# ------------------------------------------------------------- la preuve
def test_the_proof_of_identity(node):
    key = node.connect()
    challenge = info.b64url(secrets.token_bytes(32))
    app_id = apps.key_id(key)
    got = node.call("GET", f"/aiodos/v1/info?key_id={app_id}&challenge={challenge}").json()
    expected = info.b64url(hmac.new(hashlib.sha256(key.encode()).digest(),
                                    b"aiodos-proof/1\n" + challenge.encode(),
                                    hashlib.sha256).digest())
    assert hmac.compare_digest(got["proof"], expected) and got["auth"] == "none"
    unknown = [node.call("GET", f"/aiodos/v1/info?key_id=zzzzzzzz&challenge={challenge}")
               .json()["proof"] for _ in range(2)]
    assert unknown[0] != unknown[1] and len(unknown[0]) == 43       # aléatoire, même forme


def test_the_proof_matches_the_shared_vector():
    vector = json.loads((CONTRACT / "proof.json").read_text(encoding="utf-8"))["vectors"][0]
    assert info.proof(hashlib.sha256(vector["key"].encode()).digest(),
                      vector["challenge"]) == vector["proof"]
    assert apps.key_id(vector["key"]) == vector["key_id"]


# -------------------------------------------------------- découverte, journal
def test_the_discovery_file(node, tmp_path, monkeypatch):
    target = tmp_path / "decouverte" / "gateway.json"
    monkeypatch.setenv(discovery.ENV, str(target))
    node.ui.post("/api/gateway", json={}, headers=UI)              # apply : écrit
    closed = json.loads(target.read_text(encoding="utf-8"))
    assert closed["gateway"]["state"] == "closed" and closed["running"]
    key = node.connect()
    opened = json.loads(target.read_text(encoding="utf-8"))
    assert opened["gateway"]["state"] == "open" and opened["gateway"]["apps"]
    assert opened["gateway"]["url"] == node.base and opened["instance"] == info.INSTANCE
    text = target.read_text(encoding="utf-8")
    assert key not in text and "aiodos_app_" not in text and TOKEN not in text and SHOW not in text


def test_another_live_instance_keeps_its_discovery_file(tmp_path, monkeypatch):
    import os
    target = tmp_path / "gateway.json"
    monkeypatch.setenv(discovery.ENV, str(target))
    other = {"product": "aiodos", "pid": os.getppid(), "running": True, "gateway": {}}
    target.write_text(json.dumps(other), encoding="utf-8")
    assert not discovery.write({"pid": os.getpid(), "running": True})
    assert json.loads(target.read_text(encoding="utf-8"))["pid"] == os.getppid()
    other["running"] = False                             # elle s'est arrêtée : on reprend
    target.write_text(json.dumps(other), encoding="utf-8")
    assert discovery.write({"pid": os.getpid(), "running": True})


def test_only_the_usual_data_folder_writes_the_real_file(tmp_path, monkeypatch):
    """Un nœud lancé avec un autre dossier de données (nœud d'essai, --data-dir) n'écrit pas
    le vrai gateway.json : les applis du PC y trouveraient lui au lieu d'AIODOS."""
    from aiodos import instance, paths
    monkeypatch.delenv(discovery.ENV, raising=False)
    monkeypatch.delenv(instance.NO_DIALOG_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert discovery.path() is None                      # AIODOS_DATA_DIR : un dossier d'essai
    monkeypatch.setattr(paths, "installed_data_dir", lambda: tmp_path)
    found = discovery.path()                             # l'instance du dossier habituel
    assert found is not None and found.name == "gateway.json"


def test_the_log_has_no_content(node):
    key = node.connect()
    node.call("POST", "/v1/chat/completions", key=key,
              json={"model": LOCAL, "messages": [{"role": "user", "content": "TEMOIN-SECRET"}]})
    import time
    for _ in range(40):                  # la ligne s'écrit une fois la réponse partie
        usage.flush()
        rows = [dict(r) for r in db.query("SELECT * FROM gateway_log")]
        if any(r["route"] == "/v1/chat/completions" for r in rows):
            break
        time.sleep(0.05)
    assert any(r["route"] == "/v1/chat/completions" and r["status"] == 200 for r in rows)
    assert "TEMOIN-SECRET" not in json.dumps(rows) and key not in json.dumps(rows)
    summary = node.ui.get("/api/gateway/apps", headers=UI).json()[0]["usage"]
    assert summary["today"] >= 1


def test_rate_limited_with_retry_after(node, monkeypatch):
    key = node.connect()
    monkeypatch.setattr(limits, "PER_MINUTE", 2)
    limits.forget()
    codes = [node.call("GET", "/v1/models", key=key).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 429
    last = node.call("GET", "/v1/models", key=key)
    assert last.json()["error"]["code"] == "rate_limited" and int(last.headers["Retry-After"]) >= 1


def test_a_key_bound_to_its_program(node, monkeypatch):
    key = node.connect(pin=True)
    other = caller.Caller(9, r"C:\Windows\System32\curl.exe", True)
    monkeypatch.setattr(caller, "identify", lambda scope: other)
    refused = node.call("GET", "/v1/models", key=key)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "program_mismatch"


def test_open_a_page_once_every_ten_seconds(node):
    key = node.connect()
    first = node.call("POST", "/aiodos/v1/open", key=key, json={"page": "models"})
    assert first.status_code == 202
    again = node.call("POST", "/aiodos/v1/open", key=key, json={"page": "models"})
    assert again.status_code == 429


# ----------------------------------------------------------------- contrat
def _shape(example, actual, path=""):
    """Chaque clé de l'exemple existe dans la réponse, du même type (null accepté)."""
    if isinstance(example, dict):
        assert isinstance(actual, dict), path
        for key, value in example.items():
            assert key in actual, f"{path}.{key} absent"
            _shape(value, actual[key], f"{path}.{key}")
    elif isinstance(example, list):
        assert isinstance(actual, list), path
        if example and actual:
            _shape(example[0], actual[0], path + "[0]")
    elif example is not None and actual is not None:
        number = (int, float)
        same = (isinstance(example, bool) == isinstance(actual, bool)) and (
            isinstance(example, number) and isinstance(actual, number)
            or type(example) is type(actual))
        assert same, f"{path} : {type(actual).__name__} au lieu de {type(example).__name__}"


def _dig(data, dotted):
    for part in dotted.split("."):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


@pytest.mark.parametrize("name", ["info", "access", "models", "errors", "embed"])
def test_the_gateway_answers_like_the_contract(node, name, monkeypatch):
    key = node.connect()
    revoked = node.connect({**REQUEST, "client_id": "arkane.autre"})
    node.ui.delete(f"/api/gateway/apps/{apps.key_id(revoked)}", headers=UI)
    chat_only = node.connect({**REQUEST, "client_id": "arkane.texte", "scope": "chat"})
    limits.forget()
    for case in json.loads((CONTRACT / f"{name}.json").read_text(encoding="utf-8"))["interactions"]:
        if case.get("server") == "client":
            continue
        request = case["request"]
        use = {"info avec une clé retirée": revoked, "clé retirée": revoked,
               "embeddings sans le droit": chat_only}.get(case["name"], key)
        if case["name"] == "lieu plus large que l'accord":
            use = node.connect({**REQUEST, "client_id": "arkane.ici", "where": ["here", "mine"]})
        path = request["path"].replace("{key_id}", apps.key_id(use)).replace(
            "{challenge}", info.b64url(secrets.token_bytes(32)))
        body = request.get("json")
        if isinstance(body, dict) and body.get("device_code") == "{device_code}":
            asked = node.call("POST", "/aiodos/v1/access/requests",
                              json={**REQUEST, "client_id": "arkane.contrat" + secrets.token_hex(2)}).json()
            body = {**body, "device_code": asked["device_code"]}
            if case["name"] == "relève : accordé":
                node.approve(asked["request_id"])
            elif case["name"] == "relève : refusé":
                node.ui.post(f"/api/gateway/requests/{asked['request_id']}/deny", json={},
                             headers=UI)
        if case["name"] == "demande refusée : AIODOS n'accepte pas de nouvelle application":
            settings.set_value("gateway_requests", False)
        headers = {k: v.replace("{key}", use) for k, v in request.get("headers", {}).items()}
        reply = node.call(request["method"], path, json=body, headers=headers)
        settings.set_value("gateway_requests", True)
        expected = case["response"]
        assert reply.status_code == expected["status"], (case["name"], reply.text)
        for header, value in expected.get("headers", {}).items():
            assert reply.headers.get(header) == value, (case["name"], header)
        if expected.get("json") is not None:
            _shape(expected["json"], reply.json(), case["name"])
            for dotted in case.get("exact", []):
                assert _dig(reply.json(), dotted) == _dig(expected["json"], dotted), \
                    (case["name"], dotted)
        access_flow.forget()                     # pas de sourdine d'un cas à l'autre
        limits.forget()


def test_every_route_declares_a_right_or_is_public(node):
    """Méta-essai : chaque route montée sur la passerelle est publique (liste exacte), exige
    un droit, ou se contente d'une clé valide — rien n'échappe à Guard."""
    from aiodos.gateway import scopes
    gateway = gateway_server.create_gateway_app(node.ui.app.app.state, node.port).app
    publics = set()
    for route in gateway.routes:
        for method in getattr(route, "methods", None) or ():
            if scopes.required(method, route.path) == scopes.PUBLIC:
                publics.add((method, route.path))
    assert publics == {(m, p) for m, p in scopes.PUBLIC_ROUTES if (m, p) in publics}
    assert ("POST", "/aiodos/v1/access/revoke") not in publics
    assert ("POST", "/v1/chat/completions") not in publics


def test_the_helper_answers_one_json_line(tmp_path, monkeypatch):
    """Le programme d'aide : une ligne JSON lue, une ligne écrite, sans fenêtre ni journal."""
    import io
    monkeypatch.setattr(app_connect.instance, "running", lambda: True)
    monkeypatch.setattr(app_connect, "_lock", lambda: {"port": 1, "show_key": "k"})
    sent = []

    class Reply:
        ok, status_code = True, 200

        @staticmethod
        def json():
            return {"device_code": "d", "user_code": "123 456", "interval": 2,
                    "expires_in": 600, "request_id": "req_x", "url": "http://127.0.0.1:11435"}
    monkeypatch.setattr(requests, "post", lambda url, **kw: sent.append((url, kw)) or Reply())
    out = io.StringIO()
    code = app_connect.run_helper(io.StringIO(json.dumps(REQUEST) + "\n"), out)
    assert code == 0 and json.loads(out.getvalue())["user_code"] == "123 456"
    assert sent[0][0] == "http://127.0.0.1:1/instance/app-request"
    assert sent[0][1]["headers"] == {"X-Aiodos-Show": "k"} and sent[0][1]["json"]["request"] == REQUEST
    bad = io.StringIO()
    assert app_connect.run_helper(io.StringIO("pas du json\n"), bad) == 1
    assert json.loads(bad.getvalue())["error"]["code"] == "invalid_request"


def test_the_instance_route_needs_its_key(node):
    refused = node.ui.post("/instance/app-request", json={"request": REQUEST, "pid": 1},
                           headers={"X-Aiodos-Show": "fausse"})
    assert refused.status_code == 403
    ok = node.ui.post("/instance/app-request", json={"request": REQUEST, "pid": 1},
                      headers={"X-Aiodos-Show": SHOW})
    assert ok.status_code == 200 and ok.json()["user_code"]


@pytest.mark.skipif(sys.platform != "win32", reason="table TCP de Windows")
def test_the_real_caller_is_this_process():
    import os
    import socket
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname()) as conn:
            accepted, addr = server.accept()
            with accepted:
                who = caller.identify({"client": addr, "server": server.getsockname()})
    assert who.pid == os.getpid() and who.same_account is True
    assert who.program and Path(who.program).name.lower().startswith("python")
    assert who.generic
