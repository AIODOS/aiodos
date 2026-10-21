"""Les travaux ComfyUI par la passerelle (docs/conception/partage-puissance.md § 6.4 à 6.8),
contre un faux ComfyUI : droit engines:comfyui, entrées par empreinte, travail déposé, suivi
en direct, commandes, fichiers récupérés par plages, et rien d'une appli visible d'une autre.
Le nœud tourne dans le processus de l'essai ; son exécuteur calcule les unités."""
import hashlib
import json
import time

import pytest
import requests
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.engines import comfyui
from aiodos.gateway import access_flow, caller, file_store
from aiodos.jobs.kinds import engine
from aiodos.knowledge import store
from tests.comfy_stub import ComfyStub, png
from tests.nodes import free_port
from tests.ollama_stub import OllamaStub
from tests.test_gateway_apps import ME, REQUEST, SHOW, TOKEN, UI, Node

GRAPH = {
    "4": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "Une cabane", "clip": ["4", 1]}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "@in:ref0"}},
    "3": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": "@seed", "steps": 20,
                                               "cfg": 7.0, "positive": ["6", 0],
                                               "negative": ["6", 0], "latent_image": ["10", 0]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "mood"}},
}
ENGINES = {**REQUEST, "client_id": "arkane.pandora", "scope": "chat engines:comfyui"}


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setattr(caller, "of_pid", lambda pid: ME)
    monkeypatch.setattr(caller, "identify", lambda scope: ME)
    monkeypatch.setattr(comfyui, "_model_roots", lambda argv: [tmp_path / "modeles"])
    comfyui.forget_inventory()
    engine.forget_here()
    engine.forget_params()
    with OllamaStub(["petit:1b"]) as ollama, ComfyStub(tmp_path / "comfy", delay=0.3) as comfy:
        port = free_port()
        settings.set_many({"ollama_url": ollama.url, "gateway_port": port,
                           "llm_engine": "ollama", "engines_comfyui_enabled": True,
                           "engines_comfyui_url": comfy.url})
        store.ensure_defaults()
        with TestClient(create_app(TOKEN, {"testserver"}, show_key=SHOW)) as ui:
            access_flow._hooks["approval_ui"] = True
            found = Node(ui, port, ollama)
            found.comfy = found_comfy = comfy
            assert found_comfy is comfy
            yield found
    comfyui.forget_inventory()
    engine.forget_here()


def put(node, key, data: bytes):
    sha = hashlib.sha256(data).hexdigest()
    return sha, node.call("PUT", f"/aiodos/v1/inputs/{sha}", key=key, data=data,
                          headers={"Content-Type": "image/png"})


