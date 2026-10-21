"""Cadence du travail de fond : céder la place aux conversations, ménager la batterie
(docs/arriere-plan.md).

Les règles du partage de puissance (inactivité, secteur, température des téléphones :
network/worker.py) disent QUAND cette machine calcule pour les autres. Celles-ci disent
COMMENT avance le travail de fond, le sien comme celui qu'elle fait pour les autres :

  - pendant une conversation servie par cette machine, le calcul qui occupe la carte
    graphique attend, avant chaque lot (wait_turn), la fin de la réponse. La file ne
    lançait déjà plus de nouvelle unité pendant ce temps ; celles en cours, si ;
  - sur batterie, selon le réglage battery_mode :
      « eco » (par défaut) : une unité à la fois par ressource (BATTERY_SLOTS), après
        chaque lot une pause aussi longue que lui (rest : calcul la moitié du temps),
        et moins de fils de calcul (threads) pour Whisper, et pour Ollama quand le
        modèle d'embedding tourne sur le processeur ;
      « pause » : le travail de fond attend le secteur ;
      « normal » : comme sur secteur.

refresh() relit l'alimentation et le réglage (au plus toutes les REGIME_TTL secondes,
hors de la boucle asyncio : sous Android, lire la batterie peut prendre des secondes) ;
current() rend le dernier état connu sans rien relire.
"""
import os
import threading
import time

from aiodos import i18n, settings
from aiodos.jobs.registry import Interrupted
from aiodos.node import power
from aiodos.services import ollama

MODES = ("eco", "pause", "normal")
REGIME_TTL = 5               # secondes entre deux lectures de l'alimentation et du réglage
BATTERY_SLOTS = {"gpu": 1, "embed": 1, "cpu": 1, "net": 1}
BATTERY_DUTY = 0.5           # part du temps passée à calculer sur batterie (« eco »)
MAX_REST = 60                # pause la plus longue après un lot (secondes)
POLL = 0.25                  # attente d'une fin de conversation : vérification de stop
PAUSE_POLL = 5               # « pause » : le secteur est-il revenu ?
EMBED_BATCH = 16             # comme services.ollama.embed

# detail : la clé de son explication (i18n/*/jobs.json), traduite par describe()
AC = {"mode": "ac", "on_battery": False, "slots": None, "duty": 1.0, "threads": None,
      "detail": "jobs.pacing_ac"}

_lock = threading.Lock()
_state = {"at": 0.0, "regime": AC}
_idle = threading.Event()    # levé quand aucune conversation n'est servie ici
_idle.set()
_stopping = threading.Event()


