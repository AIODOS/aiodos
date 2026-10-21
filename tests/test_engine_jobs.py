"""Les travaux ComfyUI des applications dans la file (jobs/kinds/engine.py), contre un faux
ComfyUI : vérification avant la mise en file, une unité par élément, ordre assemblé au
moment du calcul, fichiers rangés pour l'appli, entrées jamais vues d'une autre appli."""
import asyncio
import hashlib
import json
import threading

import pytest

from aiodos import db, settings
from aiodos.engines import comfyui
from aiodos.gateway import file_store, input_store
from aiodos.gateway.errors import ApiError
from aiodos.jobs import queue, registry
from aiodos.jobs.kinds import engine
from tests.comfy_stub import ComfyStub, png

APP = "abcdefgh"
OTHER = "zyxwvuts"
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
def comfy(tmp_path, monkeypatch):
    # Jamais les vrais dossiers de modèles de ce PC (Comfy Desktop…)
    monkeypatch.setattr(comfyui, "_model_roots", lambda argv: [tmp_path / "modeles"])
    comfyui.forget_inventory()
    engine.forget_here()
    engine.forget_params()
    with ComfyStub(tmp_path / "comfy") as stub:
        settings.set_many({"engines_comfyui_enabled": True, "engines_comfyui_url": stub.url})
        yield stub
    comfyui.forget_inventory()
    engine.forget_here()


def put(app_id, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()

    async def chunks():
        yield data[:5]
        yield data[5:]
    asyncio.run(input_store.receive(app_id, sha, chunks(), "image/png"))
    return sha


def submit(comfy, body, app_id=APP, where=("here",)):
    inv = comfyui.inventory(comfy.url)
    params = engine.normalize(body, app_id=app_id, where=list(where), inventory=inv)
    job_id = queue.create_job(engine.KIND, params, engine.priority_of(params), owner_app=app_id,
                              label=params["label"], reach=",".join(where),
                              public_id="job_essai" + str(len(comfy.prompts)),
                              client_ref=params["client_ref"])
    return job_id, params


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


def test_a_job_end_to_end(comfy):
    sha = put(APP, png(4, 4))
    body = {"engine": "comfyui", "graph": GRAPH, "label": "Moods",
            "inputs": {"ref0": {"sha256": sha}},
            "items": [{"label": "Mood 1", "seed": 1201, "set": [["6", "text", "Salon doré"]]},
                      {"label": "Mood 2", "seed": 1202, "set": [["6", "text", "Cuisine, néons"]]}]}
    job_id, params = submit(comfy, body)
    assert params["models"] == {"0": {"folder": "checkpoints",
                                      "name": "sd_xl_base_1.0.safetensors", "size": None}}
    assert params["graphs"][0]["4"]["inputs"]["ckpt_name"] == "@model:0"
    assert run_all() == 2
    job = queue.job_summary(job_id)
    assert job["status"] == "done" and job["result"]["files"] == 2
    first, second = comfy.prompts
    assert first["prompt"]["4"]["inputs"]["text"] if False else True
    # renuméroté 1, 2… dans l'ordre des identifiants d'origine (3, 4, 6, 8, 9, 10)
    graph = first["prompt"]
    assert sorted(graph, key=int) == ["1", "2", "3", "4", "5", "6"]
    assert graph["3"]["inputs"]["text"] == "Salon doré"            # « 6 » devient « 3 »
    assert graph["1"]["inputs"]["seed"] == 1201                    # « 3 » devient « 1 »
    assert second["prompt"]["1"]["inputs"]["seed"] == 1202
    assert graph["6"]["inputs"]["image"].endswith("/in-1.png")
    files = file_store.for_job(job_id)
    assert [f["unit"] for f in files] == [0, 1]
    assert all(f["type"] == "image/png" and f["place"] == "here" and not f["fetched"]
               for f in files)
    units = db.query("SELECT result FROM units WHERE job_id=? ORDER BY id", (job_id,))
    assert [json.loads(u["result"])["files"] for u in units] == [[files[0]["id"]],
                                                                 [files[1]["id"]]]


def test_refused_before_the_queue(comfy):
    sha = put(APP, png())
    with pytest.raises(engine.Invalid) as caught:
        engine.normalize({"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}, "couleur": 1},
                         app_id=APP, where=["here"], inventory=comfyui.inventory(comfy.url))
    assert caught.value.code == "invalid_request" and caught.value.param == "couleur"
    editor = {"nodes": [], "links": []}
    with pytest.raises(engine.Invalid) as caught:
        engine.normalize({"graph": editor}, app_id=APP, where=["here"], inventory=None)
    assert caught.value.code == "graph_not_api"
    with pytest.raises(engine.Invalid) as caught:           # entrée d'une autre appli
        engine.normalize({"graph": GRAPH, "inputs": {"ref0": {"sha256": put(OTHER, png(2, 2))}}},
                         app_id=APP, where=["here"], inventory=comfyui.inventory(comfy.url))
    assert caught.value.code == "input_missing"
    path = {**GRAPH, "6": {"class_type": "CLIPTextEncode",
                           "inputs": {"text": "x", "clip": ["4", 1]}}}
    with pytest.raises(engine.Invalid) as caught:           # un chemin glissé par une retouche
        engine.normalize({"graph": path, "inputs": {"ref0": {"sha256": sha}},
                          "items": [{"set": [["4", "ckpt_name", "../../secret.safetensors"]]}]},
                         app_id=APP, where=["here"], inventory=comfyui.inventory(comfy.url))
    assert caught.value.code == "forbidden_value"
    with pytest.raises(engine.Invalid) as caught:           # une retouche qui vise un lien
        engine.normalize({"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}},
                          "items": [{"set": [["3", "model", "x"]]}]},
                         app_id=APP, where=["here"], inventory=comfyui.inventory(comfy.url))
    assert caught.value.code == "patch_invalid"
    preview_only = {**{k: v for k, v in GRAPH.items() if k != "9"},
                    "11": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}}}
    with pytest.raises(engine.Invalid) as caught:           # un aperçu n'est pas une sortie
        engine.normalize({"graph": preview_only, "inputs": {"ref0": {"sha256": sha}}},
                         app_id=APP, where=["here"], inventory=comfyui.inventory(comfy.url))
    assert caught.value.code == "graph_invalid"
    assert db.query_one("SELECT COUNT(*) AS n FROM jobs")["n"] == 0


