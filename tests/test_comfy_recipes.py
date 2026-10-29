"""Les recettes de ComfyUI (engines/recipes.py, engines/catalogue.py, tools/comfy_recipes.py) :
le catalogue livré (une image, une vidéo, nœuds de ComfyUI seulement), les bornes des
paramètres, le graphe construit sans nœud ajouté, la signature (contexte « aiodos-catalogue »)
et le refus d'une recette non signée pour le réseau public."""
import base64
import copy
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos.engines import catalogue, comfy_contract, comfy_graph, recipes
from aiodos.updates import manifest

SDXL = "comfy-templates/image_sdxl_simple"
WAN = "comfy-templates/text_to_video_wan"
ROOT = Path(__file__).resolve().parent.parent


def shipped(recipe_id):
    return copy.deepcopy(catalogue.recipe(recipe_id, catalogue=catalogue.read()))


def core_defs(recipe, module="nodes", **changes):
    """Des définitions du cœur pour chaque classe de la recette (python_module « nodes »)."""
    defs = {}
    for node in recipe["graph"].values():
        defs[node["class_type"]] = {"input": {"required": {}}, "output": [], "python_module": module}
    for cls, change in changes.items():
        if change is None:
            defs.pop(cls, None)
        else:
            defs[cls] = {**defs[cls], **change}
    return defs


# --------------------------------------------------------------- le catalogue livré
def test_the_shipped_catalogue_has_an_image_and_a_video_recipe():
    found = catalogue.read()
    assert found.errors == ()
    assert sorted(found.recipes) == [SDXL, WAN]
    assert {r["kind"] for r in found.recipes.values()} == {"image", "video"}
    assert found.signed == frozenset()                     # l'auteur signera lui-même
    for recipe in found.recipes.values():
        recipes.check(recipe)
        assert recipe["source"]["package"] == "comfyui-workflow-templates"
        assert recipe["source"]["license"] == "MIT"
        assert recipe["vram_mib"] <= 8192                  # une carte de 8 Go
        assert all(m["license"] and m["url"].startswith("https://huggingface.co/")
                   for m in recipe["models"])
        # Ni aperçu, ni lecture de fichier, ni nœud que ComfyUI ne connaît pas d'office
        classes = {n["class_type"] for n in recipe["graph"].values()}
        assert not classes & comfy_graph.PREVIEWS and not classes & set(comfy_graph.FILE_LOADERS)
        assert recipes.not_core(recipe, core_defs(recipe)) == []
    sdxl = found.recipes[SDXL]
    assert sdxl["models"][0]["sha256"] == \
        "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b"
    assert sdxl["models"][0]["size"] == 6938078334
    assert sdxl["graph"]["15"]["inputs"]["ckpt_name"] == "@model:checkpoint"
    assert sdxl["graph"]["12"]["inputs"]["seed"] == "@seed"
    wan = found.recipes[WAN]
    assert {m["key"] for m in wan["models"]} == {"diffusion", "text_encoder", "vae"}
    assert wan["graph"]["50"]["class_type"] == "SaveVideo"


def test_the_catalogue_file_is_utf8_lf_and_the_license_is_shipped():
    for path in catalogue.FOLDER.glob(catalogue.PATTERN):
        raw = path.read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n") and not raw.startswith(b"\xef\xbb\xbf")
    license_text = (catalogue.FOLDER / "LICENSE-comfyui-workflow-templates.txt").read_text(
        encoding="utf-8")
    assert "MIT License" in license_text and "Comfy Org" in license_text
    assert "comfyui-workflow-templates" in (ROOT / "NOTICE").read_text(encoding="utf-8")


def test_an_unsigned_recipe_serves_locally_never_for_the_public_network():
    assert catalogue.recipe(SDXL)["id"] == SDXL
    with pytest.raises(catalogue.CatalogueError) as caught:
        catalogue.recipe(SDXL, public=True)
    assert caught.value.code == "recipe_unsigned"
    assert "réseau public" in str(caught.value)
    assert catalogue.public_ids() == []
    with pytest.raises(catalogue.CatalogueError) as caught:
        catalogue.recipe("comfy-templates/inconnue")
    assert caught.value.code == "recipe_unknown"


# ------------------------------------------------------------------ la signature
def signed_copy(tmp_path, private=None, tamper=False):
    folder = tmp_path / "catalogue"
    folder.mkdir(parents=True)
    source = next(catalogue.FOLDER.glob(catalogue.PATTERN))
    target = folder / source.name
    shutil.copyfile(source, target)
    if private is not None:
        envelope = catalogue.sign(target.read_bytes(), private)
        (folder / (source.name + ".sig")).write_text(json.dumps(envelope), encoding="utf-8")
    if tamper:
        target.write_bytes(target.read_bytes().replace(b"dpmpp_2m", b"dpmpp_3m"))
    return folder


