"""Appels sortants vers les autres nœuds : HTTP + enveloppe scellée.

Le chemin (réseau local, ou Internet par le compagnon aiodos-net) est choisi par
route.py ; l'enveloppe, elle, est la même sur les deux.
"""
import requests
import urllib3

from aiodos.network import envelope, route
from aiodos.network.crypto import SealError

TIMEOUT = 10


class PeerError(Exception):
    """Machine injoignable, ou qui refuse la demande (avec un message lisible)."""


def error_detail(response) -> str:
    """Message lisible d'une réponse en erreur (« detail » de FastAPI ou du compagnon)."""
    try:
        return response.json().get("detail") or response.text
    except (ValueError, AttributeError):
        return f"HTTP {response.status_code}"


def _json(response) -> dict:
    if response.status_code >= 400:
        raise PeerError(error_detail(response))
    return response.json()


def _post(url, payload, timeout):
    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise PeerError(f"Machine injoignable ({exc.__class__.__name__}).") from exc
    return _json(response)


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


def _send(peer: dict, path: str, payload: dict, timeout, stream=False, pairing=False):
    """POST vers une machine appairée par le premier chemin qui répond (cf. route.py).
    `timeout` : délai de lecture, ou (connexion, lecture)."""
    paths = route.paths_for(peer, pairing=pairing)
    if not paths:
        raise PeerError("Aucun chemin vers cette machine (ni réseau local, ni Internet).")
    read = timeout[1] if isinstance(timeout, tuple) else timeout
    for i, way in enumerate(paths):
        fallback = i + 1 < len(paths)
        limits = route.timeouts(way, read, fallback)
        if isinstance(timeout, tuple) and way.kind == "lan" and not fallback:
            limits = timeout
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
    return _json(_send(peer, "/p2p/v1/pair/start", payload, timeout, pairing=True))


def call(my_id, peer: dict, action: str, payload: dict, key: bytes = None,
         timeout=TIMEOUT) -> dict:
    """Requête scellée vers une machine appairée (ou en cours d'appairage via key : alors
    par le réseau local seulement)."""
    pairing = key is not None
    key = key or peer["key"]
    env = envelope.wrap(key, action, my_id, peer["node_id"], payload)
    response = _send(peer, f"/p2p/v1/{action}", env, timeout, pairing=pairing)
    try:
        reply = _json(response)
    except ValueError as exc:
        raise PeerError("Réponse illisible.") from exc
    return envelope.unwrap_reply(key, action, peer["node_id"], my_id, env["ts"], reply)


def stream(my_id, peer: dict, action: str, payload: dict, stop=None, connect_timeout=5,
           read_timeout=600):
    """Requête scellée dont la réponse arrive en flux (une conversation…) : génère
    les morceaux un à un. stop (threading.Event) coupe la connexion, ce qui arrête
    le calcul chez l'autre machine. Flux tronqué ou altéré : PeerError."""
    key = peer["key"]
    env = envelope.wrap(key, action, my_id, peer["node_id"], payload)
    response = _send(peer, f"/p2p/v1/stream/{action}", env,
                     (connect_timeout, read_timeout), stream=True)
    with response:
        if response.status_code >= 400:
            raise PeerError(error_detail(response))
        seq = 0
        try:
            for line in response.iter_lines():
                if stop is not None and stop.is_set():
                    return
                if not line:
                    continue
                frame = envelope.unwrap_frame(key, action, peer["node_id"], my_id, env["ts"],
                                              seq, line.decode("ascii", "replace"))
                seq += 1
                if frame.get("end"):
                    return
                if frame.get("error"):
                    raise PeerError(frame["error"])
                yield frame
        except requests.RequestException as exc:
            raise PeerError(f"Connexion coupée ({exc.__class__.__name__}).") from exc
        except SealError as exc:
            raise PeerError(f"Réponse altérée : {exc}") from exc
    raise PeerError("Réponse interrompue avant la fin.")
