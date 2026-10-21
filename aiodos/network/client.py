"""Appels sortants vers les autres nœuds : HTTP + enveloppe scellée.

Le chemin (réseau local, ou Internet par le compagnon aiodos-net) est choisi par
route.py ; l'enveloppe, elle, est la même sur les deux.

Ce qu'une autre machine renvoie est borné avant d'être lu (audit du 20/10/2026, failles 19,
23 et 25, relectures du 21/10/2026). requests lisait toute la réponse en mémoire, décompressée
s'il le fallait, avec un délai de lecture qui repart à chaque octet reçu : un ami répondait à
« update/manifest » ou « update/chunk » par des centaines de Mio, ou par une bombe gzip, et un
filet d'octets retenait le fil de l'appel indéfiniment.

- Taille : la réponse est lue en flux, telle qu'elle arrive, jamais décompressée (une réponse
  compressée est refusée : le serveur entre machines n'en envoie pas), et la lecture s'arrête
  au plafond de l'action (REPLY_LIMITS, DEFAULT_REPLY pour les autres) : PeerError lisible.
- Durée : chaque appel a une échéance (_deadline). L'échange se fait dans un fil à part : à
  l'échéance, l'appel rend la main (PeerError) et la connexion est coupée. Si ce fil reste
  pris (un en-tête qui arrive goutte à goutte : requests ne laisse pas couper la connexion
  avant d'avoir l'en-tête), rien de plus n'est demandé à cette machine tant qu'il n'a pas fini
  (_within) : elle ne peut pas retenir les fils de celle-ci un à un.
- Forme : une grosse réponse scellée n'est reconnue qu'à sa forme stricte {"box": "…"}, sans
  json.loads : quelques Mio de [{}, {}, …] deviendraient des centaines de Mio d'objets (comme
  pour les requêtes, p2p_app._parse). Son contenu, une fois ouvert, n'a ni NaN ni infini
  (envelope.parse_json).

Une réponse en flux (une conversation, stream) n'a pas d'échéance une fois commencée : chacun
de ses morceaux est borné (MAX_FRAME), et son en-tête arrive avant l'échéance. Pas plus de
STREAMS_PER_PEER à la fois vers une même machine (relecture finale du 21/10/2026) : une
rafale de conversations (un outil branché sur la passerelle) attend son tour ici, au lieu
d'occuper chez l'autre machine toutes les places de son compagnon et de son serveur (les
suivantes y étaient refusées, et ses nouvelles, ses baux de travail, ne passaient plus).
"""
import http.client
import re
import threading
import time
import unicodedata

import requests
import urllib3

from aiodos.network import envelope, route
from aiodos.network.crypto import SealError

TIMEOUT = 10
DEADLINE_FACTOR = 4      # réponse entière en 4 délais de lecture (plus les connexions)
# Débit d'envoi minimal compté dans l'échéance : le plus bas qu'acceptent les machines qui
# reçoivent (connections.MIN_RATE sur le réseau local ; compagnon : relay_body::PACE,
# 16 Kio/s) ; plus lent, elles coupent elles-mêmes la requête
SEND_RATE = 8 * 1024
GRACE = 0.5              # s laissées au fil d'un appel abandonné pour finir, connexion coupée
PIECE = 64 * 1024        # lu à la fois


def _sealed(size: int) -> int:
    """Taille d'une réponse scellée dont le contenu JSON fait `size` octets : chiffré, puis
    en base64 (4/3), avec une marge pour l'enveloppe."""
    return size * 4 // 3 + 64 * 1024


