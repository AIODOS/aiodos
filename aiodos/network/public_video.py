"""Prêter ma carte graphique aux vidéos d'inconnus : le côté prêteur des vidéos du réseau
public (docs/conception/reseau-public-calcul.md § 11 ; l'auteur, le 29/10/2026 : « il faut que
ça soit pareil pour les vidéos »). Le côté demandeur : network/public_video_ask.py ; l'accord,
chez lui : chat/public_creations.py. Le même chemin que les images (network/public_imaging.py),
avec les bornes d'une vidéo.

Un inconnu du réseau public fait créer une vidéo par cette machine, sans dire qui il est (une
clé jetable par création), relayée par le compagnon comme une conversation (256 Kio, une à la
fois par clé) sur POST /public/v1/video, avec l'en-tête X-Aiodos-Public : sans lui, 404.

Le même réglage que les images, lend_images_public (« Prêter ma carte graphique aux images et
vidéos d'inconnus », activé par défaut), en plus de lend_public : comme lend_images_friends,
qui vaut pour les images et les vidéos de mes amis, et comme le dit la charte (version 4 :
« ce prêt »). Vérifications, dans l'ordre, chacune avec sa réponse, avant tout calcul (admit) :

  1. le prêt (public_imaging.lending_images : les deux réglages, le partage actif, sur
     secteur ; un téléphone seulement en charge et en Wi-Fi) : sinon 503 {"error":
     "not_lending"} ;
  2. la demande lue et bornée, sans rien prendre : {"v": 1, "model", "prompt", "negative",
     "width", "height", "frames", "steps", "seed"}. Un modèle de vidéos du catalogue qu'on
     prête (lendable : une vidéo, sans licence restrictive : Wan 2.1, pas MiniMax H3), complet
     ici avec stable-diffusion.cpp : sinon 404 {"error": "no_model"}. Une description (et une
     consigne négative) de MAX_PROMPT caractères au plus ; des côtés au multiple et dans les
     bornes du modèle, MAX_PIXELS pixels au plus ; un nombre d'images permis par le modèle
     (frame_step × k + frame_base), MAX_FRAMES au plus ; de 1 aux pas du modèle (max_steps) ;
     rien d'autre (ni image de départ) : sinon 413 {"error": "too_big"} ; mal formée : 400
     {"error": "bad_request"} ;
  3. le filtre des descriptions, le même que pour une image (public_imaging._filtered :
     imaging/prompt_filter.py, la base toujours, le modèle de conversation déjà chargé en
     plus) : 403 {"error": "refused_content"}, rien de pris ni de gardé ; le journal dit
     « Vidéo refusée par le filtre (<code>) » ;
  4. la place du public (public_lending._take_place : une seule pour tout le public,
     conversation, lecture, image ou vidéo) : sinon 503 {"error": "busy"}.

La réponse, en NDJSON (public_lending._Stream) : {"type": "meta", "model", "version"} ; tant
que la vidéo n'est pas prête, toutes les public_lending.WAIT_S secondes, {"type": "wait",
"stage": "loading" ou "computing"} (une vidéo prend des minutes : 3 min 42 pour 2 s de Wan 2.1
en 832 × 480 sur une RTX 4070 portable, le 17/10/2026) ; puis {"type": "video", "format":
"webm", "bytes", "width", "height", "frames", "fps", "seconds"}, le WebM en morceaux {"type":
"part", "data": base64} de PART octets, et {"type": "done"}. Ou {"type": "error", "error":
code} : « busy », « not_lending », « engine » (le moteur a failli, ou sa vidéo n'est pas celle
demandée, ou dépasse MAX_VIDEO octets). Le WebM part réécrit (imaging/webm.check, sans le
décoder) : ses images seules, sans les étiquettes où sd-server écrit la description et ses
réglages (noms des fichiers du modèle, version), que generate.video_body ne demande d'ailleurs
plus.

Le calcul : imaging/generate.video, comme une vidéo de mes machines (sd-server, un calcul à
la fois), avec le guidage, la cadence, le décalage et l'échantillonneur du modèle, jamais ceux
de la demande. Le temps compté pour la pause de la puissance prêtée part de la fin du
chargement du modèle : une vidéo de 5 min de calcul, à 60 % de puissance prêtée, est suivie
de 3 min 20 de pause. La veille (public_lending._watch) arrête la vidéo dès que
l'utilisateur, mes machines ou mes amis veulent la machine ; une fois la vidéo faite, elle
s'arrête.

Rien n'est gardé : sd-server rend la vidéo en mémoire (base64) ; ni la description, ni la
vidéo, ni la clé ne restent après la réponse ; le journal ne dit que le modèle, la taille, le
nombre d'images et de pas, les durées. Hors d'AIODOS, comme pour une image : sd-server garde
sa tâche finie en mémoire jusqu'à son arrêt.

Le profil public annonce les modèles de vidéos prêtés (offered) sous la marque des images,
public_imaging.MARKER (« image:wan2.1-t2v-1.3b ») : le catalogue dit qu'ils sont des vidéos.
Pas de marque « video: » à part : les nœuds d'avant (dès le 29/10/2026 au matin) réservent
déjà « image: » (placement.RESERVED) et ignorent un modèle d'images qui est une vidéo
(public_imaging.lendable), alors qu'ils prendraient « video:wan2.1-t2v-1.3b » pour un modèle
de conversation de 1,3 milliard de paramètres (public_chat.params lit « 1.3b »), plus gros
qu'un qwen3.5:0.8b, et lui enverraient des conversations refusées. Seulement si
lend_images_public est permis, comme les images.
"""
import asyncio
import base64
import math
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aiodos import __version__, settings
from aiodos.imaging import catalog, webm
from aiodos.network import connections, public_imaging, public_lending
from aiodos.network.public_lending import Refused

