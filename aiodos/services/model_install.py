"""Installer un modèle de conversation (Ollama), à la demande de l'utilisateur.

Sans modèle, AIODOS ne se contente plus de dire « aucun modèle » : il conseille des modèles
qui tiennent sur cette machine (téléphone, PC avec ou sans carte graphique) et les installe
par Ollama (/api/pull), en suivant l'avancement. Seulement des licences libres, vérifiées
dans le registre d'Ollama le 18/10/2026 (fiche et texte de licence de chaque modèle) :
Apache-2.0 pour Qwen et Mistral, MIT pour Phi. Un seul à la fois ; l'interface lit
progress() et reçoit un événement à chaque étape.
"""
import logging
import threading
import time

from aiodos.node import android, identity
from aiodos.services import ollama

log = logging.getLogger("aiodos.models")

# Tailles : tout ce que télécharge Ollama (modèle, projecteur d'images, gabarits)
CATALOG = [
    {"name": "qwen3.5:0.8b", "label": "Qwen 3.5 · 0,8 milliard", "size": 1_320_000_000,
     "license": "Apache-2.0", "about": "Le plus léger : tient sur un téléphone, pour des "
                                        "réponses simples."},
    {"name": "qwen3.5:2b", "label": "Qwen 3.5 · 2 milliards", "size": 2_680_000_000,
     "license": "Apache-2.0", "about": "Léger et correct : un téléphone récent, ou un petit PC."},
    {"name": "qwen3.5:4b", "label": "Qwen 3.5 · 4 milliards", "size": 3_320_000_000,
     "license": "Apache-2.0", "about": "Bon équilibre : une carte graphique de 4 à 6 Go, ou "
                                        "16 Go de mémoire."},
    {"name": "qwen3.5:9b", "label": "Qwen 3.5 · 9 milliards", "size": 6_550_000_000,
     "license": "Apache-2.0", "about": "Le meilleur pour une carte graphique de 8 Go (le "
                                        "modèle conseillé par AIODOS)."},
    {"name": "mistral:7b", "label": "Mistral · 7 milliards", "size": 4_370_000_000,
     "license": "Apache-2.0", "about": "Mistral AI, à l'aise en français : une carte de 6 Go, "
                                        "ou 16 Go de mémoire."},
    {"name": "phi4-mini", "label": "Phi-4 mini · 3,8 milliards", "size": 2_490_000_000,
     "license": "MIT", "about": "Microsoft : vif pour raisonner, moins à l'aise en français."},
]
NAMES = {m["name"] for m in CATALOG}

_lock = threading.Lock()
_state = {"model": None, "running": False, "completed": 0, "total": 0, "status": "",
          "error": None, "done": None}
_stop = threading.Event()


class InstallError(Exception):
    pass


def device() -> dict:
    """Ce qui compte pour conseiller un modèle : téléphone ou PC, mémoire de la carte
    graphique et mémoire vive (octets)."""
    profile = identity.profile()
    vram = max((g.get("vram_total") or 0 for g in profile.get("gpus") or []), default=0)
    return {"phone": android.is_android(), "vram": vram,
            "ram": (profile.get("memory") or {}).get("total") or 0}


def recommended(found: dict) -> str:
    """Le modèle conseillé : le plus capable qui tient (en carte graphique s'il y en a une,
    sinon en mémoire vive, avec de la marge ; un téléphone garde de quoi vivre)."""
    if found["phone"]:
        return "qwen3.5:2b" if found["ram"] >= 7.5e9 else "qwen3.5:0.8b"
    vram, ram = found["vram"], found["ram"]
    if vram >= 7.5e9:
        return "qwen3.5:9b"
    if vram >= 4.5e9 or ram >= 15e9:
        return "qwen3.5:4b"
    return "qwen3.5:2b" if ram >= 7.5e9 else "qwen3.5:0.8b"


def suggestions() -> dict:
    """Les modèles proposés, le conseillé en tête, chacun avec ce qu'il demande à cette
    machine (fits : tient-il en carte graphique, ou en mémoire vive ?) et s'il est déjà là."""
    found = device()
    try:
        installed = {m["name"] for m in ollama.list_models()}
        ollama_ok = True
    except Exception:
        installed, ollama_ok = set(), False
    best = recommended(found)
    models = []
    for spec in CATALOG:
        if found["phone"] and spec["size"] > 3e9:
            continue                          # trop lourd pour un téléphone
        fits_gpu = bool(found["vram"]) and spec["size"] < found["vram"] * 0.92
        fits_ram = spec["size"] < found["ram"] * 0.5 if found["ram"] else True
        models.append({**spec, "recommended": spec["name"] == best,
                       "fits": "gpu" if fits_gpu else "ram" if fits_ram else "heavy",
                       "installed": spec["name"] in installed
                       or f"{spec['name']}:latest" in installed})
    models.sort(key=lambda m: (not m["recommended"], m["size"]))
    return {"phone": found["phone"], "ollama": ollama_ok, "recommended": best,
            "models": models, "progress": progress()}


def progress() -> dict:
    with _lock:
        return dict(_state)


def start(name: str, changed=None) -> dict:
    """Lance l'installation (un seul à la fois). InstallError si le modèle n'est pas
    proposé, ou si une autre installation est en cours."""
    if name not in NAMES:
        raise InstallError("Ce modèle n'est pas proposé ici.")
    with _lock:
        if _state["running"]:
            raise InstallError(f"Installation de {_state['model']} déjà en cours.")
        _state.update(model=name, running=True, completed=0, total=0, status="préparation",
                      error=None, done=None)
        _stop.clear()
    threading.Thread(target=_run, args=(name, changed), daemon=True,
                     name="aiodos-installation-modele").start()
    return progress()


def cancel():
    _stop.set()


def _run(name: str, changed):
    last = [0.0]

    def advance(completed, total, status):
        with _lock:
            _state.update(completed=completed, total=total, status=status)
        if changed and time.monotonic() - last[0] > 0.5:      # pas plus de deux par seconde
            last[0] = time.monotonic()
            changed()
    try:
        finished = ollama.pull(name, progress=advance, stop=_stop)
        ollama.list_models(max_age=0)                          # la liste se relit
        with _lock:
            _state.update(done=bool(finished),
                          error=None if finished else "Installation arrêtée.")
        log.info("Modèle %s %s", name, "installé" if finished else "pas installé (arrêt)")
    except Exception as exc:
        log.warning("Installation de %s impossible : %s", name, exc)
        with _lock:
            _state.update(error=str(exc)[:300], done=False)
    finally:
        with _lock:
            _state["running"] = False
        if changed:
            changed()
