"""Créer avec ComfyUI (jobs/kinds/comfy_create.py, api/routes/comfy_creations.py), contre un faux
ComfyUI (tests/comfy_stub.py) : une recette ou un de mes workflows, ses paramètres revérifiés,
une unité engine_unit par création, le graphe construit à partir de la recette (aucun nœud
ajouté), chaque sortie rangée dans la galerie comme une image de sd.cpp ; une vidéo (MP4) ;
une création rendue par une de mes machines ; les refus."""
import base64
import hashlib
import json
import threading

import pytest
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.routes import comfy_creations
from aiodos.api.server import create_app
from aiodos.engines import catalogue, comfy_download, comfy_workflows, comfyui
from aiodos.imaging import gallery
from aiodos.jobs import queue, registry
from aiodos.jobs.kinds import comfy_create, engine
from tests import comfy_stub
from tests.comfy_stub import ComfyStub, png
from tests.test_comfy_workflows import T2I

SDXL = "comfy-templates/image_sdxl_simple"
WAN = "comfy-templates/text_to_video_wan"
TOKEN = "jeton-de-test"
AUTH = {"X-Aiodos-Token": TOKEN}
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64

# Les nœuds de la recette vidéo, comme les définit ComfyUI 0.35.1 (le cœur : « nodes »,
# « comfy_extras.* »)
VIDEO_DEFS = {
    "UNETLoader": {"input": {"required": {"unet_name": [["wan2.1_t2v_1.3B_fp16.safetensors"]],
                                          "weight_dtype": [["default"]]}},
                   "output": ["MODEL"], "output_node": False, "python_module": "nodes"},
    "CLIPLoader": {"input": {"required": {"clip_name": [["umt5_xxl_fp8_e4m3fn_scaled.safetensors"]],
                                          "type": [["wan"]]}},
                   "output": ["CLIP"], "output_node": False, "python_module": "nodes"},
    "VAELoader": {"input": {"required": {"vae_name": [["wan_2.1_vae.safetensors"]]}},
                  "output": ["VAE"], "output_node": False, "python_module": "nodes"},
    "ModelSamplingSD3": {"input": {"required": {"model": ["MODEL"], "shift": ["FLOAT", {}]}},
                         "output": ["MODEL"], "output_node": False,
                         "python_module": "comfy_extras.nodes_model_advanced"},
    "EmptyHunyuanLatentVideo": {"input": {"required": {"width": ["INT", {}]}},
                                "output": ["LATENT"], "output_node": False,
                                "python_module": "comfy_extras.nodes_hunyuan"},
    "CreateVideo": {"input": {"required": {"images": ["IMAGE"], "fps": ["FLOAT", {}]}},
                    "output": ["VIDEO"], "output_node": False,
                    "python_module": "comfy_extras.nodes_video"},
    "SaveVideo": {"input": {"required": {"video": ["VIDEO"], "filename_prefix": ["STRING", {}]}},
                  "output": ["VIDEO"], "output_node": True,
                  "python_module": "comfy_extras.nodes_video"},
}
VIDEO_MODELS = {"diffusion_models": ["wan2.1_t2v_1.3B_fp16.safetensors"],
                "text_encoders": ["umt5_xxl_fp8_e4m3fn_scaled.safetensors"],
                "vae": ["wan_2.1_vae.safetensors"]}


class VideoStub(ComfyStub):
    """Le faux ComfyUI, qui sait aussi enregistrer une vidéo (SaveVideo : un petit MP4)."""

    def _compute(self, run_id, prompt, failing):
        done = super()._compute(run_id, prompt, failing)
        entry = self._history.get(run_id) or {}
        for node_id, node in prompt.items():
            if node["class_type"] != "SaveVideo" or done.get("status") != "completed":
                continue
            sub, _, stem = str(node["inputs"]["filename_prefix"]).rpartition("/")
            folder = self.output / sub
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{stem}_00001_.mp4"
            (folder / name).write_bytes(MP4)
            entry.setdefault("outputs", {})[node_id] = {
                "images": [{"filename": name, "subfolder": sub, "type": "output"}],
                "animated": [True]}
        return done


