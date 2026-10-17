"""Choisir seul la machine qui répond à une conversation (mode « automatique »).

Un même modèle peut être installé sur cette machine et prêté par mes autres machines
(lending.py). En mode automatique, identifiant « auto:<modèle> », la conversation part
vers la mieux placée :

1. cette machine, si elle a le modèle et qu'elle est libre (aucune conversation en
   cours), sauf quand le modèle n'y tient pas en mémoire vidéo alors qu'une machine
   libre l'y fait tenir (qwen3.6, 22 Go, sur une carte de 8 Go : bien plus lent ici) ;
2. sinon une machine en ligne qui le prête et qui est libre, de préférence celle où le
   modèle tient en mémoire vidéo, puis celle qui a le moins d'unités en cours, le
   modèle déjà chargé, le GPU le moins occupé ;
3. si toutes sont occupées, celle qui a le moins de conversations en cours ; à
   égalité, cette machine : rien ne sort de la maison.

Seules mes machines servent en mode automatique : celles d'un ami seulement si le
réglage auto_route_friends le permet, car la conversation passe chez lui le temps de
la réponse. Une machine mise de côté après un contrôle échoué n'est jamais choisie
d'office. Le choix d'une machine précise (« peer:<machine>:<modèle> », ou le nom seul
pour cette machine) reste possible et passe tel quel.

Tant qu'aucun mot de la réponse n'est arrivé, une machine qui échoue (injoignable, ne
prête plus, Ollama arrêté…) cède la place à la suivante (run).

Une machine qui prête ses modèles indique dans ses nouvelles (ping, profil) combien de
conversations et d'unités elle traite en ce moment (load_field) ; on y ajoute les
conversations qu'on vient de lui envoyer, que ses dernières nouvelles ne comptent pas
encore. Une machine qui ne prête pas ne dit rien de son activité.
"""
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from aiodos import settings
from aiodos.jobs import queue
from aiodos.network import peers, remote_models
from aiodos.node import identity

log = logging.getLogger("aiodos.placement")

PREFIX = "auto:"
LOAD_TTL = 60               # nouvelles d'une machine plus vieilles : oubliées
LOAD_MAX = 1000             # valeurs annoncées par une autre machine : bornées

_lock = threading.Lock()
_loads = {}                 # machine -> (instant monotonic, {"chats": n, "units": n})
_sent = {}                  # machine -> conversations que je lui ai envoyées, en cours


class NoMachine(Exception):
    """Aucune machine ne peut répondre avec ce modèle en ce moment (message lisible)."""


def auto_id(name: str) -> str:
    return PREFIX + name


def parse(model) -> str | None:
    """« auto:<modèle> » → le nom du modèle ; None pour un autre identifiant."""
    model = str(model or "")
    name = model[len(PREFIX):] if model.startswith(PREFIX) else ""
    return name or None


# ----------------------------------------------------------- charge des machines
def load_field(manager) -> dict:
    """{"load": …} à joindre aux nouvelles de cette machine (ping, profil), seulement si
    elle prête ses modèles : c'est ce qui permet de lui envoyer une conversation."""
    executor = getattr(manager, "executor", None)
    if executor is None or not settings.get("lend_models"):
        return {}
    return {"load": {"chats": int(executor.interactive),
                     "units": int(sum(executor.busy.values()))}}


def note_load(node_id: str, load):
    """Charge annoncée par une machine appairée (None : elle n'en dit rien)."""
    with _lock:
        if not isinstance(load, dict):
            _loads.pop(node_id, None)
            return
        clean = {}
        for key in ("chats", "units"):
            try:
                clean[key] = min(LOAD_MAX, max(0, int(load.get(key) or 0)))
            except (TypeError, ValueError):
                clean[key] = 0
        _loads[node_id] = (time.monotonic(), clean)


def _reported(node_id) -> dict | None:
    with _lock:
        entry = _loads.get(node_id)
    if entry and time.monotonic() - entry[0] < LOAD_TTL:
        return entry[1]
    return None


