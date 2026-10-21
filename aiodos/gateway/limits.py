"""Limites par application (docs/api-applications.md § 2 et § 3 ; conception § 3.11).

- Débit : 120 requêtes par minute et par appli (seau à jetons) ; au-delà, 429 rate_limited
  avec Retry-After.
- Simultanéité : 4 conversations ou embeddings en cours par appli ; au-delà, 429 busy,
  Retry-After: 2. Une appli boguée ne bloque ni AIODOS ni les autres.
- Clés invalides : au-delà de 30 par minute, chaque réponse 401 attend 1 s (on ne devine pas
  une clé de 256 bits, mais on n'encourage pas un programme qui boucle).
La clé générale des autres outils garde des limites larges : un débit plus haut, et pas de
plafond de simultanéité (rien ne casse pour les outils existants).
"""
import threading
import time
from contextlib import contextmanager

from aiodos.gateway.errors import ApiError

PER_MINUTE = 120
STREAMS = 4
GENERAL_PER_MINUTE = 1200
INVALID_PER_MINUTE = 30
INVALID_DELAY = 1.0

_lock = threading.Lock()
_buckets = {}               # appli -> [jetons, instant monotonic]
_running = {}               # appli -> requêtes de calcul en cours
_invalid = []               # instants des dernières clés invalides


def _rate(app_id: str, general: bool) -> int:
    return GENERAL_PER_MINUTE if general else PER_MINUTE


def take(app_id: str, general: bool = False):
    """Une requête de plus pour cette appli, ou 429 rate_limited."""
    rate = _rate(app_id, general)
    now = time.monotonic()
    with _lock:
        tokens, at = _buckets.get(app_id, (float(rate), now))
        tokens = min(float(rate), tokens + (now - at) * rate / 60)
        if tokens < 1:
            wait = max(1, int((1 - tokens) * 60 / rate) + 1)
            _buckets[app_id] = [tokens, now]
            raise ApiError("rate_limited", retry_after=wait)
        _buckets[app_id] = [tokens - 1, now]


@contextmanager
def computing(app_id: str):
    """Une conversation ou un embedding en cours pour cette appli (429 busy au-delà)."""
    most = STREAMS
    with _lock:
        if _running.get(app_id, 0) >= most:
            raise ApiError("busy", retry_after=2)
        _running[app_id] = _running.get(app_id, 0) + 1
    try:
        yield
    finally:
        with _lock:
            left = _running.get(app_id, 1) - 1
            if left > 0:
                _running[app_id] = left
            else:
                _running.pop(app_id, None)


def invalid_delay() -> float:
    """Délai à attendre avant de répondre 401 : 0, sauf rafale de clés invalides."""
    now = time.monotonic()
    with _lock:
        _invalid[:] = [t for t in _invalid if now - t < 60]
        _invalid.append(now)
        return INVALID_DELAY if len(_invalid) > INVALID_PER_MINUTE else 0.0


def forget():
    """Tout oublier (essais)."""
    with _lock:
        _buckets.clear()
        _running.clear()
        _invalid.clear()
