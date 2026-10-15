"""Enveloppe des messages entre nœuds appairés : JSON scellé, horodaté, anti-rejeu.

Requête :  {"from", "to", "ts", "box"}   (box = JSON scellé avec la clé de la paire)
Réponse :  {"box"}                       (liée à l'horodatage de la requête)
"""
import base64
import json
import threading
import time

from coeur.network import crypto
from coeur.network.crypto import SealError

PROTOCOL = "coeur/1"
MAX_SKEW = 120          # écart d'horloge toléré entre machines (secondes)


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _aad(kind, action, sender, recipient, ts) -> bytes:
    return f"{PROTOCOL}|{kind}|{action}|{sender}|{recipient}|{ts:.6f}".encode()


def wrap(key: bytes, action: str, sender: str, recipient: str, payload: dict) -> dict:
    ts = time.time()
    data = json.dumps(payload, ensure_ascii=False).encode()
    box = crypto.seal(key, data, _aad("req", action, sender, recipient, ts))
    return {"from": sender, "to": recipient, "ts": ts, "box": b64e(box)}


def wrap_reply(key: bytes, action: str, sender: str, recipient: str, ts: float,
               payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode()
    return {"box": b64e(crypto.seal(key, data, _aad("rep", action, sender, recipient, ts)))}


def unwrap_reply(key: bytes, action: str, sender: str, recipient: str, ts: float,
                 reply: dict) -> dict:
    data = crypto.open_sealed(key, b64d(reply["box"]), _aad("rep", action, sender, recipient, ts))
    return json.loads(data)


class ReplayGuard:
    """Refuse un message déjà reçu (même émetteur, même nonce)."""

    def __init__(self):
        self._seen = {}
        self._lock = threading.Lock()

    def check(self, sender: str, nonce: bytes):
        now = time.time()
        with self._lock:
            for key, at in list(self._seen.items()):
                if now - at > 2 * MAX_SKEW:
                    del self._seen[key]
            if (sender, nonce) in self._seen:
                raise SealError("Message rejoué.")
            self._seen[(sender, nonce)] = now


def unwrap(key: bytes, action: str, recipient: str, envelope: dict,
           guard: ReplayGuard) -> dict:
    try:
        ts = float(envelope["ts"])
        sender = envelope["from"]
        box = b64d(envelope["box"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SealError("Enveloppe incomplète.") from exc
    if envelope.get("to") != recipient:
        raise SealError("Message destiné à un autre nœud.")
    if abs(time.time() - ts) > MAX_SKEW:
        raise SealError("Horloges trop décalées entre les deux machines.")
    data = crypto.open_sealed(key, box, _aad("req", action, sender, recipient, ts))
    guard.check(sender, box[:crypto.NONCE])
    return json.loads(data)