@pytest.fixture
def comfy(tmp_path, monkeypatch):
    monkeypatch.setattr(comfyui, "_model_roots", lambda argv: [tmp_path / "modeles"])
    for cls, spec in VIDEO_DEFS.items():
        monkeypatch.setitem(comfy_stub.DEFS, cls, spec)
    for folder, names in VIDEO_MODELS.items():
        monkeypatch.setitem(comfy_stub.MODELS, folder, names)
    comfyui.forget_inventory()
    engine.forget_here()
    engine.forget_params()
    catalogue.forget()
    with VideoStub(tmp_path / "comfy") as stub:
        settings.set_many({"engines_comfyui_enabled": True, "engines_comfyui_url": stub.url})
        yield stub
    comfyui.forget_inventory()
    engine.forget_here()


def run_all(limit=10):
    """Calcule les unités en file comme l'exécuteur de ce PC."""
    done = 0
    while done < limit:
        unit = queue.claim_next(["gpu"], "local")
        if unit is None:
            break
        payload = registry.payload_for(unit)
        try:
            result = registry.run_local(unit["task"], payload, threading.Event())
        except registry.Interrupted:
            queue.release_unit(unit["id"])
            continue
        except Exception as exc:
            job_id, finished = queue.fail_unit(unit["id"], str(exc), 0.1)
        else:
            job_id, finished = queue.finish_unit(unit["id"], result, 0.1)
        if finished:
            queue.finalize_job(job_id)
        done += 1
    return done


def submit(comfy, body):
    inventory = comfyui.inventory(comfy.url)
    params = comfy_create.normalize(body, inventory)
    job_id = queue.create_job(comfy_create.KIND, params, engine.priority_of(params),
                              reach=",".join(comfy_create.WHERE))
    return job_id, params


def classes(graph):
    return sorted(node["class_type"] for node in graph.values())


