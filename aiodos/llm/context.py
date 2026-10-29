"""Le contexte des modèles de conversation du moteur intégré (llm/engine.py).

Un réglage global (chat_context), avec des exceptions par modèle (chat_context_overrides).
0, par défaut : 16 384 jetons si la carte graphique a au moins 7,5 Go (l'assistant d'ARKLIGHT
PC et les découpages de PANDORA en ont besoin), sinon 8 192 ; 8 192 sur un téléphone.
Jamais réglé par une requête : une appli ne change pas le contexte des autres (num_ctx est
ignoré). Le contexte change le fichier de préréglages, et le routeur repart avec (son
empreinte change). --fit on place les couches selon ce qui reste de mémoire vidéo.
"""
import logging

from aiodos import settings
from aiodos.node import android

log = logging.getLogger("aiodos.llm")

LOW = 8192
HIGH = 16384
CHOICES = (4096, 8192, 16384, 32768, 65536)
MIN, MAX = 2048, 131072
HIGH_VRAM = 7.5e9


_seen = {"vram": 0}           # la mémoire vidéo lue la dernière fois qu'elle l'a été


def _vram() -> int:
    """La mémoire de la carte graphique. Une lecture manquée (nvidia-smi qui ne répond pas
    dans ses 5 s quand la carte calcule, node/identity.gpus) garde la dernière connue : sinon
    le contexte passait de 16 384 à 8 192 jetons, les préréglages changeaient, et le routeur
    repartait en coupant la demande en cours (PC de l'auteur, 29/10/2026 : une dizaine de
    départs pendant une indexation d'images, chacun rechargeant le modèle de vision)."""
    try:
        from aiodos.services import model_install
        found = int(model_install.device().get("vram") or 0)
    except Exception as exc:
        log.debug("Mémoire vidéo inconnue : %s", exc)
        found = 0
    if found > 0:
        _seen["vram"] = found
    return found or _seen["vram"]


def auto() -> int:
    """Le contexte choisi d'après la machine."""
    if android.is_android():
        return LOW
    return HIGH if _vram() >= HIGH_VRAM else LOW


def _clamp(value) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(MIN, min(MAX, value))


def chat_context(model: str | None = None) -> int:
    """Le contexte donné à ce modèle de conversation (ou le réglage global)."""
    overrides = settings.get("chat_context_overrides")
    if model and isinstance(overrides, dict):
        chosen = _clamp(overrides.get(model))
        if chosen:
            return chosen
    return _clamp(settings.get("chat_context")) or auto()
