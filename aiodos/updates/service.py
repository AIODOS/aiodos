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

Le compagnon Internet de la plateforme (binaries.py) se demande à part, aux machines
qui annoncent l'avoir : pour la version reçue, avant de redémarrer dessus, et pour
celle qui tourne, si elle l'attend encore (installée par une machine 2.0.x, qui ne
prend que l'archive du code, ou depuis une archive). Arrivé alors qu'un autre programme
tourne, il le remplace dès qu'aucun travail ne passe par le compagnon.

Une copie de développement (dépôt git) reçoit et transmet les versions, mais ne les
installe jamais. L'appli Android, elle, ne reçoit rien : son code et ses programmes sont
ceux de l'appli, qu'une nouvelle appli remplace (node/android_app.py).

Aucune machine appairée ne peut geler ce service : la réception tourne dans un fil à part
(une machine lente ne retient ni l'installation d'une version déjà reçue, ni le
redémarrage), chaque transfert a son échéance (p2p.py), une machine qui n'a pas fourni ce
qu'elle annonçait est laissée de côté PEER_RETRY secondes, quel que soit le numéro
qu'elle annonce ensuite, et la suivante est essayée aussitôt : mes machines avant les
amis, pour une version comme pour le compagnon et les moteurs (_sources), et un fichier
commencé chez une machine ne s'achève pas chez une autre, mais reprend chez elle quand
elle revient (store.partial_from : un début par machine). Son profil
est lu sans rien supposer de sa forme, et chaque étape du tour est isolée : l'erreur de
l'une s'affiche (status) sans empêcher les autres.
"""
import logging
import threading
import time

from aiodos import __version__, i18n, settings
from aiodos.i18n import t
from aiodos.jobs import queue
from aiodos.network import peers, sharing
from aiodos.node import android_app, identity
from aiodos.updates import binaries, install, launcher, p2p, platforms, restart, state
from aiodos.updates import manifest as manifests
from aiodos.updates import version as versions
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

log = logging.getLogger("aiodos.updates")

CHECK_EVERY = 30
PEER_RETRY = 600                 # machine qui n'a pas fourni ce qu'elle annonce : pas avant 10 min
RECEIVE_WAIT = 20                # le tour attend la réception au plus ce temps, puis continue
MAX_SOURCES = 3                  # machines essayées au plus, par tour, pour une version
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
        self._file_error = None          # (what, erreur) : la dernière d'un fichier non reçu
        self._failures = {}              # machine -> prochain essai (monotonic)
        self._receiving = None           # fil de la réception en cours
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
        self._step("updates.step_tidy", self._tidy)
        while not self._stopping:
            try:
                self.tick()
            except Exception:
                log.exception("Erreur du service de mise à jour")
            self._wake.wait(CHECK_EVERY)
            self._wake.clear()

    def tick(self):
        if not settings.get("auto_update") or android_app.in_app():
            return
        self._receive()                   # relance aussi le compagnon, une fois tout reçu
        if launcher.developer_checkout():
            return
        self._step("updates.step_install", self._install_newest)
        if self._step("updates.step_restart", self._should_restart):
            restart.request()

    def _step(self, what: str, step, **params):
        """Une étape du tour, qui ne peut empêcher ni les suivantes ni les tours suivants :
        son erreur se journalise et s'affiche (status). what : la clé du nom de l'étape
        (updates.step_*), avec ses paramètres."""
        try:
            return step()
        except Exception as exc:
            with i18n.using(i18n.SOURCE):            # au journal, son nom en français
                named = t(what, **params)
            log.exception("Service de mise à jour, %s", named)
            self._set(error=t("updates.step_failed", step=t(what, **params), error=exc))
            return None

    # ------------------------------------------------------------- réception
    def _receive(self):
        """Réception dans un fil à part, attendue au plus RECEIVE_WAIT secondes ; encore en
        cours au tour suivant, elle continue sans qu'on en lance une autre."""
        running = self._receiving
        if running is None or not running.is_alive():
            running = threading.Thread(target=self._receive_all, daemon=True,
                                       name="aiodos-updates-reception")
            self._receiving = running
            running.start()
        running.join(RECEIVE_WAIT)

    def _receive_all(self):
        self._step("updates.step_receive", self._obtain_newest)
        self._obtain_companion()
        self._obtain_engine()
        # Seul endroit où le compagnon est relancé : pas pendant la réception, qui peut
        # passer par lui, et une seule fois par tour (sa relance est asynchrone, et il reste
        # « dépassé » d'ici là)
        self._step("updates.step_companion_restart", self._switch_companion)

    def _receiving_elsewhere(self) -> bool:
        """La réception tourne, dans un autre fil que celui-ci."""
        running = self._receiving
        return (running is not None and running.is_alive()
                and running is not threading.current_thread())

    @staticmethod
    def _rank(peer: dict) -> int:
        """Ordre où demander à une machine : 0 pour mes machines, 1 pour un ami ou une
        machine au niveau de confiance inconnu (comme sharing.level), 2 pour une machine que
        l'utilisateur a mise de côté."""
        if peer.get("set_aside_at"):
            return 2
        return 0 if peer.get("trust") == sharing.MINE else 1

    def _sources(self) -> list:
        """Machines appairées en ligne qu'on n'a pas laissées de côté (_failures), dans
        l'ordre où leur demander une version ou un fichier (_rank) ; à rang égal, celui de
        peers.list_peers (le nom). Un ami nommé « AAA » ne passe donc pas devant mes
        machines, ni pour une version ni pour le compagnon ou un moteur."""
        found, now = [], time.monotonic()
        for peer in peers.list_peers():
            try:
                if peer.get("online") and now >= self._failures.get(peer["node_id"], 0):
                    found.append((self._rank(peer), peer))
            except Exception:                 # une machine ne peut pas empêcher les autres
                log.exception("Machine appairée illisible")
        return [peer for _, peer in sorted(found, key=lambda f: f[0])]

    def _offers(self) -> list:
        """[(machine, version, nom)] : les versions plus récentes que tout ce qu'on a,
        annoncées par des machines appairées en ligne qu'on n'a pas laissées de côté ; dans
        l'ordre de _sources (mes machines d'abord), puis la plus récente."""
        data = state.load(self.store.root)
        have = versions.newest(__version__, self.store.offer(),
                               (data.get("pending") or {}).get("version"))
        found = []
        for peer in self._sources():
            try:
                offered = p2p.offered_by(peer)
                if not versions.newer(offered, have) or refused(data, offered):
                    continue
                found.append(((self._rank(peer), tuple(-n for n in versions.key(offered))),
                              (peer["node_id"], offered, str(peer.get("name") or ""))))
            except Exception:                 # une machine ne peut pas empêcher les autres
                log.exception("Profil illisible d'une machine appairée")
        offers = [offer for _, offer in sorted(found, key=lambda f: f[0])]
        available = {"version": offers[0][1], "from": offers[0][2]} if offers else None
        if available != self.available:
            self._set(available=available)
        return offers

    def _obtain_newest(self):
        """Une version plus récente, chez la première machine qui la donne (dans l'ordre de
        _offers) : une qui échoue est laissée de côté, la suivante essayée aussitôt
        (MAX_SOURCES au plus par tour)."""
        for node_id, wanted, name in self._offers()[:MAX_SOURCES]:
            if self._obtain(node_id, wanted, name):
                return

    def _obtain(self, node_id: str, wanted: str, name: str) -> bool:
        peer = peers.get(node_id)
        if not peer:
            return False
        self._set(download={"version": wanted, "from": name, "received": 0, "size": 0})

        def progress(received, size):
            self.download = {"version": wanted, "from": name, "received": received, "size": size}
            self._publish(throttle=True)
        try:
            p2p.fetch(self.network.node_id, peer, wanted, self.store, progress)
        except Exception as exc:              # quelle qu'en soit la cause : machine de côté
            self._failures[node_id] = time.monotonic() + PEER_RETRY
            log.warning("Version %s non reçue de « %s » : %s", wanted, name, exc)
            self._set(download=None, error=t("updates.version_not_received", version=wanted,
                                             name=name, error=exc))
            return False
        log.info("Version %s reçue de « %s »", wanted, name)
        self._set(download=None, available=None, error=None)
        return True

    # ------------------------------------ programmes livrés pour la plateforme
    def _obtain_companion(self):
        """Compagnon Internet de cette plateforme, s'il manque."""
        self._step("updates.step_companion",
                   lambda: self._obtain_program(manifests.COMPANION, "compagnon"))

    def _obtain_engine(self):
        """Moteurs de cette plateforme (llama.cpp pour les gros modèles répartis,
        stable-diffusion.cpp pour les images), s'ils manquent. Ils restent les mêmes d'une
        version à l'autre : déjà reçus, ils sont copiés (store.reuse)."""
        for engine in manifests.ENGINES:
            self._step("updates.step_engine",
                       lambda engine=engine: self._obtain_program(engine, f"moteur {engine}"),
                       engine=engine)

    def _obtain_program(self, program: str, what: str):
        """Programme `program` de cette plateforme, s'il manque, pour la version reçue la
        plus récente et pour celle qui tourne (manifeste compris, s'il manque aussi).
        what : son nom au journal, et pour l'interface (download, « what »)."""
        kind = platforms.kind(program)
        if kind is None:
            return
        offer = self.store.offer()
        if versions.newer(__version__, offer):
            offer = None                  # plus ancienne que celle qui tourne : inutile ici
        for wanted in dict.fromkeys(v for v in (offer, __version__) if v):
            found = self.store.manifest(wanted)
            item = manifests.binary(found, program) if found else None
            if found is not None and (item is None or self.store.has(wanted, item)):
                continue                  # pas livré pour cette plateforme, ou déjà là
            if item is not None and self.store.reuse(wanted, item):
                continue                  # le même, reçu avec une autre version
            holders = [p for p in self._sources() if kind in p2p.held_by(p, wanted)]
            for peer in holders[:MAX_SOURCES]:
                if self._obtain_file(peer["node_id"], str(peer.get("name") or ""), wanted,
                                     kind, what, program):
                    break

    def _obtain_file(self, node_id: str, name: str, wanted: str, kind: str,
                     what: str = "compagnon", program: str = manifests.COMPANION) -> bool:
        peer = peers.get(node_id)
        if not peer:
            return False
        label = what[:1].upper() + what[1:]
        self._set(download={"version": wanted, "from": name, "received": 0, "size": 0,
                            "what": what})

        def progress(received, size):
            self.download = {"version": wanted, "from": name, "received": received,
                             "size": size, "what": what}
            self._publish(throttle=True)
        try:
            p2p.fetch_file(self.network.node_id, peer, wanted, kind, self.store, progress)
        except Exception as exc:              # quelle qu'en soit la cause : machine de côté
            self._failures[node_id] = time.monotonic() + PEER_RETRY
            log.warning("%s de la version %s non reçu de « %s » : %s", label, wanted, name, exc)
            if program == manifests.COMPANION:
                error = t("updates.companion_not_received", version=wanted, name=name,
                          error=exc)
            else:
                error = t("updates.engine_not_received", engine=program, version=wanted,
                          name=name, error=exc)
            self._file_error = (what, error)
            self._set(download=None, error=error)
            return False
        log.info("%s de la version %s reçu de « %s »",
                 "Compagnon Internet" if what == "compagnon" else label, wanted, name)
        # L'erreur affichée disait que ce programme n'arrivait pas : il est là, elle s'efface
        error = None if self._file_error == (what, self.error) else self.error
        self._set(download=None, error=error)
        return True

    def _switch_companion(self):
        """Compagnon livré arrivé alors qu'un autre programme tourne (celui du PATH, d'une
        version plus ancienne…) : relancé dessus, quand aucun travail ne passe par lui. Pas
        pendant la réception, qui peut passer par lui (un moteur reçu par Internet juste
        après le compagnon) : la couper mettrait de côté une machine honnête. Elle le
        relance elle-même en fin de course (_receive_all), une fois par tour : appelé aussi
        par tick, il l'était deux fois de suite, sa relance étant asynchrone."""
        internet = getattr(self.network, "internet", None)
        companion = getattr(internet, "companion", None)
        if companion is None or self._receiving_elsewhere() or not self._quiet() \
                or not companion.outdated():
            return
        log.info("Relance du compagnon Internet sur un programme plus approprié")
        internet.relaunch_from_thread()

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
            self._set(installing=None, error=t("updates.not_installed", version=candidate,
                                               error=exc))
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
        """Versions dont on garde le paquet ou le dossier : courante, en attente, celle qui
        tourne, la version d'origine (son compagnon, si elle reprend la main) et celle du
        compagnon en service (dernier recours d'une version arrivée sans le sien)."""
        base = install.declared_version(launcher.base_dir()) if launcher.supervised() else None
        return {v for v in (data.get("current"), (data.get("pending") or {}).get("version"),
                            __version__, base, binaries.active(self.store).get("version"))
                if v}

    def _tidy(self):
        """Au démarrage : manifestes gardés gonflés par une version plus ancienne remis sous
        leur forme canonique (ils sont relus à chaque store.manifest()), puis ménage."""
        rewritten = self.store.tidy_manifests()
        if rewritten:
            log.info("%d manifeste(s) remis sous leur forme canonique", rewritten)
        self.store.prune(self._keep(state.load(self.store.root)))

    # ------------------------------------------------------------- redémarrage
    def ready(self, data: dict | None = None):
        """Version installée qu'un redémarrage lancerait, ou None."""
        if launcher.developer_checkout() or android_app.in_app():
            return None
        data = state.load(self.store.root) if data is None else data
        pending = data.get("pending") or {}
        target = pending.get("version")
        if (not versions.newer(target, __version__) or target not in self.store.installed()
                or int(pending.get("attempts") or 0) >= launcher.MAX_ATTEMPTS):
            return None
        return target

    def _quiet(self) -> bool:
        """Aucun calcul ici ou pour une autre machine, aucune unité confiée dehors, aucune
        conversation."""
        ex = self.executor
        return not (ex.interactive or any(ex.busy.values()) or ex.remote
                    or queue.remote_counts())

    def _free(self) -> bool:
        if not self._quiet():
            return False
        if self.windowed:
            idle = identity.idle_seconds()
            return idle is not None and idle >= 60 * int(settings.get("idle_minutes") or 10)
        return True

    def _fetching(self, version: str) -> bool:
        """Un fichier de `version` (son compagnon, un moteur) arrive en ce moment : le
        redémarrage sur elle l'attend. C'est borné : chaque transfert a son échéance."""
        running, download = self._receiving, self.download
        return bool(running is not None and running.is_alive() and download
                    and download.get("version") == version)

    def _should_restart(self) -> bool:
        data = state.load(self.store.root)
        target = self.ready(data)
        if not target or self._fetching(target):
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
            "android_app": android_app.in_app(),
            "auto": bool(settings.get("auto_update")),
            "offer": self.store.offer(),          # ce que cette machine transmet
            "available": self.available,
            "download": self.download,
            "installing": self.installing,
            "ready": self.ready(data),
            "restart_when": "idle" if measurable else "next_start",
            "error": self.error,
            "notice": data.get("notice"),
            # Version d'origine qui ignore les amis (launcher.friends_refused) : à dire,
            # pour les amis déjà appairés, tant que ce nœud n'est pas réinstallé
            "friends_refused": launcher.friends_refused(),
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