def wait_for(node, key, job_id, until=("completed", "failed", "cancelled"), timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = node.call("GET", f"/aiodos/v1/jobs/{job_id}", key=key).json()
        if job["status"] in until:
            return job
        time.sleep(0.1)
    raise AssertionError(f"travail encore {job['status']}")


def test_a_comfyui_job_from_an_app(node):
    key = node.connect(ENGINES)
    info = node.call("GET", "/aiodos/v1/info", key=key).json()
    assert {"engines.comfyui", "jobs", "inputs", "files"} <= set(info["features"])
    engines = node.call("GET", "/aiodos/v1/engines", key=key).json()["engines"]
    assert engines[0]["here"]["version"] == "0.35.1"
    assert {"folder": "checkpoints", "name": "sd_xl_base_1.0.safetensors"}.items() <= \
        engines[0]["models"][0].items()
    data = png(4, 4)
    sha, reply = put(node, key, data)
    assert reply.status_code == 201 and reply.json() == {"sha256": sha, "size": len(data),
                                                          "type": "image/png"}
    assert put(node, key, data)[1].status_code == 200                    # déjà là
    assert node.call("HEAD", f"/aiodos/v1/inputs/{sha}", key=key).status_code == 200
    body = {"engine": "comfyui", "graph": GRAPH, "label": "Moods · Séq. 3",
            "client_ref": "pandora:mood:5f2c9a", "inputs": {"ref0": {"sha256": sha}},
            "items": [{"label": "Mood 1", "seed": 1201, "set": [["6", "text", "Salon doré"]]},
                      {"label": "Mood 2", "seed": 1202, "set": [["6", "text", "Néons"]]}]}
    plan = node.call("POST", "/aiodos/v1/engines/comfyui/plan", key=key, json=body).json()
    assert plan["ok"] and plan["units"] == 2 and plan["missing"] == []
    reply = node.call("POST", "/aiodos/v1/jobs", key=key, json=body)
    assert reply.status_code == 202, reply.text
    job = reply.json()
    assert reply.headers["Location"] == f"/aiodos/v1/jobs/{job['id']}"
    assert job["object"] == "aiodos.job" and job["label"] == "Moods · Séq. 3"
    again = node.call("POST", "/aiodos/v1/jobs", key=key, json=body)       # même client_ref
    assert again.status_code == 200 and again.json()["id"] == job["id"]
    done = wait_for(node, key, job["id"])
    assert done["status"] == "completed" and done["progress"]["done"] == 2
    assert done["progress"]["percent"] == 100
    [machine] = done["progress"]["machines"]                  # ce PC, compté une fois
    assert machine["place"] == "here" and machine["done"] == 2
    files = node.call("GET", f"/aiodos/v1/jobs/{job['id']}/files?unfetched=1", key=key).json()
    assert len(files["files"]) == 2 and all(f["type"] == "image/png" for f in files["files"])
    assert [u["files"] for u in done["units"]] == [[files["files"][0]["id"]],
                                                    [files["files"][1]["id"]]]
    first = files["files"][0]
    part = node.call("GET", f"/aiodos/v1/files/{first['id']}/content", key=key,
                     headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == part.content[:10]
    assert part.headers["Content-Range"].startswith("bytes 0-9/")
    assert part.headers["X-Aiodos-Keep-Until"] == "pending"
    whole = node.call("GET", f"/aiodos/v1/files/{first['id']}/content", key=key)
    assert whole.status_code == 200 and whole.content.startswith(b"\x89PNG")
    assert hashlib.sha256(whole.content).hexdigest() == first["sha256"]
    meta = node.call("GET", f"/aiodos/v1/files/{first['id']}", key=key).json()
    assert meta["fetched"] and meta["expires_at"]
    left = node.call("GET", f"/aiodos/v1/jobs/{job['id']}/files?unfetched=1", key=key).json()
    assert [f["id"] for f in left["files"]] == [files["files"][1]["id"]]
    listed = node.call("GET", "/aiodos/v1/jobs?client_ref=pandora:mood:5f2c9a", key=key).json()
    assert [j["id"] for j in listed["jobs"]] == [job["id"]]


def test_events_stream_until_the_end(node):
    key = node.connect(ENGINES)
    sha, _ = put(node, key, png())
    job = node.call("POST", "/aiodos/v1/jobs", key=key,
                    json={"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}}).json()
    seen = []
    with requests.get(f"{node.base}/aiodos/v1/jobs/{job['id']}/events", stream=True, timeout=30,
                      headers={"Authorization": f"Bearer {key}"}) as stream:
        kind = None
        for line in stream.iter_lines(decode_unicode=True):
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                seen.append((kind, json.loads(line[6:])))
                if kind == "end":
                    break
    kinds = [k for k, _ in seen]
    assert kinds[0] == "job" and kinds[-1] == "end" and "file" in kinds
    assert seen[-1][1]["status"] == "completed"


def test_cancel_stops_the_unit_in_comfyui(node):
    key = node.connect(ENGINES)
    node.comfy.delay = 5
    sha, _ = put(node, key, png())
    job = node.call("POST", "/aiodos/v1/jobs", key=key,
                    json={"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}}).json()
    deadline = time.time() + 10
    while not node.comfy.prompts and time.time() < deadline:
        time.sleep(0.05)
    reply = node.call("POST", f"/aiodos/v1/jobs/{job['id']}/cancel", key=key)
    assert reply.status_code == 202
    done = wait_for(node, key, job["id"], until=("cancelled",))
    assert done["cancelled_by"] == "app"
    deadline = time.time() + 10
    while not node.comfy.cancelled and time.time() < deadline:
        time.sleep(0.05)
    assert node.comfy.cancelled == [node.comfy.prompts[0]["prompt_id"]]
    assert node.call("POST", f"/aiodos/v1/jobs/{job['id']}/cancel", key=key).status_code == 409


def test_another_app_sees_nothing(node):
    key = node.connect(ENGINES)
    other = node.connect({**ENGINES, "client_id": "arkane.arklight"})
    sha, _ = put(node, key, png())
    job = node.call("POST", "/aiodos/v1/jobs", key=key,
                    json={"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}}).json()
    wait_for(node, key, job["id"])
    file_id = node.call("GET", f"/aiodos/v1/jobs/{job['id']}/files", key=key).json()["files"][0]["id"]
    assert node.call("HEAD", f"/aiodos/v1/inputs/{sha}", key=other).status_code == 404
    for method, path in (("GET", f"/aiodos/v1/jobs/{job['id']}"),
                         ("POST", f"/aiodos/v1/jobs/{job['id']}/cancel"),
                         ("GET", f"/aiodos/v1/files/{file_id}"),
                         ("GET", f"/aiodos/v1/files/{file_id}/content")):
        reply = node.call(method, path, key=other)
        assert reply.status_code == 404, path
    refused = node.call("POST", "/aiodos/v1/jobs", key=other,
                        json={"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "input_missing"
    assert node.call("GET", "/aiodos/v1/jobs", key=other).json()["jobs"] == []


def test_the_gateway_answers_like_the_jobs_contract(node):
    """clients/contract/jobs.json rejoué dans l'ordre : chaque réponse a la forme de
    l'exemple (mêmes clés, mêmes types ; « exact » : mêmes valeurs)."""
    from tests.test_gateway_apps import CONTRACT, _dig, _shape
    key = node.connect(ENGINES)
    chat_only = node.connect({**ENGINES, "client_id": "arkane.texte", "scope": "chat"})
    data = png()
    sha = hashlib.sha256(data).hexdigest()
    found = {"sha256": sha}

    def fill(value):
        if isinstance(value, str):
            for name, real in found.items():
                value = value.replace("{" + name + "}", str(real))
            return value
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value
    cases = json.loads((CONTRACT / "jobs.json").read_text(encoding="utf-8"))["interactions"]
    for case in cases:
        if case.get("server") == "client":
            continue
        request, expected = case["request"], case["response"]
        if case["name"] == "travail fini":
            wait_for(node, key, found["job_id"])
        if case["name"] == "un fichier":
            files = node.call("GET", f"/aiodos/v1/jobs/{found['job_id']}/files", key=key).json()
            found["file_id"] = files["files"][0]["id"]
        use = chat_only if case["name"] == "sans le droit engines:comfyui" else key
        headers = {k: v.replace("{key}", use) for k, v in request.get("headers", {}).items()}
        kwargs = {"headers": headers}
        if request.get("body") == "{png}":
            kwargs["data"] = data
        elif request.get("json") is not None:
            kwargs["json"] = fill(request["json"])
        reply = node.call(request["method"], fill(request["path"]), **kwargs)
        assert reply.status_code == expected["status"], (case["name"], reply.text)
        for header, value in expected.get("headers", {}).items():
            assert reply.headers.get(header) == value, (case["name"], header)
        if expected.get("json") is not None:
            _shape(expected["json"], reply.json(), case["name"])
            for dotted in case.get("exact", []):
                assert _dig(reply.json(), dotted) == _dig(expected["json"], dotted), \
                    (case["name"], dotted)
        if expected.get("body") == "{png:0-9}":
            assert reply.content == b"".join([b"\x89PNG", bytes([13, 10, 26, 10])]) + reply.content[8:10]
        if case["name"] == "travail déposé":
            found["job_id"] = reply.json()["id"]


def test_without_the_right_or_comfyui(node):
    chat_only = node.connect({**ENGINES, "client_id": "arkane.texte", "scope": "chat"})
    reply = node.call("POST", "/aiodos/v1/jobs", key=chat_only, json={"graph": GRAPH})
    assert reply.status_code == 403 and reply.json()["error"]["code"] == "scope_denied"
    assert reply.json()["error"]["aiodos"]["detail"]["scope"] == "engines:comfyui"
    key = node.connect(ENGINES)
    settings.set_value("engines_comfyui_enabled", False)        # ComfyUI pas accordé
    engine.forget_here()
    sha, _ = put(node, key, png())
    reply = node.call("POST", "/aiodos/v1/jobs", key=key,
                      json={"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}})
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "engine_unavailable"
    settings.set_value("engines_comfyui_enabled", True)
    engine.forget_here()
    absent = {**GRAPH, "4": {"class_type": "CheckpointLoaderSimple",
                             "inputs": {"ckpt_name": "absent.safetensors"}}}
    reply = node.call("POST", "/aiodos/v1/jobs", key=key,
                      json={"graph": absent, "inputs": {"ref0": {"sha256": sha}}})
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "model_unavailable"
    assert reply.json()["error"]["aiodos"]["detail"]["models"] == [
        {"folder": None, "name": "absent.safetensors"}]
    wrong = node.call("PUT", f"/aiodos/v1/inputs/{'0' * 64}", key=key, data=png(),
                      headers={"Content-Type": "image/png"})
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "input_hash_mismatch"
    assert db.query_one("SELECT COUNT(*) AS n FROM jobs")["n"] == 0
    assert file_store.unfetched_bytes("abcdefgh") == 0
