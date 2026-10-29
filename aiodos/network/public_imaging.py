"""Prêter ma carte graphique aux images d'inconnus : le côté prêteur des images du réseau
public (docs/conception/reseau-public-calcul.md § 11 ; décision de l'auteur du 29/10/2026).
Le côté demandeur : network/public_image_ask.py ; l'accord, chez lui : chat/public_creations.py.

Un inconnu du réseau public fait créer une image par cette machine, sans dire qui il est (une
clé jetable par création). Le compagnon (aiodos-net) la reçoit et la borne comme une
conversation (POST /public/v1/…, 256 Kio, une à la fois par clé, cf. public_lending.py) et la
relaie ici sur POST /public/v1/image, avec l'en-tête X-Aiodos-Public : sans lui, 404, comme une
route inconnue (public_lending.PublicOnly).

Activé par défaut (réglage lend_images_public, « Prêter ma carte graphique aux images
d'inconnus »), en plus du prêt aux inconnus (lend_public). Vérifications, dans l'ordre, chacune
avec sa réponse, avant tout calcul (admit) :

  1. les deux réglages, le partage actif, sur secteur ; un téléphone seulement en charge et en
     Wi-Fi (public_lending.lending_now) : sinon 503 {"error": "not_lending"} ;
  2. la demande lue et bornée, sans rien prendre (ni la place, ni le seau de tout le public) :
     {"v": 1, "model", "prompt", "negative", "width", "height", "steps", "seed"}. Un modèle
     d'images du catalogue (imaging/catalog.py) qu'on prête (lendable : une image, pas une
     vidéo, qui a sa route, network/public_video.py ; sans licence restrictive), complet ici
     avec stable-diffusion.cpp : sinon 404 {"error": "no_model"}. Une description de
     MAX_PROMPT caractères au plus, des côtés au
     multiple et dans les bornes du modèle, MAX_PIXELS pixels au plus, de 1 à max_steps pas, et
     rien d'autre (ni image de départ, ni force de retouche dans cette version) : sinon 413
     {"error": "too_big"} ; mal formée : 400 {"error": "bad_request"} ;
  3. le filtre des descriptions (imaging/prompt_filter.py ; décision de l'auteur du
     29/10/2026 : le prêt reste activé par défaut, et une description interdite n'est jamais
     calculée) : la base sans modèle, toujours ; le modèle de conversation de cette machine en
     plus, s'il est déjà chargé (jamais un chargement, qui prendrait la carte au moteur
     d'images) et que personne d'autre ne veut la machine. Description (ou consigne négative)
     interdite : 403 {"error": "refused_content"}, sans rien prendre ni garder ; le journal
     dit seulement « Image refusée par le filtre (<code>) » ;
  4. la place du public (public_lending._take_place : une seule pour tout le public,
     conversation, lecture, image ou vidéo ; personne d'autre ne veut la machine,
     l'utilisateur, mes machines ou mes amis ; la pause de la puissance prêtée ; la mémoire
     vive ; le seau de tout le public) : sinon 503 {"error": "busy"}.

La réponse, en NDJSON (public_lending._Stream) : {"type": "meta", "model", "version"} ; tant
que l'image n'est pas prête, toutes les public_lending.WAIT_S secondes, {"type": "wait",
"stage": "loading" (sd-server charge le modèle) ou "computing" (il calcule l'image)} ; puis
{"type": "image", "format": "png", "bytes", "width", "height", "seconds"}, le PNG en morceaux
{"type": "part", "data": base64} de PART octets au plus (une ligne reste bien sous la borne du
demandeur, client.MAX_FRAME), et {"type": "done"}. Ou, en route, {"type": "error", "error":
code} : « busy » (l'utilisateur, mes machines, mes amis ou le travail de fond reprennent la
machine : ils passent devant, public_lending._watch), « not_lending » (prêt coupé, machine
débranchée…), « engine » (le moteur a failli, ou son image n'en est pas une, ou dépasse
MAX_IMAGE octets).

Le calcul : imaging/generate.image, comme une image de mes machines (sd-server, un calcul à la
fois), avec le guidage (cfg) du modèle, jamais celui de la demande. Le temps compté pour la
pause de la puissance prêtée part de la fin du chargement du modèle (public_lending._Clock :
chaque signe de vie « loading » l'avance), envoi de l'image compris. Une fois l'image faite, la
veille s'arrête : un utilisateur qui revient pendant l'envoi ne gâche pas une image calculée.

Rien n'est gardé : sd-server rend l'image en mémoire (base64), rien n'est écrit sur le disque
ici ; la description, l'image et la clé de l'appelant sont oubliées à la fin de la réponse, et
le journal ne dit que le modèle, la taille, le nombre de pas et les durées (l'historique de la
vue Tâches, network/lent_history.py : le modèle, la taille, la durée et l'issue). Hors d'AIODOS :
sd-server garde sa tâche finie (description et image) en mémoire jusqu'à son arrêt
(imaging/engine.IDLE_S après la dernière image), et son journal (data/logs/sd-server.log, niveau
« info ») dit la taille et la graine, pas la description.

Le profil public (network/public.py) annonce les modèles prêtés (offered) dans « models »,
marqués MARKER (« image:z-image-turbo ») : le profil signé du compagnon n'a pas d'autre champ
(compagnon/src/public_profile.rs), et un champ inconnu ne passerait pas. Les modèles de vidéos
prêtés portent la même marque (network/public_video.py dit pourquoi) : le catalogue les
distingue. Ce nœud-ci ne les prend jamais pour des modèles de conversation
(placement.RESERVED) ; un nœud d'avant le
29/10/2026 pourrait en choisir un en « Automatique · réseau public », s'il n'en voit pas d'autre
chez cette machine, et recevrait « no_model ». Seulement si lend_images_public est permis : un
modèle annoncé puis refusé (« not_lending ») compterait chez le demandeur comme un refus suspect
(network/public_refusals.py).
"""
import asyncio
import base64
import logging
import math
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aiodos import __version__, settings
from aiodos.imaging import catalog, prompt_filter
from aiodos.network import connections, lent_history, public_lending
from aiodos.network.public_lending import Refused