def _file_piece(size: int) -> int:
    """Réponse scellée qui porte un morceau de fichier de `size` octets, en base64 dans son
    contenu ({"data": …}) : deux fois base64, 16/9."""
    return _sealed(size * 4 // 3 + 1024)


MANIFEST_TEXT = 256 * 1024     # caractères d'un texte signé, au plus (updates/manifest.MAX_TEXT)
# Le texte signé, en JSON : 4 octets par caractère au pire (UTF-8, ou « \" »), avec sa clé, sa
# signature et ses approbations (updates/manifest.MAX_APPROVALS)
MANIFEST_CONTENT = 4 * MANIFEST_TEXT + 16 * 1024

# Plafond de la réponse, par action. Chacun tient la plus grosse réponse honnête que fixent
# les bornes de la machine qui répond (tests/test_securite_client.py le vérifie avec leurs
# valeurs mêmes) : une réponse plus grosse ne vient pas d'une version honnête d'AIODOS.
REPLY_LIMITS = {
    "update/chunk": _file_piece(1024 ** 2),            # updates/p2p.CHUNK
    "update/manifest": _sealed(MANIFEST_CONTENT),
    "update/pending": _sealed(4 * MANIFEST_CONTENT),   # quelques versions (store.KEEP_PACKAGES)
    "images/file": _file_piece(8 * 1024 ** 2),         # imaging/share.CHUNK, réseau local
    "work/blob": _file_piece(2 * 1024 ** 2),           # network/work.BLOB_CHUNK
    "work/lease": 4 * 1024 ** 2,                       # network/worker.LEASE_REPLY
    "profile": 4 * 1024 ** 2,                          # des centaines de modèles (profile.py)
}
DEFAULT_REPLY = 1024 ** 2      # les autres : quelques Kio (ping, appairage, recherche, prêt…)
PUBLIC_REPLY = 64 * 1024       # « hello », début d'appairage
ERROR_REPLY = 64 * 1024        # un refus (« detail ») : au-delà, seul son code HTTP est rendu
MAX_FRAME = 1024 ** 2          # un morceau d'une réponse en flux
SMALL_JSON = 64 * 1024         # au-delà, une réponse n'est lue qu'à sa forme stricte
DETAIL = 500                   # caractères gardés d'un refus venu d'une autre machine
# Réponses en flux à la fois vers une même machine : nettement sous les places que lui
# réserve son compagnon (compagnon/src/pool.rs, MAX_STREAMS_PER_PEER : 32) et sous les
# connexions qu'accepte son serveur d'une adresse connue (connections.PER_KNOWN : 128)
STREAMS_PER_PEER = 16
# s : attente d'une place, au plus, comme celle d'une conversation dans la file d'un modèle
# prêté chez l'autre machine (lend_queue.MAX_WAIT_S)
STREAM_WAIT = 540
TURN_POLL = 0.25               # s : l'arrêt (stop) est regardé pendant l'attente

TOO_SLOW = "L'autre machine répond trop lentement : demande abandonnée."
STILL_BUSY = ("L'autre machine n'a toujours pas fini de répondre à une demande précédente, "
              "abandonnée : rien de plus ne lui est demandé pour l'instant.")
UNREADABLE = "Réponse illisible de l'autre machine."

# La réponse scellée d'une machine appairée (envelope.wrap_reply, sérialisée par FastAPI)
_BOX = re.compile(rb'\s*\{\s*"box"\s*:\s*"([A-Za-z0-9+/]*={0,2})"\s*\}\s*')
# Ce qui coupe une lecture en cours (connexion fermée, délai de lecture dépassé, réponse tronquée)
_CUT = (requests.RequestException, urllib3.exceptions.HTTPError, http.client.HTTPException,
        OSError)

_lock = threading.Lock()
_lingering = {}                # destination -> [fils d'appels abandonnés qui n'ont pas fini]
_turns = {}                    # machine -> places de ses réponses en flux (STREAMS_PER_PEER)


class PeerError(Exception):
    """Machine injoignable, ou qui refuse la demande (avec un message lisible)."""


def error_detail(response) -> str:
    """Message lisible d'une réponse en erreur (« detail » de FastAPI ou du compagnon).
    Réponse déjà en mémoire seulement : celle d'une autre machine se lit bornée (_refusal)."""
    try:
        return response.json().get("detail") or response.text
    except (ValueError, AttributeError):
        return f"HTTP {response.status_code}"


def limit_for(action: str) -> int:
    """Plafond de la réponse à cette action, en octets."""
    return REPLY_LIMITS.get(action, DEFAULT_REPLY)


# --------------------------------------------------------------- lecture bornée
def _size(size: int) -> str:
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} Mio".replace(".", ",")
    return f"{max(1, size // 1024)} Kio"


def _readable(text: str, size: int) -> str:
    """Texte venu d'une autre machine, à montrer : sur une ligne, sans caractère de contrôle
    ni de mise en forme invisible (U+202E…), `size` caractères au plus."""
    shown = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in text[:4 * size])
    return " ".join(shown.split())[:size]


def _too_big(limit: int, action: str | None, announced: int | None = None) -> str:
    what = f" à « {action} »" if action else ""
    seen = f"{_size(announced)}, " if announced else ""
    return f"Réponse trop volumineuse{what} ({seen}{_size(limit)} au plus) : refusée."


