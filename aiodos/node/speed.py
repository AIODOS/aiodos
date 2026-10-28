"""Vitesse de cette machine en conversation, en jetons par seconde : la médiane de ses
dernières réponses (KEEP), locales ou prêtées, mesurée au passage par services/ollama.py
(le seul chemin vers les modèles de cette machine). Le profil public la dit (« tps »,
network/public_lending.profile_fields) : un demandeur du réseau public y lit si cette
machine répond assez vite (docs/conception/reseau-public-calcul.md § 3).

Seule la génération compte, du premier morceau au dernier : ni le chargement du modèle, ni
la lecture de la demande, qui dépendent de sa longueur. Une réponse trop courte (MIN_TOKENS)
ne dit rien. Tous modèles confondus : un repère, que le demandeur affine avec ses propres
mesures. En mémoire seulement : inconnue (None) jusqu'à la première réponse mesurée.
"""
import statistics
import threading
import time
from collections import deque

KEEP = 21                     # dernières réponses retenues
MIN_TOKENS = 16               # en dessous, la mesure ne dit rien
GENERATED = ("content", "thinking")

_lock = threading.Lock()
_speeds = deque(maxlen=KEEP)


def note(tokens: int, seconds: float):
    """Une réponse de `tokens` jetons générés en `seconds` secondes."""
    if tokens >= MIN_TOKENS and seconds > 0:
        with _lock:
            _speeds.append(tokens / seconds)


def median() -> float | None:
    """Jetons par seconde (une décimale), ou None si aucune réponse n'a été mesurée."""
    with _lock:
        values = list(_speeds)
    return round(statistics.median(values), 1) if values else None


def forget():
    """Tout oublier (essais)."""
    with _lock:
        _speeds.clear()


def measured(pairs):
    """Les couples (genre, texte) d'une réponse, tels quels ; sa vitesse notée à la fin,
    même arrêtée en route (bouton Stop, machine partie) si elle a assez duré. Le nombre
    exact de jetons (couple « stats », s'il est demandé) passe avant celui des morceaux."""
    first = last = None
    chunks, counted = 0, None
    try:
        for kind, value in pairs:
            if kind in GENERATED:
                last = time.monotonic()
                if first is None:
                    first = last
                chunks += 1
            elif kind == "stats" and isinstance(value, dict):
                counted = value.get("completion_tokens")
            yield kind, value
    finally:
        if first is not None and last > first:
            tokens = counted if isinstance(counted, int) and counted >= chunks else chunks
            note(tokens - 1, last - first)       # le premier morceau ouvre la mesure
