"""Constantes de la file de tâches : priorités, états, ressources."""

# Priorités (plus petit = plus prioritaire). L'interactif (le chat) ne passe
# pas par la file : il est servi tout de suite et met en pause les ressources GPU.
URGENT, NORMAL, BACKGROUND = 1, 2, 3
PRIORITY_LABELS = {URGENT: "Urgent", NORMAL: "Normal", BACKGROUND: "De fond"}

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
CANCELLED, PAUSED, SKIPPED = "cancelled", "paused", "skipped"

# Ressources d'exécution et nombre d'unités simultanées sur ce nœud.
#   gpu   : modèles lourds (vision…)        embed : embeddings Ollama (légers)
#   cpu   : Whisper, calculs processeur     net   : lecture de pages web
DEFAULT_SLOTS = {"gpu": 1, "embed": 2, "cpu": 2, "net": 3}

# Ressources suspendues pendant qu'une conversation est en cours : la réponse
# du chat passe toujours devant le travail de fond.
GPU_BOUND = {"gpu", "embed"}

MAX_ATTEMPTS = 2
