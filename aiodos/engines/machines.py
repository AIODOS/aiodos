"""Les machines qui ont ComfyUI, et leurs modèles : ce PC (s'il est accordé et ouvert) et mes
machines (d'après le profil qu'elles m'envoient, engines/lending.summary). De quoi dire à
une appli où son travail peut tourner, avant de le mettre en file (gateway/jobs_api.py) et
dans la liste des moteurs (gateway/engines_api.py). Une de mes machines éteinte compte : le
travail l'attend. Jamais un ami en L2b."""
from aiodos import settings
from aiodos.engines import comfyui, lending
from aiodos.network import peers


def this_name() -> str:
    return str(settings.get("node_name") or settings.machine_name())[:80]


def known(circles) -> list:
    """[{name, place, online, version, vram_mib, models}] dans ces cercles."""
    out = []
    if "here" in circles:
        found, inv = lending._here()
        if found is not None:
            out.append({"name": this_name(), "place": "here", "online": True,
                        "version": inv.get("version"), "vram_mib": inv.get("vram_mib"),
                        "models": inv.get("models") or {}})
    if "mine" in circles:
        for peer in peers.list_peers():
            if peer.get("trust") != peers.MINE or peer.get("set_aside_at"):
                continue
            engines = (peer.get("profile") or {}).get("engines") or {}
            comfy = engines.get("comfyui") if isinstance(engines, dict) else None
            if not isinstance(comfy, dict):
                continue
            out.append({"name": peer.get("name") or "?", "place": "mine",
                        "online": bool(peer.get("online")), "version": comfy.get("version"),
                        "vram_mib": comfy.get("vram_mib"), "models": comfy.get("models") or {}})
    return out


def missing_on(machine: dict, models) -> list:
    """Les modèles ([{folder, name}]) que cette machine n'a pas."""
    have = machine.get("models") or {}
    return [{"folder": m.get("folder"), "name": m["name"]} for m in models
            if comfyui.local_name(have, m.get("folder"), m["name"]) is None]
