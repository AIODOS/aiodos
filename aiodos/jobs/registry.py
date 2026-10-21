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

from aiodos import settings


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
    # Chez un ami (cf. network/sharing.py) : les seules clés du payload qui partent, et
    # sample(payload, sortie, options), le contrôle par échantillon de ce qu'il a calculé
    shared_keys: tuple = ()
    sample: Optional[Callable] = None
    # here(modèle) -> bool : cette machine sait-elle calculer les unités de ce modèle (clé
    # « model » du payload : moteur présent, fichiers complets) ? Sinon elles attendent en
    # file qu'une de mes machines qui le sait les demande. None : toujours
    here: Optional[Callable] = None
    mine_only: bool = False               # jamais calculée par un ami, ni pour un ami...
    # ... sauf accord des deux côtés, deux réglages : le propriétaire accepte de confier ces
    # unités à ses amis (friends_out), l'ami de les calculer pour ses amis (friends_in)
    friends_out: Optional[str] = None
    friends_in: Optional[str] = None
    # Fichier facultatif (l'image de départ d'une retouche) : file_optional(caps) dit si la
    # machine qui calcule sait s'en servir ; sans lui (illisible, trop gros, machine d'avant),
    # l'ordre part sans le fichier ni les clés file_extras, et la tâche fait sans
    file_optional: Optional[Callable] = None
    file_extras: tuple = ()
    # checkable(payload) -> bool : ce qu'un ami rendrait pour cette unité, sample sait-il le
    # contrôler ici (un document que je sais relire ; pas une image, un son, une vidéo, ni un
    # format sans lecteur ici) ? Sinon l'unité ne part pas chez un ami (friend_checkable)
    checkable: Optional[Callable] = None
    # expand(unité, here, fresh) -> payload : l'ordre complet, assemblé au moment de calculer
    # depuis jobs.params (un graphe de moteur ne se recopie pas dans chacune des 256
    # unités) ; here : pour ce PC (avec ce qui ne le quitte jamais : chemins, appli) ;
    # fresh : une remise (ici ou ailleurs), sinon une relecture (résultat revenu, morceau
    # d'une entrée demandé)
    expand: Optional[Callable] = None

    @property
    def portable(self) -> bool:
        return self.assimilate is not None


class Interrupted(Exception):
    """Calcul arrêté en route (l'utilisateur de la machine est revenu…) :
    l'unité est rendue, elle n'a pas échoué."""


class SampleMismatch(Exception):
    """Contrôle par échantillon échoué : la sortie d'un ami ne correspond pas au calcul
    refait ici (cf. TaskSpec.sample, network/sharing.py)."""


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
                      requires=None, validate=None, shared_keys=(), sample=None, here=None,
                      mine_only=False, friends_out=None, friends_in=None, file_optional=None,
                      file_extras=(), checkable=None, expand=None):
    TASKS[name] = TaskSpec(name, compute, assimilate, file_key, options, requires, validate,
                           tuple(shared_keys), sample, here, mine_only, friends_out, friends_in,
                           file_optional, tuple(file_extras), checkable, expand)


def payload_for(unit, here: bool = True, fresh: bool = True) -> dict:
    """Le payload d'une unité au moment de la calculer : complété par expand s'il y en a un
    (unit : la ligne de la file, avec id, job_id et payload)."""
    spec = TASKS.get(unit["task"])
    payload = unit["payload"]
    if isinstance(payload, str):
        import json
        payload = json.loads(payload)
    if spec is None or not spec.expand:
        return payload
    return spec.expand({**unit, "payload": payload}, here, fresh)


def friends_may_take(spec) -> bool:
    """Chez le propriétaire : une unité de cette tâche peut-elle partir chez un ami ?"""
    return not spec.mine_only or bool(spec.friends_out and settings.get(spec.friends_out))


def friend_checkable(spec, payload) -> bool:
    """Chez le propriétaire : ce qu'un ami rendrait pour cette unité pourrait-il être pris ?
    Sinon elle ne part pas chez lui : il calculerait pour rien (son résultat serait refusé,
    puis l'unité refaite ici), et ce qu'on ne contrôle pas n'entre pas dans mes données
    (audit du 20/10/2026, relecture du 21/10).
    - Tâche contrôlée par échantillon (sample) : seulement ce que ce contrôle sait vérifier
      ici (checkable). Une telle tâche qui ne le dit pas ne part jamais chez un ami : une
      tâche ajoutée sans y penser n'ouvre pas de brèche.
    - Tâche sans contrôle : seulement celle que l'utilisateur accepte de confier à ses amis
      sans contrôle possible (mine_only avec friends_out, cf. friends_may_take : ses images,
      qu'il voit)."""
    if spec.sample is None:
        return spec.mine_only and spec.friends_out is not None
    if spec.checkable is None:
        return False
    return bool(spec.checkable(payload))


def may_compute_for_friend(spec) -> bool:
    """Chez celui qui calcule : peut-il calculer une unité de cette tâche pour un ami ?"""
    return not spec.mine_only or bool(spec.friends_in and settings.get(spec.friends_in))


def runnable_here(task, model) -> bool:
    """L'exécuteur local peut-il prendre les unités de cette tâche et de ce modèle (cf.
    TaskSpec.here) ?"""
    spec = TASKS.get(task)
    return spec is None or spec.here is None or bool(spec.here(model))


def conditional_tasks() -> list:
    """Tâches qui ne se calculent pas sur toutes les machines (TaskSpec.here)."""
    return [name for name, spec in TASKS.items() if spec.here is not None]


def run_local(name, payload, stop=None) -> dict:
    """Exécution complète sur ce nœud : calcul puis intégration."""
    spec = TASKS[name]
    options = spec.options() if spec.options else {}
    file = payload.get(spec.file_key) if spec.file_key else None
    output = spec.compute(payload, file, options, stop)
    return spec.assimilate(payload, output) if spec.assimilate else output
