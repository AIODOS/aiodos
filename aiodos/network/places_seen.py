"""Les places des machines qui calculent pour moi (docs/conception/essaim.md § 3), telles
qu'elles les annoncent à chaque demande de travail : work/lease reçoit caps.places et
caps.busy (network/worker.capabilities, relus à chaque demande). Gardées ici, en mémoire
seulement, avec l'heure, pour le chantier en direct (jobs/chantier.py) et les machines au
travail (activity.annotate, vue Tâches).

Ce qu'annonce une autre machine (un ami peut-être) est borné avant d'être gardé : les
ressources connues seulement (DEFAULT_SLOTS), des entiers de 0 à MAX_PLACES, jamais plus de
places prises que de places. Une unité qui vient de lui être confiée prend une place de plus
(took), jusqu'à sa prochaine demande, qui dit de nouveau ce qu'il en est. Une machine qui ne
demande plus rien depuis TTL secondes est oubliée.
"""
import threading
import time
from collections import OrderedDict

from aiodos.jobs.models import DEFAULT_SLOTS

MAX_PLACES = 64             # places d'une ressource, au plus (8 de processeur aujourd'hui)
MAX_MACHINES = 500          # machines gardées, au plus
TTL = 600                   # s sans demande de travail : ses places sont oubliées

_lock = threading.Lock()
_seen = OrderedDict()       # machine -> {"places": {…}, "busy": {…}, "at": instant}


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_PLACES else None


def note(node_id: str, caps) -> bool:
    """Les places et les places prises que dit la demande de travail de cette machine.
    Faux (rien de gardé) si elle n'en dit rien de lisible : une version d'avant."""
    if not isinstance(node_id, str) or not node_id or not isinstance(caps, dict):
        return False
    places, busy = caps.get("places"), caps.get("busy")
    if not isinstance(places, dict):
        return False
    busy = busy if isinstance(busy, dict) else {}
    clean = {}
    for name in DEFAULT_SLOTS:
        n = _count(places.get(name))
        if n is not None:
            clean[name] = n
    if not clean:
        return False
    taken = {name: min(n, _count(busy.get(name)) or 0) for name, n in clean.items()}
    with _lock:
        _seen[node_id] = {"places": clean, "busy": taken, "at": time.time()}
        _seen.move_to_end(node_id)
        while len(_seen) > MAX_MACHINES:
            _seen.popitem(last=False)
    return True


def took(node_id: str, resource: str):
    """Une unité vient de lui être confiée sur cette ressource : une place de plus prise."""
    with _lock:
        found = _seen.get(node_id)
        if found and resource in found["places"]:
            found["busy"][resource] = min(found["places"][resource],
                                          found["busy"].get(resource, 0) + 1)


def get(node_id: str) -> dict | None:
    """{"places", "busy", "at"} de cette machine, ou None (rien de récent)."""
    with _lock:
        found = _seen.get(node_id)
        if found is None:
            return None
        if time.time() - found["at"] > TTL:
            del _seen[node_id]
            return None
        return {"places": dict(found["places"]), "busy": dict(found["busy"]),
                "at": found["at"]}


def forget(node_id: str | None = None):
    """Une machine retirée (ou toutes, pour les essais)."""
    with _lock:
        if node_id is None:
            _seen.clear()
        else:
            _seen.pop(node_id, None)
