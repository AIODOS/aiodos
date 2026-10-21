"""Ce qu'une machine dit de ses moteurs (docs/conception/partage-puissance.md § 3.5) :

- capabilities() : dans chaque demande de travail (network/worker.py), à mes machines
  seulement en L2b : ComfyUI accordé et ouvert, sa version, l'empreinte de son cœur, la
  mémoire vidéo, les noms de ses classes et de ses modèles par dossier ;
- summary() : dans le profil (node/identity.py), le résumé que voient mes machines (version,
  modèles) : de quoi dire à une appli qui pourrait calculer son travail. Jamais aux amis
  (sharing.FRIEND_PROFILE ne le laisse pas passer) : les noms des nœuds ajoutés et des
  modèles en diraient trop de cette machine.
- can_serve(need, caps) : chez le propriétaire, la machine qui demande du travail a-t-elle
  ce qu'il faut pour cette unité ?
"""
from aiodos.engines import comfy_client, comfyui


def _here():
    from aiodos.jobs.kinds import engine
    found = engine.instance_here()
    if found is None:
        return None, None
    try:
        return found, comfyui.inventory(found.url)
    except comfy_client.Unreachable:
        return None, None


def capabilities() -> dict:
    found, inv = _here()
    if found is None:
        return {}
    return {"comfyui": {"api": comfyui.API, "version": inv.get("version"),
                        "core": inv.get("core"), "vram_mib": inv.get("vram_mib"),
                        "state": "ready", "classes": sorted(inv.get("classes") or {}),
                        "models": inv.get("models") or {}}}


def summary() -> dict:
    found, inv = _here()
    if found is None:
        return {}
    return {"comfyui": {"version": inv.get("version"), "vram_mib": inv.get("vram_mib"),
                        "models": inv.get("models") or {}}}


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