def _plain(response):
    """Une réponse compressée est refusée sans être décompressée : le serveur entre machines
    n'en envoie jamais, et quelques Kio de gzip peuvent en devenir des Gio."""
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    if encoding not in ("", "identity"):
        raise PeerError(f"Réponse compressée ({_readable(encoding, 30)}) inattendue d'une "
                        "autre machine : refusée.")


def _reader(response):
    """Lecture brute d'un morceau : ce qui est déjà arrivé (read1), sans attendre d'en avoir
    PIECE, ni rien décompresser ; read si urllib3 n'a pas read1 (avant 2.3)."""
    raw = response.raw
    read1 = getattr(raw, "read1", None)
    if read1 is not None:
        return lambda: read1(PIECE, decode_content=False)
    return lambda: raw.read(PIECE, decode_content=False)


def _read(response, limit: int, deadline: float, action: str | None = None) -> bytes:
    """Le corps de la réponse tel qu'il arrive, `limit` octets au plus ; PeerError au-delà,
    après l'échéance, ou si la connexion se coupe."""
    _plain(response)
    declared = (response.headers.get("Content-Length") or "").strip()
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise PeerError(_too_big(limit, action, int(declared)))
    read = _reader(response)
    data = bytearray()
    try:
        while piece := read():
            data += piece
            if len(data) > limit:
                raise PeerError(_too_big(limit, action))
            if time.monotonic() > deadline:
                raise PeerError(TOO_SLOW)
    except _CUT as exc:
        raise PeerError(f"Connexion coupée ({exc.__class__.__name__}).") from exc
    return bytes(data)


def _detail(data: bytes, status: int) -> str:
    try:
        detail = envelope.parse_json(data).get("detail")
    except (ValueError, AttributeError, RecursionError):
        detail = None
    text = detail if isinstance(detail, str) else data.decode("utf-8", "replace")
    return _readable(text, DETAIL) or f"HTTP {status}"


def _refusal(response, deadline: float) -> PeerError:
    """Le refus d'une autre machine (« detail » de FastAPI ou du compagnon), lu borné : trop
    long, lent ou compressé, il n'en reste que son code HTTP."""
    try:
        data = _read(response, ERROR_REPLY, deadline)
    except PeerError:
        return PeerError(f"Refus de l'autre machine (HTTP {response.status_code}).")
    return PeerError(_detail(data, response.status_code))


def _loaded(response):
    """Réponse sans flux à lire (pas de `raw`) : son contenu est déjà en mémoire, plus rien
    n'en arrive du réseau. C'est celle du faux réseau des essais qui remplacent requests.post."""
    if response.status_code >= 400:
        raise PeerError(error_detail(response))
    try:
        return response.json()
    except ValueError as exc:
        raise PeerError(UNREADABLE) from exc


def _small_json(data: bytes):
    """Un objet JSON qui n'a pas la forme stricte attendue : analysé seulement s'il est petit."""
    if len(data) > SMALL_JSON:
        raise PeerError(UNREADABLE)
    try:
        return envelope.parse_json(data)
    except (ValueError, RecursionError) as exc:
        raise PeerError(UNREADABLE) from exc


def _box(response, limit: int, deadline: float, action: str) -> dict:
    """La réponse scellée d'une machine appairée, {"box": …}, lue bornée."""
    if getattr(response, "raw", None) is None:
        reply = _loaded(response)
    else:
        if response.status_code >= 400:
            raise _refusal(response, deadline)
        data = _read(response, limit, deadline, action)
        found = _BOX.fullmatch(data)
        if found is not None:
            return {"box": found.group(1).decode("ascii")}
        reply = _small_json(data)
    if not isinstance(reply, dict) or not isinstance(reply.get("box"), str):
        raise PeerError(UNREADABLE)
    return reply


def _public(response, deadline: float) -> dict:
    """Réponse publique (« hello », début d'appairage) : un objet JSON de quelques Kio."""
    if getattr(response, "raw", None) is None:
        reply = _loaded(response)
    else:
        if response.status_code >= 400:
            raise _refusal(response, deadline)
        reply = _small_json(_read(response, PUBLIC_REPLY, deadline))
    if not isinstance(reply, dict):
        raise PeerError(UNREADABLE)
    return reply


def _streaming(response, deadline: float):
    """Réponse en flux, prête à lire : un refus est lu (borné) et rendu lisible, une
    compression refusée."""
    if response.status_code >= 400:
        raise _refusal(response, deadline)
    _plain(response)
    return response