def test_a_catalogue_signed_by_a_trusted_key_serves_the_public_network(tmp_path):
    private = Ed25519PrivateKey.generate()
    raw = manifest.raw_public(private)
    trusted = {manifest.key_id(raw): base64.b64encode(raw).decode("ascii")}
    found = catalogue.read(signed_copy(tmp_path, private), trusted=trusted)
    assert found.signed == {SDXL, WAN} and found.errors == ()
    assert catalogue.recipe(SDXL, public=True, catalogue=found)["id"] == SDXL
    assert found.keys[SDXL] == manifest.key_id(raw)
    # La signature d'un catalogue n'est pas celle d'un manifeste : le contexte est à elle
    data = (signed_copy(tmp_path / "b", private) / next(
        catalogue.FOLDER.glob(catalogue.PATTERN)).name).read_bytes()
    envelope = catalogue.sign(data, private)
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        private.public_key().verify(base64.b64decode(envelope["signature"]), data)


def test_a_wrong_or_unknown_signature_drops_the_whole_file(tmp_path):
    private = Ed25519PrivateKey.generate()
    raw = manifest.raw_public(private)
    trusted = {manifest.key_id(raw): base64.b64encode(raw).decode("ascii")}
    altered = catalogue.read(signed_copy(tmp_path / "a", private, tamper=True), trusted=trusted)
    assert altered.recipes == {} and altered.errors           # même localement
    unknown = catalogue.read(signed_copy(tmp_path / "b", private), trusted={})
    assert unknown.recipes == {} and "inconnue" in unknown.errors[0][1]
    catalogue.forget()


# ------------------------------------------------------------------ les paramètres
def test_parameters_are_checked_against_their_bounds():
    recipe = shipped(SDXL)
    values = recipes.values(recipe, {"prompt": "  un phare sous l'orage ", "width": 768,
                                     "cfg": 6, "steps": 30.0, "seed": 12})
    assert values["prompt"] == "un phare sous l'orage" and values["width"] == 768
    assert values["height"] == 1024 and values["cfg"] == 6.0 and values["steps"] == 30
    assert values["seed"] == 12
    assert "seed" not in recipes.values(recipe, {})          # tirée par unité
    refused = [{"width": 1000}, {"width": 2048}, {"width": 256}, {"width": True},
               {"width": "768"}, {"steps": 0}, {"steps": 2.5}, {"cfg": 15.5}, {"cfg": 6.25},
               {"seed": -1}, {"seed": 2 ** 60}, {"prompt": "x" * 2001}, {"prompt": 3},
               {"prompt": "a\x00b"}, {"inconnu": 1}, {"cfg": float("nan")}]
    for given in refused:
        with pytest.raises(recipes.RecipeError) as caught:
            recipes.values(recipe, given)
        assert caught.value.code == "param_invalid", given
    with pytest.raises(recipes.RecipeError):
        recipes.values(recipe, ["prompt"])
    video = shipped(WAN)
    assert recipes.values(video, {"frames": 49})["frames"] == 49
    for frames in (48, 3, 85):                               # 4n + 1, de 5 à 81
        with pytest.raises(recipes.RecipeError):
            recipes.values(video, {"frames": frames})


def links(graph):
    found = set()
    for node_id, node in graph.items():
        for name, value in node["inputs"].items():
            if isinstance(value, list):
                found.add((node_id, name, value[0], value[1]))
    return found


@pytest.mark.parametrize("recipe_id", [SDXL, WAN])
def test_the_graph_is_built_from_the_recipe_without_any_added_node(recipe_id):
    recipe = shipped(recipe_id)
    given = {"prompt": "un renard dans la neige", "negative": "flou", "seed": 7}
    built = recipes.build(recipe, recipes.values(recipe, given))
    assert set(built) == set(recipe["graph"])
    assert {k: v["class_type"] for k, v in built.items()} == \
        {k: v["class_type"] for k, v in recipe["graph"].items()}
    assert links(built) == links(recipe["graph"])
    changed = {(n, name) for n, node in built.items() for name, value in node["inputs"].items()
               if value != recipe["graph"][n]["inputs"][name]}
    targets = {tuple(t.split(".", 1)) for p in recipe["params"] for t in p["targets"]}
    assert changed <= targets and changed
    assert comfy_graph.tokens(built)["seed"]                 # la graine, posée par unité
    assert comfy_graph.tokens(built)["models"] == {m["key"] for m in recipe["models"]}
    assert recipe == shipped(recipe_id)                      # la recette n'a pas bougé


