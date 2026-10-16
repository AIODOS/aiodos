"""Service de mise à jour du nœud.

Toutes les CHECK_EVERY secondes, ou sur demande : si une machine appairée en ligne
annonce une version plus récente que tout ce que cette machine a, on la télécharge
(p2p.py), on installe la plus récente reçue (install.py), puis on redémarre dessus
quand la machine est libre :

- aucun calcul en cours ici ou pour une autre machine, aucune unité confiée
  dehors, aucune conversation ;
- fenêtre ouverte sous Windows : personne au clavier depuis le délai d'inactivité
  choisi pour le partage de puissance. Ailleurs, avec une fenêtre (inactivité non
  mesurable), la nouvelle version attend le prochain démarrage, ou le bouton
  « Redémarrer maintenant ».

Une copie de développement (dépôt git) reçoit et transmet les versions, mais ne les
installe jamais.
"""
import logging
import threading
import time

from aiodos import __version__, settings
from aiodos.jobs import queue
from aiodos.network import peers
from aiodos.network.client import PeerError
from aiodos.node import identity
from aiodos.updates import install, launcher, p2p, restart, state
from aiodos.updates import version as versions
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

log = logging.getLogger("aiodos.updates")

CHECK_EVERY = 30
PEER_RETRY = 600                 # machine qui n'a pas fourni une version : pas avant 10 min
TRIAL_RETRY = 3600               # version qui n'a pas démarré : second essai une heure après
INSTALL_RETRY = 86400            # refusée à l'installation (paquet Python absent…) : le lendemain
MAX_RESTARTS_PER_HOUR = 3
PROGRESS_EVERY = 0.5


def refused(data: dict, wanted) -> bool:
    entry = (data.get("refused") or {}).get(wanted)
    if not entry:
        return False
    retry_after = entry.get("retry_after")
    return not retry_after or time.time() < retry_after


