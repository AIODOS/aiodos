"""L'adaptateur ComfyUI (engines/comfyui.py) contre un faux ComfyUI (tests/comfy_stub.py) :
entrées envoyées sous aiodos/<run_uuid>, jetons remplacés, sorties rangées et vérifiées, ce
calcul seul arrêté, refus avant tout envoi."""
import threading

import pytest

from aiodos import settings
from aiodos.engines import comfy_client, comfy_contract, comfy_find, comfy_graph, comfyui
from aiodos.jobs.registry import Interrupted
from tests.comfy_stub import DEFS, ComfyStub, png

GRAPH = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "@model:0"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "Une cabane", "clip": ["4", 1]}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "@in:ref0"}},
    "3": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": "@seed", "steps": 20,
                                               "cfg": 7.0, "positive": ["6", 0],
                                               "negative": ["6", 0], "latent_image": ["10", 0]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "mood"}},
    "11": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},
}


@pytest.fixture
def comfy(tmp_path, monkeypatch):
    # Jamais les vrais dossiers de modèles de ce PC (Comfy Desktop…)
    monkeypatch.setattr(comfyui, "_model_roots", lambda argv: [tmp_path / "modeles"])
    comfyui.forget_inventory()
    with ComfyStub(tmp_path / "comfy") as stub:
        yield stub
    comfyui.forget_inventory()


def order(**extra):
    run_id = comfy_client.new_run_id()
    base = {"engine": "comfyui", "api": 1, "run_uuid": run_id, "graph": GRAPH, "seed": 1201,
            "inputs": [{"slot": "ref0", "sha256": "0" * 64, "size": 10, "type": "image/png"}],
            "models": [{"token": "@model:0", "folder": "checkpoints",
                        "name": "sd_xl_base_1.0.safetensors", "size": 0}],
            "outputs": ["9"], "bounds": {"unit_seconds": 30, "output_mb": 10}}
    return {**base, **extra}


def inputs(tmp_path):
    path = tmp_path / "ref.png"
    path.write_bytes(png(4, 4))
    return {"ref0": path}


