"""Fabriquer le catalogue des recettes de ComfyUI (docs/conception/partage-puissance.md § 3.9,
comfyui-createur.md § 3.1) : l'outil de l'auteur. Rien ici ne télécharge quoi que ce soit.

    py -3.14 tools/comfy_recipes.py build    [--templates DOSSIER] [--out FICHIER]
    py -3.14 tools/comfy_recipes.py check    [FICHIER]
    py -3.14 tools/comfy_recipes.py hash     [FICHIER] [--roots DOSSIER …] [--all]
    py -3.14 tools/comfy_recipes.py contract [FICHIER] (--url http://127.0.0.1:8188 | --object-info FICHIER)
    py -3.14 tools/comfy_recipes.py sign     [FICHIER]

- build : chaque recette de TEMPLATES (une table écrite à la main par gabarit : ses
  paramètres, ses modèles, ses sorties) part du gabarit officiel de ComfyUI (paquet
  comfyui-workflow-templates, licence MIT), lu dans le ComfyUI installé sur ce PC (son .venv)
  ou dans --templates : le graphe de l'éditeur devient le format API (WIDGETS : le nom de
  chaque valeur de l'éditeur, par classe), les modèles deviennent des jetons @model:<clé> et la
  graine le jeton @seed ; rien d'autre ne change. Les empreintes déjà connues (modèles,
  contrat) du fichier existant sont gardées. Un gabarit à sous-graphes, à nœud contourné ou à
  classe sans table est refusé.
- check : chaque recette vérifiée (engines/recipes.check), et la signature si elle est là.
- hash : les modèles sans empreinte, cherchés sur ce PC (dossiers de modèles de ComfyUI,
  modèles d'images d'AIODOS, --roots) et hachés en lecture seule ; --all : tous.
- contract : l'empreinte de chaque classe d'après l'object_info d'un ComfyUI de référence (lu,
  jamais lancé ici) ; une classe qui n'est pas un nœud de ComfyUI (nœud ajouté, service en
  ligne) fait tout refuser.
- sign : la signature du fichier par la clé de publication (contexte « aiodos-catalogue\\n »,
  engines/catalogue.py), dans <fichier>.sig. Seulement depuis un dépôt privé (comme
  tools/publish.py), et seulement si chaque modèle a son empreinte et sa taille et chaque
  classe son empreinte : une recette dont un modèle manque chez l'auteur n'est pas publiée.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiodos.engines import catalogue, comfy_contract, comfy_graph, recipes  # noqa: E402

DEFAULT = catalogue.FOLDER / "comfyui-2026-10-29.json"
DATE = "2026-10-29"
PACKAGE = "comfyui-workflow-templates"
PACKAGE_URL = "https://github.com/Comfy-Org/workflow_templates"

# Le nom de chaque valeur de l'éditeur (widgets_values), dans l'ordre, par classe ; None : une
# valeur de l'éditeur seul (« control_after_generate » de la graine)
WIDGETS = {
    "CheckpointLoaderSimple": ["ckpt_name"],
    "CLIPTextEncode": ["text"],
    "KSampler": ["seed", None, "steps", "cfg", "sampler_name", "scheduler", "denoise"],
    "EmptyLatentImage": ["width", "height", "batch_size"],
    "VAEDecode": [],
    "SaveImage": ["filename_prefix"],
    "UNETLoader": ["unet_name", "weight_dtype"],
    "CLIPLoader": ["clip_name", "type", "device"],
    "VAELoader": ["vae_name"],
    "ModelSamplingSD3": ["shift"],
    "EmptyHunyuanLatentVideo": ["width", "height", "length", "batch_size"],
    "CreateVideo": ["fps"],
    # « format » et « codec » : la forme d'avant les listes dynamiques, que ComfyUI 0.35.1
    # comprend encore (une entrée « codec » cachée, comfy_extras/nodes_video.py)
    "SaveVideo": ["filename_prefix", "format", "codec"],
}
NOTES = {"Note", "MarkdownNote"}          # des commentaires de l'éditeur : jamais dans le graphe

HF = "https://huggingface.co/{repo}/resolve/{commit}/{path}"
WAN_REPO, WAN_COMMIT = "Comfy-Org/Wan_2.1_ComfyUI_repackaged", "123acf1cc74bccbb9bfff8ac1ee72edc08c2341d"
SDXL_LICENSE = "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/LICENSE.md"
WAN_LICENSE = "https://huggingface.co/Wan-AI/Wan2.1-T2V-1.3B/blob/main/LICENSE.txt"

# La table écrite à la main, par gabarit : identifiant, textes, paramètres (cibles dans le
# graphe du gabarit ; défaut : la valeur du gabarit), modèles (cible → clé, licence, adresse
# officielle ; empreinte et taille quand on les connaît), sorties, mémoire vidéo visée
TEMPLATES = {
    "image_sdxl_simple": {
        "id": "comfy-templates/image_sdxl_simple", "revision": 1, "kind": "image",
        "title": {"fr": "SDXL 1.0 · image", "en": "SDXL 1.0 · image"},
        "about": {"fr": "Le gabarit « SDXL1.0: Text to Image (Simple) » de ComfyUI : une image "
                        "d'après sa description, en 25 pas. Un seul fichier de modèle ; une carte "
                        "de 8 Go suffit.",
                  "en": "ComfyUI's \"SDXL1.0: Text to Image (Simple)\" template: an image from "
                        "its description, in 25 steps. A single model file; an 8 GB graphics "
                        "card is enough."},
        "seed": ["12.seed"],
        "params": [
            {"name": "prompt", "type": "text", "targets": ["10.text"]},
            {"name": "negative", "type": "text", "targets": ["11.text"]},
            {"name": "seed", "type": "seed", "targets": ["12.seed"]},
            {"name": "width", "type": "int", "min": 512, "max": 1536, "step": 64,
             "targets": ["13.width"]},
            {"name": "height", "type": "int", "min": 512, "max": 1536, "step": 64,
             "targets": ["13.height"]},
            {"name": "steps", "type": "int", "min": 1, "max": 60, "targets": ["12.steps"]},
            {"name": "cfg", "type": "float", "min": 1.0, "max": 15.0, "step": 0.5,
             "targets": ["12.cfg"]},
        ],
        "models": {
            "15.ckpt_name": {
                "key": "checkpoint", "folder": "checkpoints",
                # Haché le 29/10/2026 sur le PC de l'auteur (dossier partagé de Comfy Desktop) :
                # l'empreinte publiée par Stability AI
                "sha256": "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b",
                "size": 6938078334,
                "license": "CreativeML Open RAIL++-M", "license_url": SDXL_LICENSE,
                "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/"
                       "main/sd_xl_base_1.0.safetensors"},
        },
        "outputs": {"7": "image"},
        # Mesuré le 21/10/2026 (partage-puissance.md § 3.6) : deux images SDXL 768 × 768 en
        # 10,6 s puis 4,4 s sur une RTX 4070 portable (8 Go) ; 1024 × 1024 en 25 pas : estimé
        "vram_mib": 8192, "typical_seconds": {"8": 15},
    },
    "text_to_video_wan": {
        "id": "comfy-templates/text_to_video_wan", "revision": 1, "kind": "video",
        "title": {"fr": "Wan 2.1 1.3B · vidéo", "en": "Wan 2.1 1.3B · video"},
        "about": {"fr": "Le gabarit « Wan 2.1 Text to Video » de ComfyUI : une courte vidéo "
                        "(2 s à 16 images par seconde par défaut) d'après sa description. "
                        "Trois fichiers de modèle ; une carte de 8 Go suffit.",
                  "en": "ComfyUI's \"Wan 2.1 Text to Video\" template: a short video (2 s at 16 "
                        "frames per second by default) from its description. Three model files; "
                        "an 8 GB graphics card is enough."},
        "seed": ["3.seed"],
        "params": [
            {"name": "prompt", "type": "text", "targets": ["6.text"]},
            {"name": "negative", "type": "text", "targets": ["7.text"]},
            {"name": "seed", "type": "seed", "targets": ["3.seed"]},
            {"name": "width", "type": "int", "min": 256, "max": 1024, "step": 16,
             "targets": ["40.width"]},
            {"name": "height", "type": "int", "min": 256, "max": 1024, "step": 16,
             "targets": ["40.height"]},
            {"name": "frames", "type": "int", "min": 5, "max": 81, "step": 4,
             "targets": ["40.length"]},
            {"name": "fps", "type": "float", "min": 8.0, "max": 30.0, "step": 1.0,
             "targets": ["49.fps"]},
            {"name": "steps", "type": "int", "min": 4, "max": 50, "targets": ["3.steps"]},
            {"name": "cfg", "type": "float", "min": 1.0, "max": 12.0, "step": 0.5,
             "targets": ["3.cfg"]},
        ],
        "models": {
            # Les mêmes fichiers que le Wan 2.1 de stable-diffusion.cpp (imaging/catalog.py),
            # au même commit : hachés par AIODOS à leur téléchargement
            "37.unet_name": {
                "key": "diffusion", "folder": "diffusion_models",
                "sha256": "be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5",
                "size": 2838303560, "license": "Apache-2.0", "license_url": WAN_LICENSE,
                "url": HF.format(repo=WAN_REPO, commit=WAN_COMMIT,
                                 path="split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors")},
            # Pas sur le PC de l'auteur le 29/10/2026 : empreinte et taille à relever par
            # « hash » une fois le fichier là (ComfyUI Desktop le propose à l'ouverture du
            # gabarit) ; d'ici là il ne se télécharge pas par AIODOS, et la recette ne se signe pas
            "38.clip_name": {
                "key": "text_encoder", "folder": "text_encoders", "sha256": None, "size": None,
                "license": "Apache-2.0", "license_url": WAN_LICENSE,
                "url": HF.format(repo=WAN_REPO, commit="main",
                                 path="split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors")},
            "39.vae_name": {
                "key": "vae", "folder": "vae",
                "sha256": "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
                "size": 253815318, "license": "Apache-2.0", "license_url": WAN_LICENSE,
                "url": HF.format(repo=WAN_REPO, commit=WAN_COMMIT,
                                 path="split_files/vae/wan_2.1_vae.safetensors")},
        },
        "outputs": {"50": "video"},
        # Estimé : 33 images en 832 × 480, 30 pas, sur une carte de 8 Go (à mesurer, M2)
        "vram_mib": 8192, "typical_seconds": {"8": 240},
    },
}


def fail(text: str):
    sys.exit(text)


def write(path: Path, doc: dict):
    """En UTF-8, fins de ligne LF (jamais la traduction de Windows), une fin de ligne finale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def load(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8"))


# ------------------------------------------------------------------ les gabarits
def installs() -> list:
    """Les dossiers de code (ComfyUI) des ComfyUI de ce PC : ceux de Comfy Desktop, puis la
    version portable. Sans les réglages d'AIODOS (engines/comfy_find.py les lirait dans sa
    base) : l'outil ne touche jamais aux données d'un nœud."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    found = sorted((local / "Comfy-Desktop" / "ComfyUI-Installs").glob("*/ComfyUI"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    found += [p / "ComfyUI" for p in (Path.home() / "ComfyUI_windows_portable",
                                      Path("C:/ComfyUI_windows_portable"))]
    return [p for p in found if (p / "main.py").is_file()]


def templates_folder() -> tuple:
    """(dossier des gabarits JSON, version de comfyui-workflow-templates, version du paquet
    JSON) du ComfyUI installé sur ce PC, lus sans rien lancer."""
    for comfyui in installs():
        for site in (comfyui / ".venv" / "Lib" / "site-packages",
                     comfyui.parent / "python_embeded" / "Lib" / "site-packages"):
            folder = site / "comfyui_workflow_templates_json" / "templates"
            if folder.is_dir():
                return folder, _version(site, "comfyui_workflow_templates"), \
                    _version(site, "comfyui_workflow_templates_json")
    fail("Gabarits de ComfyUI introuvables : --templates <dossier des gabarits JSON>.")


def _version(site: Path, name: str) -> str | None:
    for info in sorted(site.glob(f"{name}-*.dist-info")):
        found = re.search(r"^Version:\s*(\S+)", (info / "METADATA").read_text(encoding="utf-8"),
                          re.MULTILINE)
        if found:
            return found.group(1)
    return None


def to_api(template: dict, name: str) -> dict:
    """Le graphe de l'éditeur au format API, sans rien ajouter : les nœuds (sauf les notes),
    leurs valeurs nommées d'après WIDGETS, leurs liens."""
    if (template.get("definitions") or {}).get("subgraphs"):
        fail(f"{name} : gabarit à sous-graphes, non pris en charge.")
    links = {link[0]: link for link in template.get("links") or []}
    graph = {}
    for node in template["nodes"]:
        cls = node["type"]
        if cls in NOTES:
            continue
        if node.get("mode", 0) != 0:
            fail(f"{name} : le nœud {node['id']} ({cls}) est contourné ou muet.")
        names = WIDGETS.get(cls)
        if names is None:
            fail(f"{name} : classe {cls} sans table de ses valeurs (WIDGETS).")
        values = node.get("widgets_values") or []
        if len(values) != len(names):
            fail(f"{name} : {cls} a {len(values)} valeurs, la table en attend {len(names)}.")
        inputs = {key: value for key, value in zip(names, values) if key}
        for slot in node.get("inputs") or []:
            if slot.get("link") is not None:
                link = links[slot["link"]]
                inputs[slot["name"]] = [str(link[1]), int(link[2])]
        graph[str(node["id"])] = {"class_type": cls, "inputs": inputs}
    return dict(sorted(graph.items(), key=lambda kv: int(kv[0])))


def _at(graph, target):
    node, _, name = target.partition(".")
    return graph[node]["inputs"][name]


def _put(graph, target, value):
    node, _, name = target.partition(".")
    if node not in graph or name not in graph[node]["inputs"]:
        fail(f"Cible absente du graphe : {target}.")
    graph[node]["inputs"][name] = value


def recipe_of(name: str, table: dict, template: dict, raw: bytes, versions: tuple,
              known: dict | None) -> dict:
    graph = to_api(template, name)
    models = []
    for target, model in table["models"].items():
        filename = _at(graph, target)
        _put(graph, target, comfy_graph.TOKEN_MODEL + model["key"])
        before = next((m for m in (known or {}).get("models") or []
                       if m.get("name") == filename), {})
        models.append({"key": model["key"], "folder": model["folder"], "name": filename,
                       "sha256": model.get("sha256") or before.get("sha256"),
                       "size": model.get("size") or before.get("size"),
                       "license": model["license"], "license_url": model["license_url"],
                       "url": model["url"]})
    params = []
    for param in table["params"]:
        found = dict(param)
        if param["type"] != "seed":
            current = _at(graph, param["targets"][0])
            found["default"] = float(current) if param["type"] == "float" else current
            if param["type"] == "text":
                found["max_length"] = recipes.MAX_TEXT
        params.append(found)
    for target in table["seed"]:
        _put(graph, target, comfy_graph.TOKEN_SEED)
    classes = sorted({node["class_type"] for node in graph.values()})
    contract = (known or {}).get("contract") or {}
    prints = contract.get("classes") or {}
    return {
        "id": table["id"], "version": f"{versions[0]}-{table['revision']}", "kind": table["kind"],
        "title": table["title"], "about": table["about"],
        "source": {"template": name, "package": PACKAGE, "version": versions[0],
                   "json_version": versions[1], "sha256": hashlib.sha256(raw).hexdigest(),
                   "license": "MIT", "url": PACKAGE_URL},
        "graph": graph, "params": params, "models": models,
        "contract": {"comfyui": contract.get("comfyui"), "core": contract.get("core"),
                     "classes": {cls: prints.get(cls) for cls in classes}},
        "outputs": [{"node": node, "kind": kind} for node, kind in table["outputs"].items()],
        "inputs": [], "vram_mib": table["vram_mib"],
        "typical_seconds": table["typical_seconds"],
        "verify": {"ssim": None, "content": "prompt_filter"},
    }


def cmd_build(args):
    if args.templates:
        folder, versions = Path(args.templates), (args.version, args.json_version)
        if not versions[0]:
            fail("--version : la version de comfyui-workflow-templates de ce dossier.")
    else:
        found = templates_folder()
        folder, versions = found[0], found[1:]
    out = Path(args.out) if args.out else DEFAULT
    previous = {r["id"]: r for r in load(out)["recipes"]} if out.exists() else {}
    built = []
    for name, table in TEMPLATES.items():
        raw = (folder / f"{name}.json").read_bytes()
        recipe = recipe_of(name, table, json.loads(raw.decode("utf-8")), raw, versions,
                           previous.get(table["id"]))
        recipes.check(recipe)
        built.append(recipe)
        print(f"{recipe['id']} {recipe['version']} : {len(recipe['graph'])} nœuds, "
              f"{len(recipe['params'])} paramètres, {len(recipe['models'])} modèles")
    write(out, {"kind": catalogue.KIND, "format": catalogue.FORMAT, "date": DATE,
                "templates": {"package": PACKAGE, "version": versions[0], "license": "MIT",
                              "url": PACKAGE_URL},
                "recipes": built})
    print(f"Écrit : {out}")


# ----------------------------------------------------------------- vérifier
def cmd_check(args):
    path = Path(args.file) if args.file else DEFAULT
    data = path.read_bytes()
    try:
        found = catalogue.parse(data)
    except catalogue.CatalogueError as exc:
        fail(f"{path.name} : {exc}")
    sig = path.with_name(path.name + ".sig")
    if sig.exists():
        try:
            key = catalogue.verify(data, json.loads(sig.read_text(encoding="utf-8")))
            print(f"Signé par la clé {key}.")
        except (catalogue.CatalogueError, ValueError) as exc:
            fail(f"Signature refusée : {exc}")
    else:
        print("Non signé : ses recettes ne servent que localement (jamais au réseau public).")
    for recipe in found:
        unpinned = [m["name"] for m in recipe["models"] if not (m.get("sha256") and m.get("size"))]
        unprinted = [c for c, v in recipe["contract"]["classes"].items() if not v]
        print(f"- {recipe['id']} {recipe['version']} ({recipes.digest(recipe)[:16]})"
              + (f" ; sans empreinte : {', '.join(unpinned)}" if unpinned else "")
              + (f" ; contrat à calculer ({len(unprinted)} classes)" if unprinted else ""))


# -------------------------------------------------------------- les empreintes
def model_roots(extra) -> tuple:
    """(dossiers de modèles de ComfyUI, dossiers à plat des modèles d'images d'AIODOS)."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    roots = [Path(p) for p in extra or []]
    roots += [local / "Comfy-Desktop" / "ComfyUI-Shared" / "models",
              Path.home() / "Documents" / "ComfyUI" / "models"]
    roots += [comfyui / "models" for comfyui in installs()]
    flat = [ROOT / "data" / "images" / "models", local / "AIODOS" / "data" / "images" / "models"]
    return roots, flat


def find(model, roots, flat) -> Path | None:
    for root in roots:
        path = root / model["folder"] / model["name"].replace("\\", "/")
        if path.is_file():
            return path
    for folder in flat:                       # les modèles d'images d'AIODOS : à plat
        path = folder / Path(model["name"]).name
        if path.is_file():
            return path
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cmd_hash(args):
    path = Path(args.file) if args.file else DEFAULT
    doc = load(path)
    roots, flat = model_roots(args.roots)
    changed = False
    for recipe in doc["recipes"]:
        for model in recipe["models"]:
            if model.get("sha256") and model.get("size") and not args.all:
                continue
            found = find(model, roots, flat)
            if found is None:
                print(f"- {model['name']} : absent de ce PC")
                continue
            size, digest = found.stat().st_size, sha256_of(found)
            if model.get("sha256") and (model["sha256"], model.get("size")) != (digest, size):
                print(f"- {model['name']} : {found} DIFFÈRE de l'empreinte déclarée : laissé tel quel")
                continue
            model["sha256"], model["size"] = digest, size
            changed = True
            print(f"- {model['name']} : {digest} ({size} octets, {found})")
    if changed:
        write(path, doc)
        print(f"Écrit : {path} (à resigner : sign)")


def cmd_contract(args):
    path = Path(args.file) if args.file else DEFAULT
    if args.object_info:
        defs, version = json.loads(Path(args.object_info).read_text(encoding="utf-8")), args.version
    elif args.url:
        from aiodos.engines import comfy_client
        client = comfy_client.Client(args.url)
        defs = client.object_info()
        version = str((client.system_stats().get("system") or {}).get("comfyui_version") or "")
    else:
        fail("--url ou --object-info.")
    doc = load(path)
    for recipe in doc["recipes"]:
        wrong = [(cls, why) for cls, why in recipes.not_core(recipe, defs) if why != "different"]
        if wrong:
            fail(f"{recipe['id']} : {', '.join(f'{c} ({w})' for c, w in wrong)} : refusé.")
        recipe["contract"] = {"comfyui": version or None, "core": comfy_contract.core(defs),
                              "classes": {cls: comfy_contract.fingerprint(defs[cls])
                                          for cls in sorted(recipe["contract"]["classes"])}}
        print(f"- {recipe['id']} : {len(recipe['contract']['classes'])} classes du cœur")
    write(path, doc)
    print(f"Écrit : {path} (à resigner : sign)")


# ------------------------------------------------------------------- signer
def cmd_sign(args):
    path = Path(args.file) if args.file else DEFAULT
    from aiodos.updates import private
    try:
        private.require_private_repository(str(ROOT))
    except private.NotPrivate as exc:
        fail(f"{exc}\nRien n'a été signé.")
    data = path.read_bytes()
    try:
        found = catalogue.parse(data)
    except catalogue.CatalogueError as exc:
        fail(f"{path.name} : {exc}")
    for recipe in found:
        if any(not (m.get("sha256") and m.get("size")) for m in recipe["models"]):
            fail(f"{recipe['id']} : un modèle sans empreinte ni taille (hash) : rien n'est signé.")
        if not all(recipe["contract"]["classes"].values()):
            fail(f"{recipe['id']} : contrat à calculer (contract) : rien n'est signé.")
    sys.path.insert(0, str(ROOT / "tools"))
    import publish
    envelope = catalogue.sign(data, publish.load_key())
    try:
        catalogue.verify(data, envelope)
    except catalogue.CatalogueError as exc:
        fail(f"Signature faite, mais refusée par cette version : {exc} (clé dans keys.py ?)")
    sig = path.with_name(path.name + ".sig")
    sig.write_bytes((json.dumps(envelope) + "\n").encode("utf-8"))
    print(f"Signé par la clé {envelope['key']} : {sig} ({catalogue.digest_of(path)[:16]})")


def main():
    for stream in (sys.stdout, sys.stderr):    # accents lisibles même dans un tube (Git Bash)
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="comfy_recipes",
                                     description="Catalogue des recettes de ComfyUI")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="les recettes, d'après les gabarits officiels")
    build.add_argument("--templates", help="dossier des gabarits JSON (défaut : le ComfyUI de ce PC)")
    build.add_argument("--version", help="avec --templates : version de comfyui-workflow-templates")
    build.add_argument("--json-version", help="avec --templates : version du paquet JSON")
    build.add_argument("--out", help=f"fichier écrit (défaut : {DEFAULT.name})")
    build.set_defaults(run=cmd_build)
    check = commands.add_parser("check", help="vérifier un catalogue et sa signature")
    check.add_argument("file", nargs="?")
    check.set_defaults(run=cmd_check)
    hashing = commands.add_parser("hash", help="hacher les modèles présents sur ce PC")
    hashing.add_argument("file", nargs="?")
    hashing.add_argument("--roots", nargs="*", help="dossiers de modèles en plus")
    hashing.add_argument("--all", action="store_true", help="tous les modèles, pas seulement ceux sans empreinte")
    hashing.set_defaults(run=cmd_hash)
    contract = commands.add_parser("contract", help="le contrat des classes, d'après un ComfyUI")
    contract.add_argument("file", nargs="?")
    contract.add_argument("--url", help="un ComfyUI ouvert de ce PC (lu, jamais lancé)")
    contract.add_argument("--object-info", help="un object_info enregistré")
    contract.add_argument("--version", help="avec --object-info : la version de ce ComfyUI")
    contract.set_defaults(run=cmd_contract)
    sign = commands.add_parser("sign", help="signer avec la clé de publication (l'auteur)")
    sign.add_argument("file", nargs="?")
    sign.set_defaults(run=cmd_sign)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
