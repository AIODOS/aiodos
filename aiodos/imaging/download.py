"""Téléchargement d'un modèle d'images, à la demande de l'utilisateur (vue Images).

Les fichiers viennent des adresses épinglées du catalogue (Hugging Face, à un commit
précis), ou d'une de mes machines qui a le modèle (share.py : par le réseau local, sans
Internet) ; chacun est vérifié par sa taille et son empreinte avant d'être gardé. Un
téléchargement coupé reprend où il en était (fichier « .part »), y compris après un
redémarrage d'AIODOS. Un seul téléchargement à la fois ; l'interface lit son avancement
(progress()).
"""
import hashlib
import logging
import os
import threading
import time

import requests

from aiodos.imaging import catalog, share
from aiodos.network.client import PeerError

log = logging.getLogger("aiodos.images")

CHUNK = 1 << 20
TIMEOUT = 60
ATTEMPTS = 6                      # coupures successives tolérées par fichier
RETRY_S = 5

_lock = threading.Lock()
_state = {"model": None, "running": False, "base": 0, "current": 0, "total": 0,
          "file": None, "error": None, "rate": 0.0, "source": None}
_stop = threading.Event()


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


def _snapshot() -> dict:
    state = dict(_state)
    state["done"] = state.pop("base") + state.pop("current")
    return state


def progress() -> dict:
    """{"model", "running", "done", "total", "file", "error", "rate" (octets/s)}."""
    with _lock:
        return _snapshot()


def start(model_id: str, changed=None, source: str | None = None) -> dict:
    """Lance le téléchargement des fichiers qui manquent, depuis Hugging Face, ou depuis
    la machine `source` (identifiant d'une de mes machines) ; `changed()` est appelé quand
    il finit (ou échoue)."""
    catalog.model(model_id)                      # ModelError si inconnu
    machine = None
    if source:
        try:
            machine = share.Source(source)
        except PeerError as exc:
            raise DownloadError(str(exc)) from exc
    with _lock:
        if _state["running"]:
            if _state["model"] != model_id:
                raise DownloadError("Un autre modèle se télécharge déjà.")
            return _snapshot()
        _stop.clear()
        _state.update(model=model_id, running=True, base=0, current=0, file=None, error=None,
                      rate=0.0, total=catalog.total_size(model_id),
                      source=machine.name if machine else "Hugging Face")
    threading.Thread(target=_run, args=(model_id, changed, machine), daemon=True,
                     name="aiodos-images-telechargement").start()
    return progress()


def cancel():
    _stop.set()


def _run(model_id: str, changed, machine=None):
    try:
        folder = catalog.folder()
        folder.mkdir(parents=True, exist_ok=True)
        specs = list(catalog.model(model_id)["files"].values())
        for spec in specs:
            if catalog.file_state(spec, check=False) == "ok":
                _advance(spec["size"])
                continue
            with _lock:
                _state["file"] = spec["name"]
            _fetch(spec, folder, machine)
        log.info("Modèle d'images %s téléchargé", model_id)
    except Cancelled:
        with _lock:
            _state["error"] = "Téléchargement arrêté."
    except Exception as exc:
        log.warning("Téléchargement de %s impossible : %s", model_id, exc)
        with _lock:
            _state["error"] = str(exc)[:300]
    finally:
        with _lock:
            _state.update(running=False, file=None)
        if changed is not None:
            changed()


def _advance(n: int):
    """Un fichier de plus, complet et vérifié."""
    with _lock:
        _state["base"] += n
        _state["current"] = 0


def _fetch(spec: dict, folder, machine=None):
    target = folder / spec["name"]
    if target.exists():
        # Fichier posé à la main, pas encore vérifié : vérifié plutôt que retéléchargé
        if target.stat().st_size == spec["size"] and catalog.verify(target, spec):
            _advance(spec["size"])
            return
        target.unlink()
    part = target.with_name(target.name + ".part")
    for attempt in range(ATTEMPTS):
        try:
            if machine is None:
                _resume(spec, part)
            else:
                _resume_from(machine, spec, part)
            break
        except (requests.RequestException, PeerError, OSError) as exc:
            if attempt == ATTEMPTS - 1:
                raise DownloadError(f"{spec['name']} : téléchargement coupé ({exc})") from exc
            log.info("%s : coupure (%s), reprise dans %s s", spec["name"], exc, RETRY_S)
            if _stop.wait(RETRY_S):
                raise Cancelled() from exc
    digest = catalog.sha256_of(part, _stop)
    if digest is None:
        raise Cancelled()
    if part.stat().st_size != spec["size"] or digest != spec["sha256"]:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{spec['name']} : fichier reçu différent de celui attendu "
                            "(taille ou empreinte).")
    os.replace(part, target)
    catalog.record(target, spec["sha256"])
    _advance(spec["size"])


def _resume_from(machine, spec: dict, part):
    """La suite du fichier « .part », lue chez une de mes machines (share.py)."""
    offset = part.stat().st_size if part.exists() else 0
    if offset > spec["size"]:
        part.unlink()
        offset = 0
    with _lock:
        _state["current"] = offset
    began, received = time.monotonic(), 0
    with open(part, "ab" if offset else "wb") as out:
        while offset + received < spec["size"]:
            if _stop.is_set():
                raise Cancelled()
            data = machine.read(spec, offset + received)[:spec["size"] - offset - received]
            if not data:
                raise PeerError("transfert interrompu par l'autre machine")
            out.write(data)
            received += len(data)
            with _lock:
                _state["current"] = offset + received
                _state["rate"] = received / max(0.001, time.monotonic() - began)


def _resume(spec: dict, part):
    """Télécharge la suite du fichier « .part » (ou tout le fichier)."""
    offset = part.stat().st_size if part.exists() else 0
    if offset > spec["size"]:
        part.unlink()
        offset = 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with requests.get(spec["url"], headers=headers, stream=True, timeout=TIMEOUT) as reply:
        if offset and reply.status_code == 200:
            offset = 0                            # le serveur ignore la reprise : tout refaire
        elif reply.status_code == 416 and offset == spec["size"]:
            return                                # déjà complet
        elif reply.status_code not in (200, 206):
            raise DownloadError(f"{spec['name']} : le serveur répond {reply.status_code}.")
        with _lock:
            _state["current"] = offset
        began, received = time.monotonic(), 0
        with open(part, "ab" if offset else "wb") as out:
            for block in reply.iter_content(CHUNK):
                if _stop.is_set():
                    raise Cancelled()
                out.write(block)
                received += len(block)
                with _lock:
                    _state["current"] = offset + received
                    _state["rate"] = received / max(0.001, time.monotonic() - began)
        if offset + received < spec["size"]:
            raise requests.ConnectionError("réponse incomplète")