def _lines(response):
    """Les lignes d'une réponse en flux, au fil de l'eau : MAX_FRAME octets au plus chacune
    (une ligne sans fin ne remplit pas la mémoire), rien de décompressé."""
    read = _reader(response)
    pending = bytearray()
    while piece := read():
        pending += piece
        start = 0
        while (end := pending.find(b"\n", start)) >= 0:
            yield bytes(pending[start:end]).rstrip(b"\r")
            start = end + 1
        del pending[:start]
        if len(pending) > MAX_FRAME:
            raise PeerError(f"Morceau de réponse trop volumineux ({_size(MAX_FRAME)} au plus) : "
                            "réponse interrompue.")
    if pending:
        yield bytes(pending)


def _close(response):
    close = getattr(response, "close", None)
    if close is not None:
        try:
            close()
        except OSError:
            pass


def _cut(response):
    """Coupe la connexion d'une réponse qu'un autre fil lit encore (urllib3 2.3 et plus) : sa
    lecture s'arrête aussitôt sous Linux et Android ; sous Windows, au prochain octet reçu ou
    au délai de lecture."""
    shutdown = getattr(getattr(response, "raw", None), "shutdown", None)
    if shutdown is None:
        return
    try:
        shutdown()
    except (OSError, ValueError, RuntimeError):
        pass                              # déjà fermée


# --------------------------------------------------------------------- échéance
class _Watch:
    """Ce que l'échéance d'un appel peut couper : sa réponse, dès que son en-tête est là."""

    def __init__(self):
        self._lock = threading.Lock()
        self._response = None
        self.expired = False

    def holds(self, response):
        """La réponse qu'a reçue le fil de l'appel ; échéance déjà passée : fermée aussitôt."""
        with self._lock:
            if not self.expired:
                self._response = response
                return response
        _close(response)
        raise PeerError(TOO_SLOW)

    def expire(self):
        with self._lock:
            self.expired = True
            response = self._response
        if response is not None:
            _cut(response)


def _within(destination, deadline: float, work):
    """work(watch), dans un fil à part, rendu au plus tard à l'échéance : sinon PeerError, et
    la connexion de la réponse commencée est coupée (watch). Un fil qui ne finit pas pour
    autant (en-tête qui arrive goutte à goutte) est retenu pour `destination` : tant qu'un de
    ces fils n'a pas fini, elle n'est plus appelée. Une machine ne retient donc, au plus, que
    les appels qui lui étaient déjà partis, et non les fils de celle-ci un à un ; tous sont
    retenus, pour qu'elle ne puisse pas en libérer un et garder les autres."""
    with _lock:
        for other, threads in list(_lingering.items()):
            alive = [thread for thread in threads if thread.is_alive()]
            if alive:
                _lingering[other] = alive
            else:
                del _lingering[other]
        if destination in _lingering:
            raise PeerError(STILL_BUSY)
    watch = _Watch()
    box = {}
    finished = threading.Event()

    def run():
        try:
            box["value"] = work(watch)
        except BaseException as exc:      # rendue à l'appelant telle quelle
            box["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True, name="aiodos-appel")
    thread.start()
    if not finished.wait(max(0.0, deadline - time.monotonic())):
        watch.expire()
        if not finished.wait(GRACE):
            with _lock:
                _lingering.setdefault(destination, []).append(thread)
        raise PeerError(TOO_SLOW)
    if "error" in box:
        raise box.pop("error")
    return box["value"]


# ---------------------------------------------------------------------- chemins
def _not_sent(exc) -> bool:
    """L'échec est-il survenu avant l'envoi (connexion impossible) ? Alors un autre chemin
    peut être essayé sans risque."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        reason = exc.args[0] if exc.args else None
        reason = getattr(reason, "reason", reason)
        return isinstance(reason, (urllib3.exceptions.NewConnectionError,
                                   urllib3.exceptions.ConnectTimeoutError))
    return False


def _plan(peer: dict, timeout, pairing: bool) -> list:
    """Chemins à essayer dans l'ordre (route.py) : [(chemin, (connexion, lecture), un autre
    chemin attend derrière)]. `timeout` : délai de lecture, ou (connexion, lecture)."""
    paths = route.paths_for(peer, pairing=pairing)
    if not paths:
        raise PeerError("Aucun chemin vers cette machine (ni réseau local, ni Internet).")
    read = timeout[1] if isinstance(timeout, tuple) else timeout
    plan = []
    for i, way in enumerate(paths):
        fallback = i + 1 < len(paths)
        limits = route.timeouts(way, read, fallback)
        if isinstance(timeout, tuple) and way.kind == "lan" and not fallback:
            limits = timeout
        plan.append((way, limits, fallback))
    return plan


def _sent_size(payload) -> int:
    """Taille, à peu de chose près, de la requête qui porte `payload` : son contenu scellé
    (« box », en base64) en fait l'essentiel ; sans refaire tout le JSON (un résultat de
    calcul peut peser des centaines de Mio)."""
    box = payload.get("box") if isinstance(payload, dict) else None
    return (len(box) if isinstance(box, str) else 0) + 4096


def _deadline(plan: list, sent: int = 0) -> float:
    """Échéance d'un appel : la connexion de chaque chemin essayé, le temps d'envoyer
    `sent` octets au débit minimal SEND_RATE, puis DEADLINE_FACTOR délais de lecture du plus
    long. L'en-tête de la réponse doit déjà venir dans un délai de lecture, le reste laisse
    le temps de recevoir le plus gros morceau (images/file, update/chunk). Relecture finale
    du 21/10/2026 : sans le temps d'envoi, un gros résultat honnête sur un lien lent (un
    téléphone mal couvert) était déclaré « trop lent » en plein envoi, et cette machine
    restait « occupée » pour tous ses appels jusqu'à la fin de l'envoi abandonné."""
    connect = sum(float(limits[0] or 0) for _, limits, _ in plan)
    read = max(float(limits[1] or TIMEOUT) for _, limits, _ in plan)
    return time.monotonic() + connect + sent / SEND_RATE + DEADLINE_FACTOR * read


def _attempt(peer: dict, plan: list, path: str, payload: dict, stream: bool):
    """POST par le premier chemin qui répond ; un autre n'est essayé qu'après un échec avant
    l'envoi."""
    for way, limits, fallback in plan:
        try:
            response = requests.post(way.base + path, json=payload, headers=way.headers or None,
                                     proxies=way.proxies, timeout=limits, stream=stream)
        except requests.RequestException as exc:
            sent = not _not_sent(exc)
            if way.kind == "lan" and not sent:
                route.lan_failed(peer.get("node_id"))
            if fallback and not sent:
                continue
            raise PeerError(f"Machine injoignable par {way.label} "
                            f"({exc.__class__.__name__}).") from exc
        if way.kind == "lan":
            route.lan_ok(peer.get("node_id"))
        return response
    raise PeerError("Machine injoignable.")


