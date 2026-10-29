"""Créer avec ComfyUI (docs/conception/comfyui-createur.md § 3.2 et § 3.3) : une recette
(engines/recipes.py, du catalogue engines/catalogue.py) ou un de mes workflows importés
(engines/comfy_workflows.py), ses paramètres revérifiés contre leurs bornes, devient un
travail de la file, genre « comfy_create » : une unité engine_unit par image ou vidéo demandée
(count), chacune sa graine (celle donnée, puis les suivantes), aux paramètres de la forme de
ceux des applis (jobs/kinds/engine.normalize, qui refait tous ses contrôles : forme, valeurs
interdites, nœuds du cloud, sorties, modèles).

- Où : dans le ComfyUI de ce PC (celui de l'utilisateur, ouvert, ou celui qu'AIODOS lance sans
  fenêtre : engines/comfy_instance.py), ou chez une de mes machines qui a ComfyUI et les
  modèles, comme les travaux ComfyUI des applis (L2b : engine_unit est mine_only, jamais un
  ami ni un inconnu ; reach « here,mine »). Mes workflows ne quittent donc jamais mes machines.
- Le graphe d'une recette est construit à partir de la recette (recipes.build), jamais reçu ;
  avec les définitions du ComfyUI de ce PC, chacune de ses classes doit être un nœud de
  ComfyUI (recipes.check_here : ni nœud ajouté, ni service en ligne). Une recette non signée
  sert ici et chez mes machines (catalogue.recipe(…, public=False)), jamais au réseau public.
- Le rangement (assimilate, appelé par jobs/kinds/engine.assimilate : OWNERS) : chaque sortie
  image ou vidéo entre dans la galerie comme une création de sd.cpp (imaging/gallery.add_media),
  avec sa description, sa taille, sa graine, la machine qui l'a calculée et son temps.
"""
import base64
import secrets
import shutil
from pathlib import Path

from aiodos import i18n, paths
from aiodos.engines import catalogue, comfy_graph, comfy_instance, comfy_workflows, machines
from aiodos.engines import media, recipes
from aiodos.i18n import t
from aiodos.imaging import gallery
from aiodos.jobs.kinds import engine
from aiodos.jobs.registry import register_job_kind

KIND = "comfy_create"
WHERE = ("here", "mine")
MAX_COUNT = {"image": 16, "video": 4}
UNIT_SECONDS = {"image": 1800, "video": 4 * 3600}
SEED_SPACE = 2 ** 32
# Ce que la galerie retient d'une création (les paramètres du même nom, s'ils existent)
SHOWN = ("prompt", "negative", "width", "height", "steps", "cfg", "frames", "fps")


class CreateError(Exception):
    """Création refusée : code stable, message, statut HTTP."""

    def __init__(self, code: str, message: str, status: int = 400, **detail):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail


def _count(value, kind) -> int:
    if value in (None, ""):
        return 1
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_COUNT[kind]:
        raise CreateError("invalid_request", t("comfy_create.count", max=MAX_COUNT[kind]))
    return value


def _short(text: str, n: int = 50) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _source(body: dict, live_defs):
    """(propriétaire : recette ou workflow, ce qu'en dit la création, définitions des
    consignes, modèles déclarés ou « auto »)."""
    recipe_id, workflow_id = body.get("recipe"), body.get("workflow")
    if bool(recipe_id) == bool(workflow_id):
        raise CreateError("invalid_request", t("comfy_create.recipe_or_workflow"))
    if recipe_id:
        try:
            recipe = catalogue.recipe(str(recipe_id), public=False)
            if live_defs:
                recipes.check_here(recipe, live_defs)
        except catalogue.CatalogueError as exc:
            raise CreateError(exc.code, str(exc), 404) from exc
        except recipes.RecipeError as exc:
            raise CreateError(exc.code, str(exc), 409) from exc
        about = {"source": "recipe", "id": recipe["id"], "version": recipe["version"],
                 "digest": recipes.digest(recipe), "name": recipes.localized(recipe["title"]),
                 "model": f"comfyui/{recipe['id']}", "kind": recipe["kind"]}
        return recipe, about, recipes.text_defs(recipe), recipes.declared_models(recipe)
    try:
        workflow = comfy_workflows.get(str(workflow_id))
    except comfy_workflows.WorkflowError as exc:
        raise CreateError(exc.code, str(exc), 404) from exc
    about = {"source": "workflow", "id": workflow["id"], "name": workflow["name"],
             "model": f"comfyui/workflow/{workflow['id']}", "kind": workflow.get("kind", "image")}
    return workflow, about, comfy_workflows.text_defs(workflow), "auto"


