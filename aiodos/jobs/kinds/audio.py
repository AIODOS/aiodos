"""Travail « audio » (docs/conception/audio.md § 3) : chaque création demandée (une voix, une
musique, un effet sonore) est une unité indépendante (modèle, texte ou description, durée,
graine → WAV). Elle se calcule ici, ou chez une de mes machines qui a audio.cpp et le même
modèle, aux mêmes empreintes (network/work.py), puis n'est gardée qu'ici (imaging/gallery.py,
avec les images et les vidéos).

Comme une image (jobs/kinds/images.py) : pas confiée à un ami ni calculée pour un ami, sauf
accord des deux côtés, les mêmes réglages que les images (images_from_friends chez le
propriétaire, lend_images_friends chez l'ami : un son se range avec les créations). Le texte
peut être personnel : il passe chez l'ami le temps du calcul, sans y être gardé. Une machine
qui reçoit un ordre en revérifie les réglages (checked), et le WAV qui revient est contrôlé
avant d'être gardé (audio/wav.py : un format connu, une durée, pas un silence). Le réseau
public ne calcule pas encore d'audio.
"""
import base64
import math
import secrets

from aiodos import i18n, settings
from aiodos.audio import catalog, files, generate, wav
from aiodos.audio import engine as audio_engine
from aiodos.i18n import t
from aiodos.imaging import gallery
from aiodos.jobs import pacing
from aiodos.jobs.registry import UnitSpec, register_job_kind, register_portable

KIND = "generate_audio"
TASK = "generate_audio"
MAX_COUNT = 4
MAX_WAV = 256 * 1024 ** 2         # 6 min de stéréo 48 kHz en flottants 32 bits : 138 Mo
SEED_SPACE = 2 ** 31
LANGUAGE = "fr"                   # une voix sans langue dite : celle de l'interface, sinon


def normalize(params: dict) -> dict:
    """Demande vérifiée et complétée (graine tirée au hasard, voix et durée du modèle) ;
    ValueError avec un message pour l'utilisateur sinon."""
    model_id = str(params.get("model") or "")
    if model_id not in catalog.MODELS:
        raise ValueError(t("audio.unknown_model_short"))
    spec = catalog.model(model_id)
    genre = spec["genre"]
    try:
        count = int(params.get("count") or 1)
        seed = params.get("seed")
        seed = secrets.randbelow(SEED_SPACE) if seed in (None, "") else int(seed) % SEED_SPACE
    except (TypeError, ValueError) as exc:
        raise ValueError(t("audio.bad_settings")) from exc
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(t("audio.bad_count", max=MAX_COUNT))
    found = {"model": model_id, "genre": genre, "seed": seed, "count": count,
             "batch": secrets.token_hex(6)}
    if genre == catalog.VOICE:
        text = " ".join(str(params.get("text") or params.get("prompt") or "").split())
        if not text:
            raise ValueError(t("audio.text_missing"))
        if len(text) > catalog.MAX_TEXT:
            raise ValueError(t("audio.text_too_long", max=catalog.MAX_TEXT))
        language = str(params.get("language") or "").strip().lower()[:8] or None
        voices = [v for v, _ in catalog.voices(model_id, language)]
        voice = str(params.get("voice") or "").strip() or (voices[0] if voices else "")
        if voices and voice not in voices:
            # Une voix d'une autre langue du même modèle reste permise
            every = {v for lang in catalog.model(model_id).get("voices") or {}
                     for v, _ in catalog.model(model_id)["voices"][lang]}
            if voice not in every:
                raise ValueError(t("audio.unknown_voice"))
        found.update(text=text, voice=voice, language=language)
        return found
    prompt = " ".join(str(params.get("prompt") or "").split())
    if not prompt:
        raise ValueError(t("audio.describe_music" if genre == catalog.MUSIC
                           else "audio.describe_sound"))
    if len(prompt) > catalog.MAX_TEXT:
        raise ValueError(t("audio.text_too_long", max=catalog.MAX_TEXT))
    try:
        duration = int(round(float(params.get("duration") or spec["default_seconds"])))
    except (TypeError, ValueError) as exc:
        raise ValueError(t("audio.bad_settings")) from exc
    if not 1 <= duration <= spec["max_seconds"]:
        raise ValueError(t("audio.bad_duration", max=spec["max_seconds"]))
    lyrics = str(params.get("lyrics") or "").strip()[:catalog.MAX_TEXT] \
        if spec.get("lyrics") else ""
    found.update(prompt=prompt, duration=duration, lyrics=lyrics)
    return found


