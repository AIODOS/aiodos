"""Au démarrage : quelle version lancer ?

La version d'origine (le dossier où AIODOS a été installé) cherche dans
data/updates/ une version plus récente : celle qui a déjà fait ses preuves
(« current »), ou une nouvelle à essayer (« pending »). Elle la lance dans un
processus à part, attend qu'elle se déclare en bonne santé (serveur prêt depuis
HEALTHY_AFTER secondes), puis l'accompagne jusqu'à sa fermeture :

- si la nouvelle version s'arrête avant, la précédente démarre aussitôt à sa
  place ; la nouvelle sera réessayée une fois, plus tard, puis écartée ;
- si une version sort avec le code RESTART (une plus récente vient d'être
  installée), le choix recommence.

Rien de tout cela dans une copie de développement (dépôt git) : on y fait tourner
le code qu'on écrit. AIODOS_NO_UPDATE=1 le désactive aussi.

Ce code tourne dans la version d'origine, qui ne change jamais : il doit rester
simple, et ce qu'il attend des versions lancées, stable (variables CHILD_ENV,
READY_ENV, TRIAL_ENV, BASE_ENV ; codes de sortie RESTART et ALREADY_RUNNING).

Revenir à la version d'origine n'est pas anodin : elle tourne sur les données d'une
version plus récente. Une 2.0.x ne connaît pas les niveaux de confiance (amis, machines
mises de côté) et traiterait tout pair comme une de mes machines ; là où les clés des
paires sont en clair (Termux, Linux), elle s'en servirait. friends_refused() le dit à la
version lancée (UpdateService.status : « friends_refused ») ; l'appairage d'un ami, et
l'accueil d'un ami présenté par mes machines, doivent le consulter et refuser tant que ce
nœud n'est pas réinstallé depuis une version récente (network/pairing.py,
network/group.py). Et quand le lanceur reprend la main parce que la version courante ne
démarre pas, il le note (avis « rollback ») au lieu de se taire.
"""
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from aiodos import __version__, instance, paths
from aiodos.updates import state
from aiodos.updates import version as versions
from aiodos.updates.store import Store

log = logging.getLogger("aiodos.updates")

CHILD_ENV = "AIODOS_UPDATE_CHILD"      # « 1 » : version lancée par le lanceur
READY_ENV = "AIODOS_UPDATE_READY"      # fichier que la version crée une fois en bonne santé
TRIAL_ENV = "AIODOS_UPDATE_TRIAL"      # « 1 » : premier démarrage, pas de message d'erreur
BASE_ENV = "AIODOS_BASE_DIR"           # dossier de la version d'origine
RESTART = 75                           # code de sortie : relancer (nouvelle version prête)
ALREADY_RUNNING = 3                    # code de sortie : AIODOS déjà ouvert
MAX_ATTEMPTS = 2
HEALTHY_AFTER = 10
START_TIMEOUT = 240                    # pas en bonne santé au-delà : arrêtée, écartée
MAX_RESTARTS = 5                       # par lancement : garde-fou contre une boucle
TRUST_SINCE = "2.0.2-dev"              # première version qui connaît les amis (2.0.0, 2.0.1 : non)
# Ce qui le prouve dans son code : des copies « 2.0.2-dev » ont précédé les amis (804bd7c)
TRUST_MARK = ("aiodos/network/sharing.py", 'FRIEND = "friend"')


def developer_checkout() -> bool:
    """Dépôt git : le code est celui qu'on écrit, jamais remplacé."""
    return (paths.APP_ROOT / ".git").exists()


def supervised() -> bool:
    """Cette version a-t-elle été lancée par le lanceur d'une version d'origine ?"""
    return os.environ.get(CHILD_ENV) == "1"


def base_dir() -> Path:
    return Path(os.environ.get(BASE_ENV) or paths.APP_ROOT)


def base_version() -> str | None:
    """Numéro de la version d'origine, celle qui reprendrait la main (lancée par un
    lanceur), ou None."""
    if not supervised():
        return None
    from aiodos.updates import install           # pas au chargement : le lanceur reste léger
    return install.declared_version(base_dir())