log = public_imaging.log

PATH = "/public/v1/video"
MARKER = public_imaging.MARKER     # la marque des images, pour les vidéos aussi (cf. plus haut)
FIELDS = {"v", "model", "prompt", "negative", "width", "height", "frames", "steps", "seed"}
MAX_PROMPT = public_imaging.MAX_PROMPT
# Pixels d'une image de la vidéo, au plus : les formats proposés de Wan 2.1 (832 × 480,
# 480 × 832, 624 × 624) y tiennent
MAX_PIXELS = 832 * 480
# Images d'une vidéo, au plus : la plus longue durée proposée de Wan 2.1, 5 s à 16 images par
# seconde (81 images : 2,5 fois le calcul des 2 s mesurées, environ 8 min sur une RTX 4070)
MAX_FRAMES = 81
SEED_SPACE = public_imaging.SEED_SPACE
# Le WebM rendu, au plus : 2 Mo pour 2 s en 832 × 480 (mesuré le 17/10/2026), chaque image
# en image clé (libwebp) ; 81 images en font 5 environ
MAX_VIDEO = 32 * 1024 ** 2
PART = public_imaging.PART         # octets du WebM par morceau (256 Kio de base64 par ligne)
COMPUTING = public_imaging.COMPUTING
FORMAT = "webm"


# ------------------------------------------------------------- prêter, ou non
def lendable(model_id) -> bool:
    """Un modèle du catalogue qu'on prête aux inconnus, et qu'on leur demande : une vidéo, sans
    licence restrictive (qui demande un accord lu)."""
    spec = catalog.MODELS.get(str(model_id or ""))
    return bool(spec) and spec.get("kind") == "video" and not spec.get("notice")


def max_steps(spec: dict) -> int:
    """Pas d'une vidéo, au plus : ceux du modèle (20 pour Wan 2.1), jamais plus que
    public_imaging.MAX_STEPS : chaque pas d'une vidéo coûte des dizaines d'images."""
    return min(public_imaging.MAX_STEPS, max(1, int(spec.get("steps") or 1)))