log = logging.getLogger("aiodos.net")

PATH = "/public/v1/image"
MARKER = "image:"              # dans « models » du profil public : un modèle d'images prêté
COMPUTING = "computing"        # étape d'un signe de vie : sd-server calcule l'image
FIELDS = {"v", "model", "prompt", "negative", "width", "height", "steps", "seed"}
MAX_PROMPT = 2000              # comme une demande d'ici (jobs/kinds/images.MAX_PROMPT)
# Pixels d'une image, au plus : les formats proposés de Z-Image Turbo (1024 × 1024, 768 × 1344,
# 1344 × 768) y tiennent ; 23 s sur une RTX 4070 pour 1024 × 1024 en 8 pas (essai du 17/10)
MAX_PIXELS = 1024 * 1024
STEPS_FACTOR = 2               # pas d'une image, au plus : deux fois ceux du modèle…
MAX_STEPS = 50                 # … et jamais plus que cela
SEED_SPACE = 2 ** 31           # comme jobs/kinds/images.SEED_SPACE
# Le PNG rendu, au plus : 1024 × 1024 en RVB pèse 3 Mo sans compression ; sd-server en rend
# 1 à 2 Mo
MAX_IMAGE = 6 * 1024 ** 2
PART = 192 * 1024              # octets du PNG par morceau (256 Kio de base64 par ligne)
REFUSED = prompt_filter.REFUSED      # « refused_content » : une description interdite


# ------------------------------------------------------------- prêter, ou non
def lending_images(manager) -> bool:
    """Cette machine prête-t-elle sa carte aux images d'inconnus en ce moment ? Le réglage, et
    les conditions du prêt aux inconnus (public_lending.lending_now)."""
    return bool(settings.get("lend_images_public")) and public_lending.lending_now(manager)


def lendable(model_id) -> bool:
    """Un modèle du catalogue qu'on prête aux inconnus, et qu'on leur demande : une image (une
    vidéo : network/public_video.py). Une licence restrictive aussi, depuis le 29/10/2026
    (décision de l'auteur : « chacun est responsable de ses actes ») : qui le prête a lu sa
    licence en le téléchargeant, et un avertissement s'il encadre le prêt
    (catalog.lend_notice, GET /api/images/models) ; qui le demande ne le prend pas en
    « Automatique » sans l'avoir ici ou chez ses machines, ou sans avoir accepté
    sa licence ici (imaging/ranking.py, catalog.accepted)."""
    spec = catalog.MODELS.get(str(model_id or ""))
    return bool(spec) and spec.get("kind", "image") == "image"


