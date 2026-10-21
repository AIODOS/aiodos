"""Enveloppe des messages entre nœuds appairés : JSON scellé, horodaté, anti-rejeu.

Requête :  {"from", "to", "ts", "box"}   (box = JSON scellé avec la clé de la paire)
Réponse :  {"box"}                       (liée à l'horodatage de la requête)
Flux    :  une ligne par morceau, scellé et numéroté (lié à la requête et à son rang :
           un morceau retiré, déplacé ou rejoué est détecté) ; le dernier dit « fin ».

Le contenu ouvert est du JSON strict : ni NaN ni infini (audit du 20/10/2026, faille 16).
json.loads les acceptait, et une seule de ces valeurs, gardée puis servie à l'interface,
faisait tomber toute la réponse (Starlette sérialise avec allow_nan=False). Un message qui en
contient est refusé (SealError) ; pour que cela n'arrive jamais entre machines honnêtes, une
valeur non finie (un capteur illisible…) part ici en null.
"""
import base64
import binascii
import json
import math
import threading
import time

from aiodos.i18n import t
from aiodos.network import crypto
from aiodos.network.crypto import SealError

PROTOCOL = "aiodos/1"
MAX_SKEW = 120          # écart d'horloge toléré entre machines (secondes)


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class NotFinite(ValueError):
    """Nombre non fini dans du JSON reçu : NaN, Infinity, ou 1e999 qui en deviendrait un."""


def _constant(name):
    raise NotFinite(name)


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise NotFinite(text[:24])
    return value


def parse_json(data):
    """JSON reçu d'une autre machine, en refusant tout nombre non fini (NotFinite, une
    ValueError) : NaN et Infinity, que json.loads accepte, et les nombres qui débordent
    (« 1e999 » devient l'infini sans passer par eux)."""
    return json.loads(data, parse_constant=_constant, parse_float=_float)


def _opened(data: bytes):
    """Le contenu d'un message ouvert, ou SealError s'il n'est pas du JSON strict."""
    try:
        return parse_json(data)
    except NotFinite as exc:
        raise SealError(t("p2p.not_finite", value=exc)) from exc
    except (ValueError, RecursionError) as exc:
        raise SealError(t("p2p.content_unreadable")) from exc


def _finite(value):
    """`value` où chaque nombre non fini devient None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _dumps(payload) -> bytes:
    """Le contenu d'un message à sceller, sans nombre non fini, que l'autre machine refuserait
    (_opened) : il y devient null. La recherche ne coûte rien tant qu'il n'y en a pas."""
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except ValueError:
        text = json.dumps(_finite(payload), ensure_ascii=False, allow_nan=False)
    return text.encode()


def _aad(kind, action, sender, recipient, ts) -> bytes:
    return f"{PROTOCOL}|{kind}|{action}|{sender}|{recipient}|{ts:.6f}".encode()


def wrap(key: bytes, action: str, sender: str, recipient: str, payload: dict) -> dict:
    ts = time.time()
    box = crypto.seal(key, _dumps(payload), _aad("req", action, sender, recipient, ts))
    return {"from": sender, "to": recipient, "ts": ts, "box": b64e(box)}


def wrap_reply(key: bytes, action: str, sender: str, recipient: str, ts: float,
               payload: dict) -> dict:
    return {"box": b64e(crypto.seal(key, _dumps(payload),
                                    _aad("rep", action, sender, recipient, ts)))}


def unwrap_reply(key: bytes, action: str, sender: str, recipient: str, ts: float,
                 reply: dict) -> dict:
    try:
        box = b64d(reply["box"])
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SealError(t("p2p.reply_incomplete")) from exc
    return _opened(crypto.open_sealed(key, box, _aad("rep", action, sender, recipient, ts)))


def _frame_aad(action, sender, recipient, ts, seq) -> bytes:
    return f"{PROTOCOL}|frame|{action}|{sender}|{recipient}|{ts:.6f}|{seq}".encode()


def wrap_frame(key: bytes, action: str, sender: str, recipient: str, ts: float, seq: int,
               payload: dict) -> str:
    """Un morceau d'une réponse en flux (une ligne de texte)."""
    return b64e(crypto.seal(key, _dumps(payload), _frame_aad(action, sender, recipient, ts, seq)))


def unwrap_frame(key: bytes, action: str, sender: str, recipient: str, ts: float, seq: int,
                 line: str) -> dict:
    try:
        box = b64d(line.strip())
    except (binascii.Error, ValueError) as exc:
        raise SealError(t("p2p.frame_unreadable")) from exc
    return _opened(crypto.open_sealed(key, box, _frame_aad(action, sender, recipient, ts, seq)))


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
                raise SealError(t("p2p.replayed"))
            self._seen[(sender, nonce)] = now


def unwrap(key: bytes, action: str, recipient: str, envelope: dict,
           guard: ReplayGuard) -> dict:
    try:
        ts = float(envelope["ts"])
        sender = envelope["from"]
        box = b64d(envelope["box"])
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SealError(t("p2p.envelope_incomplete")) from exc
    # Un horodatage NaN passerait le contrôle d'écart (toute comparaison avec NaN est fausse)
    if not math.isfinite(ts):
        raise SealError(t("p2p.envelope_incomplete"))
    if envelope.get("to") != recipient:
        raise SealError(t("p2p.wrong_recipient"))
    if abs(time.time() - ts) > MAX_SKEW:
        raise SealError(t("p2p.clock_skew"))
    data = crypto.open_sealed(key, box, _aad("req", action, sender, recipient, ts))
    guard.check(sender, box[:crypto.NONCE])
    return _opened(data)
