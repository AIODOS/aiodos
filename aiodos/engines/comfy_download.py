"""Les modèles des recettes de ComfyUI sur ce PC (engines/recipes.py) : où ils sont, et leur
téléchargement, sur le clic de l'utilisateur seulement, licence affichée (vue Images et
vidéos ; règle des licences de modèles). Sur le modèle du téléchargement des modèles
d'images (imaging/download.py) : un seul à la fois, reprise d'un « .part » coupé, taille et
empreinte vérifiées avant de garder le fichier.

- Où : dans les dossiers de modèles du ComfyUI de ce PC (comfy_instance.model_roots : le
  dossier partagé de Comfy Desktop d'abord, puis celui de l'installation), sous
  <dossier>/<nom> : là où ComfyUI les cherche, qu'il soit ouvert par l'utilisateur ou lancé
  par AIODOS. Sans ComfyUI installé, rien ne se télécharge.
- Présent : un fichier de ce nom dans ce dossier, de la taille déclarée (quand elle l'est).
- Le téléchargement : seulement un modèle épinglé (empreinte et taille déclarées par la
  recette) et une adresse https de Hugging Face. Le même fichier déjà ailleurs sur ce PC (les
  modèles d'images d'AIODOS, un autre ComfyUI, PANDORA…) est repris au lieu d'être
  téléchargé : un lien physique quand il est privé, une copie sinon (imaging/local_copies.py),
  son empreinte vérifiée.
"""
import logging
import os
import threading
import time
from pathlib import Path

import requests

from aiodos.engines import comfy_find, comfy_instance
from aiodos.i18n import t

log = logging.getLogger("aiodos.engines")

CHUNK = 1 << 20
TIMEOUT = 60
ATTEMPTS = 6
RETRY_S = 5
HOSTS = ("https://huggingface.co/",)

_lock = threading.Lock()
_state = {"recipe": None, "model": None, "name": None, "running": False, "done": 0,
          "total": 0, "error": None, "rate": 0.0, "from": None}
_stop = threading.Event()


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------- où
def roots() -> list:
    """Les dossiers de modèles du ComfyUI de ce PC, le préféré d'abord ; [] sans ComfyUI."""
    install = comfy_instance.install() or next(iter(comfy_find.installs()), None)
    return comfy_instance.model_roots(install) if install is not None else []


def locate(model: dict) -> Path | None:
    """Le fichier de ce modèle sur ce PC (même dossier, même nom, même taille), ou None."""
    for root in roots():
        path = root / model["folder"] / str(model["name"]).replace("\\", "/")
        try:
            if path.is_file() and (not model.get("size") or path.stat().st_size == model["size"]):
                return path
        except OSError:
            continue
    return None


def pinned(model: dict) -> bool:
    """Téléchargeable par AIODOS : empreinte, taille et adresse officielle déclarées."""
    return bool(model.get("sha256") and model.get("size")
                and str(model.get("url") or "").startswith(HOSTS))


def state(model: dict) -> str:
    """present | downloading | missing (téléchargeable) | unpinned (empreinte à déclarer par
    l'auteur) | no_comfyui (aucun dossier où le ranger)."""
    if locate(model) is not None:
        return "present"
    with _lock:
        if _state["running"] and _state["name"] == model["name"]:
            return "downloading"
    if not roots():
        return "no_comfyui"
    return "missing" if pinned(model) else "unpinned"


# -------------------------------------------------------------- télécharger
def progress() -> dict:
    with _lock:
        return dict(_state)


def start(recipe: dict, key: str, changed=None) -> dict:
    """Lance le téléchargement du modèle `key` de la recette (sur le clic de l'utilisateur) ;
    DownloadError s'il n'est pas épinglé, s'il n'y a pas de ComfyUI, ou si un autre se
    télécharge déjà."""
    model = next((m for m in recipe["models"] if m["key"] == key), None)
    if model is None:
        raise DownloadError(t("comfy_recipes.model_unknown", key=str(key)[:40]))
    if not pinned(model):
        raise DownloadError(t("comfy_recipes.download_unpinned", name=model["name"]))
    found = roots()
    if not found:
        raise DownloadError(t("comfy_recipes.download_no_comfyui"))
    with _lock:
        if _state["running"]:
            if (_state["recipe"], _state["model"]) != (recipe["id"], key):
                raise DownloadError(t("comfy_recipes.download_busy"))
            return dict(_state)
        _stop.clear()
        _state.update(recipe=recipe["id"], model=key, name=model["name"], running=True, done=0,
                      total=model["size"], error=None, rate=0.0, **{"from": None})
    target = found[0] / model["folder"] / model["name"].replace("\\", "/")
    threading.Thread(target=_run, args=(dict(model), target, changed), daemon=True,
                     name="aiodos-comfyui-telechargement").start()
    return progress()


def cancel():
    _stop.set()