def test_only_comfyui_nodes_and_the_contract():
    recipe = shipped(SDXL)
    assert recipes.not_core(recipe, core_defs(recipe, KSampler={"python_module":
                                                                "custom_nodes.malin"})) == \
        [("KSampler", "custom")]
    assert recipes.not_core(recipe, core_defs(recipe, SaveImage={"api_node": True})) == \
        [("SaveImage", "custom")]
    assert recipes.not_core(recipe, core_defs(recipe, VAEDecode=None)) == [("VAEDecode", "missing")]
    defs = core_defs(recipe)
    recipe["contract"]["classes"] = {c: comfy_contract.fingerprint(d) for c, d in defs.items()}
    assert recipes.not_core(recipe, defs) == []
    defs["KSampler"] = {**defs["KSampler"], "output": ["LATENT"]}
    assert recipes.not_core(recipe, defs) == [("KSampler", "different")]
    with pytest.raises(recipes.RecipeError) as caught:
        recipes.check_here(recipe, defs)
    assert caught.value.code == "contract"


@pytest.mark.parametrize("damage", [
    lambda r: r["params"][0]["targets"].__setitem__(0, "15.ckpt_name"),     # un modèle
    lambda r: r["params"][0]["targets"].__setitem__(0, "7.filename_prefix"),
    lambda r: r["params"][0]["targets"].__setitem__(0, "12.model"),          # un lien
    lambda r: r["params"][3].update(default=1000),                           # hors du pas
    lambda r: r["params"][3].update(max=100),
    lambda r: r["graph"].update({"99": {"class_type": "PreviewImage",
                                        "inputs": {"images": ["14", 0]}}}),
    lambda r: r["graph"]["15"]["inputs"].update(ckpt_name="@model:autre"),
    lambda r: r["graph"]["15"]["inputs"].update(ckpt_name="C:/modeles/sd.safetensors"),
    lambda r: r["models"][0].update(sha256="pas une empreinte"),
    lambda r: r["models"][0].update(folder="../ailleurs"),
    lambda r: r["contract"]["classes"].pop("KSampler"),
    lambda r: r["outputs"][0].update(node="12"),
    lambda r: r.update(id="Pas un identifiant"),
])
def test_a_damaged_recipe_is_refused(damage):
    recipe = shipped(SDXL)
    damage(recipe)
    with pytest.raises(recipes.RecipeError) as caught:
        recipes.check(recipe)
    assert caught.value.code == "recipe_invalid"


def test_the_digest_follows_the_content():
    recipe = shipped(SDXL)
    assert recipes.digest(recipe) == recipes.digest(shipped(SDXL))
    recipe["graph"]["12"]["inputs"]["steps"] = 26
    assert recipes.digest(recipe) != recipes.digest(shipped(SDXL))


# ------------------------------------------------------------------ l'outil
def load_tool():
    spec = importlib.util.spec_from_file_location("comfy_recipes_tool",
                                                  ROOT / "tools" / "comfy_recipes.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def test_the_tool_turns_an_editor_template_into_an_api_graph_without_adding_anything():
    tool = load_tool()
    template = {
        "nodes": [
            {"id": 4, "type": "CheckpointLoaderSimple", "mode": 0,
             "widgets_values": ["sd_xl_base_1.0.safetensors"], "inputs": []},
            {"id": 6, "type": "CLIPTextEncode", "mode": 0, "widgets_values": ["un chat"],
             "inputs": [{"name": "clip", "link": 1}]},
            {"id": 9, "type": "MarkdownNote", "mode": 0, "widgets_values": ["note"]},
            {"id": 3, "type": "KSampler", "mode": 0,
             "widgets_values": [42, "randomize", 20, 7, "euler", "normal", 1],
             "inputs": [{"name": "model", "link": 2}, {"name": "positive", "link": 3}]},
        ],
        "links": [[1, 4, 1, 6, 0, "CLIP"], [2, 4, 0, 3, 0, "MODEL"],
                  [3, 6, 0, 3, 1, "CONDITIONING"]]}
    graph = tool.to_api(template, "essai")
    assert sorted(graph, key=int) == ["3", "4", "6"]                    # sans la note
    assert graph["3"]["inputs"] == {"seed": 42, "steps": 20, "cfg": 7, "sampler_name": "euler",
                                    "scheduler": "normal", "denoise": 1, "model": ["4", 0],
                                    "positive": ["6", 0]}
    assert graph["6"]["inputs"] == {"text": "un chat", "clip": ["4", 1]}
    with pytest.raises(SystemExit):
        tool.to_api({"nodes": [{"id": 1, "type": "NoeudAjoute", "mode": 0}], "links": []}, "x")
    with pytest.raises(SystemExit):
        tool.to_api({**template, "definitions": {"subgraphs": [{}]}}, "x")