# ------------------------------------------------------------------ une recette
def test_a_recipe_becomes_creations_in_the_gallery(comfy):
    job_id, params = submit(comfy, {"recipe": SDXL, "count": 2, "params": {
        "prompt": "un phare sous l'orage", "width": 768, "seed": 41}})
    assert params["creation"]["model"] == f"comfyui/{SDXL}" and params["creation"]["seed"] == 41
    assert [i["seed"] for i in params["items"]] == [41, 42]
    assert params["models"] == {"0": {"folder": "checkpoints",
                                      "name": "sd_xl_base_1.0.safetensors", "size": None}}
    assert run_all() == 2
    job = queue.job_summary(job_id)
    assert job["status"] == "done" and job["result"]["images"] == 2
    assert job["title"].startswith("SDXL 1.0") and "×2" in job["title"]
    # Le graphe envoyé : celui de la recette, rien d'ajouté, les réglages posés
    recipe = catalogue.recipe(SDXL)
    sent = comfy.prompts[0]["prompt"]
    assert classes(sent) == classes(recipe["graph"])
    texts = [n["inputs"]["text"] for n in sent.values() if n["class_type"] == "CLIPTextEncode"]
    assert "un phare sous l'orage" in texts
    latent = next(n for n in sent.values() if n["class_type"] == "EmptyLatentImage")
    assert (latent["inputs"]["width"], latent["inputs"]["height"]) == (768, 1024)
    seeds = sorted(next(n for n in p["prompt"].values() if n["class_type"] == "KSampler")
                   ["inputs"]["seed"] for p in comfy.prompts)
    assert seeds == [41, 42]
    loader = next(n for n in sent.values() if n["class_type"] == "CheckpointLoaderSimple")
    assert loader["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    # La galerie, comme une image de sd.cpp
    rows = gallery.batch(params["creation"]["batch"])
    assert [r["seed"] for r in rows] == [41, 42]
    assert all(r["model"] == f"comfyui/{SDXL}" and r["prompt"] == "un phare sous l'orage"
               and r["media"] == "image" and r["file"].endswith(".png")
               and (r["width"], r["height"]) == (8, 8) and r["steps"] == 25 for r in rows)
    assert all(gallery.path_of(r).is_file() for r in rows)
    assert not any(engine.paths.engine_runs_dir().iterdir())     # dossiers de calcul vidés


def test_a_video_recipe_gives_an_mp4(comfy):
    job_id, params = submit(comfy, {"recipe": WAN, "params": {"prompt": "un renard",
                                                              "frames": 17, "fps": 12}})
    assert params["bounds"]["unit_seconds"] == 4 * 3600
    assert run_all() == 1
    assert queue.job_summary(job_id)["status"] == "done"
    sent = comfy.prompts[0]["prompt"]
    assert classes(sent) == classes(catalogue.recipe(WAN)["graph"])
    save = next(n for n in sent.values() if n["class_type"] == "SaveVideo")
    assert save["inputs"]["filename_prefix"].startswith("aiodos/")
    row = gallery.batch(params["creation"]["batch"])[0]
    assert row["media"] == "video" and row["file"].endswith(".mp4")
    assert (row["width"], row["height"], row["frames"], row["fps"]) == (832, 480, 17, 12)
    assert gallery.path_of(row).read_bytes() == MP4


def test_a_workflow_of_mine_becomes_a_creation(comfy, monkeypatch):
    # Le faux ComfyUI ne connaît pas ConditioningZeroOut : le même workflow, sans lui
    graph = {k: v for k, v in T2I.items() if k != "10"}
    graph["3"] = {**graph["3"], "inputs": {**graph["3"]["inputs"], "positive": ["6", 0]}}
    workflow = comfy_workflows.add(comfy_workflows.load(json.dumps(graph).encode(), "Portrait"))
    job_id, params = submit(comfy, {"workflow": workflow["id"], "params": {
        "prompt": "un chat roux", "width": 640}})
    assert params["models"]["0"]["name"] == "sd_xl_base_1.0.safetensors"
    assert run_all() == 1
    assert queue.job_summary(job_id)["status"] == "done"
    sent = comfy.prompts[0]["prompt"]
    assert "PreviewImage" not in classes(sent)                   # retiré, comme pour les applis
    assert classes(sent) == sorted(n["class_type"] for n in graph.values()
                                   if n["class_type"] != "PreviewImage")
    row = gallery.batch(params["creation"]["batch"])[0]
    assert row["model"] == f"comfyui/workflow/{workflow['id']}"
    assert row["prompt"] == "un chat roux"


def test_a_creation_made_by_one_of_my_machines(comfy):
    job_id, params = submit(comfy, {"recipe": SDXL, "params": {"seed": 5}})
    unit = queue.claim_next(["gpu"], "local")
    order = registry.payload_for(unit)
    data = png(16, 16)
    output = {"files": [{"name": "out_00001_.png", "node": order["outputs"][0], "size": len(data),
                         "sha256": hashlib.sha256(data).hexdigest(), "type": "image/png",
                         "data": base64.b64encode(data).decode("ascii")}],
              "machine": "PC du salon", "engine": "comfyui 0.35.1", "seconds": 4.2}
    engine.validate(order, output)
    result = engine.assimilate(order, output)
    row = gallery.get(result["image_id"])
    assert (row["machine"], row["seconds"], row["width"], row["seed"]) == ("PC du salon", 4.2, 16, 5)
    with pytest.raises(ValueError):                  # ni image ni vidéo : rien n'entre
        engine.assimilate(order, {**output, "files": [{**output["files"][0], "data":
                                                        base64.b64encode(b"fLaC....").decode()}]})


# ------------------------------------------------------------------ les refus
@pytest.mark.parametrize("body, code", [
    ({"recipe": "comfy-templates/inconnue"}, "recipe_unknown"),
    ({"recipe": SDXL, "workflow": "abcdefabcdef"}, "invalid_request"),
    ({}, "invalid_request"),
    ({"recipe": SDXL, "count": 17}, "invalid_request"),
    ({"recipe": WAN, "count": 5}, "invalid_request"),
    ({"recipe": SDXL, "params": {"width": 4096}}, "param_invalid"),
    ({"recipe": SDXL, "params": {"prompt": "x" * 3000}}, "param_invalid"),
    ({"recipe": SDXL, "graph": {}}, "invalid_request"),
    ({"workflow": "abcdefabcdef"}, "workflow_unknown"),
])
def test_what_is_refused(comfy, body, code):
    with pytest.raises(comfy_create.CreateError) as caught:
        comfy_create.normalize(body, comfyui.inventory(comfy.url))
    assert caught.value.code == code


def test_a_recipe_is_not_replayed_on_an_added_node(comfy, monkeypatch):
    monkeypatch.setitem(comfy_stub.DEFS, "KSampler", {**comfy_stub.DEFS["KSampler"],
                                                      "python_module": "custom_nodes.malin"})
    comfyui.forget_inventory()
    with pytest.raises(comfy_create.CreateError) as caught:
        comfy_create.normalize({"recipe": SDXL}, comfyui.inventory(comfy.url))
    assert caught.value.code == "contract" and caught.value.status == 409


def test_nothing_is_queued_where_nothing_can_compute(comfy, monkeypatch):
    monkeypatch.setitem(comfy_stub.MODELS, "checkpoints", [])
    comfyui.forget_inventory()
    engine.forget_here()
    with pytest.raises(comfy_create.CreateError) as caught:
        comfy_creations._create({"recipe": SDXL})
    assert caught.value.status == 409 and "télécharge" in str(caught.value)
    settings.set_many({"engines_comfyui_enabled": False})
    engine.forget_here()
    with pytest.raises(comfy_create.CreateError) as caught:
        comfy_creations._create({"recipe": SDXL})
    assert "permets-le" in str(caught.value)
    assert db.query_one("SELECT 1 FROM jobs") is None


# ------------------------------------------------------------------ l'API et la vue
def test_the_view_sees_comfyui_the_recipes_and_their_models(comfy, tmp_path, monkeypatch):
    shared = tmp_path / "partage"
    (shared / "checkpoints").mkdir(parents=True)
    monkeypatch.setattr(comfy_download, "roots", lambda: [shared])
    seen = comfy_creations.overview()
    assert seen["comfyui"]["state"] == "open" and seen["comfyui"]["granted"] is True
    assert seen["comfyui"]["install_url"] == "https://www.comfy.org/download"
    sdxl = next(r for r in seen["recipes"] if r["id"] == SDXL)
    assert sdxl["signed"] is False and sdxl["kind"] == "image" and "graph" not in sdxl
    model = sdxl["models"][0]
    # Pas dans le dossier, mais vu par ComfyUI (l'inventaire du faux ComfyUI) : présent
    assert model["state"] == "present" and model["license"] == "CreativeML Open RAIL++-M"
    wan = next(r for r in seen["recipes"] if r["id"] == WAN)
    encoder = next(m for m in wan["models"] if m["key"] == "text_encoder")
    assert encoder["pinned"] is False                       # empreinte à déclarer par l'auteur
    settings.set_many({"engines_comfyui_enabled": False})
    engine.forget_here()
    hidden = next(r for r in comfy_creations.overview()["recipes"] if r["id"] == SDXL)
    assert hidden["models"][0]["state"] == "missing"        # épinglé : téléchargeable sur clic
    (shared / "checkpoints" / "sd_xl_base_1.0.safetensors").write_bytes(b"x")
    assert comfy_download.state(hidden["models"][0]) == "missing"    # pas la bonne taille


def test_a_download_is_refused_without_a_fingerprint_or_without_comfyui(monkeypatch):
    wan = catalogue.recipe(WAN)
    monkeypatch.setattr(comfy_download, "roots", lambda: [])
    with pytest.raises(comfy_download.DownloadError) as caught:
        comfy_download.start(wan, "text_encoder")
    assert "empreinte" in str(caught.value)
    with pytest.raises(comfy_download.DownloadError) as caught:
        comfy_download.start(wan, "vae")
    assert "ComfyUI n'est pas installé" in str(caught.value)
    assert comfy_download.state(wan["models"][2]) == "no_comfyui"
    assert comfy_download.progress()["running"] is False


def test_create_through_the_api(comfy):
    with TestClient(create_app(TOKEN, {"testserver"})) as client:
        listed = client.get("/api/engines/comfyui/recipes", headers=AUTH).json()
        assert {r["id"] for r in listed["recipes"]} == {SDXL, WAN}
        reply = client.post("/api/engines/comfyui/create", headers=AUTH,
                            json={"recipe": SDXL, "params": {"prompt": "une forêt"}, "count": 1})
        assert reply.status_code == 200, reply.text
        made = reply.json()
        assert made["here"] is True and made["count"] == 1
        job = queue.job_summary(made["job_id"])
        assert job["kind"] == "comfy_create" and job["params"]["creation"]["prompt"] == "une forêt"
        refused = client.post("/api/engines/comfyui/create", headers=AUTH,
                              json={"recipe": SDXL, "params": {"steps": 0}})
        assert refused.status_code == 400 and "steps" in refused.json()["detail"]
        assert client.post("/api/engines/comfyui/create", headers=AUTH,
                           json={"recipe": "x/y"}).status_code == 404
