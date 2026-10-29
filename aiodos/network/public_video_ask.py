"""Faire créer mes vidéos par des inconnus : le côté demandeur des vidéos du réseau public
(docs/conception/reseau-public-calcul.md § 11 ; l'auteur, le 29/10/2026 : « il faut que ça
soit pareil pour les vidéos »). Le côté prêteur : network/public_video.py ; l'accord et le
choix du modèle : chat/public_creations.py ; l'unité de la file : jobs/kinds/public_videos.py.
Le même chemin que les images (network/public_image_ask.py), avec les bornes et les délais
d'une vidéo.

Les machines candidates : celles du réseau public qui prêtent, ont une place et annoncent ce
modèle de vidéos (la marque des images, public_video.MARKER, sur un modèle que le catalogue dit
être une vidéo, public_video.lendable), dans l'ordre des images (public_image_ask.lenders :
celles qui ont déjà servi ici d'abord, puis la plus rapide, la plus récemment vue),
PUBLIC_TRIES au plus, chacune notée comme proposée.

L'envoi (video) : par le compagnon, POST /net/v1/public/ask/<clé>/public/v1/video, avec une
clé jetable par création (public_chat.tag). Ce qui part (body) : le modèle, la description
(enrichie ici, jamais chez un inconnu), la consigne négative, la taille, le nombre d'images, le
nombre de pas, la graine ; jamais d'image de départ, ni rien de la conversation. Une demande
hors des bornes du prêteur ne part pas ; une description que la base du filtre juge interdite
non plus (PublicError « refused_content », rien d'envoyé).

Ce qui revient (le NDJSON de public_video.py) est borné et vérifié ici avant d'être gardé :
« meta » au modèle demandé ; l'en-tête de la vidéo (WebM, MAX_VIDEO octets au plus, la taille
et le nombre d'images demandés) ; ses morceaux (jamais plus que les octets annoncés) ; « done »
avec exactement ces octets ; puis la vidéo elle-même (check, imaging/webm.py : lue en entier,
une seule piste vidéo de la taille demandée, exactement ce nombre d'images, la première en
image clé ; réécrite avec ses images seules, à la cadence du modèle ; avec PyAV, décodée en
entier et pas d'une seule couleur). Délais (une vidéo prend des minutes) : FIRST_EVENT_S pour
le premier événement ; chaque signe de vie (« wait », toutes les 5 s) repousse l'échéance de
ALIVE_GRACE_S, tant que la vidéo n'est pas annoncée ; TOTAL_S en tout depuis la demande, signes
de vie compris (un prêteur qui en enverrait sans fin est coupé) ; MAX_STREAM octets de flux.

Un échec du prêteur (FAULTS : lent, coupé, illisible, trop long, moteur arrêté, vidéo
invalide) l'écarte quelques minutes (reputation.bench) ; un refus « avant calcul » qui
contredit son profil est compté (public_refusals) ; le refus de son filtre (« refused_content »)
n'est ni l'un ni l'autre. Une vidéo reçue et valide compte comme une réponse servie. Pas de
contrôle au hasard des vidéos dans cette version. Le journal ne dit que la clé abrégée et le
code d'un refus, jamais la description.
"""
import json
import logging
import threading
import time

import requests

from aiodos.imaging import catalog, prompt_filter, webm
from aiodos.network import (client, placement, public_chat, public_image_ask, public_refusals,
                            public_video, reputation)
from aiodos.network.client import PeerError
from aiodos.network.public_chat import PublicError
from aiodos.network.route import NO_PROXY

log = logging.getLogger("aiodos.net")

MARKER = public_video.MARKER
PUBLIC_TRIES = placement.PUBLIC_TRIES
FIRST_EVENT_S = public_image_ask.FIRST_EVENT_S     # 60 s : le compagnon, la clé, l'admission
ALIVE_GRACE_S = public_image_ask.ALIVE_GRACE_S     # 20 s : trois signes de vie perdus
# Toute la vidéo, depuis la demande, au plus : le chargement du modèle (7,2 Go à lire, une
# minute ou plus sur un disque lent), le calcul (3 min 42 pour 2 s en 832 × 480 sur une RTX 4070
# portable ; 5 s, la plus longue permise, deux fois et demie plus ; une petite carte, deux ou
# trois fois plus encore), l'envoi par un relais (quelques Mo)
TOTAL_S = 45 * 60
WATCH_S = public_chat.WATCH_S
MAX_VIDEO = public_video.MAX_VIDEO
# Le flux en entier, au plus : la vidéo en base64, un signe de vie toutes les 5 s pendant
# TOTAL_S, une marge
MAX_STREAM = MAX_VIDEO * 4 // 3 + 2 * 1024 ** 2
FAULTS = public_chat.FAULTS


# ------------------------------------------------------------------ les machines
def offered_by(node) -> list:
    """Les modèles de vidéos (identifiants du catalogue) que ce nœud du réseau public prête,
    parmi ceux qu'on demande aux inconnus (public_video.lendable)."""
    found = []
    for name in node.get("models") or []:
        if isinstance(name, str) and name.startswith(MARKER):
            model = name[len(MARKER):]
            if public_video.lendable(model) and model not in found:
                found.append(model)
    return found