def fits(spec: dict, width: int, height: int) -> bool:
    """La taille est-elle dans les bornes du modèle et du réseau public ?"""
    multiple, (low, high) = spec["multiple"], spec["side"]
    return (all(side % multiple == 0 and low <= side <= high for side in (width, height))
            and width * height <= min(MAX_PIXELS, spec["max_pixels"]))


def frames_allowed(spec: dict, frames: int) -> bool:
    """Un nombre d'images que le modèle permet (frame_step × k + frame_base), MAX_FRAMES au
    plus."""
    step, base = spec["frame_step"], spec["frame_base"]
    return base <= frames <= min(MAX_FRAMES, spec["max_frames"]) and (frames - base) % step == 0


def fps_of(spec: dict) -> int:
    return int(spec.get("fps") or 16)


def _ready(model_id) -> bool:
    """stable-diffusion.cpp et le modèle complet ici (à la demande : imaging dépend du réseau)."""
    from aiodos.jobs.kinds import images as kind
    return kind.runnable_here(model_id)


def offered() -> list:
    """Les modèles de vidéos prêtés, pour le profil public : [MARKER + modèle] ; aucun sans le
    réglage lend_images_public (cf. le début du module)."""
    if not settings.get("lend_images_public"):
        return []
    try:
        return [MARKER + m for m in catalog.MODELS if lendable(m) and _ready(m)]
    except Exception:  # noqa: BLE001 — modèles illisibles : on n'annonce rien
        log.debug("Modèles de vidéos illisibles pour le profil public", exc_info=True)
        return []


# ------------------------------------------------------------------ admission
def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _request(raw: bytes) -> dict:
    """La demande lue et bornée (cf. le début du module) : les réglages de la vidéo, ou
    Refused."""
    body = public_lending._parse(raw)
    if set(body) - FIELDS:
        raise Refused(413, "too_big")              # une image de départ… : pas ici
    model = body.get("model")
    if not isinstance(model, str) or not model or len(model) > public_lending.MAX_MODEL_NAME:
        raise public_lending._bad()
    if not lendable(model) or not _ready(model):
        raise Refused(404, "no_model")
    spec = catalog.model(model)
    prompt, negative = body.get("prompt"), body.get("negative")
    negative = "" if negative is None else negative
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(negative, str):
        raise public_lending._bad()
    if len(prompt) > MAX_PROMPT or len(negative) > MAX_PROMPT:
        raise Refused(413, "too_big")
    numbers = [body.get(k) for k in ("width", "height", "frames", "steps", "seed")]
    width, height, frames, steps, seed = numbers
    if not all(_integer(v) for v in numbers) or min(width, height, frames, steps) < 1 \
            or not 0 <= seed < SEED_SPACE:
        raise public_lending._bad()
    if steps > max_steps(spec) or not fits(spec, width, height) \
            or not frames_allowed(spec, frames):
        raise Refused(413, "too_big")
    return {"model": model, "prompt": prompt.strip(),
            "negative": negative.strip() if spec["negative"] else "", "width": width,
            "height": height, "frames": frames, "steps": steps, "seed": seed,
            "cfg": spec["cfg"]}


def admit(manager, raw: bytes) -> dict:
    """Les vérifications, dans l'ordre (cf. le début du module) ; Refused au premier refus,
    avant tout calcul. → {"model", "params", "place"} : la place est prise."""
    if not public_imaging.lending_images(manager):
        raise Refused(503, "not_lending")
    params = _request(raw)                         # rien de pris pour une demande refusée
    # ni pour une description interdite (i18n: tel quel, journal)
    public_imaging._filtered(manager, params, "Vidéo refusée par le filtre (%s)")
    return {"model": params["model"], "params": params,
            "place": public_lending._take_place(manager)}


# ------------------------------------------------------------------- réponse
def _generate(params: dict, stop) -> dict:
    """{"webm", "frames", "fps", "seconds"} : la vidéo calculée ici (imaging/generate.video)."""
    from aiodos.imaging import generate
    return generate.video(params, stop)


