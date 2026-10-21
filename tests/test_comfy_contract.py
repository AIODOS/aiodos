"""Le contrat des nœuds de ComfyUI (engines/comfy_contract.py)."""
import copy

from aiodos.engines import comfy_contract as contract

KSAMPLER = {"input": {"required": {"seed": ["INT", {"min": 0}], "steps": ["INT", {"default": 20}]}},
            "input_order": {"required": ["seed", "steps"]}, "output": ["LATENT"],
            "output_is_list": [False], "output_node": False, "python_module": "nodes"}
LOADER = {"input": {"required": {"ckpt_name": [["a.safetensors", "b.safetensors"]]}},
          "output": ["MODEL", "CLIP", "VAE"], "output_node": False, "python_module": "nodes"}
CUSTOM = {"input": {"required": {}}, "output": [], "python_module": "custom_nodes.paquet"}
DEFS = {"KSampler": KSAMPLER, "CheckpointLoaderSimple": LOADER, "Truc": CUSTOM}
GRAPH = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "@model:0"}},
         "2": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}}}


def test_a_class_fingerprint_ignores_the_files_of_a_machine():
    other = copy.deepcopy(LOADER)
    other["input"]["required"]["ckpt_name"] = [["c.safetensors"]]
    assert contract.fingerprint(LOADER) == contract.fingerprint(other)
    changed = copy.deepcopy(KSAMPLER)
    changed["input"]["required"]["steps"] = ["INT", {"default": 30}]
    assert contract.fingerprint(changed) != contract.fingerprint(KSAMPLER)
    assert len(contract.fingerprint(KSAMPLER)) == 32


def test_core_leaves_out_added_nodes():
    assert contract.core(DEFS) == contract.core({"KSampler": KSAMPLER,
                                                 "CheckpointLoaderSimple": LOADER})


def test_a_graph_replays_where_its_classes_match():
    signed = contract.of_graph(GRAPH, DEFS, "0.35.1")
    assert set(signed["classes"]) == {"KSampler", "CheckpointLoaderSimple"}
    assert contract.mismatches(signed, GRAPH, DEFS, strict=True) == []
    elsewhere = {"KSampler": copy.deepcopy(KSAMPLER), "CheckpointLoaderSimple": LOADER}
    elsewhere["KSampler"]["python_module"] = "custom_nodes.imposteur"
    assert contract.mismatches(signed, GRAPH, elsewhere, strict=True) == [("KSampler", "different")]
    assert contract.mismatches(signed, GRAPH, {"KSampler": KSAMPLER}, strict=False) == [
        ("CheckpointLoaderSimple", "missing")]
