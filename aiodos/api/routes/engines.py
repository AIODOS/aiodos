"""Les moteurs des applications vus dans AIODOS (Réglages → Moteurs des applications,
docs/conception/partage-puissance.md § 3.2) : ComfyUI trouvé sur ce PC (installation,
instance ouverte), accordé ou non, et les travaux des applis qu'il calcule."""
import asyncio

from fastapi import APIRouter, Request

from aiodos import db, settings
from aiodos.engines import comfy_client, comfy_find, comfy_instance, comfyui
from aiodos.jobs.kinds import engine

router = APIRouter(prefix="/api")
ENGINE_OPTIONS = {"engines_comfyui_enabled", "engines_comfyui_path", "engines_comfyui_url",
                  "engines_comfyui_launch"}


def forget():
    """Après un changement de réglage : ComfyUI se recherche, son inventaire se relit ; s'il
    peut maintenant être lancé et qu'on ne sait pas encore ce qu'il sait faire, il est lancé
    le temps de le lire (comfy_instance.learn_soon)."""
    engine.forget_here()
    comfyui.forget_inventory()
    comfy_instance.learn_soon()


def status() -> dict:
    found = comfy_find.instance()
    models = 0
    if found is not None:
        try:
            models = sum(len(v) for v in comfyui.inventory(found.url)["models"].values())
        except comfy_client.Unreachable:
            models = 0
    jobs = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE kind=? GROUP BY status", (engine.KIND,))}
    return {"comfyui": {
        "enabled": bool(settings.get("engines_comfyui_enabled")),
        "installs": [{"kind": i.kind, "version": i.version, "path": str(i.comfyui.parent),
                      "name": i.name} for i in comfy_find.installs()],
        "instance": ({"url": found.url, "version": found.version, "vram_mib": found.vram_mib,
                      "device": found.device, "program": found.program, "models": models}
                     if found is not None else None),
        "launch": bool(settings.get("engines_comfyui_launch")),
        "launched": comfy_instance.current() is not None,
        "learned": comfy_instance.cached_inventory() is not None,
        "jobs": {"active": sum(jobs.get(s, 0) for s in ("queued", "running", "paused")),
                 "done": jobs.get("done", 0)}}}


@router.get("/engines")
async def engines_status():
    return await asyncio.to_thread(status)


@router.post("/engines/comfyui/refresh")
async def refresh(request: Request):
    forget()
    request.app.state.executor.wake()
    return await asyncio.to_thread(status)
