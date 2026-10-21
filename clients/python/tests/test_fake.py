# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Le faux AIODOS sert le contrat : ses réponses ont la forme des exemples de
clients/contract/ (la règle de son README : mêmes clés, mêmes types JSON, chemins `exact`
égaux, en-têtes présents), comme la vraie passerelle."""

import http.client
import json
import subprocess
import sys

import pytest

from aiodos_client import _proof
from aiodos_client.fake import FakeAiodos
from support import assert_shape, at_path, load_contract


def _send(fake, request, key=None, device_code=None, key_id=None, challenge=None):
    path = request["path"].replace("{key_id}", key_id or "").replace("{challenge}",
                                                                     challenge or "")
    headers = {name: value.replace("{key}", key or "")
               for name, value in request["headers"].items()}
    body = request.get("json")
    if body is not None:
        body = json.loads(json.dumps(body).replace("{device_code}", device_code or ""))
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
    try:
        conn.request(request["method"], path,
                     body=json.dumps(body).encode("utf-8") if body is not None else None,
                     headers=headers)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


def _check(item, status, headers, raw):
    expected = item["response"]
    assert status == expected["status"], item["name"]
    for name, value in expected["headers"].items():
        assert headers.get(name) == value, f"{item['name']} : en-tête {name}"
    if "sse" in expected:
        lines = [line for line in raw.decode("utf-8").split("\n\n") if line]
        assert len(lines) == len(expected["sse"]), item["name"]
        for mine, theirs in zip(lines, expected["sse"]):
            if theirs.startswith("data: {"):
                assert_shape(json.loads(theirs[6:]), json.loads(mine[6:]), item["name"])
            else:
                assert mine.split(" ")[:3] == theirs.split(" ")[:3]
        return lines
    actual = json.loads(raw.decode("utf-8"))
    assert_shape(expected["json"], actual, item["name"])
    for path in item.get("exact", []):
        assert at_path(actual, path) == at_path(expected["json"], path), f"{item['name']} {path}"
    return actual


def _request(fake, key=None, scope="chat embed"):
    body = {"client_name": "PANDORA", "client_id": "arkane.pandora", "scope": scope}
    status, _, raw = _send(fake, {"method": "POST", "path": "/aiodos/v1/access/requests",
                                  "headers": {"Authorization": "Bearer {key}"} if key else {},
                                  "json": body}, key=key)
    assert status == 200
    return json.loads(raw)["device_code"]


def _items(name):
    return [pytest.param(item, id=item["name"]) for item in load_contract(name)["interactions"]]


@pytest.mark.parametrize("item", _items("info.json"))
def test_info(item):
    if item.get("server") == "client":
        pytest.skip("réponse d'un autre programme (modes old et ollama du faux AIODOS)")
    with FakeAiodos() as fake:
        key = fake.add_key()
        if "retirée" in item["name"]:
            fake.revoke(key)
        actual = _check(item, *_send(fake, item["request"], key=key, key_id=key[11:19],
                                     challenge=_proof.challenge()))
        if "défi" in item["name"]:
            assert isinstance(actual["proof"], str) and len(actual["proof"]) == 43


@pytest.mark.parametrize("item", _items("access.json"))
def test_access(item):
    name = item["name"]
    with FakeAiodos() as fake:
        key = fake.add_key()
        device_code = None
        if name.startswith("relève") and "inconnu" not in name:
            device_code = _request(fake, key=key if "droits en plus" in name else None)
            if "accordé" in name:
                fake.approve()
            elif "refusé" in name:
                fake.deny()
            elif "expirée" in name:
                fake.expire()
            elif "trop souvent" in name:
                fake.slow_down(1)
        if "nouvelle application" in name:
            fake.requests = False
        _check(item, *_send(fake, item["request"], key=key, device_code=device_code))


@pytest.mark.parametrize("item", _items("models.json") + _items("embed.json"))
def test_models_and_embeddings(item):
    with FakeAiodos() as fake:
        key = fake.add_key(scopes=("chat",) if "sans le droit" in item["name"]
                           else ("chat", "embed"))
        _check(item, *_send(fake, item["request"], key=key))


@pytest.mark.parametrize("item", _items("errors.json"))
def test_errors(item):
    name = item["name"]
    with FakeAiodos() as fake:
        key = fake.add_key()
        if "retirée" in name:
            fake.revoke(key)
        expected = item["response"]
        if item.get("server") == "client":          # à la demande, comme le ferait un essai
            body = expected["json"]
            error = body["error"]
            if isinstance(error, dict):
                fake.fail_next(error["code"], status=expected["status"],
                               message=error["message"], hint=error["aiodos"]["hint"],
                               retryable=error["aiodos"]["retryable"],
                               detail=error["aiodos"]["detail"], param=error["param"],
                               retry_after=expected["headers"].get("Retry-After"))
            else:
                fake.fail_next(body["code"], status=expected["status"], message=error,
                               hint=body["hint"])
        _check(item, *_send(fake, item["request"], key=key))


@pytest.mark.parametrize("item", _items("chat.json"))
def test_chat(item):
    name = item["name"]
    if "coupée" in name or "panne" in name:
        pytest.skip("cas que le faux ne produit que sur demande (chat_script)")
    with FakeAiodos() as fake:
        key = fake.add_key()
        request = item["request"]
        if "autre machine" in name:
            error = item["response"]["json"]["error"]
            fake.fail_next("tools_unavailable", message=error["message"], param="tools",
                           hint=error["aiodos"]["hint"])
        lines = _check(item, *_send(fake, request, key=key))
        if name == "conversation en flux : chargement, réflexion, texte":
            assert lines == item["response"]["sse"]          # le même flux, ligne à ligne


def test_discovery_file_has_the_contract_shape():
    with FakeAiodos() as fake:
        with open(fake.discovery_file, encoding="utf-8") as handle:
            written = json.load(handle)
        assert_shape(load_contract("discovery.json")["files"]["open"], written)
        assert written["gateway"]["url"] == fake.url


def test_helper_answers_like_the_contract(tmp_path):
    contract = load_contract("discovery.json")["app_request"]
    with FakeAiodos() as fake:
        home = fake.helper_home()
        done = subprocess.run([sys.executable, "-m", "aiodos", "--app-request"], cwd=home,
                              input=json.dumps(contract["stdin"]) + "\n", capture_output=True,
                              text=True, timeout=30)
        assert done.returncode == contract["exit_ok"]
        assert_shape(contract["stdout_ok"], json.loads(done.stdout.strip().splitlines()[-1]))
        assert fake.helper_stdin(home) == contract["stdin"]
        home = fake.helper_home(error=contract["stdout_error"]["error"])
        done = subprocess.run([sys.executable, "-m", "aiodos", "--app-request"], cwd=home,
                              input="{}\n", capture_output=True, text=True, timeout=30)
        assert done.returncode == contract["exit_error"]
        assert json.loads(done.stdout.strip().splitlines()[-1]) == contract["stdout_error"]