def test_one_unit_end_to_end(comfy, tmp_path, monkeypatch):
    monkeypatch.setattr(comfyui, "POLL", 0.1)
    comfy.delay = 0.5
    job = order()
    stages = []
    outs = comfyui.run(job, inputs(tmp_path), tmp_path / "sorties", threading.Event(),
                       progress=lambda stage, **info: stages.append(stage), url=comfy.url)
    [out] = outs
    assert out.node == "9" and out.type == "image/png" and out.size == out.path.stat().st_size
    assert out.path.read_bytes().startswith(b"\x89PNG") and len(out.sha256) == 64
    [sent] = comfy.prompts
    run_id = job["run_uuid"]
    assert sent["prompt_id"] == run_id and sent["client_id"] == comfyui.client_id()
    assert "extra_data" not in sent                       # rien venu d'ailleurs
    graph = sent["prompt"]
    assert graph["10"]["inputs"]["image"] == f"aiodos/{run_id}/in-1.png"
    assert graph["3"]["inputs"]["seed"] == 1201
    assert graph["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert graph["9"]["inputs"]["filename_prefix"] == f"aiodos/{run_id}/out"
    assert "11" not in graph                              # l'aperçu est retiré
    assert comfy.uploads[0][:2] == (f"aiodos/{run_id}", "in-1.png")
    assert stages[0] == "sending_inputs" and "running" in stages and stages[-1] == "receiving"
    assert comfy.status(run_id) == "completed"
    assert comfy_client.Client(comfy.url).history(run_id) is None      # historique effacé


def test_refused_before_anything_is_sent(comfy, tmp_path):
    inv = comfyui.inventory(comfy.url)
    unknown = {**GRAPH, "12": {"class_type": "NoeudAjoute", "inputs": {}}}
    with pytest.raises(comfyui.Refused) as caught:
        comfyui.check(order(graph=unknown), inv)
    assert caught.value.reason == "class"
    missing = order(models=[{"token": "@model:0", "folder": "checkpoints",
                             "name": "absent.safetensors"}])
    with pytest.raises(comfyui.Refused) as caught:
        comfyui.check(missing, inv)
    assert caught.value.reason == "model"
    contract = {"classes": {"KSampler": "0" * 32}}
    with pytest.raises(comfyui.Refused) as caught:
        comfyui.check(order(strict=True, contract=contract), inv)
    assert caught.value.reason == "contract"
    same = {"classes": {"KSampler": comfy_contract.fingerprint(DEFS["KSampler"])}}
    comfyui.check(order(strict=True, contract=same), inv)               # même empreinte : bon
    path = {**GRAPH, "6": {"class_type": "CLIPTextEncode",
                           "inputs": {"text": "x", "clip": ["4", 1]}},
            "4": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": "C:/Windows/win.ini"}}}
    with pytest.raises(comfy_graph.GraphError) as caught:
        comfyui.check(order(graph=path), inv)
    assert caught.value.code == "forbidden_value"
    with pytest.raises(comfyui.Refused):
        comfyui.run(order(graph=unknown), inputs(tmp_path), tmp_path / "o", threading.Event(),
                    url=comfy.url)
    assert comfy.prompts == [] and comfy.uploads == []


def test_a_model_by_name_in_a_subfolder(comfy):
    inv = comfyui.inventory(comfy.url)
    job = order(models=[{"token": "@model:0", "folder": "checkpoints",
                         "name": "sub\\autre.safetensors"}])
    assert comfyui.check(job, inv)["models"] == {"0": "sub/autre.safetensors"}


def test_stop_cancels_this_unit_only(comfy, tmp_path):
    comfy.delay = 5
    stop = threading.Event()
    job = order()
    threading.Timer(0.6, stop.set).start()
    with pytest.raises(Interrupted):
        comfyui.run(job, inputs(tmp_path), tmp_path / "o", stop, url=comfy.url)
    assert comfy.cancelled == [job["run_uuid"]]
    assert comfy.status(job["run_uuid"]) == "cancelled"


def test_engine_failure_names_the_node(comfy, tmp_path):
    comfy.fail_next = "KSampler"
    with pytest.raises(comfyui.Failed) as caught:
        comfyui.run(order(), inputs(tmp_path), tmp_path / "o", threading.Event(), url=comfy.url)
    assert caught.value.code == "engine_error" and caught.value.node == "3"
    assert "out of memory" in str(caught.value)


def test_too_long_and_too_heavy(comfy, tmp_path, monkeypatch):
    comfy.delay = 3
    monkeypatch.setattr(comfyui, "POLL", 0.2)
    with pytest.raises(comfyui.Failed) as caught:
        comfyui.run(order(bounds={"unit_seconds": 0.5, "output_mb": 10}), inputs(tmp_path),
                    tmp_path / "o", threading.Event(), url=comfy.url)
    assert caught.value.code == "engine_timeout"
    comfy.delay = 0
    with pytest.raises(comfyui.Failed) as caught:
        comfyui.run(order(bounds={"unit_seconds": 30, "output_mb": 0.00001}), inputs(tmp_path),
                    tmp_path / "o2", threading.Event(), url=comfy.url)
    assert caught.value.code == "output_too_large"


def test_comfyui_closed_gives_the_unit_back(tmp_path):
    settings.set_value("engines_comfyui_enabled", True)
    settings.set_value("engines_comfyui_url", "http://127.0.0.1:9")
    with pytest.raises(Interrupted):
        comfyui.run(order(), inputs(tmp_path), tmp_path / "o", threading.Event(),
                    url="http://127.0.0.1:9")


def test_desktop_and_portable_installs_are_found(tmp_path, monkeypatch):
    """Desktop : le Python de ComfyUI\\.venv (standalone-env n'en est que la base, sans
    torch) ; la version : celle du code. Le dossier réglé passe d'abord."""
    local = tmp_path / "local"
    desktop = local / "Comfy-Desktop" / "ComfyUI-Installs" / "Moi"
    code = desktop / "ComfyUI"
    (code / "comfy").mkdir(parents=True)
    (code / "main.py").write_text("", encoding="utf-8")
    (code / "comfyui_version.py").write_text('__version__ = "0.35.1"\n', encoding="utf-8")
    for python in (code / ".venv" / "Scripts" / "python.exe",
                   desktop / "standalone-env" / "python.exe"):
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")
    (desktop / "manifest.json").write_text('{"version": "v0.34.0-env1"}', encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(comfy_find.Path, "home", lambda: tmp_path / "home")
    [found] = comfy_find.installs()
    assert (found.kind, found.version, found.name) == ("desktop", "0.35.1", "Moi")
    assert found.python == code / ".venv" / "Scripts" / "python.exe"
    assert comfy_find.desktop_manifest(found)["version"] == "v0.34.0-env1"
    own = tmp_path / "ailleurs" / "ComfyUI"
    (own / "comfy").mkdir(parents=True)
    (own / "main.py").write_text("", encoding="utf-8")
    settings.set_value("engines_comfyui_path", str(own))
    assert [i.kind for i in comfy_find.installs()] == ["setting", "desktop"]


def test_only_this_pc():
    assert comfy_find.local_url("http://192.168.1.20:8188") is None
    assert comfy_find.local_url("https://127.0.0.1:8188") is None
    assert comfy_find.local_url("http://127.0.0.1:8188/prompt") == "http://127.0.0.1:8188"
    with pytest.raises(ValueError):
        comfy_client.Client("http://exemple.fr:8188")