def _made(made, params: dict) -> tuple | None:
    """(WebM réécrit, secondes) d'un calcul réussi, ou None : pas la vidéo demandée (taille,
    nombre d'images, imaging/webm.py), ou trop grosse (MAX_VIDEO)."""
    data = made.get("webm") if isinstance(made, dict) else None
    if not isinstance(data, bytes) or not data or len(data) > MAX_VIDEO:
        return None
    try:
        clean = webm.check(data, params["width"], params["height"], params["frames"],
                           fps_of(catalog.model(params["model"])), deep=False)
    except ValueError:
        return None
    seconds = made.get("seconds")
    seconds = round(float(seconds), 1) if isinstance(seconds, (int, float)) \
        and not isinstance(seconds, bool) and math.isfinite(seconds) and seconds >= 0 else None
    return clean, seconds


def answer(manager, asked: dict, stop, clock=None):
    """Les événements de la réponse (meta, video, part…, done ; ou error ; les signes de vie,
    public_lending._Stream), comme public_imaging.answer : veille, place rendue, pause
    comptée, profil public redit au début et à la fin."""
    model, place, params = asked["model"], asked["place"], asked["params"]
    clock = clock if clock is not None else public_lending._Clock()
    started = time.monotonic()
    done, why = threading.Event(), []
    number = public_lending.begin(manager, model)
    try:
        yield {"type": "meta", "model": model, "version": __version__}
        if stop.is_set():
            return
        public_lending.watching(manager, stop, done, why)
        found, failed = None, False
        try:
            found = _made(_generate(params, stop), params)
            failed = found is None and not stop.is_set()
            if failed:
                # i18n: tel quel (journal)
                log.warning("Vidéo pour le réseau public : %s, vidéo absente, autre ou trop "
                            "grosse", model)
        except Exception as exc:  # noqa: BLE001 — l'appelant reçoit un code, sans détail
            failed = not stop.is_set()
            if failed:
                # Le type seulement : le message du moteur pourrait citer la description
                # i18n: tel quel (journal)
                log.warning("Vidéo pour le réseau public : %s, moteur en échec (%s)", model,
                            type(exc).__name__)
        if why or failed:
            # i18n: tel quel (journal)
            log.info("Vidéo pour le réseau public : %s, arrêtée (%s)", model,
                     why[0] if why else "engine")
            yield {"type": "error", "error": why[0] if why else "engine"}
            return
        if stop.is_set():
            return
        done.set()                     # la vidéo est faite : la veille s'arrête
        data, seconds = found
        yield {"type": "video", "format": FORMAT, "bytes": len(data), "width": params["width"],
               "height": params["height"], "frames": params["frames"],
               "fps": fps_of(catalog.model(model)), "seconds": seconds}
        for at in range(0, len(data), PART):
            if stop.is_set():
                return                 # l'appelant est parti
            yield {"type": "part", "data": base64.b64encode(data[at:at + PART]).decode("ascii")}
        yield {"type": "done"}
        # i18n: tel quel (journal) ; ni la description, ni la clé
        log.info("Vidéo pour le réseau public : %s, %d × %d, %d images, %d pas, %s s de calcul,"
                 " sur %s s", model, params["width"], params["height"], params["frames"],
                 params["steps"], seconds, round(time.monotonic() - started, 1))
    finally:
        public_lending.end(manager, number, done, clock, place)


def mount(app, manager):
    """La route publique des vidéos, dans l'application du serveur entre machines
    (p2p_app.create_p2p_app), déclarée avant ses routes génériques."""

    @app.post(PATH)
    async def public_video(request: Request):
        if not connections.public_key(request.scope):
            raise HTTPException(404)          # comme une route inconnue (cf. PublicOnly)
        raw = await request.body()
        try:
            asked = await asyncio.to_thread(admit, manager, raw)
        except Refused as exc:
            return JSONResponse({"error": exc.code}, status_code=exc.status)
        stop, clock = threading.Event(), public_lending._Clock()
        return public_lending._Stream(answer(manager, asked, stop, clock), asked["place"], stop,
                                      asked["model"], clock, stage=public_imaging._stage_seen)
