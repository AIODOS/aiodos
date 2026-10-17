"""Une image : demandée à sd-server (son API native, asynchrone), suivie, arrêtée au besoin.

sd-server range la demande dans sa file et la calcule ; on relit son état toutes les
POLL_S secondes. Un arrêt (unité rendue, AIODOS qui s'arrête) annule le calcul là-bas.
Une image à la fois sur cette machine (sd-server les calcule l'une après l'autre) : une
image d'ici et une image pour une autre machine ne se gênent pas. Si sd-server manque de
mémoire (Ollama a repris la carte entre-temps), il est relancé et l'image refaite, une fois.
L'image n'emporte pas sa consigne dans ses métadonnées : AIODOS la garde à part, et une
image partagée ne la dévoile pas.
"""
import base64
import logging
import threading
import time

import requests

from aiodos.imaging import catalog, engine
from aiodos.jobs.registry import Interrupted

log = logging.getLogger("aiodos.images")

POLL_S = 0.5
MAX_SECONDS = 1800                # une image ne prend jamais une demi-heure
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageError(Exception):
    pass


class Shortage(ImageError):
    """sd-server a manqué de mémoire (carte graphique prise entre-temps) : relancé, il
    refait son plan de placement avec la mémoire libre."""


_SHORTAGE = ("no results", "memory", "capacity", "alloc")
_turn = threading.Lock()          # une image à la fois sur cette machine


def request_body(params: dict) -> dict:
    spec = catalog.model(params["model"])
    return {
        "prompt": params["prompt"],
        "negative_prompt": params.get("negative") or "",
        "width": int(params["width"]), "height": int(params["height"]),
        "seed": int(params["seed"]), "batch_count": 1,
        "sample_params": {
            "sample_steps": int(params.get("steps") or spec["steps"]),
            "guidance": {"txt_cfg": float(params.get("cfg") or spec["cfg"])},
        },
        "embed_image_metadata": False,
        "preview": "none",
        "output_format": "png",
    }


def png_size(data: bytes) -> tuple:
    """(largeur, hauteur) d'un PNG ; ValueError si ce n'en est pas un."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("ce n'est pas une image PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _detail(reply) -> str:
    try:
        error = reply.json().get("error")
        return (error.get("message") if isinstance(error, dict) else str(error or "")) \
            or reply.text[:200]
    except ValueError:
        return reply.text[:200]


def _cancel(base: str, job: str):
    try:
        requests.post(f"{base}/sdcpp/v1/jobs/{job}/cancel", timeout=5, proxies=engine.NO_PROXY)
    except requests.RequestException:
        pass


def image(params: dict, stop=None, waiting=None) -> dict:
    """{"png": octets, "seconds": durée du calcul} pour une demande (modèle, consigne,
    graine, taille, pas). Interrupted si `stop` est levé en route, y compris en attendant
    son tour."""
    while not _turn.acquire(timeout=0.5):
        if stop is not None and stop.is_set():
            raise Interrupted()
    try:
        try:
            return _once(params, stop, waiting)
        except Shortage as exc:
            log.info("%s : sd-server relancé avec la mémoire libre", exc)
            engine.stop()
            return _once(params, stop, waiting)
    finally:
        _turn.release()


def _once(params: dict, stop=None, waiting=None) -> dict:
    base = engine.acquire(params["model"], stop=stop, waiting=waiting)
    began = time.monotonic()
    try:
        reply = requests.post(f"{base}/sdcpp/v1/img_gen", json=request_body(params),
                              timeout=30, proxies=engine.NO_PROXY)
        if reply.status_code != 202:
            raise ImageError(f"sd-server refuse la demande ({reply.status_code}) : "
                             f"{_detail(reply)}")
        job = reply.json()["id"]
        while True:
            if stop is not None and stop.is_set():
                _cancel(base, job)
                raise Interrupted()
            status = requests.get(f"{base}/sdcpp/v1/jobs/{job}", timeout=10,
                                  proxies=engine.NO_PROXY).json()
            if status.get("status") == "completed":
                images = (status.get("result") or {}).get("images") or []
                if not images:
                    raise ImageError("sd-server n'a rendu aucune image.")
                return {"png": base64.b64decode(images[0]["b64_json"]),
                        "seconds": time.monotonic() - began}
            if status.get("status") in ("failed", "cancelled"):
                error = status.get("error") or {}
                message = str(error.get("message") or status["status"])
                if any(word in message.lower() for word in _SHORTAGE):
                    raise Shortage(f"Image non calculée : {message}")
                raise ImageError(f"Image non calculée : {message}")
            if time.monotonic() - began > MAX_SECONDS:
                _cancel(base, job)
                raise ImageError("Image toujours pas calculée après 30 minutes.")
            if stop is not None:
                stop.wait(POLL_S)
            else:
                time.sleep(POLL_S)
    except requests.RequestException as exc:
        raise ImageError(f"sd-server ne répond plus ({exc.__class__.__name__}).") from exc
    finally:
        engine.release()