def forget():
    """Tout oublier (tests)."""
    with _lock:
        _loads.clear()
        _sent.clear()


# --------------------------------------------------------------- candidates
@dataclass
class Candidate:
    model: str                  # identifiant concret : « qwen3.5:9b », « peer:<machine>:… »
    node_id: str | None         # None : cette machine
    machine: str                # nom de la machine
    trust: str                  # « local », « mine » ou « friend »
    chats: int = 0              # conversations en cours
    units: int = 0              # unités de travail en cours
    fits: bool | None = None    # le modèle tient-il dans sa mémoire vidéo ?
    loaded: bool = False        # déjà chargé : pas d'attente de chargement
    gpu: int = 0                # occupation du GPU (%), si elle est connue

    @property
    def local(self) -> bool:
        return self.node_id is None

    def key(self) -> tuple:
        """Ordre de préférence (cf. le début du module) : libre, modèle en mémoire vidéo,
        le moins de conversations, cette machine, le moins d'unités, déjà chargé, GPU."""
        return (self.chats > 0, self.fits is False, self.chats, not self.local,
                self.units, not self.loaded, self.gpu)


def _chat_capable(caps) -> bool:
    return not caps or "completion" in caps          # pas un modèle d'embedding seul


def _local_profile() -> dict:
    """Profil de cette machine (modèles, mémoire vidéo), gardé quelques secondes."""
    try:
        return identity.profile()
    except Exception as exc:
        log.warning("Profil de cette machine illisible : %s", exc)
        return {}


def _gpu_load(profile: dict) -> int:
    return max((int(g.get("utilization") or 0) for g in profile.get("gpus") or []), default=0)


def _local(name: str, executor) -> Candidate | None:
    profile = _local_profile()
    for m in (profile.get("ollama") or {}).get("models") or []:
        if m.get("name") == name and _chat_capable(m.get("caps")):
            return Candidate(
                model=name, node_id=None, machine=profile.get("name") or "cette machine",
                trust="local",
                chats=int(executor.interactive) if executor else 0,
                units=int(sum(executor.busy.values())) if executor else 0,
                fits=m.get("fits_vram"), loaded=bool(m.get("loaded")), gpu=_gpu_load(profile))
    return None


def eligible(peer: dict) -> bool:
    """Cette machine appairée peut-elle répondre d'office à mes conversations ?"""
    if not peer.get("online") or peer.get("set_aside_at"):
        return False
    if not (peer.get("address") or peer.get("net_id")):
        return False
    profile = peer.get("profile") or {}
    if not profile.get("lends_models"):
        return False
    return peer.get("trust") != "friend" or bool(settings.get("auto_route_friends"))


def _remote(name: str) -> list:
    out = []
    working = {}
    for counts in queue.remote_counts().values():   # unités qu'elles calculent pour moi
        for node_id, n in counts.items():
            working[node_id] = working.get(node_id, 0) + n
    for peer in peers.list_peers():
        if not eligible(peer):
            continue
        profile = peer.get("profile") or {}
        model = next((m for m in (profile.get("ollama") or {}).get("models") or []
                      if m.get("name") == name and _chat_capable(m.get("caps"))), None)
        if model is None:
            continue
        node_id = peer["node_id"]
        reported = _reported(node_id)
        with _lock:
            sent = _sent.get(node_id, 0)
        out.append(Candidate(
            model=remote_models.model_id(node_id, name), node_id=node_id,
            machine=peer["name"], trust=peer.get("trust") or "mine",
            chats=max(sent, reported["chats"] if reported else 0),
            units=reported["units"] if reported else working.get(node_id, 0),
            fits=model.get("fits_vram"), loaded=bool(model.get("loaded")),
            gpu=_gpu_load(profile)))
    return out


def candidates(name: str, executor=None) -> list:
    """Machines qui peuvent répondre avec ce modèle, la mieux placée d'abord."""
    found = _remote(name)
    here = _local(name, executor)
    if here:
        found.append(here)
    return sorted(found, key=Candidate.key)