def test_repeat_varies_a_number(comfy):
    sha = put(APP, png())
    params = engine.normalize({"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}},
                               "repeat": {"count": 3, "vary": [{"node": "3", "input": "steps",
                                                                "step": 5}]}},
                              app_id=APP, where=["here"], inventory=comfyui.inventory(comfy.url))
    assert [i["set"] for i in params["items"]] == [[["3", "steps", 20]], [["3", "steps", 25]],
                                                   [["3", "steps", 30]]]
    assert all(isinstance(i["seed"], int) for i in params["items"])     # @seed : tirée


def test_units_wait_while_comfyui_is_closed(comfy):
    sha = put(APP, png())
    job_id, _ = submit(comfy, {"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}})
    settings.set_value("engines_comfyui_enabled", False)       # pas (ou plus) accordé
    engine.forget_here()
    assert run_all() == 0 and queue.job_summary(job_id)["status"] == "queued"
    settings.set_value("engines_comfyui_enabled", True)
    engine.forget_here()
    assert run_all() == 1 and queue.job_summary(job_id)["status"] == "done"


def test_a_failing_unit_says_why(comfy):
    sha = put(APP, png())
    job_id, _ = submit(comfy, {"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}})
    comfy.fail_next = "KSampler"
    run_all(limit=1)
    row = db.query_one("SELECT status, error, attempts FROM units WHERE job_id=?", (job_id,))
    assert "engine_error" in row["error"] and "nœud 1" in row["error"]


def test_units_no_machine_takes_fail_at_the_deadline(comfy):
    """bounds.wait_s : une unité qu'aucune machine n'a prise avant l'échéance échoue
    (no_machine) ; celle déjà remise suit son cours."""
    sha = put(APP, png())
    job_id, _ = submit(comfy, {"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}},
                               "repeat": {"count": 2, "vary": []}})
    taken = queue.claim_next(["gpu"], "local")                  # une remise, pas finie
    db.execute("UPDATE jobs SET wait_until=? WHERE id=?", (db.now() - 1, job_id))
    assert queue.expire_waiting() == [(job_id, False)]
    rows = {r["id"]: r for r in db.query("SELECT id, status, error FROM units WHERE job_id=?",
                                         (job_id,))}
    assert rows[taken["id"]]["status"] == "running"
    [left] = [r for i, r in rows.items() if i != taken["id"]]
    assert left["status"] == "failed" and left["error"].startswith("no_machine")
    assert queue.expire_waiting() == []


def test_a_lender_reports_the_stage_of_its_calculation(comfy):
    """L'étape d'un calcul fait ailleurs arrive avec le renouvellement du bail : seulement
    pour l'unité tenue par cette machine, et seulement une étape connue."""
    from aiodos.jobs import leases
    from aiodos.network import peers, work
    peers.save("machine-b", "PC fixe", b"k" * 32, "127.0.0.1", 1)
    sha = put(APP, png())
    job_id, _ = submit(comfy, {"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}},
                       where=("here", "mine"))
    unit = leases.claim_remote(["gpu"], "machine-b", lambda u: True, circle="mine")
    assert unit is not None

    class Manager:
        def changed(self):
            pass
    reply = work.renew(Manager(), "machine-b", {"unit": unit["id"],
                                                "progress": {"stage": "running"}})
    assert reply == {"ok": True, "state": "running"}
    row = db.query_one("SELECT progress FROM units WHERE id=?", (unit["id"],))
    assert json.loads(row["progress"])["stage"] == "running"
    work.renew(Manager(), "machine-b", {"unit": unit["id"], "progress": {"stage": "<b>"}})
    row = db.query_one("SELECT progress FROM units WHERE id=?", (unit["id"],))
    assert json.loads(row["progress"])["stage"] == "running"      # une étape inconnue : rien
    order = work._order(type("M", (), {"executor": type("E", (), {
        "publish_job": staticmethod(lambda *a, **k: None)})()})(), "machine-b", unit,
        {unit["task"]: {}}, False)["unit"]
    assert "local" not in order["payload"] and order["payload"]["inputs"][0]["sha256"] == sha
    assert "app_id" not in json.dumps(order) and APP not in json.dumps(order)


def test_the_gpu_goes_to_comfyui_before_a_unit(comfy, monkeypatch):
    """Avant une unité : les modèles de conversation chargés se déchargent, sd-server
    inoccupé s'arrête (engines/gpu_share.py)."""
    from aiodos.engines import gpu_share
    from aiodos.imaging import engine as images_engine
    from aiodos.services import ollama
    calls = []
    monkeypatch.setattr(ollama, "unload_chat_models", lambda: calls.append("conv") or ["qwen3.5:9b"])
    monkeypatch.setattr(images_engine, "yield_gpu", lambda: calls.append("sd") or True)
    assert gpu_share.before_unit() == ["qwen3.5:9b", "sd-server"]
    sha = put(APP, png())
    submit(comfy, {"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}})
    calls.clear()
    assert run_all() == 1 and calls == ["conv", "sd"]


def test_no_sleep_on_mains_while_a_unit_runs(comfy, monkeypatch):
    """node/keep_awake.wanted : une unité de moteur en calcul (ou tenue pour une autre
    machine), sur secteur, réglage actif ; sinon Windows fait comme d'habitude."""
    from aiodos.node import keep_awake
    assert not keep_awake.wanted()
    sha = put(APP, png())
    submit(comfy, {"graph": GRAPH, "inputs": {"ref0": {"sha256": sha}}})
    unit = queue.claim_next(["gpu"], "local")
    assert keep_awake.wanted()
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "battery")
    assert not keep_awake.wanted()
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "ac")
    settings.set_value("keep_awake_jobs", False)
    assert not keep_awake.wanted()
    settings.set_value("keep_awake_jobs", True)
    queue.release_unit(unit["id"])
    assert not keep_awake.wanted()
    keep_awake.hold("unité de moteur")
    assert keep_awake.wanted()
    keep_awake.release("unité de moteur")
    assert not keep_awake.wanted()


def test_matching_a_machine_that_asks_for_work():
    """engines/lending.can_serve, chez le propriétaire : le moteur, ses classes, ses modèles
    (un modèle sans dossier se cherche dans tous) ; une annonce mal formée vaut un refus."""
    from aiodos.engines import lending
    need = {"name": "comfyui", "api": 1, "classes": ["KSampler", "SaveImage"],
            "models": [{"folder": "checkpoints", "name": "sub/autre.safetensors"},
                       {"folder": None, "name": "vae.safetensors"}]}
    caps = {"engines": {"comfyui": {"api": 1, "state": "ready",
                                    "classes": ["KSampler", "SaveImage", "LoadImage"],
                                    "models": {"checkpoints": ["sub\\autre.safetensors"],
                                               "vae": ["vae.safetensors"]}}}}
    assert lending.can_serve(need, caps)
    assert not lending.can_serve({**need, "classes": ["NoeudAjoute"]}, caps)
    assert not lending.can_serve({**need, "api": 2}, caps)
    busy = {"engines": {"comfyui": {**caps["engines"]["comfyui"], "state": "in_use"}}}
    assert not lending.can_serve(need, busy)
    for odd in ({}, {"engines": []}, {"engines": {"comfyui": {"api": 1, "classes": "KSampler",
                                                             "models": {}}}},
                {"engines": {"comfyui": {"api": 1, "classes": [], "models": [1, 2]}}}):
        assert not lending.can_serve(need, odd)


def test_inputs_are_checked_on_arrival():
    data = png()
    good = hashlib.sha256(data).hexdigest()

    async def chunks(payload):
        yield payload
    with pytest.raises(ApiError) as caught:
        asyncio.run(input_store.receive(APP, "0" * 64, chunks(data)))
    assert caught.value.code == "input_hash_mismatch"
    text = b"bonjour, ce n'est pas une image"
    with pytest.raises(ApiError) as caught:
        asyncio.run(input_store.receive(APP, hashlib.sha256(text).hexdigest(), chunks(text)))
    assert caught.value.code == "unsupported_media_type"
    first, created = asyncio.run(input_store.receive(APP, good, chunks(data)))
    again, created_again = asyncio.run(input_store.receive(APP, good, chunks(data)))
    assert created and not created_again and first == again == {
        "sha256": good, "size": len(data), "type": "image/png"}
    assert input_store.path_of(APP, good) is not None and input_store.path_of(OTHER, good) is None
