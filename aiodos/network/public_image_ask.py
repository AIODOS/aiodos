"""Faire créer mes images par des inconnus : le côté demandeur des images du réseau public
(docs/conception/reseau-public-calcul.md § 11 ; décision de l'auteur du 29/10/2026). Le côté
prêteur : network/public_imaging.py ; l'accord et le choix du modèle : chat/
public_creations.py ; l'unité de la file qui s'en sert : jobs/kinds/public_images.py.

Les machines candidates (lenders) : celles du réseau public qui prêtent (placement._lending :
« lends », vues récemment, ni mises de côté ni écartées après un échec ou des refus répétés),
ont une place (placement._with_room : « free », moins ce que je viens de leur envoyer,
conversations et images comprises) et annoncent ce modèle d'images (public_imaging.MARKER dans
« models »). Celles qui ont déjà servi ici sans contrôle raté d'abord (placement._served_well),
puis la plus rapide qu'elles annoncent pour converser (« tps », seule idée de leur puissance,
qui ne sert qu'à les départager), la plus récemment vue ; PUBLIC_TRIES au plus, chacune notée
comme proposée (public_refusals.offered), pour qu'un refus qui contredit son profil compte.

L'envoi (image) : par le compagnon, comme une conversation (public_chat : POST
/net/v1/public/ask/<clé>/public/v1/image), avec une clé jetable par création
(X-Aiodos-Conversation : un identifiant tiré au hasard pour le lot, gardé en mémoire
seulement, public_chat.tag) : le prêteur ne sait pas qui demande, et ne peut pas relier deux
créations. Ce qui part (body) : le modèle, la description (enrichie ici, jamais chez un
inconnu : chat/enrich.py), la consigne négative, la taille, le nombre de pas, la graine ;
jamais d'image de départ, ni rien de la conversation. Une demande hors des bornes du prêteur
ne part pas : il la refuserait. Une description que la base du filtre juge interdite
(imaging/prompt_filter.py ; la conversation a déjà passé le filtre entier, modèle compris,
chat/creations.py) ne part pas non plus : PublicError « refused_content », rien d'envoyé.

Ce qui revient (le NDJSON de public_imaging.py) est borné et vérifié ici avant d'être gardé :
« meta » au modèle demandé ; l'en-tête de l'image (PNG, MAX_IMAGE octets au plus, la taille
demandée) ; ses morceaux (base64, jamais plus que les octets annoncés) ; « done » avec
exactement ces octets ; puis le PNG lui-même (check : signature, en-tête, taille, fin IEND ;
avec Pillow, décodé en entier, pas d'une seule couleur, et réécrit sans ses métadonnées).
Délais : FIRST_EVENT_S pour le premier événement ; chaque signe de vie (« wait ») repousse
l'échéance de ALIVE_GRACE_S, tant que l'image n'est pas annoncée ; TOTAL_S en tout depuis la
demande ; MAX_STREAM octets de flux en tout. Un type d'événement inconnu est ignoré.

Un échec du prêteur (FAULTS : lent, coupé, illisible, trop gros, moteur arrêté, image
invalide) l'écarte quelques minutes (reputation.bench), comme pour une conversation : il a pu
lire la description sans rien rendre. Un refus « avant calcul » qui contredit son profil est
compté (public_refusals). Le refus de son filtre des descriptions (« refused_content »,
public_refusals.INNOCENT) n'est ni l'un ni l'autre : il fait son travail. Une image reçue et
valide compte comme une réponse servie (reputation.note_served, sans vitesse). Pas de
contrôle au hasard des images dans cette version. Le journal ne dit que la clé abrégée et le
code d'un refus, jamais la description.
"""
import base64
import binascii
import io
import json
import logging
import math
import threading
import time

import requests

from aiodos.imaging import catalog, prompt_filter
from aiodos.network import (client, placement, public_chat, public_imaging, public_refusals,
                            reputation)
from aiodos.network.client import PeerError
from aiodos.network.public_chat import PublicError
from aiodos.network.route import NO_PROXY

log = logging.getLogger("aiodos.net")

MARKER = public_imaging.MARKER
PUBLIC_TRIES = placement.PUBLIC_TRIES
# Premier événement attendu au plus : la recherche et la connexion du compagnon, une clé
# jetable neuve (1 à 3 s), le prêteur qui admet la demande
FIRST_EVENT_S = 60
# Chaque signe de vie (le prêteur en envoie toutes les 5 s) repousse l'échéance d'autant :
# trois de perdus, il s'est tu
ALIVE_GRACE_S = 20
# Toute l'image, depuis la demande, au plus : le chargement du modèle (une minute sur un
# disque lent), le calcul (23 s sur une RTX 4070, plusieurs minutes sur une petite carte),
# l'envoi par un relais. Celle d'une conversation : public_chat._watch coupe à cette échéance
TOTAL_S = public_chat.TOTAL_S
MAX_IMAGE = public_imaging.MAX_IMAGE
# Le flux en entier, au plus : l'image en base64, les signes de vie, une marge
MAX_STREAM = MAX_IMAGE * 4 // 3 + 1024 ** 2
FAULTS = public_chat.FAULTS
IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"       # le dernier bloc d'un PNG