def _send(peer: dict, path: str, payload: dict, timeout, stream=True, pairing=False,
          then=None):
    """POST vers une machine appairée par le premier chemin qui répond (cf. route.py), dans
    l'échéance de l'appel (_within). Rend la réponse dès son en-tête, ou, avec
    then(réponse, échéance), ce que then en tire dans la même échéance (la réponse est alors
    fermée, sauf si then la rend). `timeout` : délai de lecture, ou (connexion, lecture)."""
    plan = _plan(peer, timeout, pairing)
    deadline = _deadline(plan, _sent_size(payload))

    def run(watch):
        response = watch.holds(_attempt(peer, plan, path, payload, stream))
        if then is None:
            return response
        try:
            value = then(response, deadline)
        except BaseException:
            _close(response)
            raise
        if value is not response:
            _close(response)
        return value

    return _within(peer.get("node_id"), deadline, run)


def _post(url: str, payload: dict, timeout) -> dict:
    """POST public (« hello », début d'appairage) vers une adresse : réponse bornée et rendue
    avant l'échéance, comme celle d'une machine appairée."""
    connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
    deadline = time.monotonic() + connect + DEADLINE_FACTOR * read

    def run(watch):
        try:
            response = requests.post(url, json=payload, timeout=timeout, stream=True)
        except requests.RequestException as exc:
            raise PeerError(f"Machine injoignable ({exc.__class__.__name__}).") from exc
        response = watch.holds(response)
        try:
            return _public(response, deadline)
        finally:
            _close(response)

    return _within(url.split("/p2p/", 1)[0], deadline, run)


# ------------------------------------------------------------------------ appels
def hello(address, port, timeout=4) -> dict:
    """Présentation publique d'un nœud (sans secret)."""
    info = _post(f"http://{address}:{port}/p2p/v1/hello", {}, timeout)
    if info.get("protocol") != envelope.PROTOCOL:
        raise PeerError("Version d'AIODOS incompatible sur cette machine.")
    return info


