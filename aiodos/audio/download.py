"""Téléchargement d'un modèle d'audio, à la demande de l'utilisateur (Réglages → Modèles).

Les fichiers viennent des adresses épinglées du catalogue (Hugging Face, à un commit précis) ;
chacun est vérifié par sa taille et son empreinte avant d'être gardé. Un téléchargement coupé
reprend où il en était (fichier « .part »), y compris après un redémarrage d'AIODOS. Un seul
téléchargement d'audio à la fois ; l'interface lit son avancement (progress()). Comme celui
des images (imaging/download.py), sans la copie depuis mes machines : elle viendra.
"""
import logging
import os
import threading
import time

import requests

from aiodos.audio import catalog, files
from aiodos.i18n import t
from aiodos.imaging import catalog as images_catalog

log = logging.getLogger("aiodos.audio")

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
    """{"model", "running", "done", "total", "file", "error", "rate" (octets/s), "source"}."""
    with _lock:
        return _snapshot()


def start(model_id: str, changed=None) -> dict:
    """Lance le téléchargement des fichiers qui manquent ; changed() est appelé quand il finit
    (ou échoue). catalog.ModelError si le modèle est inconnu, DownloadError si un autre se
    télécharge déjà."""
    catalog.model(model_id)
    with _lock:
        if _state["running"]:
            if _state["model"] != model_id:
                raise DownloadError(t("audio.download_busy"))
            return _snapshot()
        _stop.clear()
        _state.update(model=model_id, running=True, base=0, current=0, file=None, error=None,
                      rate=0.0, total=catalog.total_size(model_id), source="Hugging Face")
    # Dossier et cache fixés maintenant : le fil peut survivre à son contexte (essais)
    where = (files.folder(), files.cache_file_path())
    threading.Thread(target=_run, args=(model_id, changed, where), daemon=True,
                     name="aiodos-audio-telechargement").start()
    return progress()


def cancel():
    _stop.set()


def _run(model_id: str, changed, where):
    base, cache_file = where
    try:
        for spec in catalog.model(model_id)["files"].values():
            if files.file_state(spec, check=False) == "ok":
                _advance(spec["size"])
                continue
            with _lock:
                _state["file"] = spec["path"].rsplit("/", 1)[-1]
            _fetch(spec, files.path_of(spec, base), cache_file)
        log.info("Modèle d'audio %s téléchargé", model_id)
    except Cancelled:
        with _lock:
            _state["error"] = t("audio.download_stopped")
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
    with _lock:
        _state["base"] += n
        _state["current"] = 0


def _fetch(spec: dict, target, cache_file):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Fichier posé à la main, pas encore vérifié : vérifié plutôt que retéléchargé
        if target.stat().st_size == spec["size"] and files.verify(target, spec, cache_file):
            _advance(spec["size"])
            return
        target.unlink()
    part = target.with_name(target.name + ".part")
    for attempt in range(ATTEMPTS):
        try:
            _resume(spec, part)
            break
        except (requests.RequestException, OSError) as exc:
            if attempt == ATTEMPTS - 1:
                raise DownloadError(t("audio.download_cut", file=target.name,
                                      error=exc)) from exc
            log.info("%s : coupure (%s), reprise dans %s s", target.name, exc, RETRY_S)
            if _stop.wait(RETRY_S):
                raise Cancelled() from exc
    digest = images_catalog.sha256_of(part, _stop)
    if digest is None:
        raise Cancelled()
    if part.stat().st_size != spec["size"] or digest != spec["sha256"]:
        part.unlink(missing_ok=True)
        raise DownloadError(t("audio.download_mismatch", file=target.name))
    os.replace(part, target)
    files.record(target, spec["sha256"], cache_file)
    _advance(spec["size"])


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
            raise DownloadError(t("audio.server_status", file=part.name[:-5],
                                  status=reply.status_code))
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
            raise requests.ConnectionError(t("audio.incomplete_reply"))