def offered_models() -> list:
    """Les modèles de vidéos qu'une machine du réseau public peut créer pour moi en ce moment,
    dans l'ordre du catalogue ; aucun sans compagnon prêt ni réseau public rejoint."""
    return public_image_ask.offered_models(offers=offered_by)


def lenders(model: str, offer: bool = True) -> list:
    """[(clé, modèle)] : les machines du réseau public à essayer pour une vidéo de ce modèle,
    la mieux placée d'abord, PUBLIC_TRIES au plus (public_image_ask.lenders)."""
    return public_image_ask.lenders(model, offer, offers=offered_by)


def lent(model: str) -> bool:
    """Une machine du réseau public prête-t-elle ce modèle de vidéos, même sans place en ce
    moment (public_image_ask.lent) ?"""
    return public_image_ask.lent(model, offers=offered_by)


# ------------------------------------------------------------------- la demande
def body(params: dict) -> bytes:
    """Le corps envoyé au prêteur ({"v": 1, "model", "prompt", "negative", "width", "height",
    "frames", "steps", "seed"}) ; PublicError « too_big » s'il dépasse ses bornes
    (public_video), « bad_request » s'il est mal formé, « refused_content » si la base du
    filtre juge la description interdite."""
    model = str(params.get("model") or "")
    if not public_video.lendable(model):
        raise PublicError("bad_request")
    spec = catalog.model(model)
    try:
        asked = {"v": 1, "model": model, "prompt": str(params["prompt"]).strip(),
                 "negative": str(params.get("negative") or "").strip() if spec["negative"] else "",
                 "width": int(params["width"]), "height": int(params["height"]),
                 "frames": int(params["frames"]),
                 "steps": int(params.get("steps") or spec["steps"]),
                 "seed": int(params["seed"]) % public_video.SEED_SPACE}
    except (KeyError, TypeError, ValueError):
        raise PublicError("bad_request") from None
    if not asked["prompt"]:
        raise PublicError("bad_request")
    if len(asked["prompt"]) > public_video.MAX_PROMPT \
            or len(asked["negative"]) > public_video.MAX_PROMPT \
            or not 1 <= asked["steps"] <= public_video.max_steps(spec) \
            or not public_video.fits(spec, asked["width"], asked["height"]) \
            or not public_video.frames_allowed(spec, asked["frames"]):
        raise PublicError("too_big")
    if prompt_filter.base(asked["prompt"], asked["negative"]) is not None:
        raise PublicError(prompt_filter.REFUSED)
    data = json.dumps(asked, ensure_ascii=False).encode()
    if len(data) > public_chat.MAX_BODY:
        raise PublicError("too_big")
    return data


# --------------------------------------------------------------- la vidéo reçue
def check(data: bytes, params: dict) -> bytes:
    """La vidéo reçue, vérifiée et réécrite (imaging/webm.check) ; PublicError « unreadable »
    si ce n'est pas la vidéo demandée."""
    spec = catalog.model(params["model"])
    try:
        return webm.check(data, params["width"], params["height"], params["frames"],
                          public_video.fps_of(spec))
    except ValueError:
        raise PublicError("unreadable") from None


def _header(event: dict, params: dict) -> dict:
    """L'en-tête de la vidéo : WebM, MAX_VIDEO octets au plus (« too_long » au-delà), la
    taille et le nombre d'images demandés (« unreadable » sinon)."""
    size = event.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise PublicError("unreadable")
    if size > MAX_VIDEO:
        raise PublicError("too_long")
    if event.get("format") != public_video.FORMAT or event.get("width") != params["width"] \
            or event.get("height") != params["height"] or event.get("frames") != params["frames"]:
        raise PublicError("unreadable")
    seconds = event.get("seconds")
    return {"bytes": size, "seconds": float(seconds) if public_image_ask._number(seconds)
            and seconds >= 0 else None}


def _alive(deadline: list, started: float):
    """Un signe de vie du prêteur : l'échéance repoussée de ALIVE_GRACE_S, TOTAL_S au plus
    depuis la demande."""
    deadline[0] = max(deadline[0], min(started + TOTAL_S, time.monotonic() + ALIVE_GRACE_S))


def _watch(response, stop, first, done, why: list, deadline: list, started: float):
    """Coupe la réponse à l'arrêt (stop), après TOTAL_S secondes (« too_long » : même avec des
    signes de vie), et à l'échéance tant que la vidéo n'est pas annoncée (« slow » : first ;
    deadline[0], repoussée par les signes de vie) : public_chat._watch, avec le délai d'une
    vidéo."""
    while not done.wait(WATCH_S):
        if stop is not None and stop.is_set():
            why.append("stop")
        elif time.monotonic() > started + TOTAL_S:
            why.append("too_long")
        elif not first.is_set() and time.monotonic() > deadline[0]:
            why.append("slow")
        else:
            continue
        client._cut(response)
        return