def normalize(body, inventory: dict | None = None) -> dict:
    """La demande vérifiée → les paramètres du travail (ceux d'un travail de moteur, plus
    « creation ») ; CreateError sinon. inventory : celui du ComfyUI de ce PC s'il est ouvert
    (engines/comfyui.inventory), sinon None : la machine qui calculera revérifie tout."""
    if not isinstance(body, dict):
        raise CreateError("invalid_request", t("gateway_api.json_not_object"))
    unknown = sorted(set(body) - {"recipe", "workflow", "params", "count"})
    if unknown:
        raise CreateError("invalid_request", t("engine_jobs.unknown_field", field=unknown[0][:40]))
    live_defs = (inventory or {}).get("defs") or None
    owner, about, texts, models = _source(body, live_defs)
    count = _count(body.get("count"), about["kind"])
    try:
        chosen = recipes.values(owner, body.get("params"))
    except recipes.RecipeError as exc:
        raise CreateError(exc.code, str(exc), **exc.detail) from exc
    graph = recipes.build(owner, chosen)
    seed = chosen.get("seed")
    seed = secrets.randbelow(SEED_SPACE) if seed is None else seed
    seeded = comfy_graph.tokens(graph)["seed"]
    items = [{"seed": (seed + i) % recipes.SEED_MAX if seeded else None, "set": []}
             for i in range(count)]
    # Sans le ComfyUI de ce PC : les consignes de la recette ou du workflow, pour que
    # check_values ne regarde dans une description que le caractère nul
    seen = inventory if live_defs else {"defs": texts}
    request = {"engine": "comfyui", "graph": graph, "models": models, "items": items,
               "priority": "high", "bounds": {"unit_seconds": UNIT_SECONDS[about["kind"]]}}
    try:
        params = engine.normalize(request, app_id=None, where=list(WHERE), inventory=seen)
    except engine.Invalid as exc:
        raise CreateError(exc.code, str(exc), exc.status or 400, **exc.detail) from exc
    params["creation"] = {**about, "count": count, "seed": seed, "batch": secrets.token_hex(6),
                          **{k: chosen[k] for k in SHOWN if k in chosen}}
    return params


# ------------------------------------------------------------------- où
def places(params: dict, inventory: dict | None) -> dict:
    """{"here": ce PC peut-il la calculer, "machines": mes machines qui ont ComfyUI et ses
    modèles} : sans l'un ni l'autre, rien n'est mis en file."""
    wanted = list(params["models"].values())
    here = False
    if engine.instance_here() is not None and inventory is not None:
        here = not engine.missing_here(params, inventory)
    elif comfy_instance.launchable():
        have = comfy_instance.disk_models()
        here = not machines.missing_on({"models": have}, wanted)
    names = [m["name"] for m in machines.known(["mine"]) if not machines.missing_on(m, wanted)]
    return {"here": here, "machines": names}


# -------------------------------------------------------------- découper
def split(params):
    """Les unités d'un travail de moteur (une par image ou vidéo), sous un titre traduit dans
    la langue de qui le lit : « SDXL 1.0 · image : « … » ×2 »."""
    _, units, _ = engine.split(params)
    creation = params["creation"]
    title = i18n.lazy("comfy_create.job_title", name=creation["name"],
                      prompt=_short(creation.get("prompt") or ""), n=creation["count"])
    return title, units, {"batch": creation["batch"]}


# --------------------------------------------------------------- ranger
def assimilate(order, output) -> dict:
    """Les fichiers produits pour une unité, dans la galerie (chez le propriétaire : ce PC),
    depuis le dossier de calcul (calculés ici) ou leurs octets (une de mes machines). Les
    sorties qui ne sont ni une image ni une vidéo (un son…) ne sont pas gardées."""
    local = order.get("local") or {}
    params = engine.params_of(local["job_id"])
    creation = params["creation"]
    item = int(local.get("item") or 0)
    seed = order.get("seed")
    meta = {**creation, "index": item,
            "seed": seed if seed is not None else (creation["seed"] + item) % recipes.SEED_MAX}
    folder = paths.engine_runs_dir() / order["run_uuid"]
    machine = str(output.get("machine") or "")[:80]
    ids, n = [], 0
    try:
        for f in output.get("files") or []:
            if "data" in f:                       # calculé par une de mes machines
                source = base64.b64decode(str(f["data"]))
                kind = media.sniff(source[:media.HEAD]) or "application/octet-stream"
            else:
                source, kind = Path(f["path"]), str(f["type"])
                if not source.is_file() or source.stat().st_size != int(f["size"]):
                    raise ValueError(t("engine_jobs.output_missing"))
            if kind not in gallery.MEDIA:
                continue
            n += 1
            row = gallery.add_media(meta, source, kind, machine=machine,
                                    seconds=_seconds(output.get("seconds")), n=n)
            ids.append(row["id"])
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if not ids:
        raise ValueError(t("comfy_create.no_media"))
    return {"images": ids, "image_id": ids[0], "seed": meta["seed"],
            "machine": output.get("machine"), "engine": output.get("engine")}


def _seconds(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def finalize(job_id, params, units) -> dict:
    engine.forget_params(job_id)
    done = [u for u in units if u["status"] == "done"]
    return {"images": sum(len((u["result"] or {}).get("images") or []) for u in done),
            "batch": params["creation"]["batch"],
            "machines": sorted({(u["result"] or {}).get("machine") or "?" for u in done})}


engine.register_owner(KIND, assimilate)
register_job_kind(KIND, split, finalize)
