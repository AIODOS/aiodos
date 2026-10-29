"""Mes workflows de ComfyUI importés (engines/comfy_workflows.py) : le format API seulement, une
taille bornée, les paramètres repérés d'office (descriptions, graine, pas, guidage, taille,
durée, cadence), gardés dans le dossier de données, ajustables ; et leur import par l'API."""
import json

import pytest
from fastapi.testclient import TestClient

from aiodos.api.server import create_app
from aiodos.engines import comfy_graph, comfy_workflows

TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}

T2I = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "un phare, http://pas-une-adresse",
                                                     "clip": ["4", 1]},
          "_meta": {"title": "Positif"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "flou, texte", "clip": ["4", 1]}},
    "3": {"class_type": "KSampler", "inputs": {"seed": 156680208700286, "steps": 20, "cfg": 8.0,
                                               "sampler_name": "euler", "scheduler": "normal",
                                               "denoise": 1.0, "model": ["4", 0],
                                               "positive": ["10", 0], "negative": ["7", 0],
                                               "latent_image": ["5", 0]}},
    "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["6", 0]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
    "11": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},
}
VIDEO = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan.safetensors", "weight_dtype": "default"}},
    "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5.safetensors", "type": "wan"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "un renard", "clip": ["2", 0]}},
    "4": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": 832, "height": 480,
                                                              "length": 33, "batch_size": 1}},
    "5": {"class_type": "RandomNoise", "inputs": {"noise_seed": 5}},
    "6": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["3", 0],
                                                "negative": ["3", 0], "cfg": 6}},
    "7": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "simple",
                                                     "steps": 30, "denoise": 1.0}},
    "8": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": 16}},
    "9": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 0]}},
    "10": {"class_type": "SaveVideo", "inputs": {"video": ["8", 0], "filename_prefix": "video/x",
                                                 "format": "auto", "codec": "auto"}},
}


def raw(graph) -> bytes:
    return json.dumps(graph).encode("utf-8")


def params_of(workflow) -> dict:
    return {p["name"]: p for p in workflow["params"]}


def test_parameters_are_found_automatically():
    workflow = comfy_workflows.load(raw(T2I), "Mon SDXL  portrait")
    assert workflow["name"] == "Mon SDXL portrait" and workflow["kind"] == "image"
    found = params_of(workflow)
    # La description : en amont de l'entrée positive, à travers un nœud qui la transforme
    assert found["prompt"]["targets"] == ["6.text"]
    assert found["prompt"]["default"].startswith("un phare")
    assert found["negative"]["targets"] == ["7.text"]
    assert found["seed"]["targets"] == ["3.seed"]
    assert workflow["graph"]["3"]["inputs"]["seed"] == comfy_graph.TOKEN_SEED
    assert (found["steps"]["default"], found["cfg"]["default"]) == (20, 8.0)
    assert (found["width"]["default"], found["height"]["default"]) == (832, 1216)
    assert found["width"]["step"] == 8 and "frames" not in found
    assert ["CLIPTextEncode", "text"] in workflow["texts"]
    video = comfy_workflows.load(raw(VIDEO), "")
    found = params_of(video)
    assert video["kind"] == "video" and video["name"] == "Mon workflow"
    assert found["prompt"]["targets"] == ["3.text"] and "negative" not in found
    assert found["seed"]["targets"] == ["5.noise_seed"]
    assert found["steps"]["targets"] == ["7.steps"] and found["cfg"]["targets"] == ["6.cfg"]
    assert found["frames"]["targets"] == ["4.length"] and found["fps"]["targets"] == ["8.fps"]


@pytest.mark.parametrize("data, code", [
    (raw({"nodes": [], "links": []}), "graph_not_api"),
    (b"pas du json", "workflow_invalid"),
    (raw({**T2I, "12": {"class_type": "LoadImage", "inputs": {"image": "chat.png"}}}),
     "workflow_inputs"),
    (raw({k: v for k, v in T2I.items() if k not in ("9", "11")}), "workflow_invalid"),
    (raw({**T2I, "4": {"class_type": "CheckpointLoaderSimple",
                       "inputs": {"ckpt_name": "C:/ailleurs/x.safetensors"}}}), "forbidden_value"),
    (raw({**T2I, "4": {"class_type": "CheckpointLoaderSimple",
                       "inputs": {"ckpt_name": "@model:0"}}}), "workflow_invalid"),
    (b" " * (comfy_workflows.MAX_BYTES + 1), "workflow_too_big"),
], ids=["editor", "not_json", "load_image", "no_output", "path", "token", "too_big"])
def test_what_is_refused(data, code):
    with pytest.raises(comfy_workflows.WorkflowError) as caught:
        comfy_workflows.load(data, "x")
    assert caught.value.code == code


def test_the_body_of_a_prompt_request_is_accepted():
    workflow = comfy_workflows.load(raw({"prompt": T2I, "client_id": "x"}), "x")
    assert set(workflow["graph"]) == set(T2I)


def test_kept_listed_adjusted_and_deleted():
    added = comfy_workflows.add(comfy_workflows.load(raw(T2I), "Portrait"))
    assert (comfy_workflows.folder() / f"{added['id']}.json").is_file()
    listed = comfy_workflows.listing()
    assert [w["id"] for w in listed] == [added["id"]] and "graph" not in listed[0]
    renamed = comfy_workflows.update(added["id"], name="Portrait doux")
    assert renamed["name"] == "Portrait doux"
    params = [p for p in renamed["params"] if p["name"] != "negative"]
    assert [p["name"] for p in comfy_workflows.update(added["id"], params=params)["params"]] \
        == [p["name"] for p in params]
    wrong = params + [{"name": "lien", "type": "int", "min": 0, "max": 9, "default": 1,
                       "targets": ["3.model"]}]
    with pytest.raises(comfy_workflows.WorkflowError):
        comfy_workflows.update(added["id"], params=wrong)
    assert comfy_workflows.delete(added["id"]) and comfy_workflows.listing() == []
    for bad in ("../../x", "zz" * 6, ""):
        with pytest.raises(comfy_workflows.WorkflowError):
            comfy_workflows.get(bad)


def test_import_through_the_api_with_a_bounded_size():
    with TestClient(create_app(TOKEN, {"testserver"})) as client:
        reply = client.post("/api/engines/comfyui/workflows", content=raw(T2I),
                            headers={**AUTH, "Content-Type": "application/json",
                                     "X-File-Name": "mon%20workflow.json"})
        assert reply.status_code == 200, reply.text
        assert reply.json()["name"] == "mon workflow"
        assert {p["name"] for p in reply.json()["params"]} >= {"prompt", "seed", "width"}
        too_big = client.post("/api/engines/comfyui/workflows",
                              content=b" " * (comfy_workflows.MAX_BYTES + 10), headers=AUTH)
        assert too_big.status_code == 413
        editor = client.post("/api/engines/comfyui/workflows", headers=AUTH,
                             content=raw({"nodes": [], "links": []}))
        assert editor.status_code == 400 and "Exporter (API)" in editor.json()["detail"]
        listed = client.get("/api/engines/comfyui/workflows", headers=AUTH).json()
        assert len(listed) == 1
        wid = listed[0]["id"]
        assert client.patch(f"/api/engines/comfyui/workflows/{wid}", headers=AUTH,
                            json={"name": "Autre"}).json()["name"] == "Autre"
        assert client.delete(f"/api/engines/comfyui/workflows/{wid}", headers=AUTH).status_code == 200
        assert client.delete(f"/api/engines/comfyui/workflows/{wid}", headers=AUTH).status_code == 404