def _run(model: dict, target: Path, changed):
    try:
        if locate(model) is None:
            _fetch(model, target)
            log.info("Modèle de ComfyUI %s téléchargé dans %s", model["name"], target.parent)
        _forget()
    except Cancelled:
        with _lock:
            _state["error"] = t("comfy_recipes.download_stopped")
    except Exception as exc:
        log.warning("Téléchargement de %s impossible : %s", model["name"], exc)
        with _lock:
            _state["error"] = str(exc)[:300]
    finally:
        with _lock:
            _state["running"] = False
        if changed is not None:
            changed()


def _forget():
    """ComfyUI et la file relisent leurs modèles."""
    from aiodos.engines import comfyui
    from aiodos.jobs.kinds import engine
    comfyui.forget_inventory()
    engine.forget_here()
    with comfy_instance._lock:
        comfy_instance._disk.update(at=0.0, value=None)


def _spec(model: dict) -> dict:
    """Le modèle sous la forme qu'attend imaging/local_copies (nom de fichier, taille,
    empreinte)."""
    return {"name": Path(model["name"]).name, "size": model["size"], "sha256": model["sha256"]}


def _reuse(model: dict, target: Path) -> str | None:
    """Le même fichier déjà sur ce PC, repris (lien ou copie, empreinte vérifiée) : le nom de
    l'endroit, ou None."""
    from aiodos.imaging import catalog, local_copies
    spec = _spec(model)
    own = catalog.folder() / spec["name"]            # les modèles d'images d'AIODOS (sd.cpp)
    try:
        if own.is_file() and own.stat().st_size == spec["size"] \
                and catalog.sha256_of(own, _stop) == spec["sha256"]:
            part = target.with_name(target.name + ".part")
            part.unlink(missing_ok=True)
            linked = False
            if local_copies._linkable(own):
                try:
                    os.link(own, part)
                    linked = True
                except OSError:
                    pass
            if not linked and not local_copies._copy(own, part, _stop, limit=spec["size"]):
                part.unlink(missing_ok=True)
                raise Cancelled()
            if catalog.sha256_of(part, _stop) == spec["sha256"]:
                os.replace(part, target)
                return "AIODOS"
            part.unlink(missing_ok=True)
    except OSError as exc:
        log.info("Copie de %s depuis les modèles d'images impossible (%s)", spec["name"], exc)
    return local_copies.bring(spec, target, _stop)


def _fetch(model: dict, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = _reuse(model, target)
    if _stop.is_set():
        raise Cancelled()
    if origin:
        log.info("%s repris de %s, sur ce PC", model["name"], origin)
        with _lock:
            _state.update(done=model["size"], **{"from": origin})
        return
    from aiodos.imaging import catalog
    part = target.with_name(target.name + ".part")
    for attempt in range(ATTEMPTS):
        try:
            _resume(model, part)
            break
        except (requests.RequestException, OSError) as exc:
            if attempt == ATTEMPTS - 1:
                raise DownloadError(t("comfy_recipes.download_cut", file=model["name"],
                                      error=exc)) from exc
            log.info("%s : coupure (%s), reprise dans %s s", model["name"], exc, RETRY_S)
            if _stop.wait(RETRY_S):
                raise Cancelled() from exc
    digest = catalog.sha256_of(part, _stop)
    if digest is None:
        raise Cancelled()
    if part.stat().st_size != model["size"] or digest != model["sha256"]:
        part.unlink(missing_ok=True)
        raise DownloadError(t("comfy_recipes.download_mismatch", file=model["name"]))
    os.replace(part, target)


def _resume(model: dict, part: Path):
    """La suite du fichier « .part » (ou tout le fichier), depuis l'adresse épinglée."""
    offset = part.stat().st_size if part.exists() else 0
    if offset > model["size"]:
        part.unlink()
        offset = 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with requests.get(model["url"], headers=headers, stream=True, timeout=TIMEOUT) as reply:
        if offset and reply.status_code == 200:
            offset = 0                            # le serveur ignore la reprise : tout refaire
        elif reply.status_code == 416 and offset == model["size"]:
            return
        elif reply.status_code not in (200, 206):
            raise DownloadError(t("comfy_recipes.server_status", file=model["name"],
                                  status=reply.status_code))
        with _lock:
            _state["done"] = offset
        began, received = time.monotonic(), 0
        with open(part, "ab" if offset else "wb") as out:
            for block in reply.iter_content(CHUNK):
                if _stop.is_set():
                    raise Cancelled()
                out.write(block)
                received += len(block)
                if offset + received > model["size"]:
                    raise DownloadError(t("comfy_recipes.download_mismatch", file=model["name"]))
                with _lock:
                    _state["done"] = offset + received
                    _state["rate"] = received / max(0.001, time.monotonic() - began)
        if offset + received < model["size"]:
            raise requests.ConnectionError(t("comfy_recipes.incomplete_reply"))