# -------------------------------------------------------------------- l'envoi
def video(key, params: dict, stop=None, conversation=None) -> dict | None:
    """{"webm": octets réécrits, "frames", "fps", "seconds": durée du calcul chez lui (ou
    None)} : la vidéo créée par la machine du réseau public `key` (cf. le début du module) ;
    None si l'arrêt (stop) est venu avant la fin. conversation : l'identifiant opaque de la
    création (public_chat.tag) ; sans lui, une clé jetable pour cette seule vidéo. PublicError
    à tout refus ou échec, comme public_image_ask.image."""
    data = body(params)                         # nos bornes : ce qui part, elles l'acceptent
    model = params["model"]
    promised = public_refusals.promised(key, MARKER + model)    # proposée ? lu avant l'envoi
    try:
        made = _video(key, data, params, stop, conversation)
    except PublicError as exc:
        if exc.code in public_refusals.INNOCENT:
            pass                                # son filtre a fait son travail
        elif exc.code in FAULTS:
            reputation.bench(key, exc.code)
        elif promised and exc.code in public_refusals.SUSPECT:
            public_refusals.refused(key, exc.code)
        raise
    if made is not None:
        reputation.note_served(key, 0, made["seconds"] or 0.0)  # servie, sans vitesse
    return made


def _video(key, data: bytes, params: dict, stop, conversation) -> dict | None:
    """video, le corps `data` prêt."""
    companion = public_chat.ready()
    if companion is None:
        raise PublicError("companion")
    if stop is not None and stop.is_set():
        return None
    headers = {**companion.headers,
               public_chat.CONVERSATION_HEADER: conversation or public_chat.tag(None),
               "Content-Type": "application/json", "Accept": "application/x-ndjson"}
    started = time.monotonic()
    try:
        response = requests.post(
            f"http://{companion.listen}{public_chat.ROUTE}{key}{public_video.PATH}",
            data=data, headers=headers, stream=True,
            timeout=(public_chat.CONNECT_S, FIRST_EVENT_S), proxies=NO_PROXY)
    except requests.Timeout as exc:
        raise PublicError("slow") from exc
    except requests.RequestException as exc:
        raise PublicError("unreachable") from exc
    with response:
        if response.status_code >= 400:
            code = public_chat._refusal(response)
            log.info("Réseau public : %s refuse la vidéo (%s)", reputation.short(key), code)
            raise PublicError(code)
        client._plain(response)
        first, done, why = threading.Event(), threading.Event(), []
        deadline = [started + FIRST_EVENT_S]
        threading.Thread(target=_watch, daemon=True, name="aiodos-public-video",
                         args=(response, stop, first, done, why, deadline, started)).start()
        try:
            return _read(key, response, params, stop, first, why, deadline, started)
        except PublicError:
            raise
        except PeerError as exc:                  # une ligne sans fin (client.MAX_FRAME)
            raise PublicError("too_long") from exc
        except client._CUT as exc:
            if stop is not None and stop.is_set():
                return None
            raise PublicError(public_chat._ended(why, first, deadline)) from exc
        finally:
            done.set()


def _read(key, response, params, stop, first, why: list, deadline: list, started: float):
    """Les événements de la réponse, jusqu'à « done » (cf. le début du module)."""
    header, received, parts = None, 0, bytearray()
    for line in client._lines(response):
        if stop is not None and stop.is_set():
            return None
        received += len(line) + 1
        if received > MAX_STREAM:
            log.info("Réseau public : %s, vidéo trop longue, coupée", reputation.short(key))
            raise PublicError("too_long")
        if not line.strip():
            continue
        event = public_chat._event(line)
        kind = event["type"]
        if kind == "meta":
            if event.get("model") != params["model"]:
                raise PublicError("unreadable")          # un autre modèle que celui demandé
        elif kind == "wait":
            if header is None:
                _alive(deadline, started)
        elif kind == "video":
            if header is not None:
                raise PublicError("unreadable")
            header = _header(event, params)
            first.set()                                  # plus d'échéance : TOTAL_S seul
        elif kind == "part":
            if header is None:
                raise PublicError("unreadable")
            parts += public_image_ask._part(event)
            if len(parts) > header["bytes"]:
                raise PublicError("unreadable")
        elif kind == "done":
            if header is None or len(parts) != header["bytes"]:
                raise PublicError("unreadable")
            clean = check(bytes(parts), params)
            return {"webm": clean, "frames": params["frames"],
                    "fps": public_video.fps_of(catalog.model(params["model"])),
                    "seconds": header["seconds"]}
        elif kind == "error":
            code = event.get("error")
            log.info("Réseau public : %s s'arrête (%s)", reputation.short(key), code)
            raise PublicError(code if isinstance(code, str) else "refused")
    if stop is not None and stop.is_set():
        return None
    raise PublicError(public_chat._ended(why, first, deadline))     # flux fini sans « done »
