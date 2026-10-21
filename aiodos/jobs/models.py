"""Constantes de la file de tâches : priorités, états, ressources."""

# Priorités (plus petit = plus prioritaire). L'interactif (le chat) ne passe
# pas par la file : il est servi tout de suite et met en pause les ressources GPU.
URGENT, NORMAL, BACKGROUND = 1, 2, 3
PRIORITIES = (URGENT, NORMAL, BACKGROUND)      # libellés : i18n/*/jobs.json, priority_1…3

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

# La file comme un torrent (docs/conception/partage-puissance.md § 2.5 et 2.6). Seul un vrai
# échec du calcul compte un essai ; le reste a ses propres compteurs :
MAX_INTERRUPTS = 5      # arrêts d'AIODOS pendant son calcul ici : au-delà, l'essai compte
LOST_MINE_ONLY = 2      # disparitions de la machine qui la calculait : ensuite, mes machines seules
MAX_LOST = 5            # … et à 5, elle échoue
MAX_HANDOUTS = 8        # remises (ici ou ailleurs) : à 8, « aucune machine n'a pu la finir »
RECOVER_GRACE = 900     # s : après un redémarrage d'ici, une unité confiée ailleurs garde sa
                        # machine, le temps qu'elle se raccroche ou livre son résultat
PAUSE_AFTER, PAUSE_NOW = "after_current", "now"
PAUSE_MODES = (PAUSE_AFTER, PAUSE_NOW)
