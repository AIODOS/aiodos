"""Les places de cette machine : combien d'unités de travail de fond elle calcule en même
temps, ressource par ressource, d'après sa puissance et la part qu'elle prête
(docs/conception/essaim.md § 3).

Décision de l'auteur (27/10/2026) : une machine ne donne jamais toute sa puissance ; 60 % par
défaut, réglable (« Puissance prêtée », lend_power, qui remplace « GPU prêté au maximum »). La
part s'applique :

- au processeur : au plus cette part des processeurs logiques, à raison de CPU_UNIT_THREADS
  par unité (32 processeurs à 60 % : 4 places ; un téléphone à 8 : 1) ;
- à la carte graphique : une place (un modèle y tient ou n'y tient pas), mais au plus cette
  part de son temps : pause après chaque unité prêtée (network/worker._rest_gpu) ;
- à la mémoire vive : rien de nouveau ne commence quand il en reste moins de la moitié de la
  part gardée (20 % à 60 %). La conception proposait toute la part gardée (40 %) : sur un
  portable de 8 Go avec un navigateur ouvert, ou un téléphone (Android remplit sa mémoire
  de caches), rien ne serait jamais calculé. La mémoire des moteurs d'AIODOS (le routeur
  llama.cpp et ses modèles, sd-server… : node/children.resident) compte comme disponible :
  c'est celle que la part prêtée utilise, pas celle de l'utilisateur, et le routeur décharge
  le plus ancien de ses modèles. Sinon un modèle chargé pour servir rendait la machine
  « pleine » (PC de l'auteur, 29/10/2026 : 8,7 Go chargés sur 32, 19 % disponibles, plus
  aucune place annoncée au réseau public, le téléphone ne trouvait plus personne) ;
- ni au réseau (« net »), ni aux embeddings, qui ne prennent presque rien : leurs places ne
  changent pas, sauf une seule place d'embeddings sous 50 %.

Une machine pleine ne demande rien (network/worker._offered) : c'est la parité de l'essaim,
jamais toujours la même qui travaille. Les places bornent aussi ce qu'une autre machine peut
faire calculer ici. Sur batterie, jobs/pacing.py réduit encore (une place par ressource).
"""
import os
import threading
import time

from aiodos import settings
from aiodos.jobs.models import DEFAULT_SLOTS
from aiodos.node import children, identity

DEFAULT_SHARE = 60
MIN_SHARE, MAX_SHARE = 10, 100
CPU_UNIT_THREADS = 4          # processeurs logiques qu'occupe une unité « cpu » (Whisper…)
MAX_CPU = 8
TTL = 2.0                     # s entre deux lectures du réglage et de la mémoire vive
# Essais : part de mémoire vive libre simulée (comme AIODOS_SIMULATE_POWER, _IDLE), pour
# qu'un test ne dépende pas de ce qui tourne sur la machine qui le lance
SIMULATE_MEMORY_ENV = "AIODOS_SIMULATE_MEMORY"

_lock = threading.Lock()
# Relus au plus toutes les TTL s : l'exécuteur les demande à chaque tour, dans la boucle
_share = {"at": -TTL, "value": DEFAULT_SHARE}
_memory = {"at": -TTL, "free": None}


def forget():
    """Relire le réglage et la mémoire à la prochaine demande (réglage changé, essais)."""
    with _lock:
        _share["at"] = _memory["at"] = -TTL


def share() -> int:
    """La part prêtée, en % (« Puissance prêtée »), bornée ; illisible : celle par défaut."""
    with _lock:
        if time.monotonic() - _share["at"] < TTL:
            return _share["value"]
    try:
        value = max(MIN_SHARE, min(MAX_SHARE, int(settings.get("lend_power") or DEFAULT_SHARE)))
    except (TypeError, ValueError):
        value = DEFAULT_SHARE
    with _lock:
        _share.update(at=time.monotonic(), value=value)
    return value


def count(part: int | None = None, cpus: int | None = None) -> dict:
    """Les places de cette machine, ressource par ressource."""
    part = share() if part is None else part
    cpus = cpus or os.cpu_count() or 4
    return {"gpu": DEFAULT_SLOTS["gpu"],
            "embed": DEFAULT_SLOTS["embed"] if part >= 50 else 1,
            "cpu": max(1, min(MAX_CPU, cpus * part // (100 * CPU_UNIT_THREADS))),
            "net": DEFAULT_SLOTS["net"]}


def memory_free() -> float | None:
    """Part de la mémoire vive encore disponible (0 à 1) ; None si illisible."""
    simulated = os.environ.get(SIMULATE_MEMORY_ENV, "").strip()
    if simulated:
        try:
            return max(0.0, min(1.0, float(simulated) / 100))
        except ValueError:
            pass
    with _lock:
        if time.monotonic() - _memory["at"] < TTL:
            return _memory["free"]
    found = identity.memory()
    total = found.get("total") or 0
    # Celle des moteurs d'AIODOS compte comme disponible (cf. le début du module)
    free = min(found["available"] + children.resident(), total) / total if total else None
    with _lock:
        _memory.update(at=time.monotonic(), free=free)
    return free


def memory_room(part: int | None = None) -> bool:
    """Assez de mémoire vive laissée à l'utilisateur pour commencer une unité ? Illisible :
    oui (comme avant)."""
    free = memory_free()
    part = share() if part is None else part
    return free is None or free * 100 >= (100 - part) / 2
