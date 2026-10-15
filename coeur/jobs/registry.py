"""Catalogue des types de travaux et des tâches unitaires.

Un *travail* (job) est ce que l'utilisateur demande (« indexer ce dossier »).
Il est découpé en *unités* indépendantes (une par fichier…) qui peuvent
s'exécuter dans n'importe quel ordre — et, en phase 1, sur n'importe quel nœud.
Une tâche unitaire ne reçoit qu'un payload JSON : elle doit être autonome.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class UnitSpec:
    task: str          # nom de la tâche unitaire (cf. TASKS)
    resource: str      # gpu | embed | cpu | net
    payload: dict = field(default_factory=dict)


@dataclass
class JobKind:
    name: str
    # split(params) -> (titre, [UnitSpec], infos) ; infos peut contenir "skipped"
    split: Callable
    # finalize(job_id, params, unités) -> résumé ajouté au résultat du travail
    finalize: Optional[Callable] = None


JOB_KINDS: dict = {}
TASKS: dict = {}


def register_job_kind(name, split, finalize=None):
    JOB_KINDS[name] = JobKind(name, split, finalize)


def task(name):
    """Décorateur : enregistre une tâche unitaire, fonction(payload) -> dict."""
    def decorator(fn):
        TASKS[name] = fn
        return fn
    return decorator