# ------------------------------------------------------------------ les machines
def offered_by(node) -> list:
    """Les modèles d'images (identifiants du catalogue) que ce nœud du réseau public prête,
    parmi ceux qu'on demande aux inconnus (public_imaging.lendable)."""
    found = []
    for name in node.get("models") or []:
        if isinstance(name, str) and name.startswith(MARKER):
            model = name[len(MARKER):]
            if public_imaging.lendable(model) and model not in found:
                found.append(model)
    return found


def _nodes() -> list:
    """Les nœuds du réseau public qui prêtent et ont une place (cf. le début du module)."""
    return placement._with_room(placement._lending())


def offered_models(offers=None) -> list:
    """Les modèles d'images qu'une machine du réseau public prête, dans l'ordre du catalogue,
    même sans place en ce moment : la création est acceptée et son unité attend son tour
    (jobs/kinds/public_images.in_turn). Sinon une machine occupée, ou dont la mémoire était
    juste sous le seuil, faisait dire « aucun modèle » au téléphone sans modèle d'images (S23
    de l'auteur, 29/10/2026 : le PC prêtait z-image-turbo, « free » à 0). Aucun sans
    compagnon prêt ni réseau public rejoint. offers : ce qu'un nœud prête (offered_by ;
    celui des vidéos : network/public_video_ask.py)."""
    offers = offers or offered_by
    seen = {model for node in placement._lending() for model in offers(node)}
    return [model for model in catalog.MODELS if model in seen]


def lent(model: str, offers=None) -> bool:
    """Une machine du réseau public prête-t-elle ce modèle, même sans place en ce moment
    (occupée par une autre de mes créations ou par un inconnu, ou dans la pause de sa
    puissance prêtée) ? L'unité attend alors son tour (jobs/kinds/public_images.create)."""
    offers = offers or offered_by
    return any(model in offers(node) for node in placement._lending())


def lenders(model: str, offer: bool = True, offers=None) -> list:
    """[(clé, modèle)] : les machines du réseau public à essayer pour une image de ce modèle,
    la mieux placée d'abord (cf. le début du module), PUBLIC_TRIES au plus. offer : notées
    comme proposées (public_refusals.offered) ; offers : comme pour offered_models."""
    offers = offers or offered_by
    found = []
    for node in _nodes():
        if model in offers(node):
            key = node["key"]
            found.append((not placement._served_well(key), -(node.get("tps") or 0),
                          int(node.get("seen_s") or 0), key))
    chosen = [(key, model) for *_order, key in sorted(found)[:PUBLIC_TRIES]]
    if offer:
        for key, name in chosen:
            public_refusals.offered(key, MARKER + name)
    return chosen


# ------------------------------------------------------------------- la demande
def body(params: dict) -> bytes:
    """Le corps envoyé au prêteur ({"v": 1, "model", "prompt", "negative", "width", "height",
    "steps", "seed"}) ; PublicError « too_big » s'il dépasse ses bornes (public_imaging),
    « bad_request » s'il est mal formé, « refused_content » si la base du filtre juge la
    description interdite (cf. le début du module)."""
    model = str(params.get("model") or "")
    if not public_imaging.lendable(model):
        raise PublicError("bad_request")
    spec = catalog.model(model)
    try:
        asked = {"v": 1, "model": model, "prompt": str(params["prompt"]).strip(),
                 "negative": str(params.get("negative") or "").strip() if spec["negative"] else "",
                 "width": int(params["width"]), "height": int(params["height"]),
                 "steps": int(params.get("steps") or spec["steps"]),
                 "seed": int(params["seed"]) % public_imaging.SEED_SPACE}
    except (KeyError, TypeError, ValueError):
        raise PublicError("bad_request") from None
    if not asked["prompt"]:
        raise PublicError("bad_request")
    if len(asked["prompt"]) > public_imaging.MAX_PROMPT \
            or len(asked["negative"]) > public_imaging.MAX_PROMPT \
            or not 1 <= asked["steps"] <= public_imaging.max_steps(spec) \
            or not public_imaging.fits(spec, asked["width"], asked["height"]):
        raise PublicError("too_big")
    if prompt_filter.base(asked["prompt"], asked["negative"]) is not None:
        raise PublicError(prompt_filter.REFUSED)
    data = json.dumps(asked, ensure_ascii=False).encode()
    if len(data) > public_chat.MAX_BODY:
        raise PublicError("too_big")
    return data


