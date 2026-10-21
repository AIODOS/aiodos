"""Les graphes de ComfyUI au format API, en fonctions pures (engines/comfy_graph.py) : forme,
liens et cycles, retouches, renumérotation, jetons, valeurs interdites, sorties."""
import copy

import pytest

from aiodos.engines import comfy_graph as g
from aiodos.engines.comfy_graph import GraphError

T2I = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "@model:ckpt"},
          "_meta": {"title": "Charger"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "flou", "clip": ["4", 1]}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024,
                                                       "batch_size": 1}},
    "3": {"class_type": "KSampler", "inputs": {"seed": "@seed", "steps": 30, "cfg": 7.0,
                                               "model": ["4", 0], "positive": ["6", 0],
                                               "negative": ["7", 0], "latent_image": ["5", 0]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "mood"}},
    "10": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},
}
DEFS = {"CLIPTextEncode": {"input": {"required": {"text": ["STRING", {"multiline": True}],
                                                  "clip": ["CLIP"]}}},
        "CheckpointLoaderSimple": {"input": {"required": {
            "ckpt_name": [["sd_xl_base_1.0.safetensors", "sdxl/juggernaut.safetensors"]]}}},
        "SaveImage": {"output_node": True, "input": {"required": {"images": ["IMAGE"]}}},
        "PreviewImage": {"output_node": True, "input": {"required": {"images": ["IMAGE"]}}},
        "GeminiNode": {"api_node": True, "python_module": "comfy_api_nodes.nodes_gemini"}}


def test_a_well_formed_graph_passes():
    g.check_shape(T2I)
    g.check_values(T2I, DEFS)


@pytest.mark.parametrize("graph, code", [
    ({"nodes": [], "links": []}, "graph_not_api"),
    ({}, "graph_invalid"),
    ({"1": {"inputs": {}}}, "graph_invalid"),                                  # sans classe
    ({"1": {"class_type": "A", "inputs": {"x": ["9", 0]}}}, "graph_invalid"),   # lien vers rien
    ({"1": {"class_type": "A", "inputs": {"x": ["2", 0]}},
      "2": {"class_type": "B", "inputs": {"y": ["1", 0]}}}, "graph_invalid"),   # cycle
    ({"1": {"class_type": "A", "inputs": {}, "extra_data": {}}}, "graph_invalid"),
])
def test_malformed_graphs_are_refused_with_their_code(graph, code):
    with pytest.raises(GraphError) as refused:
        g.check_shape(graph)
    assert refused.value.code == code


def test_too_many_nodes_for_a_friend():
    big = {str(i): {"class_type": "Note", "inputs": {}} for i in range(600)}
    g.check_shape(big, "mine")
    with pytest.raises(GraphError):
        g.check_shape(big, "friends")


def test_patches_replace_literals_only():
    patched = g.apply_set(T2I, [["6", "text", "Salon baigné de lumière dorée"],
                                ["5", "width", 832]])
    assert patched["6"]["inputs"]["text"] == "Salon baigné de lumière dorée"
    assert T2I["6"]["inputs"]["text"] == ""                 # l'original n'a pas bougé
    for wrong in (["6", "clip", ["4", 0]],                   # un lien
                  ["5", "width", "grand"],                   # autre type
                  ["99", "text", "x"],                       # nœud absent
                  ["6", "absent", "x"]):                     # entrée absente
        with pytest.raises(GraphError) as refused:
            g.apply_set(T2I, [wrong])
        assert refused.value.code == "patch_invalid"


def test_renumbering_is_deterministic_and_clears_meta():
    renumbered, mapping = g.renumber(T2I)
    assert list(renumbered) == [str(i) for i in range(1, 9)]
    assert mapping == {"3": "1", "4": "2", "5": "3", "6": "4", "7": "5", "8": "6", "9": "7",
                       "10": "8"}
    assert renumbered["1"]["inputs"]["model"] == ["2", 0]    # liens suivis
    assert all(node["_meta"] == {} for node in renumbered.values())
    g.check_shape(renumbered)


def test_previews_are_removed():
    assert "10" not in g.without_previews(T2I)


def test_tokens_are_found_and_replaced():
    assert g.tokens(T2I) == {"inputs": set(), "models": {"ckpt"}, "seed": True}
    done = g.replace_tokens(T2I, {}, {"ckpt": "sd_xl_base_1.0.safetensors"}, seed=1201)
    assert done["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert done["3"]["inputs"]["seed"] == 1201
    assert done["3"]["inputs"]["model"] == ["4", 0]          # un lien ne bouge pas
    with pytest.raises(GraphError) as refused:
        g.replace_tokens(T2I, {}, {}, seed=1)
    assert refused.value.code == "model_unresolved"


@pytest.mark.parametrize("value, reason", [
    ("C:\\Users\\moi\\secret.png", "absolute_path"), ("/etc/passwd", "absolute_path"),
    ("\\\\serveur\\partage\\x", "absolute_path"), ("~/x", "absolute_path"),
    ("../../x.png", "parent_path"), ("a/%2e%2e/b", "parent_path"),
    ("image.png [output]", "annotation"), ("image.png[input]", "annotation"),
    ("https://exemple.fr/x.png", "url"), ("file:x", "url"), ("www.exemple.fr", "url"),
    ("data:image/png;base64,AAAA", "data_url"), ("NUL", "reserved_name"),
    ("com1.txt", "reserved_name"), ("a\x00b", "nul"),
])
def test_forbidden_values(value, reason):
    assert g.forbidden(value) == reason


def test_ordinary_values_pass():
    for value in ("Salon baigné de lumière dorée", "sdxl/juggernaut.safetensors", "euler",
                  "1024x1024", "mood"):
        assert g.forbidden(value) is None


def test_a_prompt_may_contain_an_address():
    """Les consignes (STRING multiline) vont aux encodeurs de texte : seuls le caractère nul et
    la longueur y comptent."""
    graph = g.apply_set(T2I, [["6", "text", "un panneau « https://exemple.fr » sur un mur"]])
    g.check_values(graph, DEFS)
    graph = g.apply_set(T2I, [["6", "text", "a\x00b"]])
    with pytest.raises(GraphError):
        g.check_values(graph, DEFS)


def test_files_come_only_as_input_tokens():
    graph = copy.deepcopy(T2I)
    graph["11"] = {"class_type": "LoadImage", "inputs": {"image": "photo.png"}}
    with pytest.raises(GraphError) as refused:
        g.check_values(graph, DEFS)
    assert refused.value.detail["reason"] == "file_input"
    graph["11"]["inputs"]["image"] = "@in:ref0"
    g.check_values(graph, DEFS)
    assert g.tokens(graph)["inputs"] == {"ref0"}


def test_a_path_outside_a_prompt_is_refused():
    graph = copy.deepcopy(T2I)
    graph["4"]["inputs"]["ckpt_name"] = "C:\\modeles\\pirate.safetensors"
    with pytest.raises(GraphError):
        g.check_values(graph, DEFS)


def test_outputs_and_their_prefix():
    assert g.outputs(T2I, DEFS) == ["9", "10"]
    assert g.outputs(T2I) == ["9"]                          # sans définitions : les Save…
    prefixed = g.set_prefix(T2I, ["9"], "aiodos/4f0c/out")
    assert prefixed["9"]["inputs"]["filename_prefix"] == "aiodos/4f0c/out"
    assert T2I["9"]["inputs"]["filename_prefix"] == "mood"


def test_cloud_nodes_are_spotted():
    graph = {**T2I, "12": {"class_type": "GeminiNode", "inputs": {}}}
    assert g.cloud_nodes(graph, DEFS) == ["12"]
