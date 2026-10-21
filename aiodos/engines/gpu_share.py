"""Partager la carte graphique avec le moteur d'une appli (docs/conception/partage-puissance.md
§ 3.8) : avant une unité ComfyUI, AIODOS lui rend la mémoire vidéo qu'il tient sans s'en
servir, sur la machine qui calcule : ses modèles de conversation chargés (la prochaine
conversation recharge le sien) et son sd-server s'il ne calcule pas d'image. Une
conversation en cours passe d'abord (jobs/pacing.wait_turn). AIODOS ne touche jamais au
travail de l'utilisateur dans son propre ComfyUI."""
import logging

from aiodos.imaging import engine as images_engine
from aiodos.services import ollama

log = logging.getLogger("aiodos.engines")


def before_unit() -> list:
    """Libère la carte → ce qui a été déchargé ou arrêté."""
    freed = []
    try:
        freed += ollama.unload_chat_models()
    except Exception:                       # moteur de conversation injoignable : rien à rendre
        log.debug("Modèles de conversation non déchargés", exc_info=True)
    if images_engine.yield_gpu():
        freed.append("sd-server")
    if freed:
        log.info("Carte graphique rendue à ComfyUI pour une unité : %s", ", ".join(freed))
    return freed