# --------------------------------------------------------------- l'image reçue
def check(data: bytes, width: int, height: int) -> bytes:
    """Le PNG reçu, vérifié (cf. le début du module) et, avec Pillow, réécrit sans ses
    métadonnées ; PublicError « unreadable » s'il n'est pas l'image demandée."""
    from aiodos.imaging import generate
    try:
        if len(data) > MAX_IMAGE or generate.png_size(data) != (width, height) \
                or not data.endswith(IEND):
            raise ValueError("png")                 # i18n: tel quel (jamais montré)
    except ValueError:
        raise PublicError("unreadable") from None
    try:
        from PIL import Image
    except ImportError:              # sans Pillow : signature, taille et fin vérifiées
        return data
    try:
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format != "PNG" or picture.size != (width, height):
                raise ValueError("png")             # i18n: tel quel (jamais montré)
            picture.load()                          # tronqué, ou abîmé : refusé ici
            clean = picture.convert("RGB")
        if all(low == high for low, high in clean.getextrema()):
            raise ValueError("unie")                # i18n: tel quel (jamais montré)
        out = io.BytesIO()
        clean.save(out, "PNG")
    except Exception:  # noqa: BLE001 — tout ce que Pillow refuse : l'image ne l'est pas
        raise PublicError("unreadable") from None
    return out.getvalue()


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _header(event: dict, params: dict) -> dict:
    """L'en-tête de l'image : PNG, MAX_IMAGE octets au plus (« too_long » au-delà), la taille
    demandée (« unreadable » sinon)."""
    size = event.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise PublicError("unreadable")
    if size > MAX_IMAGE:
        raise PublicError("too_long")
    if event.get("format") != "png" or event.get("width") != params["width"] \
            or event.get("height") != params["height"]:
        raise PublicError("unreadable")
    seconds = event.get("seconds")
    return {"bytes": size, "seconds": float(seconds) if _number(seconds) and seconds >= 0
            else None}


def _part(event: dict) -> bytes:
    data = event.get("data")
    if not isinstance(data, str):
        raise PublicError("unreadable")
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise PublicError("unreadable") from None


def _alive(deadline: list, started: float):
    """Un signe de vie du prêteur : l'échéance repoussée de ALIVE_GRACE_S, TOTAL_S au plus
    depuis la demande."""
    deadline[0] = max(deadline[0], min(started + TOTAL_S, time.monotonic() + ALIVE_GRACE_S))


# -------------------------------------------------------------------- l'envoi
def image(key, params: dict, stop=None, conversation=None) -> dict | None:
    """{"png": octets, "seconds": durée du calcul chez lui (ou None)} : l'image créée par la
    machine du réseau public `key` (cf. le début du module) ; None si l'arrêt (stop) est
    venu avant la fin. conversation : l'identifiant opaque de la création (public_chat.tag) ;
    sans lui, une clé jetable pour cette seule image. PublicError à tout refus ou échec ; un
    échec de FAULTS écarte le prêteur quelques minutes, un refus qui contredit son profil est
    compté ; le refus de son filtre (public_refusals.INNOCENT), ni l'un ni l'autre."""
    data = body(params)                         # nos bornes : ce qui part, elles l'acceptent
    model = params["model"]
    promised = public_refusals.promised(key, MARKER + model)    # proposée ? lu avant l'envoi
    try:
        made = _image(key, data, params, stop, conversation)
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


def _image(key, data: bytes, params: dict, stop, conversation) -> dict | None:
    """image, le corps `data` prêt."""
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
            f"http://{companion.listen}{public_chat.ROUTE}{key}{public_imaging.PATH}",
            data=data, headers=headers, stream=True,
            timeout=(public_chat.CONNECT_S, FIRST_EVENT_S), proxies=NO_PROXY)
    except requests.Timeout as exc:
        raise PublicError("slow") from exc
    except requests.RequestException as exc:
        raise PublicError("unreachable") from exc
    with response:
        if response.status_code >= 400:
            code = public_chat._refusal(response)
            log.info("Réseau public : %s refuse l'image (%s)", reputation.short(key), code)
            raise PublicError(code)
        client._plain(response)
        first, done, why = threading.Event(), threading.Event(), []
        deadline = [started + FIRST_EVENT_S]
        # public_chat._watch : coupée à l'arrêt, à l'échéance tant que l'image n'est pas
        # annoncée (first), et après public_chat.TOTAL_S (= TOTAL_S) secondes
        threading.Thread(target=public_chat._watch, daemon=True, name="aiodos-public-image",
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
            log.info("Réseau public : %s, image trop longue, coupée", reputation.short(key))
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
        elif kind == "image":
            if header is not None:
                raise PublicError("unreadable")
            header = _header(event, params)
            first.set()                                  # plus d'échéance : TOTAL_S seul
        elif kind == "part":
            if header is None:
                raise PublicError("unreadable")
            parts += _part(event)
            if len(parts) > header["bytes"]:
                raise PublicError("unreadable")
        elif kind == "done":
            if header is None or len(parts) != header["bytes"]:
                raise PublicError("unreadable")
            png = check(bytes(parts), params["width"], params["height"])
            return {"png": png, "seconds": header["seconds"]}
        elif kind == "error":
            code = event.get("error")
            log.info("Réseau public : %s s'arrête (%s)", reputation.short(key), code)
            raise PublicError(code if isinstance(code, str) else "refused")
    if stop is not None and stop.is_set():
        return None
    raise PublicError(public_chat._ended(why, first, deadline))     # flux fini sans « done »
