"""Les travaux ComfyUI d'une appli calculés par une autre de mes machines, de bout en bout,
sur ce seul PC (docs/conception/partage-puissance.md § 2.4, 3.5, 5.1, 5.2) : la machine A,
où tourne l'appli, n'a pas ComfyUI ; B l'a (le faux ComfyUI des essais), accordé, et calcule
pour mes machines. L'appli dépose un travail chez A : B le demande, reçoit l'ordre (sans
rien de l'appli), tire l'entrée par son empreinte, calcule, rend les fichiers ; A seule les
garde, et l'appli les récupère chez A."""
import hashlib
import time

import pytest
import requests

from tests.comfy_stub import ComfyStub, png
from tests.nodes import Node, free_port, pair

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


@pytest.fixture
def machines(tmp_path):
    gateway = free_port()
    with ComfyStub(tmp_path / "comfy", delay=0.3) as comfy:
        a = Node(tmp_path / "a", "PC portable").preset(gateway_apps=True,
                                                         gateway_port=gateway).start()
        b = Node(tmp_path / "b", "PC fixe").preset(
            engines_comfyui_enabled=True, engines_comfyui_url=comfy.url, share_enabled=True,
            share_when_idle=False, share_on_ac_only=False).start()
        try:
            yield a, b, comfy, f"http://127.0.0.1:{gateway}"
        finally:
            a.stop()
            b.stop()


def test_my_other_machine_runs_the_comfyui_job(machines):
    a, b, comfy, base = machines
    _, b_id = pair(a, b)
    key = a.api("POST", "/gateway/apps", {"name": "PANDORA", "scopes": ["engines:comfyui"],
                                          "where": ["here", "mine"]})["key"]
    headers = {"Authorization": f"Bearer {key}"}

    def call(method, path, **kw):
        return requests.request(method, base + path, headers={**headers, **kw.pop("headers", {})},
                                timeout=30, **kw)

    def b_announced():
        a.api("POST", f"/network/peers/{b_id}/refresh")
        engines = call("GET", "/aiodos/v1/engines").json()["engines"][0]
        return engines["machines"]["mine"] == 1 and engines
    engines = a.wait_for(b_announced, timeout=60, what="ComfyUI de B vu par A")
    assert engines["here"]["version"] is None                     # A n'a pas ComfyUI
    assert engines["models"][0]["where"] == {"here": False, "mine": ["PC fixe"],
                                             "friends": None, "public": None}
    data = png(4, 4)
    sha = hashlib.sha256(data).hexdigest()
    assert call("PUT", f"/aiodos/v1/inputs/{sha}", data=data,
                headers={"Content-Type": "image/png"}).status_code == 201
    body = {"graph": GRAPH, "label": "Moods", "inputs": {"ref0": {"sha256": sha}},
            "items": [{"label": "Mood 1", "seed": 1201, "set": [["6", "text", "Salon doré"]]},
                      {"label": "Mood 2", "seed": 1202, "set": [["6", "text", "Néons"]]}]}
    plan = call("POST", "/aiodos/v1/engines/comfyui/plan", json=body).json()
    assert plan["ok"] and plan["where_effective"] == ["mine"]
    reply = call("POST", "/aiodos/v1/jobs", json=body)
    assert reply.status_code == 202, reply.text
    job_id = reply.json()["id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        job = call("GET", f"/aiodos/v1/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.3)
    assert job["status"] == "completed", job
    assert {(f["machine"], f["place"]) for f in job["files"]} == {("PC fixe", "mine")}
    assert [m["name"] for m in job["progress"]["machines"]] == ["PC fixe"]
    for f in job["files"]:
        content = call("GET", f"/aiodos/v1/files/{f['id']}/content").content
        assert hashlib.sha256(content).hexdigest() == f["sha256"]
        assert content.startswith(b"\x89PNG")
    # B a calculé : son ComfyUI a reçu l'entrée tirée par empreinte et deux graphes
    assert len(comfy.prompts) == 2 and comfy.uploads[0][2] == data
    seeds = sorted(p["prompt"]["1"]["inputs"]["seed"] for p in comfy.prompts)
    assert seeds == [1201, 1202]
    # ... sans rien garder de l'appli ni des fichiers
    assert not list((b.data_dir / "gateway" / "out").glob("*/*/*")) \
        if (b.data_dir / "gateway" / "out").exists() else True
    assert b.api("GET", "/jobs")["jobs"] == []