def eco_threads(default: int | None = None) -> int:
    """Fils de calcul sur batterie : la moitié de ceux d'habitude (`default`). Pour
    Ollama, dont le choix n'est pas connu d'ici, un quart des processeurs logiques : sur
    le PC de mesure (32), 8 au lieu de ses 16, pour le même débit et moitié moins de
    cœurs occupés quand le modèle tourne sur le processeur (docs/arriere-plan.md)."""
    if default:
        return max(1, default // 2)
    return max(1, (os.cpu_count() or 4) // 4)


def _on_battery(supply: dict) -> bool:
    """Sur batterie. Une alimentation inconnue compte comme le secteur sur un PC (machine
    de bureau, état illisible), comme la batterie sur un téléphone, qui en a toujours une."""
    plugged = supply.get("plugged")
    return plugged is False or (plugged is None and bool(supply.get("mobile")))


def _regime(mode: str, supply: dict) -> dict:
    if not _on_battery(supply):
        return AC
    if mode == "normal":
        return {**AC, "mode": "normal", "on_battery": True,
                "detail": "jobs.pacing_normal"}
    if mode == "pause":
        return {**AC, "mode": "pause", "on_battery": True, "slots": dict.fromkeys(BATTERY_SLOTS, 0),
                "detail": "jobs.pacing_pause"}
    return {"mode": "eco", "on_battery": True, "slots": dict(BATTERY_SLOTS),
            "duty": BATTERY_DUTY, "threads": eco_threads(),
            "detail": "jobs.pacing_eco"}


def refresh(force: bool = False) -> dict:
    """État à jour (alimentation et réglage relus au plus toutes les REGIME_TTL s).
    Bloquant : à appeler hors de la boucle asyncio."""
    with _lock:
        if not force and time.monotonic() - _state["at"] < REGIME_TTL:
            return _state["regime"]
    mode = str(settings.get("battery_mode") or "eco")
    regime = _regime(mode if mode in MODES else "eco", power.read())
    with _lock:
        _state.update(at=time.monotonic(), regime=regime)
    ollama.set_thread_limit(regime["threads"])
    return regime


def current() -> dict:
    """Dernier état connu, sans rien relire (depuis la boucle asyncio)."""
    return _state["regime"]


def slots(configured: dict) -> dict:
    """Unités simultanées permises en ce moment, ressource par ressource."""
    caps = current()["slots"]
    if not caps:
        return dict(configured)
    return {name: min(n, caps.get(name, n)) for name, n in configured.items()}


def threads(default: int) -> int | None:
    """Fils de calcul à demander à une bibliothèque qui en prend `default` d'habitude
    (Whisper : 4) : la moitié sur batterie (« eco »), sinon None (son choix habituel)."""
    return eco_threads(default) if refresh()["mode"] == "eco" else None


# ----------------------------------------------------------- conversations
_chat_listeners = []


def on_chat(callback):
    """callback() est appelé, dans un fil à part, quand une conversation commence ici
    (moteur d'images qui rend la carte graphique, cf. imaging/engine.py)."""
    _chat_listeners.append(callback)


def set_interactive(count: int):
    """Nombre de conversations servies ici en ce moment (cf. Executor.interactive_begin)."""
    if count > 0:
        began = _idle.is_set()
        _idle.clear()
        if began:
            for callback in list(_chat_listeners):
                threading.Thread(target=callback, daemon=True,
                                 name="aiodos-conversation-commence").start()
    else:
        _idle.set()


def chatting() -> bool:
    return not _idle.is_set()


# ------------------------------------------------------------------- lots
_waits = threading.local()


def waited() -> float:
    """Secondes passées par ce thread à attendre et à se reposer (wait_turn, rest) : à
    retirer du temps de calcul d'une unité (réciprocité, statistiques)."""
    return getattr(_waits, "seconds", 0.0)


def _count_wait(began: float):
    _waits.seconds = waited() + time.monotonic() - began


def _check(stop):
    if _stopping.is_set() or (stop is not None and stop.is_set()):
        raise Interrupted()


def _sleep(seconds: float, stop):
    """Attente qui s'interrompt (Interrupted) si l'unité est arrêtée ou AIODOS s'arrête."""
    deadline = time.monotonic() + seconds
    while True:
        _check(stop)
        left = deadline - time.monotonic()
        if left <= 0:
            return
        _stopping.wait(min(left, POLL))


def wait_turn(stop=None, gpu: bool = True):
    """Avant un lot : attendre la fin d'une conversation servie ici (lot qui occupe la
    carte graphique), et le retour du secteur en mode « pause ». Une unité qui a un arrêt
    (`stop` : calculée pour une autre machine, ou ici par l'exécuteur, jobs/executor.py)
    n'attend pas le secteur : elle est rendue (Interrupted), refaite au retour du secteur,
    par une de mes machines qui la demande, ou ailleurs par son propriétaire."""
    began = time.monotonic()
    try:
        while True:
            _check(stop)
            if gpu and chatting():
                _idle.wait(POLL)
                continue
            if refresh()["mode"] == "pause":
                if stop is not None:
                    raise Interrupted()
                _sleep(PAUSE_POLL, stop)
                continue
            return
    finally:
        _count_wait(began)


def rest(worked: float, stop=None):
    """Après un lot de `worked` secondes : sur batterie (« eco »), une pause qui ramène
    le calcul à BATTERY_DUTY du temps."""
    duty = refresh()["duty"]
    if duty < 1 and worked > 0:
        began = time.monotonic()
        try:
            _sleep(min(MAX_REST, worked * (1 - duty) / duty), stop)
        finally:
            _count_wait(began)


def step(worked: float, stop=None):
    """Entre deux pas d'un calcul sur le processeur (page d'un document, segment d'une
    transcription) : la pause sur batterie, puis l'attente du secteur en mode « pause »."""
    rest(worked, stop)
    wait_turn(stop, gpu=False)


def paced(call, stop=None, gpu: bool = True):
    """call() entre wait_turn et rest : un lot du travail de fond."""
    wait_turn(stop, gpu)
    began = time.monotonic()
    result = call()
    rest(time.monotonic() - began, stop)
    return result


def embed(texts: list, model=None, stop=None) -> list:
    """ollama.embed par lots, chacun cédant la place et se reposant comme il faut."""
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        vectors.extend(paced(lambda: ollama.embed(batch, model=model), stop))
    return vectors


# ---------------------------------------------------------------- cycle de vie
def start():
    """Démarrage de la file : rien n'attend, l'alimentation sera relue."""
    _stopping.clear()
    _idle.set()
    with _lock:
        _state.update(at=0.0, regime=AC)


def stopping() -> threading.Event:
    """Levé quand AIODOS s'arrête : un calcul long lancé sans arrêt à lui (hors de
    l'exécuteur, qui donne le sien à chaque unité) s'interrompt dessus."""
    return _stopping


def shutdown():
    """Arrêt d'AIODOS : les lots en attente ou en pause s'arrêtent (Interrupted)."""
    _stopping.set()


def describe() -> dict:
    regime = current()
    return {"mode": regime["mode"], "on_battery": regime["on_battery"],
            "detail": i18n.t(regime["detail"]), "chatting": chatting()}
