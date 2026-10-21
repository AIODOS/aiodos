"""Les moteurs vus par une appli (docs/conception/partage-puissance.md § 6.5) :
GET /aiodos/v1/engines dit qui a ComfyUI et quels modèles, selon les cercles de l'appli ;
POST /aiodos/v1/engines/comfyui/plan vérifie un travail comme POST /aiodos/v1/jobs, sans
rien mettre en file (« pré-vol »). Cette version calcule sur ce PC ; mes machines suivront."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aiodos import settings
from aiodos.engines import comfy_client, comfyui
from aiodos.gateway import errors, scopes
from aiodos.gateway.errors import ApiError
from aiodos.jobs.kinds import engine

router = APIRouter(prefix="/aiodos/v1")
CAPABILITY = "engines:comfyui"


def _circles(request: Request) -> set:
    allowed = scopes.for_request(request, CAPABILITY)
    return set(scopes.CIRCLES) if allowed is None else set(allowed)


def _here():
    """(instance, inventaire) du ComfyUI accordé de ce PC, ou (None, None)."""
    found = engine.instance_here()
    if found is None:
        return None, None
    try:
        return found, comfyui.inventory(found.url)
    except comfy_client.Unreachable:
        return None, None


def describe(circles: set) -> dict:
    found, inv = _here() if "here" in circles else (None, None)
    models = []
    for folder, names in sorted(((inv or {}).get("models") or {}).items()):
        for name in names:
            models.append({"folder": folder, "name": name.replace("\\", "/"), "sha256": None,
                           "size": None,
                           "where": {"here": True, "mine": [], "friends": None, "public": None},
                           "license": None})
    machine = str(settings.get("node_name") or settings.machine_name())[:80]
    return {"id": "comfyui", "label": "ComfyUI",
            "here": ({"enabled": True, "version": found.version, "vram_mib": found.vram_mib}
                     if found is not None else
                     {"enabled": bool(settings.get("engines_comfyui_enabled")),
                      "version": None, "vram_mib": None}) if "here" in circles else None,
            "machines": {"here": 1 if found is not None else 0,
                         "mine": 0 if "mine" in circles else None,
                         "friends": None, "public": None},
            "models": models,
            "object_info": {"available": inv is not None, "machine": machine if inv else None,
                            "digest": inv.get("core") if inv else None},
            "recipes": 0}


@router.get("/engines")
async def engines(request: Request):
    circles = _circles(request)
    return JSONResponse({"object": "list",
                         "engines": [await asyncio.to_thread(describe, circles)]})


@router.post("/engines/comfyui/plan")
async def plan(request: Request):
    context = request.state.aiodos_app
    lang = errors.lang_of(request.headers)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return errors.response(request.url.path, ApiError(
            "invalid_request", "Corps JSON attendu : un objet."), lang)
    circles = _circles(request)
    where = [c for c in scopes.CIRCLES if c in circles]

    def check():
        found, inv = _here() if "here" in circles else (None, None)
        params = engine.normalize({k: v for k, v in body.items() if k != "where"},
                                  app_id=context.id, where=where, inventory=inv)
        missing = [{**m, "machine": str(settings.get("node_name") or settings.machine_name())}
                   for m in engine.missing_here(params, inv)] if inv else []
        return {"ok": found is not None and not missing, "where_effective":
                ["here"] if found is not None else [],
                "where_reason": None if found is not None else
                "ComfyUI n'est pas ouvert ou pas accordé sur ce PC",
                "machines": {"here": found is not None, "mine": 0 if "mine" in circles else None,
                             "friends": None, "public": None},
                "missing": missing, "refused": {}, "licenses": [],
                "units": len(params["items"]), "estimate": None}
    try:
        return JSONResponse(await asyncio.to_thread(check))
    except engine.Invalid as exc:
        return errors.response(request.url.path, ApiError(
            exc.code, str(exc), param=exc.param, detail=exc.detail, status=exc.status), lang)
