"""Créer avec ComfyUI depuis la vue « Images et vidéos » (comfyui-createur.md § 3.2) : l'état du
ComfyUI de ce PC (ouvert, lancé par AIODOS, trouvé, absent : alors « Installer ComfyUI » ouvre
la page officielle de téléchargement, AIODOS ne télécharge rien lui-même dans cette version),
les recettes du catalogue et leurs modèles (présents, ou à télécharger sur le clic, licence
affichée), l'import de mes workflows, et « Créer ».

    GET    /api/engines/comfyui/recipes          l'état, les recettes, mes workflows, le
                                                 téléchargement en cours
    POST   /api/engines/comfyui/models/download  {recipe, key} : télécharger un modèle d'une recette
    DELETE /api/engines/comfyui/models/download  l'arrêter
    GET    /api/engines/comfyui/workflows        mes workflows
    POST   /api/engines/comfyui/workflows        importer un workflow (le fichier JSON tel quel,
                                                 son nom dans X-File-Name), 2 Mo au plus
    PATCH  /api/engines/comfyui/workflows/{id}   {name?, params?}
    DELETE /api/engines/comfyui/workflows/{id}
    POST   /api/engines/comfyui/create           {recipe | workflow, params, count}
"""
import asyncio
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos import settings
from aiodos.engines import (catalogue, comfy_client, comfy_download, comfy_find, comfy_instance,
                            comfy_workflows, comfyui, machines, recipes)
from aiodos.i18n import t
from aiodos.jobs import queue
from aiodos.jobs.kinds import comfy_create, engine

router = APIRouter(prefix="/api/engines/comfyui")
INSTALL_URL = "https://www.comfy.org/download"


class DownloadIn(BaseModel):
    recipe: str
    key: str


class WorkflowPatch(BaseModel):
    name: str | None = None
    params: list | None = None


# --------------------------------------------------------------------- l'état
def _installable() -> bool:
    """ComfyUI Desktop existe pour Windows, macOS et Linux ; jamais sur un téléphone."""
    from aiodos.node import android
    return not android.is_android()


def _inventory():
    """L'inventaire du ComfyUI de ce PC s'il est accordé et ouvert, sinon None."""
    found = engine.instance_here()
    if found is None:
        return None
    try:
        return comfyui.inventory(found.url)
    except comfy_client.Unreachable:
        return None


def comfy_state() -> dict:
    installs = comfy_find.installs()
    found = comfy_find.instance()
    launched = comfy_instance.current()
    first = installs[0] if installs else None
    running = found or launched
    state = "open" if found else "launched" if launched else "found" if first else "absent"
    return {"state": state, "granted": bool(settings.get("engines_comfyui_enabled")),
            "launch": bool(settings.get("engines_comfyui_launch")),
            "version": (running.version if running else None) or (first.version if first else None),
            "kind": first.kind if first else None, "url": found.url if found else None,
            "vram_mib": running.vram_mib if running else None,
            "installable": _installable(), "install_url": INSTALL_URL}


def _recipes(inventory) -> list:
    found = catalogue.read()
    mine = machines.known(["mine"])
    have = (inventory or {}).get("models") or {}
    out = []
    for recipe in found.recipes.values():
        listed = recipes.summary(recipe)
        for model in listed["models"]:
            model["state"] = comfy_download.state(model)
            if model["state"] != "present" and comfyui.local_name(have, model["folder"],
                                                                  model["name"]):
                model["state"] = "present"           # vu par ComfyUI (un autre dossier réglé)
            model["pinned"] = comfy_download.pinned(model)
        wanted = [{"folder": m["folder"], "name": m["name"]} for m in recipe["models"]]
        listed.update(
            signed=recipe["id"] in found.signed,
            ready_here=all(m["state"] == "present" for m in listed["models"]),
            machines=[m["name"] for m in mine if not machines.missing_on(m, wanted)])
        out.append(listed)
    return out


