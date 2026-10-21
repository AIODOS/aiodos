"""Ce qu'une machine dit de ses moteurs (docs/conception/partage-puissance.md § 3.5) :

- local_view() : le ComfyUI de ce PC tel qu'on peut s'en servir : ouvert (son inventaire),
  ou fermé mais qu'AIODOS peut lancer (engines/comfy_instance.py : son inventaire gardé, ses
  modèles lus sur le disque) ;
- capabilities() : dans chaque demande de travail (network/worker.py), à mes machines
  seulement en L2b : version, cœur, mémoire vidéo, noms des classes et des modèles ;
- summary() : dans le profil (node/identity.py), le résumé que voient mes machines (version,
  modèles) : de quoi dire à une appli qui pourrait calculer son travail. Jamais aux amis
  (sharing.FRIEND_PROFILE ne le laisse pas passer) : les noms des nœuds ajoutés et des
  modèles en diraient trop de cette machine.
- can_serve(need, caps) : chez le propriétaire, la machine qui demande du travail a-t-elle
  ce qu'il faut pour cette unité ?
"""
from aiodos.engines import comfy_client, comfy_instance, comfyui


def _here():
    from aiodos.jobs.kinds import engine
    found = engine.instance_here()
    if found is None:
        return None, None
    try:
        return found, comfyui.inventory(found.url)
    except comfy_client.Unreachable:
        return None, None


def local_view() -> dict | None:
    """{"version", "core", "vram_mib", "classes": [noms] | None, "models", "launch"} : le
    ComfyUI ouvert de ce PC, ou celui qu'AIODOS lancera ; None s'il n'y en a pas."""
    found, inv = _here()
    if found is not None:
        return {"version": inv.get("version"), "core": inv.get("core"),
                "vram_mib": inv.get("vram_mib"), "classes": sorted(inv.get("classes") or {}),
                "models": inv.get("models") or {}, "launch": False}
    if not comfy_instance.launchable():
        return None
    cached = comfy_instance.cached_inventory() or {}
    return {"version": cached.get("version") or (comfy_instance.install().version),
            "core": cached.get("core"), "vram_mib": cached.get("vram_mib"),
            "classes": cached.get("classes"), "models": comfy_instance.disk_models(),
            "launch": True}


def capabilities() -> dict:
    view = local_view()
    if view is None or view["classes"] is None:
        return {}                       # ses classes pas encore lues : rien d'annoncé
    return {"comfyui": {"api": comfyui.API, "version": view["version"], "core": view["core"],
                        "vram_mib": view["vram_mib"], "state": "ready",
                        "classes": view["classes"], "models": view["models"]}}


def summary() -> dict:
    view = local_view()
    if view is None:
        return {}
    return {"comfyui": {"version": view["version"], "vram_mib": view["vram_mib"],
                        "models": view["models"]}}


def can_serve(need: dict, caps: dict) -> bool:
    """need : {"name": "comfyui", "api", "classes": [...], "models": [{folder, name}]}.
    Ce que la machine annonce vient d'elle : une forme inattendue vaut un refus."""
    engines = caps.get("engines") if isinstance(caps, dict) else None
    theirs = engines.get(need.get("name") or "") if isinstance(engines, dict) else None
    if not isinstance(theirs, dict) or theirs.get("api") != need.get("api"):
        return False
    if theirs.get("state") not in (None, "ready"):
        return False
    classes = theirs.get("classes")
    models = theirs.get("models")
    if not isinstance(classes, list) or not isinstance(models, dict):
        return False
    if not set(need.get("classes") or []) <= {c for c in classes if isinstance(c, str)}:
        return False
    listed = {f: [n for n in names if isinstance(n, str)] for f, names in models.items()
              if isinstance(f, str) and isinstance(names, list)}
    return all(comfyui.local_name(listed, m.get("folder"), m["name"]) is not None
               for m in need.get("models") or [])