def max_steps(spec: dict) -> int:
    return min(MAX_STEPS, STEPS_FACTOR * max(1, int(spec.get("steps") or 1)))


def fits(spec: dict, width: int, height: int) -> bool:
    """La taille est-elle dans les bornes du modèle et du réseau public ?"""
    multiple, (low, high) = spec["multiple"], spec["side"]
    return (all(side % multiple == 0 and low <= side <= high for side in (width, height))
            and width * height <= min(MAX_PIXELS, spec["max_pixels"]))


def _ready(model_id) -> bool:
    """stable-diffusion.cpp et le modèle complet ici (à la demande : imaging dépend du réseau)."""
    from aiodos.jobs.kinds import images as kind
    return kind.runnable_here(model_id)


def offered() -> list:
    """Les modèles d'images prêtés, pour le profil public : [MARKER + modèle] ; aucun sans le
    réglage lend_images_public (cf. le début du module)."""
    if not settings.get("lend_images_public"):
        return []
    try:
        return [MARKER + m for m in catalog.MODELS if lendable(m) and _ready(m)]
    except Exception:  # noqa: BLE001 — modèles illisibles : on n'annonce rien
        log.debug("Modèles d'images illisibles pour le profil public", exc_info=True)
        return []


# ------------------------------------------------------------------ admission
def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _request(raw: bytes) -> dict:
    """La demande lue et bornée (cf. le début du module) : les réglages de l'image, ou Refused."""
    body = public_lending._parse(raw)
    if set(body) - FIELDS:
        raise Refused(413, "too_big")              # une image de départ, une force… : pas ici
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
    width, height, steps, seed = (body.get(k) for k in ("width", "height", "steps", "seed"))
    if not all(_integer(v) for v in (width, height, steps, seed)) or steps < 1 \
            or width < 1 or height < 1 or not 0 <= seed < SEED_SPACE:
        raise public_lending._bad()
    if steps > max_steps(spec) or not fits(spec, width, height):
        raise Refused(413, "too_big")
    return {"model": model, "prompt": prompt.strip(),
            "negative": negative.strip() if spec["negative"] else "", "width": width,
            "height": height, "steps": steps, "seed": seed, "cfg": spec["cfg"]}


# i18n: tel quel (journal)
def _filtered(manager, params: dict, said: str = "Image refusée par le filtre (%s)"):
    """Le filtre des descriptions (cf. le début du module, 3) : Refused 403 « refused_content »
    pour une description interdite. Rien n'est gardé : le journal ne dit que le code (said :
    sa phrase, celle d'une vidéo pour network/public_video.py)."""
    model = prompt_filter.warm_model() if public_lending._room(manager) else None
    code = prompt_filter.check(params["prompt"], params["negative"], model)
    if code is not None:
        # Ni la description, ni la clé
        log.info(said, code)
        raise Refused(403, REFUSED)


def admit(manager, raw: bytes) -> dict:
    """Les vérifications, dans l'ordre (cf. le début du module) ; Refused au premier refus,
    avant tout calcul. → {"model", "params", "place"} : la place est prise."""
    if not lending_images(manager):
        raise Refused(503, "not_lending")
    params = _request(raw)                         # rien de pris pour une demande refusée
    try:
        _filtered(manager, params)                 # ni pour une description interdite
        place = public_lending._take_place(manager)
    except Refused as exc:
        exc.model = params["model"]                # du catalogue (lent_history)
        raise
    return {"model": params["model"], "params": params, "place": place}


# ------------------------------------------------------------------- réponse
def _stage_seen(model) -> tuple:
    """(étape, instant monotonic de la fin de sa lecture) : « computing » si sd-server a ce
    modèle chargé, sinon LOADING (public_lending._Stream, _Clock)."""
    from aiodos.imaging import engine
    try:
        loaded = engine.loaded()
    except Exception:  # noqa: BLE001 — illisible : on dit qu'il charge
        loaded = None
    return (COMPUTING if loaded == model else public_lending.LOADING), time.monotonic()


def _generate(params: dict, stop) -> dict:
    """{"png", "seconds"} : l'image calculée ici (imaging/generate.image)."""
    from aiodos.imaging import generate
    return generate.image(params, stop)