def pair_start(address, port, payload, timeout=TIMEOUT) -> dict:
    return _post(f"http://{address}:{port}/p2p/v1/pair/start", payload, timeout)


def pair_start_via(peer: dict, payload: dict, timeout=TIMEOUT) -> dict:
    """Début d'un appairage par le chemin de `peer` : le compagnon, sur invitation
    (cf. invitations.py, Pairing.start_remote)."""
    return _send(peer, "/p2p/v1/pair/start", payload, timeout, pairing=True, then=_public)


def _key(peer: dict) -> bytes:
    """La clé de la paire ; illisible ici (node/vault.py) : il faut appairer de nouveau."""
    if not peer.get("key"):
        raise PeerError(f"Clé de « {peer.get('name') or peer['node_id']} » illisible sur cette "
                        "machine : appaire-la de nouveau.")
    return peer["key"]


def call(my_id, peer: dict, action: str, payload: dict, key: bytes = None,
         timeout=TIMEOUT, limit: int | None = None) -> dict:
    """Requête scellée vers une machine appairée (ou en cours d'appairage via key : alors
    par le réseau local seulement). Réponse de `limit` octets au plus (limit_for(action) par
    défaut), reçue avant l'échéance de l'appel ; PeerError sinon, comme pour un refus."""
    pairing = key is not None
    key = key or _key(peer)
    env = envelope.wrap(key, action, my_id, peer["node_id"], payload)
    limit = limit or limit_for(action)
    reply = _send(peer, f"/p2p/v1/{action}", env, timeout, pairing=pairing,
                  then=lambda response, deadline: _box(response, limit, deadline, action))
    try:
        return envelope.unwrap_reply(key, action, peer["node_id"], my_id, env["ts"], reply)
    except SealError as exc:
        raise PeerError(f"Réponse refusée : {exc}") from exc


def _turn(node_id, stop):
    """Une place pour une réponse en flux vers cette machine (STREAMS_PER_PEER à la fois),
    attendue au plus STREAM_WAIT ; None si l'arrêt (stop) est demandé entre-temps. À rendre
    (release) une fois la réponse finie."""
    with _lock:
        places = _turns.setdefault(node_id, threading.BoundedSemaphore(STREAMS_PER_PEER))
    deadline = time.monotonic() + STREAM_WAIT
    while not places.acquire(timeout=TURN_POLL):
        if stop is not None and stop.is_set():
            return None
        if time.monotonic() >= deadline:
            raise PeerError(f"Déjà {STREAMS_PER_PEER} conversations en cours avec cette machine "
                            f"depuis {STREAM_WAIT // 60} min : réessaie plus tard.")
    return places


def stream(my_id, peer: dict, action: str, payload: dict, stop=None, connect_timeout=5,
           read_timeout=180):
    """Requête scellée dont la réponse arrive en flux (une conversation…) : génère
    les morceaux un à un. stop (threading.Event) coupe la connexion, ce qui arrête
    le calcul chez l'autre machine. Flux tronqué ou altéré, morceau démesuré : PeerError.
    Elle attend d'abord sa place (_turn) ; scellée ensuite seulement : l'autre machine
    refuse un message trop ancien (envelope.MAX_SKEW)."""
    key = _key(peer)
    places = _turn(peer["node_id"], stop)
    if places is None:
        return                    # arrêtée pendant qu'elle attendait sa place
    try:
        env = envelope.wrap(key, action, my_id, peer["node_id"], payload)
        response = _send(peer, f"/p2p/v1/stream/{action}", env,
                         (connect_timeout, read_timeout), then=_streaming)
        with response:
            seq = 0
            try:
                for line in _lines(response):
                    if stop is not None and stop.is_set():
                        return
                    if not line:
                        continue
                    frame = envelope.unwrap_frame(key, action, peer["node_id"], my_id,
                                                  env["ts"], seq, line.decode("ascii", "replace"))
                    seq += 1
                    if not isinstance(frame, dict):
                        raise PeerError("Morceau de réponse illisible.")
                    if frame.get("end"):
                        return
                    if frame.get("error"):
                        raise PeerError(_readable(str(frame["error"]), DETAIL))
                    yield frame
            except _CUT as exc:
                raise PeerError(f"Connexion coupée ({exc.__class__.__name__}).") from exc
            except SealError as exc:
                raise PeerError(f"Réponse altérée : {exc}") from exc
        raise PeerError("Réponse interrompue avant la fin.")
    finally:
        places.release()