def overview() -> dict:
    inventory = _inventory()
    found = catalogue.read()
    return {"comfyui": comfy_state(), "recipes": _recipes(inventory),
            "workflows": comfy_workflows.listing(), "download": comfy_download.progress(),
            "catalogue": {"files": sorted(set(found.files.values())), "signed": sorted(found.signed),
                          "errors": [{"file": f, "error": e} for f, e in found.errors]}}


@router.get("/recipes")
async def list_recipes():
    return await asyncio.to_thread(overview)


# -------------------------------------------------------------- les modèles
@router.post("/models/download")
def start_download(body: DownloadIn, request: Request):
    bus = request.app.state.bus
    try:
        recipe = catalogue.recipe(body.recipe)
        return comfy_download.start(recipe, body.key,
                                    changed=lambda: bus.publish({"type": "images"}))
    except catalogue.CatalogueError as exc:
        raise HTTPException(404, str(exc))
    except comfy_download.DownloadError as exc:
        raise HTTPException(409, str(exc))


@router.delete("/models/download")
def cancel_download():
    comfy_download.cancel()
    return comfy_download.progress()


# ------------------------------------------------------------- mes workflows
async def _limited(request: Request, limit: int) -> bytes:
    """Le corps, sans jamais en lire plus de limit octets (413 au-delà)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, t("comfy_workflows.too_big", mb=limit // 1024 ** 2))
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, t("comfy_workflows.too_big", mb=limit // 1024 ** 2))
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/workflows")
async def list_workflows():
    return await asyncio.to_thread(comfy_workflows.listing)


@router.post("/workflows")
async def import_workflow(request: Request):
    raw = await _limited(request, comfy_workflows.MAX_BYTES)
    name = unquote(request.headers.get("x-file-name") or "").removesuffix(".json")

    def store():
        inventory = _inventory()
        workflow = comfy_workflows.load(raw, name, (inventory or {}).get("defs"))
        return comfy_workflows.summary(comfy_workflows.add(workflow))
    try:
        return await asyncio.to_thread(store)
    except comfy_workflows.WorkflowError as exc:
        raise HTTPException(413 if exc.code == "workflow_too_big" else 400, str(exc))


@router.patch("/workflows/{workflow_id}")
def patch_workflow(workflow_id: str, body: WorkflowPatch):
    try:
        return comfy_workflows.summary(comfy_workflows.update(workflow_id, body.name, body.params))
    except comfy_workflows.WorkflowError as exc:
        raise HTTPException(404 if exc.code == "workflow_unknown" else 400, str(exc))


@router.delete("/workflows/{workflow_id}")
def delete_workflow(workflow_id: str):
    try:
        if not comfy_workflows.delete(workflow_id):
            raise HTTPException(404, t("comfy_workflows.unknown"))
    except comfy_workflows.WorkflowError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


# ------------------------------------------------------------------- créer
def _create(body: dict) -> tuple:
    inventory = _inventory()
    params = comfy_create.normalize(body, inventory)
    where = comfy_create.places(params, inventory)
    if not where["here"] and not where["machines"]:
        key = "comfy_create.nowhere" if settings.get("engines_comfyui_enabled") \
            else "comfy_create.not_granted"
        raise comfy_create.CreateError("nowhere", t(key), 409)
    job_id = queue.create_job(comfy_create.KIND, params, engine.priority_of(params),
                              reach=",".join(comfy_create.WHERE))
    return job_id, params, where


@router.post("/create")
async def create(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, t("gateway_api.json_not_object"))
    try:
        job_id, params, where = await asyncio.to_thread(_create, body)
    except comfy_create.CreateError as exc:
        raise HTTPException(exc.status, str(exc))
    executor = request.app.state.executor
    executor.wake()
    executor.publish_job(job_id, force=True)
    creation = params["creation"]
    return {"job_id": job_id, "batch": creation["batch"], "seed": creation["seed"],
            "count": creation["count"], "here": where["here"], "machines": where["machines"]}