def knows_friends(folder: Path) -> bool:
    """Le code de `folder` connaît-il les niveaux de confiance ? Lu dans le code même
    (TRUST_MARK) : le numéro seul ne suffit pas, des copies « 2.0.2-dev » les ignoraient."""
    name, mark = TRUST_MARK
    try:
        return mark in (Path(folder) / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def friends_refused() -> str | None:
    """Pourquoi ce nœud ne doit pas appairer d'ami (ni en accueillir un présenté par mes
    machines), ou None. Sa version d'origine ne connaît pas les niveaux de confiance : si
    elle reprenait la main (version courante qui ne démarre pas, trop de redémarrages),
    elle traiterait l'ami comme une de mes machines. Version d'origine illisible : refusé
    aussi, par prudence. Sous Windows, rien à craindre : la clé de la paire est scellée
    (node/vault.py, DPAPI), et une 2.0.x ne peut rien en faire."""
    if not supervised():
        return None
    from aiodos.node import vault
    if vault.available():
        return None
    base = base_version()
    if base is not None and not versions.newer(TRUST_SINCE, base) and knows_friends(base_dir()):
        return None
    shown = f"AIODOS {base}" if base else "une version d'AIODOS illisible ici"
    return (f"Ce nœud a été installé avec {shown}, qui ne connaît pas les amis : si elle "
            "reprenait la main, elle traiterait un ami comme une de tes machines. Mets AIODOS "
            "à jour sur ce nœud en le réinstallant depuis une version récente (installeur, "
            "ou archive), puis appaire ton ami.")


def child_argv(argv: list) -> list:
    """Arguments transmis à une version lancée : sans --data-dir, dont le chemin relatif
    changerait de sens dans un autre dossier (AIODOS_DATA_DIR le remplace)."""
    result, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--data-dir":
            skip = True
        elif not arg.startswith("--data-dir="):
            result.append(arg)
    return result


def choose(data: dict, installed: list, base_version: str) -> list:
    """[(version, essai ?)] dans l'ordre où les lancer ; la version d'origine n'y figure
    pas (elle vient en dernier, dans ce processus-ci)."""
    refused = data.get("refused") or {}
    plan, best = [], base_version
    current = data.get("current")
    if current in installed and current not in refused and versions.newer(current, best):
        plan.append((current, False))
        best = current
    pending = data.get("pending") or {}
    candidate = pending.get("version")
    if (candidate in installed and candidate not in refused and versions.newer(candidate, best)
            and int(pending.get("attempts") or 0) < MAX_ATTEMPTS):
        plan.insert(0, (candidate, True))
    return plan


def dispatch(argv: list, store: Store | None = None, base_version: str = __version__):
    """Lance la version qu'il faut. None : c'est celle-ci qui doit tourner, dans ce
    processus. Sinon : le code de sortie de la version lancée, une fois fermée."""
    if supervised() or developer_checkout() or os.environ.get("AIODOS_NO_UPDATE"):
        return None
    store = store or Store()
    restarts = 0
    while True:
        if instance.running():
            return None                   # « déjà ouvert » : dit par la suite du démarrage
        outcome, wanted = None, None
        for wanted, trial in choose(state.load(store.root), store.installed(), base_version):
            outcome = run(store, wanted, trial, argv)
            if outcome is not None:
                break
        if outcome != RESTART:
            return outcome
        restarts += 1
        if restarts > MAX_RESTARTS:
            log.warning("Trop de redémarrages d'affilée : la version d'origine reprend la main")
            _note_fallback(store, wanted, "trop de redémarrages d'affilée")
            return None


def _child_env(ready: Path, trial: bool) -> dict:
    """Environnement d'une version lancée. Le compagnon Internet, elle le trouve seule
    (network/companion.locate) : le sien d'abord, celui de la version d'origine en
    dernier recours (BASE_ENV). Les lanceurs 2.0.x, eux, mettent dans AIODOS_NET_BIN
    celui de la version d'origine : locate le reconnaît et le fait passer après."""
    env = {k: v for k, v in os.environ.items() if k not in (READY_ENV, TRIAL_ENV)}
    env.update({CHILD_ENV: "1", READY_ENV: str(ready), BASE_ENV: str(paths.APP_ROOT),
                "AIODOS_DATA_DIR": str(paths.data_dir())})
    if trial:
        env[TRIAL_ENV] = "1"
    return env


def run(store: Store, wanted: str, trial: bool, argv: list):
    """Lance une version et l'accompagne jusqu'à sa fermeture : son code de sortie, ou
    None si elle n'a pas démarré (la suivante sera essayée)."""
    folder = store.version_dir(wanted)
    ready = store.root / f"ready-{os.getpid()}"
    ready.unlink(missing_ok=True)
    if trial:
        state.update(store.root, lambda d: _count_attempt(d, wanted))
    log.info("Lancement de la version %s%s", wanted, " (essai)" if trial else "")
    started = time.monotonic()
    try:
        process = subprocess.Popen([sys.executable, "-m", "aiodos", *child_argv(argv)],
                                   cwd=folder, env=_child_env(ready, trial))
    except OSError as exc:
        return _failed(store, wanted, trial, f"lancement impossible ({exc})")
    healthy = False
    try:
        while process.poll() is None:
            if ready.exists():
                healthy = True
                if trial:
                    _promote(store, wanted)
                process.wait()            # elle tourne seule : on attend sa fermeture
                break
            if time.monotonic() - started > START_TIMEOUT:
                process.kill()
                process.wait()
                return _failed(store, wanted, trial, "pas prête après "
                                                     f"{START_TIMEOUT} s")
            time.sleep(0.2)
        if not healthy and ready.exists():    # prête, puis fermée entre deux coups d'œil
            healthy = True
            if trial:
                _promote(store, wanted)
    except KeyboardInterrupt:            # Ctrl+C dans la console : elle l'a reçu aussi
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0
    finally:
        ready.unlink(missing_ok=True)
    code = process.returncode
    if healthy or code in (0, RESTART):
        if trial and not healthy:
            _promote(store, wanted)       # fermée tôt, mais proprement
        return code
    if code == ALREADY_RUNNING:
        return code
    return _failed(store, wanted, trial, f"arrêtée au démarrage (code {code})")


def _count_attempt(data: dict, wanted: str):
    pending = data.get("pending") or {}
    if pending.get("version") == wanted:
        pending["attempts"] = int(pending.get("attempts") or 0) + 1


def _promote(store: Store, wanted: str):
    """L'essai est réussi : cette version devient la version courante."""
    def change(data):
        pending = data.get("pending") or {}
        data["current"] = wanted
        if pending.get("version") == wanted:
            data["pending"] = None
            data["notice"] = {"kind": "updated", "version": wanted,
                              "previous": pending.get("previous"),
                              "notes": pending.get("notes") or "", "at": time.time()}
    state.update(store.root, change)
    log.info("Version %s en service", wanted)


def _failed(store: Store, wanted: str, trial: bool, reason: str):
    log.warning("La version %s n'a pas démarré : %s", wanted, reason)
    if trial:
        def change(data):
            pending = data.get("pending") or {}
            if pending.get("version") != wanted:
                return
            if int(pending.get("attempts") or 0) >= MAX_ATTEMPTS:
                state.refuse(data, wanted, f"Ne démarre pas sur cette machine ({reason}).")
                data["notice"] = {"kind": "rollback", "version": wanted, "reason": reason,
                                  "at": time.time()}
            else:
                pending["failed_at"] = time.time()
        state.update(store.root, change)
    else:
        _note_fallback(store, wanted, reason)  # la version d'origine va reprendre la main
    return None


def _note_fallback(store: Store, wanted: str | None, reason: str):
    """La version courante n'a pas tenu : dit à l'utilisateur (avis « rollback ») que la
    version d'origine reprend la main, au lieu de le taire. Elle sera réessayée au
    prochain lancement."""
    if not wanted:
        return
    state.update(store.root, lambda data: data.update(notice={
        "kind": "rollback", "version": wanted, "reason": reason, "at": time.time()}))


def confirm_when_healthy(alive):
    """Dans une version lancée par le lanceur : se déclarer en bonne santé après
    HEALTHY_AFTER secondes de service (alive() dit si le nœud tourne toujours)."""
    path = os.environ.get(READY_ENV)
    if not supervised() or not path:
        return

    def confirm():
        time.sleep(HEALTHY_AFTER)
        if alive():
            try:
                Path(path).write_text(__version__, encoding="utf-8")
            except OSError as exc:
                log.warning("Lanceur injoignable (%s)", exc)
            os.environ.pop(TRIAL_ENV, None)   # essai réussi : les erreurs se montrent de nouveau

    threading.Thread(target=confirm, daemon=True, name="aiodos-sante").start()
