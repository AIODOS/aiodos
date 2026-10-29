"""Une image ou une vidéo : demandée à sd-server (son API native, asynchrone : img_gen,
vid_gen), suivie, arrêtée au besoin.

sd-server range la demande dans sa file et la calcule ; on relit son état toutes les
POLL_S secondes. Rien de ce qui répond sur son port n'est cru sans vérifier que c'est bien
le sd-server lancé par AIODOS, seul sur ce port (engine.serving) : avant la demande (une
retouche y joint son image de départ), puis à chaque réponse, avant d'en croire un
« terminé ». S'il est tombé en route, qui s'installe sur son port rendrait sa propre image.
Restent l'instant entre la vérification et l'envoi, et ce qu'un programme de la machine
peut dire à sd-server pendant qu'il tourne (il n'a pas de clé : imaging/engine.py).
Un arrêt (unité rendue, AIODOS qui s'arrête) annule le calcul là-bas.
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

from aiodos.i18n import t
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
# Les messages d'une image et d'une vidéo (i18n/*/imaging.json) : sd-server tombé avant la
# demande (before) ou pendant le calcul (during), calcul en échec, calcul trop long
_TEXTS = {
    "image": {"before": "imaging.gone_before_image", "during": "imaging.gone_during_image",
              "failed": "imaging.image_failed", "slow": "imaging.image_too_slow"},
    "video": {"before": "imaging.gone_before_video", "during": "imaging.gone_during_video",
              "failed": "imaging.video_failed", "slow": "imaging.video_too_slow"},
}


def _sampling(spec: dict, steps, cfg) -> dict:
    """sample_params : pas, guidage, et ceux du modèle (échantillonneur, décalage) quand le
    catalogue les dit ; sinon ceux que sd-server choisit pour son architecture."""
    sample = {"sample_steps": int(steps), "guidance": {"txt_cfg": float(cfg)}}
    if spec.get("sampler"):
        sample["sample_method"] = spec["sampler"]
    if spec.get("flow_shift"):
        sample["flow_shift"] = float(spec["flow_shift"])
    return sample


def request_body(params: dict) -> dict:
    """Le corps de img_gen ; une retouche part de l'image de départ (init_png, octets) avec
    sa force (strength) : l'API native de sd-server l'attend en data URL."""
    spec = catalog.model(params["model"])
    body = {
        "prompt": params["prompt"],
        "negative_prompt": params.get("negative") or "",
        "width": int(params["width"]), "height": int(params["height"]),
        "seed": int(params["seed"]), "batch_count": 1,
        "sample_params": _sampling(spec, params.get("steps") or spec["steps"],
                                   params.get("cfg") or spec["cfg"]),
        "embed_image_metadata": False,
        "preview": "none",
        "output_format": "png",
    }
    if params.get("init_png"):
        body["init_image"] = "data:image/png;base64," + base64.b64encode(
            params["init_png"]).decode("ascii")
        body["strength"] = float(params.get("strength") or 0.75)
    return body


def video_body(params: dict) -> dict:
    """Le corps de vid_gen ; pour Wan 2.2 A14B, les réglages de l'expert « bruit fort »
    (high_noise_sample_params ; ses pas à -1 : selon la frontière des experts)."""
    spec = catalog.model(params["model"])
    sample = _sampling({"sampler": "euler", **spec}, params.get("steps") or spec["steps"],
                       params.get("cfg") or spec["cfg"])
    body = {
        "prompt": params["prompt"],
        "negative_prompt": params.get("negative") or spec.get("default_negative") or "",
        "width": int(params["width"]), "height": int(params["height"]),
        "seed": int(params["seed"]),
        "video_frames": int(params["frames"]), "fps": int(spec.get("fps") or 16),
        "sample_params": sample,
        # Comme une image : sans cela, le WebM dit la description entière et les réglages
        # dans son étiquette COMMENT (vu le 29/10/2026 avec sd-cli, même code ;
        # --disable-image-metadata l'en retire)
        "embed_image_metadata": False,
        "preview": "none",
        "output_format": "webm",
    }
    high = spec.get("high_noise")
    if high:
        body["high_noise_sample_params"] = {
            "sample_steps": int(high.get("steps") or -1),
            "sample_method": high.get("sampler") or "euler",
            "guidance": {"txt_cfg": float(high.get("cfg") or spec["cfg"])}}
        if high.get("flow_shift"):
            body["high_noise_sample_params"]["flow_shift"] = float(high["flow_shift"])
    return body


def png_size(data: bytes) -> tuple:
    """(largeur, hauteur) d'un PNG ; ValueError si ce n'en est pas un."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError(t("imaging.not_png"))
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def is_webm(data: bytes) -> bool:
    """Un WebM, ou un Matroska : sd-server écrit le son de LTX-2.5 et de MiniMax H3 en PCM
    (A_PCM/INT/LIT, examples/common/media_io.cpp de master-1027), que le WebM n'admet pas ;
    libwebm marque alors le fichier « matroska »."""
    return len(data) > 32 and data.startswith(WEBM_SIGNATURE) \
        and (b"webm" in data[:64] or b"matroska" in data[:64])


