"""Une création audio : la commande d'audio.cpp pour ce modèle, son calcul, le WAV contrôlé.

La commande suit la documentation d'audio.cpp 0.9.3 (docs/models/*.md, docs/tts.md) :
- une voix (« tts ») : le texte à lire, et la voix du modèle (--voice-id pour PocketTTS, dont
  les voix sont des fichiers à côté de son GGUF ; --speaker pour Qwen3-TTS ; l'option de
  requête voice_id pour Voxtral) ;
- une musique ou un effet (« gen ») : la description (en anglais : chat/enrich.py l'a
  traduite), la durée ; les paroles d'une chanson (ACE-Step : --lyrics ; MiniMax Music 3 :
  l'option lyrics, obligatoire) ; MiDashengLM-Gen reçoit une description par couches
  (<|caption|>, <|music|>, <|sfx|>…), les couches inutiles marquées <|unknown|>.
Le WAV est écrit dans un dossier de calcul (data/tmp/audio/<unité>), lu, contrôlé (audio/wav.py :
un format connu, une durée, pas un silence), puis le dossier est effacé.
"""
import logging
import secrets
import shutil
import time

from aiodos import paths
from aiodos.audio import catalog, engine, files, wav
from aiodos.i18n import t

log = logging.getLogger("aiodos.audio")

TIMEOUT = {catalog.VOICE: 900, catalog.MUSIC: 3600, catalog.SOUND: 1200}
SEED_MAX = 2 ** 31


def _midasheng(prompt: str, genre: str) -> str:
    """La description par couches de MiDashengLM-Gen : la demande en légende, et dans la
    couche de son genre (musique ou effet) ; les autres couches vides."""
    layers = {"asr": "<|unknown|>", "speech": "<|unknown|>", "music": "<|unknown|>",
              "sfx": "<|unknown|>", "env": "<|unknown|>"}
    layers["music" if genre == catalog.MUSIC else "sfx"] = prompt
    return f"<|caption|> {prompt} " + " ".join(f"<|{k}|> {v}" for k, v in layers.items())


def command(payload: dict, out) -> list:
    """Les arguments d'audiocpp_cli pour cette création (payload : jobs/kinds/audio.normalize)."""
    model_id = payload["model"]
    spec = catalog.model(model_id)
    args = ["--task", spec["task"], "--family", spec["family"],
            "--model", str(files.model_path(model_id)), *engine.device_args(),
            "--out", str(out), "--seed", str(int(payload["seed"]) % SEED_MAX)]
    if spec["genre"] == catalog.VOICE:
        args += ["--text", payload["text"]]
        voice = payload.get("voice")
        if voice:
            option = spec.get("voice_option")
            if option == "id":
                args += ["--voice-id", voice]
            elif option == "speaker":
                args += ["--speaker", voice]
            elif option == "request":
                args += ["--request-option", f"voice_id={voice}"]
        return args
    prompt = payload["prompt"]
    if spec.get("prompt") == "midasheng":
        # Sa durée est un plafond : il s'arrête quand le son est fini (essai du 29/10/2026 :
        # 10 s demandées, 6,4 s rendues ; forcé à 10 s par stop_threshold=1.1, les deux
        # dernières secondes ne sont qu'un souffle)
        prompt = _midasheng(prompt, spec["genre"])
    args += [*spec.get("args", []), "--text", prompt]
    seconds = int(payload["duration"])
    family = spec["family"]
    if family in ("stable_audio", "ace_step"):
        args += ["--duration-seconds", str(seconds)]
    else:
        args += ["--request-option", f"duration_sec={seconds}"]
    lyrics = (payload.get("lyrics") or "").strip()
    if family == "ace_step" and lyrics:
        args += ["--lyrics", lyrics]
    elif family == "minimax_music3":
        # Les paroles y sont obligatoires : une musique sans paroles le dit
        args += ["--request-option", f"lyrics={lyrics or '[Instrumental]'}"]
    return args


def create(payload: dict, stop=None) -> dict:
    """Calcule la création → {"wav": octets, "seconds": temps de calcul, "duration": durée du
    son, "rate", "channels"} ; engine.EngineError ou wav.WavError (message pour
    l'utilisateur) si elle échoue."""
    from aiodos.engines import gpu_share
    work = paths.data_dir() / "tmp" / "audio" / secrets.token_hex(6)
    work.mkdir(parents=True, exist_ok=True)
    out = work / "out.wav"
    try:
        gpu_share.before_unit()           # la carte rendue : conversation, sd-server inoccupé
        args = command(payload, out)
        began = time.monotonic()
        engine.run(args, stop, TIMEOUT.get(catalog.genre(payload["model"]), 1800),
                   log_file=paths.log_path().with_name("audiocpp.log"))
        seconds = time.monotonic() - began
        try:
            data = out.read_bytes()
        except OSError as exc:
            raise engine.EngineError(t("audio.no_output")) from exc
        try:
            info = wav.check(data)
        except wav.WavError as exc:
            raise engine.EngineError(t(f"audio.wav_{exc}")) from exc
        log.info("Audio %s : %.1f s de son en %.1f s (%s Hz, %s canaux)", payload["model"],
                 info["seconds"], seconds, info["rate"], info["channels"])
        return {"wav": data, "seconds": seconds, "duration": round(info["seconds"], 2),
                "rate": info["rate"], "channels": info["channels"]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