class UpdateService:
    def __init__(self, bus, executor, network, windowed=False, store: Store | None = None):
        self.bus = bus
        self.executor = executor
        self.network = network
        self.windowed = windowed
        self.store = store or Store()
        self.available = None            # {"version", "from"} : vue chez une machine appairée
        self.download = None             # {"version", "from", "received", "size"}
        self.installing = None
        self.error = None
        self._failures = {}              # (machine, version) -> prochain essai (monotonic)
        self._published = 0.0
        self._wake = threading.Event()
        self._stopping = False

    # ------------------------------------------------------------ cycle de vie
    # Un fil à part, qui ne retient pas la fermeture d'AIODOS : un essai à blanc peut
    # durer une minute sur un téléphone, et personne ne doit attendre sa fin pour fermer.
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="aiodos-updates").start()

    def stop(self):
        self._stopping = True
        self._wake.set()

    def check_now(self):
        """Vérifier sans attendre (appelable depuis n'importe quel thread)."""
        self._wake.set()

    def _run(self):
        self._tidy()
        while not self._stopping:
            try:
                self.tick()
            except Exception:
                log.exception("Erreur du service de mise à jour")
            self._wake.wait(CHECK_EVERY)
            self._wake.clear()

    def tick(self):
        if not settings.get("auto_update"):
            return
        found = self._best_offer()
        if found:
            self._obtain(*found)
        if launcher.developer_checkout():
            return
        self._install_newest()
        if self._should_restart():
            restart.request()

    # ------------------------------------------------------------- réception
    def _best_offer(self):
        """(machine, version, nom) : la version la plus récente annoncée par une machine
        appairée en ligne, si elle l'est plus que tout ce qu'on a."""
        data = state.load(self.store.root)
        have = versions.newest(__version__, self.store.offer(),
                               (data.get("pending") or {}).get("version"))
        best, now = None, time.monotonic()
        for peer in peers.list_peers():
            offered = ((peer.get("profile") or {}).get("updates") or {}).get("offer")
            if not peer["online"] or not versions.newer(offered, have) or refused(data, offered):
                continue
            if now < self._failures.get((peer["node_id"], offered), 0):
                continue
            if best is None or versions.newer(offered, best[1]):
                best = (peer["node_id"], offered, peer["name"])
        available = {"version": best[1], "from": best[2]} if best else None
        if available != self.available:
            self._set(available=available)
        return best

    def _obtain(self, node_id: str, wanted: str, name: str):
        peer = peers.get(node_id)
        if not peer:
            return
        self._set(download={"version": wanted, "from": name, "received": 0, "size": 0})

        def progress(received, size):
            self.download = {"version": wanted, "from": name, "received": received, "size": size}
            self._publish(throttle=True)
        try:
            p2p.fetch(self.network.node_id, peer, wanted, self.store, progress)
        except (PeerError, UpdateError, OSError, ValueError) as exc:
            self._failures[(node_id, wanted)] = time.monotonic() + PEER_RETRY
            log.warning("Version %s non reçue de « %s » : %s", wanted, name, exc)
            self._set(download=None, error=f"Version {wanted} non reçue de « {name} » : {exc}")
            return
        log.info("Version %s reçue de « %s »", wanted, name)
        self._set(download=None, available=None, error=None)

    # ----------------------------------------------------------- installation
    def _install_newest(self):
        data = state.load(self.store.root)
        candidate = self.store.offer()
        if not versions.newer(candidate, __version__) or refused(data, candidate):
            return
        if candidate in self.store.installed() and candidate in (
                data.get("current"), (data.get("pending") or {}).get("version")):
            return
        found = self.store.manifest(candidate)
        if found is None:
            return
        self._set(installing=candidate)
        try:
            install.install(self.store, found)
        except (UpdateError, OSError) as exc:
            log.warning("Version %s non installée : %s", candidate, exc)

            def remember(d):
                state.refuse(d, candidate, str(exc))
                d["refused"][candidate]["retry_after"] = time.time() + INSTALL_RETRY
            state.update(self.store.root, remember)
            self._set(installing=None, error=f"Version {candidate} non installée : {exc}")
            return

        def ready(d):
            (d.get("refused") or {}).pop(candidate, None)
            d["pending"] = {"version": candidate, "attempts": 0, "previous": __version__,
                            "notes": str(found.get("notes") or "")[:500]}
        data = state.update(self.store.root, ready)
        log.info("Version %s installée : elle démarrera au prochain redémarrage", candidate)
        self.store.prune(self._keep(data))
        self._set(installing=None, error=None)

    def _keep(self, data: dict) -> set:
        return {v for v in (data.get("current"), (data.get("pending") or {}).get("version"),
                            __version__) if v}

    def _tidy(self):
        self.store.prune(self._keep(state.load(self.store.root)))

    # ------------------------------------------------------------- redémarrage
    def ready(self, data: dict | None = None):
        """Version installée qu'un redémarrage lancerait, ou None."""
        if launcher.developer_checkout():
            return None
        data = state.load(self.store.root) if data is None else data
        pending = data.get("pending") or {}
        target = pending.get("version")
        if (not versions.newer(target, __version__) or target not in self.store.installed()
                or int(pending.get("attempts") or 0) >= launcher.MAX_ATTEMPTS):
            return None
        return target

    def _free(self) -> bool:
        ex = self.executor
        if ex.interactive or any(ex.busy.values()) or ex.remote or queue.remote_counts():
            return False
        if self.windowed:
            idle = identity.idle_seconds()
            return idle is not None and idle >= 60 * int(settings.get("idle_minutes") or 10)
        return True

    def _should_restart(self) -> bool:
        data = state.load(self.store.root)
        if not self.ready(data):
            return False
        if time.time() - float((data.get("pending") or {}).get("failed_at") or 0) < TRIAL_RETRY:
            return False
        recent = [t for t in data.get("restarts") or [] if time.time() - t < 3600]
        if len(recent) >= MAX_RESTARTS_PER_HOUR or not self._free():
            return False
        self._note_restart()
        return True

    def _note_restart(self):
        def change(d):
            recent = [t for t in d.get("restarts") or [] if time.time() - t < 3600]
            d["restarts"] = recent + [time.time()]
        state.update(self.store.root, change)

    def restart_now(self) -> bool:
        """Bouton « Redémarrer maintenant »."""
        if not self.ready():
            return False
        self._note_restart()
        restart.request()
        return True

    # --------------------------------------------------------------- interface
    def notice_seen(self):
        state.update(self.store.root, lambda d: d.update(notice=None))

    def status(self) -> dict:
        data = state.load(self.store.root)
        measurable = not self.windowed or identity.idle_seconds() is not None
        return {
            "version": __version__,
            "developer": launcher.developer_checkout(),
            "auto": bool(settings.get("auto_update")),
            "offer": self.store.offer(),          # ce que cette machine transmet
            "available": self.available,
            "download": self.download,
            "installing": self.installing,
            "ready": self.ready(data),
            "restart_when": "idle" if measurable else "next_start",
            "error": self.error,
            "notice": data.get("notice"),
        }

    def _set(self, **changes):
        for name, value in changes.items():
            setattr(self, name, value)
        self._publish()

    def _publish(self, throttle=False):
        now = time.monotonic()
        if throttle and now - self._published < PROGRESS_EVERY:
            return
        self._published = now
        self.bus.publish({"type": "updates"})