def _made(made) -> tuple | None:
    """(PNG, largeur, hauteur, secondes) d'un calcul réussi, ou None : pas un PNG, ou trop
    gros (MAX_IMAGE)."""
    from aiodos.imaging import generate
    png = made.get("png") if isinstance(made, dict) else None
    if not isinstance(png, bytes) or not png or len(png) > MAX_IMAGE:
        return None
    try:
        width, height = generate.png_size(png)
    except ValueError:
        return None
    seconds = made.get("seconds")
    seconds = round(float(seconds), 1) if isinstance(seconds, (int, float)) \
        and not isinstance(seconds, bool) and math.isfinite(seconds) and seconds >= 0 else None
    return png, width, height, seconds


def answer(manager, asked: dict, stop, clock=None):
    """Les événements de la réponse (meta, image, part…, done ; ou error ; les signes de vie,
    public_lending._Stream), comme public_lending.answer : veille, place rendue, pause comptée,
    profil public redit au début et à la fin."""
    model, place, params = asked["model"], asked["place"], asked["params"]
    clock = clock if clock is not None else public_lending._Clock()
    started = time.monotonic()
    done, why = threading.Event(), []
    number = public_lending.begin(manager, model)
    # L'historique du travail pour d'autres : le modèle, la taille, la durée ; ni la
    # description, ni l'image
    lent = lent_history.begin(lent_history.IMAGE, lent_history.PUBLIC, model=model)
    sizes = {"width": params["width"], "height": params["height"]}
    try:
        yield {"type": "meta", "model": model, "version": __version__}
        if stop.is_set():
            return
        public_lending.watching(manager, stop, done, why)
        found, failed = None, False
        try:
            found = _made(_generate(params, stop))
            failed = found is None and not stop.is_set()
            if failed:
                # i18n: tel quel (journal)
                log.warning("Image pour le réseau public : %s, image absente ou trop grosse", model)
        except Exception as exc:  # noqa: BLE001 — l'appelant reçoit un code, sans détail
            failed = not stop.is_set()
            if failed:
                # Le type seulement : le message du moteur pourrait citer la description
                # i18n: tel quel (journal)
                log.warning("Image pour le réseau public : %s, moteur en échec (%s)", model,
                            type(exc).__name__)
        if why or failed:
            # i18n: tel quel (journal)
            log.info("Image pour le réseau public : %s, arrêtée (%s)", model,
                     why[0] if why else "engine")
            lent.end(lent_history.STOPPED if why else lent_history.FAILED,
                     why[0] if why else "engine", clock.counted(), **sizes)
            yield {"type": "error", "error": why[0] if why else "engine"}
            return
        if stop.is_set():
            return
        done.set()                     # l'image est faite : la veille s'arrête (cf. plus haut)
        png, width, height, seconds = found
        sizes = {"width": width, "height": height}
        yield {"type": "image", "format": "png", "bytes": len(png), "width": width,
               "height": height, "seconds": seconds}
        for at in range(0, len(png), PART):
            if stop.is_set():
                return                 # l'appelant est parti
            yield {"type": "part", "data": base64.b64encode(png[at:at + PART]).decode("ascii")}
        yield {"type": "done"}
        # i18n: tel quel (journal) ; ni la description, ni la clé
        log.info("Image pour le réseau public : %s, %d × %d, %d pas, %s s de calcul, sur %s s",
                 model, width, height, params["steps"], seconds,
                 round(time.monotonic() - started, 1))
        lent.end(lent_history.DONE, None, seconds if seconds is not None else clock.counted(),
                 **sizes)
    finally:
        # Sans fin dite plus haut : l'appelant est parti en route (image faite ou non)
        lent.end(lent_history.STOPPED, "left", clock.counted(), **sizes)
        public_lending.end(manager, number, done, clock, place)


def mount(app, manager):
    """La route publique des images, dans l'application du serveur entre machines
    (p2p_app.create_p2p_app), déclarée avant ses routes génériques."""

    @app.post(PATH)
    async def public_image(request: Request):
        if not connections.public_key(request.scope):
            raise HTTPException(404)          # comme une route inconnue (cf. PublicOnly)
        raw = await request.body()
        try:
            asked = await asyncio.to_thread(admit, manager, raw)
        except Refused as exc:
            lent_history.refused(lent_history.IMAGE, lent_history.PUBLIC, exc.code,
                                 model=exc.model)
            return JSONResponse({"error": exc.code}, status_code=exc.status)
        stop, clock = threading.Event(), public_lending._Clock()
        return public_lending._Stream(answer(manager, asked, stop, clock), asked["place"], stop,
                                      asked["model"], clock, stage=_stage_seen)
