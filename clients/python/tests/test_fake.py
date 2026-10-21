# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Le faux AIODOS sert le contrat : ses réponses ont la forme des exemples de
clients/contract/ (la règle de son README : mêmes clés, mêmes types JSON, chemins `exact`
égaux, en-têtes présents), comme la vraie passerelle. Pour les travaux ComfyUI, jobs.json
rejoué dans l'ordre, cas « client » compris (le faux les produit à la demande)."""

import hashlib
import http.client
import json
import subprocess
import sys
import threading

import pytest

from aiodos_client import _proof
from aiodos_client.fake import FakeAiodos, png
from support import COMFY, assert_shape, at_path, fill, load_contract


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


# --- Les travaux ComfyUI (jobs.json) -----------------------------------------------------------
def _call(fake, method, path, key, body=None, raw=None, headers=None):
    sent = {"Authorization": f"Bearer {key}", **(headers or {})}
    payload = raw
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        sent["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
    try:
        conn.request(method, path, body=payload, headers=sent)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


def _events(fake, path, key):
    """Les événements d'un flux SSE : [(nom, données)], jusqu'à « end »."""
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {key}"})
        response = conn.getresponse()
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/event-stream"
        found, name = [], None
        while True:
            line = response.fp.readline().decode("utf-8")
            if not line:
                return found
            line = line.rstrip("\r\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                found.append((name, json.loads(line[6:])))
                if name == "end":
                    return found
    finally:
        conn.close()


def test_jobs_contract_in_order():
    """jobs.json rejoué dans l'ordre contre le faux AIODOS, comme
    tests/test_gateway_engines.py le fait contre la vraie passerelle."""
    cases = load_contract("jobs.json")["interactions"]
    examples = {item["name"]: item for item in cases}
    data = png()
    found = {"sha256": hashlib.sha256(data).hexdigest()}
    with FakeAiodos(job_delay=None) as fake:
        key = fake.add_key(scopes=COMFY)
        chat_only = fake.add_key(scopes=("chat",))
        for case in cases:
            name, request, expected = case["name"], case["request"], case["response"]
            if name == "travail en cours":
                fake.advance(found["job_id"], steps=2)  # Mood 1 fini, Mood 2 en cours
            if name == "travail fini":
                fake.advance(found["job_id"])
            if name == "pause après l'unité en cours":   # un autre travail, Mood 1 en cours
                body = fill(examples["travail déposé"]["request"]["json"], **found)
                status, _, raw = _call(fake, "POST", "/aiodos/v1/jobs", key,
                                       body={**body, "client_ref": "pandora:mood:pause"})
                found["job_id"] = json.loads(raw)["id"]
                fake.advance(found["job_id"])
            if name == "annulation d'un travail fini":
                found["job_id"] = found["first_job"]
            if name == "événements jusqu'à la fin":
                _check_events(fake, examples, key, found)
                continue
            body = fill(request.get("json"), **found) if request.get("json") is not None else None
            raw = data if request.get("body") == "{png}" else None
            headers = {k: v for k, v in request.get("headers", {}).items()
                       if k not in ("Authorization", "Content-Type")}
            if raw is not None:
                headers["Content-Type"] = request["headers"]["Content-Type"]
            use = chat_only if name == "sans le droit engines:comfyui" else key
            status, reply_headers, reply = _call(fake, request["method"],
                                                 fill(request["path"], **found), use,
                                                 body=body, raw=raw, headers=headers)
            assert status == expected["status"], (name, reply)
            for header, value in expected["headers"].items():
                assert reply_headers.get(header) == value, (name, header)
            if expected.get("json") is not None:
                actual = json.loads(reply)
                assert_shape(expected["json"], actual, name)
                for dotted in case.get("exact", []):
                    assert at_path(actual, dotted) == at_path(expected["json"], dotted), \
                        (name, dotted)
            if expected.get("body") == "{png:0-9}":
                assert reply == fake._files[found["file_id"]]["data"][:10]
                assert reply.startswith(b"\x89PNG\r\n\x1a\n")
                assert reply_headers["Content-Range"].startswith("bytes 0-9/")
            if name == "travail déposé":
                found["job_id"] = found["first_job"] = json.loads(reply)["id"]
            if name == "fichiers pas encore récupérés":
                found["file_id"] = json.loads(reply)["files"][0]["id"]


def _check_events(fake, examples, key, found):
    """Un flux suivi en direct : « job » d'abord, des « file », « end » enfin ; chaque
    événement a la forme de son exemple."""
    sse = examples["événements jusqu'à la fin"]["response"]["sse"]
    shapes, name = {}, None
    for line in sse:
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            shapes.setdefault(name, []).append(json.loads(line[6:]))
    body = fill(examples["travail déposé"]["request"]["json"], **found)
    _, _, raw = _call(fake, "POST", "/aiodos/v1/jobs", key,
                      body={**body, "client_ref": "pandora:mood:events"})
    job_id = json.loads(raw)["id"]
    stepper = threading.Timer(0.3, fake.advance, kwargs={"job_id": job_id, "steps": 3})
    stepper.start()
    try:
        events = _events(fake, f"/aiodos/v1/jobs/{job_id}/events", key)
    finally:
        stepper.join()
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "job" and kinds[-1] == "end" and kinds.count("file") == 2
    assert events[-1][1] == {"id": job_id, "status": "completed"}
    by_status = {example["status"]: example for example in shapes["job"]}
    for kind, data in events:
        if kind == "job" and data["status"] in by_status:
            assert_shape(by_status[data["status"]], data, f"job {data['status']}")
        elif kind != "job":
            assert_shape(shapes[kind][0], data, kind)
