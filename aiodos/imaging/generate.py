"""Une image ou une vidéo : demandée à sd-server (son API native, asynchrone : img_gen,
vid_gen), suivie, arrêtée au besoin.

sd-server range la demande dans sa file et la calcule ; on relit son état toutes les
POLL_S secondes. Un arrêt (unité rendue, AIODOS qui s'arrête) annule le calcul là-bas.
Un calcul à la fois sur cette machine (sd-server les fait l'un après l'autre) : une image
d'ici et une image pour une autre machine ne se gênent pas. Si sd-server manque de
mémoire (Ollama a repris la carte entre-temps), il est relancé et le calcul refait, une
fois. Une image n'emporte pas sa consigne dans ses métadonnées : AIODOS la garde à part,
et une image partagée ne la dévoile pas.

La vidéo sort en WebM (le format natif de sd-server, que l'interface lit telle quelle) ;
même protocole que le moteur local de PANDORA (api/h3_local.py), du même auteur.
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
MAX_SECONDS = 3 * 3600            # une vidéo longue peut prendre une heure ou plus
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
WEBM_SIGNATURE = b"\x1a\x45\xdf\xa3"          # en-tête EBML (Matroska, WebM)


class ImageError(Exception):
    pass


class Shortage(ImageError):
    """sd-server a manqué de mémoire (carte graphique prise entre-temps) : relancé, il
    refait son plan de placement avec la mémoire libre."""


_SHORTAGE = ("no results", "memory", "capacity", "alloc")
_turn = threading.Lock()          # un calcul à la fois sur cette machine


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


def video_body(params: dict) -> dict:
    spec = catalog.model(params["model"])
    sample = {"sample_steps": int(params.get("steps") or spec["steps"]),
              "sample_method": spec.get("sampler") or "euler",
              "guidance": {"txt_cfg": float(params.get("cfg") or spec["cfg"])}}
    if spec.get("flow_shift"):
        sample["flow_shift"] = float(spec["flow_shift"])
    return {
        "prompt": params["prompt"],
        "negative_prompt": params.get("negative") or spec.get("default_negative") or "",
        "width": int(params["width"]), "height": int(params["height"]),
        "seed": int(params["seed"]),
        "video_frames": int(params["frames"]), "fps": int(spec.get("fps") or 16),
        "sample_params": sample,
        "preview": "none",
        "output_format": "webm",
    }


def png_size(data: bytes) -> tuple:
    """(largeur, hauteur) d'un PNG ; ValueError si ce n'en est pas un."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("ce n'est pas une image PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def is_webm(data: bytes) -> bool:
    return len(data) > 32 and data.startswith(WEBM_SIGNATURE) and b"webm" in data[:64]


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


def _png_of(result: dict) -> dict:
    images = result.get("images") or []
    if not images:
        raise ImageError("sd-server n'a rendu aucune image.")
    return {"png": base64.b64decode(images[0]["b64_json"])}


def _webm_of(result: dict) -> dict:
    data = base64.b64decode(result.get("b64_json") or "")
    if not is_webm(data):
        raise ImageError("sd-server n'a pas rendu de vidéo WebM.")
    return {"webm": data, "frames": int(result.get("frame_count") or 0),
            "fps": int(result.get("fps") or 0)}


def image(params: dict, stop=None, waiting=None) -> dict:
    """{"png": octets, "seconds": durée du calcul} pour une demande (modèle, consigne,
    graine, taille, pas). Interrupted si `stop` est levé en route, y compris en attendant
    son tour."""
    return _in_turn(lambda: _once(params, "img_gen", request_body(params), _png_of, "Image",
                                  stop, waiting), stop)


def video(params: dict, stop=None, waiting=None) -> dict:
    """{"webm": octets, "frames", "fps", "seconds"} pour une demande de vidéo (modèle,
    consigne, graine, taille, nombre d'images, pas)."""
    return _in_turn(lambda: _once(params, "vid_gen", video_body(params), _webm_of, "Vidéo",
                                  stop, waiting), stop)


def _in_turn(run, stop):
    while not _turn.acquire(timeout=0.5):
        if stop is not None and stop.is_set():
            raise Interrupted()
    try:
        try:
            return run()
        except Shortage as exc:
            log.info("%s : sd-server relancé avec la mémoire libre", exc)
            engine.stop()
            return run()
    finally:
        _turn.release()


def _once(params: dict, endpoint: str, body: dict, extract, what: str, stop=None,
          waiting=None) -> dict:
    base = engine.acquire(params["model"], stop=stop, waiting=waiting)
    began = time.monotonic()
    try:
        reply = requests.post(f"{base}/sdcpp/v1/{endpoint}", json=body, timeout=30,
                              proxies=engine.NO_PROXY)
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
                made = extract(status.get("result") or {})
                made["seconds"] = time.monotonic() - began
                return made
            if status.get("status") in ("failed", "cancelled"):
                error = status.get("error") or {}
                message = str(error.get("message") or status["status"])
                if any(word in message.lower() for word in _SHORTAGE):
                    raise Shortage(f"{what} non calculée : {message}")
                raise ImageError(f"{what} non calculée : {message}")
            if time.monotonic() - began > MAX_SECONDS:
                _cancel(base, job)
                raise ImageError(f"{what} toujours pas calculée après "
                                 f"{MAX_SECONDS // 3600} heures.")
            if stop is not None:
                stop.wait(POLL_S)
            else:
                time.sleep(POLL_S)
    except requests.RequestException as exc:
        raise ImageError(f"sd-server ne répond plus ({exc.__class__.__name__}).") from exc
    finally:
        engine.release()