def plan(model, executor=None) -> list:
    """Identifiants concrets à essayer, dans l'ordre. Un modèle précis (cette machine,
    une machine appairée, le cloud) passe tel quel ; « auto:<modèle> » est réparti."""
    name = parse(model)
    if name is None:
        return [model]
    ranked = candidates(name, executor)
    if not ranked:
        raise NoMachine(f"Aucune machine ne peut répondre avec « {name} » en ce moment : "
                        "ni cette machine, ni une de tes machines en ligne qui le prête.")
    log.info("Mode automatique, « %s » : %s", name,
             ", ".join(f"{c.machine} ({c.chats} conv., {c.units} unités)" for c in ranked))
    return [c.model for c in ranked]


def machine_of(model) -> str | None:
    """Nom de la machine qui répond avec cet identifiant concret (None : cette machine)."""
    parsed = remote_models.parse(model)
    if not parsed:
        return None
    peer = peers.get(parsed[0])
    return peer["name"] if peer else "autre machine"


def destination(model) -> str | None:
    """Niveau de confiance de la machine qui répond : None (cette machine, ou le cloud
    après validation), « mine » ou « friend » (cf. pipeline.build_messages)."""
    parsed = remote_models.parse(model)
    if not parsed:
        return None
    return peers.trust_of(parsed[0]) or "friend"


# ------------------------------------------------------------------ réponse
@contextmanager
def _occupy(model, executor):
    """Pendant une réponse : ici, les unités GPU de fond attendent (l'interactif
    d'abord) ; ailleurs, la conversation compte dans la charge de cette machine."""
    parsed = remote_models.parse(model)
    local = executor is not None and not parsed and not str(model).startswith("cloud:")
    if local:
        executor.interactive_begin()
    elif parsed:
        with _lock:
            _sent[parsed[0]] = _sent.get(parsed[0], 0) + 1
    try:
        yield
    finally:
        if local:
            executor.interactive_end()
        elif parsed:
            with _lock:
                left = _sent.get(parsed[0], 1) - 1
                if left > 0:
                    _sent[parsed[0]] = left
                else:
                    _sent.pop(parsed[0], None)


QUIET = {"route", "sources"}    # ce qui n'engage pas encore la réponse


def run(models: list, attempt, executor=None, stop=None):
    """Couples (genre, valeur) de la réponse, par la première machine du plan qui répond.

    attempt(modèle) produit les couples d'un essai. Avant chacun vient ("route", modèle).
    Tant que l'essai n'a produit que des couples QUIET, un échec fait passer à la machine
    suivante ; après, ou au dernier essai, ou si la réponse a été arrêtée, il remonte."""
    for i, model in enumerate(models):
        yield "route", model
        started = False
        try:
            with _occupy(model, executor):
                for kind, value in attempt(model):
                    if kind not in QUIET:
                        started = True
                    yield kind, value
            return
        except Exception as exc:
            if started or i + 1 == len(models) or (stop is not None and stop.is_set()):
                raise
            log.info("« %s » n'a pas pu répondre (%s) : essai de la machine suivante",
                     model, exc)


# ------------------------------------------------------- choix proposé à l'interface
def auto_entries(models: list) -> list:
    """Modèles proposés en mode automatique, une fois chacun, à partir des modèles de
    cette machine et de ceux prêtés (pipeline.available_models) : un modèle n'y figure
    que si au moins une machine peut le servir d'office."""
    by_name = {}
    for m in models:
        if m.get("group") == "local":
            by_name.setdefault(m["label"], []).append(("cette machine", m))
        elif m.get("group") == "remote" and m.get("auto"):
            by_name.setdefault(m["label"], []).append((m["peer"], m))
    entries = []
    for name, found in by_name.items():
        first = found[0][1]
        entries.append({"id": auto_id(name), "label": name, "group": "auto",
                        "size": first.get("size", 0), "caps": first.get("caps") or [],
                        "machines": [machine for machine, _ in found]})
    return entries
