"""Les moteurs vus par une appli (docs/conception/partage-puissance.md § 6.5) :
GET /aiodos/v1/engines dit qui a ComfyUI et quels modèles, selon les cercles de l'appli ;
POST /aiodos/v1/engines/comfyui/plan vérifie un travail comme POST /aiodos/v1/jobs, sans
rien mettre en file (« pré-vol »). Cette version calcule sur ce PC ; mes machines suivront."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aiodos import settings
from aiodos.engines import comfy_client, comfyui, machines
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
    """Le moteur ComfyUI tel que l'appli le voit : ce PC, le nombre de mes machines qui
    l'ont, et chaque modèle avec où il se trouve (« mine » : les noms de mes machines)."""
    found, inv = _here() if "here" in circles else (None, None)
    known = machines.known(circles)
    models = {}
    for machine in known:
        for folder, names in (machine.get("models") or {}).items():
            for name in names:
                key = (folder, name.replace("\\", "/"))
                entry = models.setdefault(key, {
                    "folder": folder, "name": key[1], "sha256": None, "size": None,
                    "where": {"here": False, "mine": [] if "mine" in circles else None,
                              "friends": None, "public": None}, "license": None})
                if machine["place"] == "here":
                    entry["where"]["here"] = True
                elif machine["name"] not in entry["where"]["mine"]:
                    entry["where"]["mine"].append(machine["name"])
    return {"id": "comfyui", "label": "ComfyUI",
            "here": ({"enabled": True, "version": found.version, "vram_mib": found.vram_mib}
                     if found is not None else
                     {"enabled": bool(settings.get("engines_comfyui_enabled")),
                      "version": None, "vram_mib": None}) if "here" in circles else None,
            "machines": {"here": 1 if found is not None else 0,
                         "mine": sum(1 for m in known if m["place"] == "mine")
                         if "mine" in circles else None,
                         "friends": None, "public": None},
            "models": [models[k] for k in sorted(models)],
            "object_info": {"available": inv is not None,
                            "machine": machines.this_name() if inv else None,
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
        _, inv = _here()
        params = engine.normalize({k: v for k, v in body.items() if k != "where"},
                                  app_id=context.id, where=where, inventory=inv)
        known = machines.known(circles)
        needed = list(params["models"].values())
        gaps = [(machines.missing_on(m, needed), m) for m in known]
        able = [m for missing, m in gaps if not missing]
        reason = None if able else ("aucune machine permise n'a ComfyUI accordé à AIODOS"
                                    if not known else "il manque des modèles")
        return {"ok": bool(able),
                "where_effective": [c for c in scopes.CIRCLES
                                    if any(m["place"] == c for m in able)],
                "where_reason": reason,
                "machines": {"here": any(m["place"] == "here" for m in able),
                             "mine": sum(1 for m in able if m["place"] == "mine")
                             if "mine" in circles else None,
                             "friends": None, "public": None},
                "missing": [{**x, "machine": m["name"]} for missing, m in gaps for x in missing],
                "refused": {}, "licenses": [], "units": len(params["items"]), "estimate": None}
    try:
        return JSONResponse(await asyncio.to_thread(check))
    except engine.Invalid as exc:
        return errors.response(request.url.path, ApiError(
            exc.code, str(exc), param=exc.param, detail=exc.detail, status=exc.status), lang)