def _short(text: str, n: int = 50) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


SHARED = ("model", "genre", "text", "voice", "language", "prompt", "lyrics", "duration",
          "batch")


def split(params):
    """Une unité par création, sous un titre traduit dans la langue de qui le lit (« Voix :
    « … » ×2 »)."""
    common = {k: params[k] for k in SHARED if k in params}
    units = [UnitSpec(TASK, "gpu", {**common, "seed": (params["seed"] + i) % SEED_SPACE,
                                    "index": i})
             for i in range(params["count"])]
    # audio.job_voice, audio.job_music, audio.job_sound
    title = i18n.lazy(f"audio.job_{params['genre']}",
                      prompt=_short(params.get("text") or params.get("prompt")),
                      n=params["count"])
    return title, units, {"batch": params["batch"]}


def runnable_here(model) -> bool:
    """Sans audio.cpp ou sans le modèle complet ici, la création attend en file qu'une de mes
    machines qui les a la demande."""
    return audio_engine.folder() is not None and files.ready(str(model or ""))


def requires(unit, options) -> dict:
    """Ce qu'il faut à une machine : audio.cpp, et les fichiers du modèle aux mêmes
    empreintes."""
    model_id = unit["payload"]["model"]
    return {"audio": {"model": model_id, "files": catalog.hashes(model_id)}}


def checked(payload) -> dict:
    """Les réglages d'une création reçue en ordre de calcul, revérifiés ici (mêmes bornes
    qu'une demande faite ici) ; ValueError sinon."""
    params = normalize({**payload, "count": 1})
    if params["model"] != payload.get("model") or params["seed"] != int(payload["seed"]):
        raise ValueError(t("audio.order_mismatch"))
    return {**payload, **{k: params[k] for k in SHARED if k in params and k != "batch"}}


def compute(payload, file, options, stop=None):
    payload = checked(payload)
    halt = stop if stop is not None else pacing.stopping()
    machine = str(settings.get("node_name") or settings.machine_name())[:80]
    made = pacing.paced(lambda: generate.create(payload, halt), stop)
    return {"wav": base64.b64encode(made["wav"]).decode("ascii"),
            "seconds": round(made["seconds"], 2), "duration": made["duration"],
            "machine": machine}


def _decoded(payload, output) -> tuple:
    """(WAV, ce qu'en dit audio/wav.check) d'une sortie, contrôlée : un WAV lisible, pas trop
    lourd, qui dure, pas un silence ; une musique ou un effet pas beaucoup plus long que
    demandé."""
    try:
        data = base64.b64decode(str(output.get("wav") or ""), validate=True)
    except ValueError as exc:
        raise ValueError(t("audio.wav_not_wav")) from exc
    if len(data) > MAX_WAV:
        raise ValueError(t("audio.wav_too_heavy"))
    try:
        info = wav.check(data)
    except wav.WavError as exc:
        raise ValueError(t(f"audio.wav_{exc}")) from exc
    asked = payload.get("duration")
    if asked and info["seconds"] > float(asked) * 1.5 + 10:
        raise ValueError(t("audio.wav_too_long"))
    return data, info


def check(payload, output):
    """Contrôle d'un son calculé ailleurs, avant de le garder."""
    _decoded(payload, output)


def _seconds(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and 0 <= seconds <= gallery.MAX_SECONDS else None


def assimilate(payload, output):
    data, info = _decoded(payload, output)
    row = gallery.add_audio(payload, data, info["seconds"],
                            machine=str(output.get("machine") or "")[:80],
                            seconds=_seconds(output.get("seconds")))
    return {"image_id": row["id"], "seed": row["seed"], "machine": row["machine"]}


def finalize(job_id, params, units):
    done = [u for u in units if u["status"] == "done"]
    return {"audio": len(done), "batch": params.get("batch"),
            "machines": sorted({(u["result"] or {}).get("machine") or "?" for u in done})}


register_portable(TASK, compute, assimilate, requires=requires, validate=check,
                  here=runnable_here, mine_only=True, friends_out="images_from_friends",
                  friends_in="lend_images_friends",
                  shared_keys=("model", "genre", "text", "voice", "language", "prompt",
                               "lyrics", "duration", "seed"))
register_job_kind(KIND, split, finalize)