def _detail(reply) -> str:
    try:
        error = reply.json().get("error")
        return (error.get("message") if isinstance(error, dict) else str(error or "")) \
            or reply.text[:200]
    except ValueError:
        return reply.text[:200]


def _served(base: str, stop, then: str):
    """Le sd-server d'AIODOS répond-il toujours, seul, à `base` (engine.serving) ? Revu
    avant d'envoyer la demande et après chaque réponse, avant de s'en servir : tombé en
    route (un programme de la machine peut le faire tomber, puisqu'il peut lui parler), son
    port est libre, et qui s'y installe répondrait « terminé » avec sa propre image ; un
    programme qui s'ajoute sur son port recevrait les demandes. Sinon ImageError (then : la
    clé de son message, _TEXTS), rien de ce qui a répondu n'étant cru ; Interrupted si
    l'arrêt était demandé (AIODOS qui s'arrête arrête aussi sd-server : l'unité est rendue,
    elle n'a pas échoué)."""
    if engine.serving(base):
        return
    if stop is not None and stop.is_set():
        raise Interrupted()
    raise ImageError(t(then))


def _cancel(base: str, job: str):
    try:
        requests.post(f"{base}/sdcpp/v1/jobs/{job}/cancel", timeout=5, proxies=engine.NO_PROXY)
    except requests.RequestException:
        pass


def _png_of(result: dict) -> dict:
    images = result.get("images") or []
    if not images:
        raise ImageError(t("imaging.no_image"))
    return {"png": base64.b64decode(images[0]["b64_json"])}


def _webm_of(result: dict) -> dict:
    data = base64.b64decode(result.get("b64_json") or "")
    if not is_webm(data):
        raise ImageError(t("imaging.no_webm"))
    return {"webm": data, "frames": int(result.get("frame_count") or 0),
            "fps": int(result.get("fps") or 0)}


def image(params: dict, stop=None, waiting=None) -> dict:
    """{"png": octets, "seconds": durée du calcul} pour une demande (modèle, consigne,
    graine, taille, pas). Interrupted si `stop` est levé en route, y compris en attendant
    son tour."""
    return _in_turn(lambda: _once(params, "img_gen", request_body(params), _png_of, "image",
                                  stop, waiting), stop)


def video(params: dict, stop=None, waiting=None) -> dict:
    """{"webm": octets, "frames", "fps", "seconds"} pour une demande de vidéo (modèle,
    consigne, graine, taille, nombre d'images, pas)."""
    return _in_turn(lambda: _once(params, "vid_gen", video_body(params), _webm_of, "video",
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


def _once(params: dict, endpoint: str, body: dict, extract, media: str, stop=None,
          waiting=None) -> dict:
    """media : « image » ou « video », pour ses messages (_TEXTS)."""
    texts = _TEXTS[media]
    base = engine.acquire(params["model"], stop=stop, waiting=waiting)
    began = time.monotonic()
    dropped = texts["during"]
    try:
        _served(base, stop, texts["before"])
        reply = requests.post(f"{base}/sdcpp/v1/{endpoint}", json=body, timeout=30,
                              proxies=engine.NO_PROXY)
        _served(base, stop, dropped)
        if reply.status_code != 202:
            raise ImageError(t("imaging.server_refused", status=reply.status_code,
                               detail=_detail(reply)))
        job = reply.json()["id"]
        while True:
            if stop is not None and stop.is_set():
                _cancel(base, job)
                raise Interrupted()
            answer = requests.get(f"{base}/sdcpp/v1/jobs/{job}", timeout=10,
                                  proxies=engine.NO_PROXY)
            _served(base, stop, dropped)          # avant d'en croire un « terminé »
            status = answer.json()
            if status.get("status") == "completed":
                made = extract(status.get("result") or {})
                made["seconds"] = time.monotonic() - began
                return made
            if status.get("status") in ("failed", "cancelled"):
                error = status.get("error") or {}
                message = str(error.get("message") or status["status"])
                if any(word in message.lower() for word in _SHORTAGE):
                    raise Shortage(t(texts["failed"], message=message))
                raise ImageError(t(texts["failed"], message=message))
            if time.monotonic() - began > MAX_SECONDS:
                _cancel(base, job)
                raise ImageError(t(texts["slow"], hours=MAX_SECONDS // 3600))
            if stop is not None:
                stop.wait(POLL_S)
            else:
                time.sleep(POLL_S)
    except requests.RequestException as exc:
        if stop is not None and stop.is_set():
            raise Interrupted() from exc     # arrêté avec AIODOS : l'unité est rendue
        raise ImageError(t("imaging.server_silent", error=exc.__class__.__name__)) from exc
    finally:
        engine.release()
