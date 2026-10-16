"""Catalogue des types de travaux et des tâches unitaires.

Un *travail* (job) est ce que l'utilisateur demande (« indexer ce dossier »).
Il est découpé en *unités* indépendantes (une par fichier…) qui peuvent
s'exécuter dans n'importe quel ordre, et sur n'importe quel nœud.

Une tâche *portable* se fait en deux temps, pour pouvoir confier le calcul à
une autre machine sans lui ouvrir les données :
  - compute(payload, fichier, options, stop) -> sortie : le calcul pur
    (extraction du texte, embeddings…), exécutable n'importe où ;
  - assimilate(payload, sortie) -> résultat : l'intégration dans les
    bibliothèques, toujours chez le propriétaire du travail.
Une tâche simple fait tout d'un coup, sur ce nœud seulement.
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


@dataclass
class TaskSpec:
    name: str
    compute: Callable                       # (payload, fichier, options, stop) -> sortie
    assimilate: Optional[Callable] = None   # (payload, sortie) -> résultat ; None = non portable
    file_key: Optional[str] = None          # clé du payload désignant le fichier à transmettre
    options: Optional[Callable] = None      # () -> réglages du propriétaire, figés pour le calcul
    requires: Optional[Callable] = None     # (unité, options) -> modèles, format… requis (cf. network.work)
    validate: Optional[Callable] = None     # (payload, sortie) : contrôle d'une sortie venue d'ailleurs

    @property
    def portable(self) -> bool:
        return self.assimilate is not None


class Interrupted(Exception):
    """Calcul arrêté en route (l'utilisateur de la machine est revenu…) :
    l'unité est rendue, elle n'a pas échoué."""


JOB_KINDS: dict = {}
TASKS: dict = {}


def register_job_kind(name, split, finalize=None):
    JOB_KINDS[name] = JobKind(name, split, finalize)


def task(name):
    """Décorateur : tâche simple, exécutée sur ce nœud, fonction(payload) -> dict."""
    def decorator(fn):
        TASKS[name] = TaskSpec(name, lambda payload, file, options, stop: fn(payload))
        return fn
    return decorator


def register_portable(name, compute, assimilate, file_key=None, options=None,
                      requires=None, validate=None):
    TASKS[name] = TaskSpec(name, compute, assimilate, file_key, options, requires, validate)


def run_local(name, payload, stop=None) -> dict:
    """Exécution complète sur ce nœud : calcul puis intégration."""
    spec = TASKS[name]
    options = spec.options() if spec.options else {}
    file = payload.get(spec.file_key) if spec.file_key else None
    output = spec.compute(payload, file, options, stop)
    return spec.assimilate(payload, output) if spec.assimilate else output
