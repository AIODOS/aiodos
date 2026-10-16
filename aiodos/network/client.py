"""Appels sortants vers les autres nœuds : HTTP + enveloppe scellée."""
import requests

from aiodos.network import envelope
from aiodos.network.crypto import SealError

TIMEOUT = 10


class PeerError(Exception):
    """Machine injoignable, ou qui refuse la demande (avec un message lisible)."""


def _detail(response) -> str:
    try:
        return response.json().get("detail") or response.text
    except ValueError:
        return f"HTTP {response.status_code}"


def _post(url, payload, timeout):
    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise PeerError(f"Machine injoignable ({exc.__class__.__name__}).") from exc
    if response.status_code >= 400:
        raise PeerError(_detail(response))
    return response.json()


def hello(address, port, timeout=4) -> dict:
    """Présentation publique d'un nœud (sans secret)."""
    info = _post(f"http://{address}:{port}/p2p/v1/hello", {}, timeout)
    if info.get("protocol") != envelope.PROTOCOL:
        raise PeerError("Version de Aiodos incompatible sur cette machine.")
    return info


def pair_start(address, port, payload, timeout=TIMEOUT) -> dict:
    return _post(f"http://{address}:{port}/p2p/v1/pair/start", payload, timeout)


def call(my_id, peer: dict, action: str, payload: dict, key: bytes = None,
         timeout=TIMEOUT) -> dict:
    """Requête scellée vers une machine appairée (ou en cours d'appairage via key)."""
    key = key or peer["key"]
    env = envelope.wrap(key, action, my_id, peer["node_id"], payload)
    reply = _post(f"http://{peer['address']}:{peer['port']}/p2p/v1/{action}", env, timeout)
    return envelope.unwrap_reply(key, action, peer["node_id"], my_id, env["ts"], reply)


def stream(my_id, peer: dict, action: str, payload: dict, stop=None, connect_timeout=5,
           read_timeout=600):
    """Requête scellée dont la réponse arrive en flux (une conversation…) : génère
    les morceaux un à un. stop (threading.Event) coupe la connexion, ce qui arrête
    le calcul chez l'autre machine. Flux tronqué ou altéré : PeerError."""
    key = peer["key"]
    env = envelope.wrap(key, action, my_id, peer["node_id"], payload)
    url = f"http://{peer['address']}:{peer['port']}/p2p/v1/stream/{action}"
    try:
        response = requests.post(url, json=env, stream=True,
                                 timeout=(connect_timeout, read_timeout))
    except requests.RequestException as exc:
        raise PeerError(f"Machine injoignable ({exc.__class__.__name__}).") from exc
    with response:
        if response.status_code >= 400:
            raise PeerError(_detail(response))
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
