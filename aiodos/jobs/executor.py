"""Exécuteur local : vide la file en respectant les ressources de la machine.

Tourne dans la boucle asyncio du nœud ; chaque unité s'exécute dans un thread de
fond, à priorité abaissée (node/priority.py : les tâches sont bloquantes : réseau,
fichiers, modèles). Pendant une conversation, aucune nouvelle unité GPU ne démarre,
et celles en cours attendent entre deux lots : l'interactif d'abord. Sur batterie,
moins d'unités à la fois, ou aucune, selon le réglage (jobs/pacing.py).

Les ressources sont partagées avec le calcul fait ici pour d'autres machines
(begin_remote / end_remote, cf. network.worker) ; l'exécuteur surveille aussi
les baux des unités que ce nœud a confiées ailleurs (cf. jobs.leases).

Chaque unité calculée ici reçoit son arrêt (stop), comme celles calculées pour une autre
machine : il est levé quand son travail est annulé ou supprimé (vue Tâches, création
retirée, réindexation remplacée ; vu à chaque tour de la boucle), et quand AIODOS
s'arrête. La lecture d'une page, une transcription, une image s'interrompent alors au lieu
d'aller au bout (une page lente jusqu'à son délai total, une longue vidéo jusqu'à sa fin),
et ce qu'elles calculaient n'entre pas dans les bibliothèques d'un travail annulé : l'unité
retourne en file sans compter d'essai. Une pause « après l'unité en cours » la laisse finir ;
une pause « tout de suite » (du travail, ou de toute la file) l'arrête et la rend de même.
Sur batterie en « attendre le secteur », une unité en cours est rendue de même au lot
suivant (pacing.wait_turn rend toute unité qui a un arrêt) : refaite plus tard, au retour
du secteur, ou par une de mes machines qui la demande.
"""
import asyncio
import logging
import threading
import time

import requests

from aiodos.jobs import leases, pacing, queue, registry
from aiodos.jobs.models import DEFAULT_SLOTS, GPU_BOUND
from aiodos.node import priority
from aiodos.services import ollama

log = logging.getLogger("aiodos.executor")

ENGINE_RETRY_DELAY = 20      # secondes de pause d'une ressource quand Ollama ne répond pas
LEASE_CHECK_EVERY = 10       # vérification des baux échus (unités confiées ailleurs)


def engine_down(exc) -> bool:
    """Vrai si l'erreur vient d'Ollama injoignable (arrêté, en redémarrage, en
    mise à jour…) : passager, l'unité n'y est pour rien. Une page web
    introuvable, elle, reste une vraie erreur."""
    if not isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return False
    url = getattr(getattr(exc, "request", None), "url", "") or ""
    return url.startswith(ollama.base())


def _run_unit(task, payload, stop=None) -> tuple:
    """Une unité de la file (thread de fond), et le temps passé à attendre ou à se reposer
    (jobs/pacing.py) : il ne compte pas dans le temps de calcul. stop : son arrêt (travail
    annulé ou supprimé, AIODOS qui s'arrête), transmis au calcul ; déjà levé, l'unité ne
    commence pas (Interrupted). Appelée sans arrêt (hors de l'exécuteur), run_local l'est
    aussi."""
    if stop is not None and stop.is_set():
        raise registry.Interrupted()
    before = pacing.waited()
    if stop is None:
        result = registry.run_local(task, payload)
    else:
        result = registry.run_local(task, payload, stop)
    return result, pacing.waited() - before


def _withdrawn(job_ids) -> set:
    """Parmi ces travaux, ceux annulés, supprimés, ou mis en pause « tout de suite » (eux
    ou toute la file) depuis : leurs unités en cours ici n'ont plus à aller au bout."""
    return queue.halted_now(job_ids)


class Executor:
    def __init__(self, node_id: str, publish, slots: dict | None = None):
        self.node_id = node_id
        self.publish = publish            # publish(event: dict), sûr depuis tout thread
        self.slots = {name: max(0, int(n)) for name, n in {**DEFAULT_SLOTS, **(slots or {})}.items()
                      if name in DEFAULT_SLOTS}
        self.busy = {name: 0 for name in self.slots}
        self.remote = {}                  # unités calculées ici pour d'autres machines
        self.interactive = 0
        # Conversations commencées et finies depuis des fils différents (chat local,
        # modèle prêté) : sans verrou, le compte et l'attente du travail de fond dériveraient
        self._interactive_lock = threading.Lock()
        self._loop = None
        self._wake = None
        self._task = None
        self._running = set()
        self._halts = {}                  # unité calculée ici -> (son travail, son arrêt)
        self._last_publish = {}
        self.cooldown = {}                # ressource -> instant de reprise (monotonic)

    # ------------------------------------------------------------ cycle de vie
    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        pacing.start()
        await asyncio.to_thread(queue.recover)
        await asyncio.to_thread(pacing.refresh, True)
        self._task = asyncio.create_task(self._run(), name="aiodos-executor")

    async def stop(self):
        pacing.shutdown()              # les lots en attente s'arrêtent : rien ne retient l'arrêt
        for _job, halt in self._halts.values():
            halt.set()                 # page en lecture, transcription, image : interrompues
        # Rendues tout de suite, sans compter d'essai : le processus peut s'arrêter avant
        # que leur calcul ait fini de s'interrompre (queue.recover rattrape un arrêt brutal)
        try:
            await asyncio.to_thread(queue.interrupt_units, list(self._halts))
        except Exception:
            log.exception("Unités en cours non rendues à l'arrêt")
        if self._task:
            self._task.cancel()

    def wake(self):
        """Réveille l'exécuteur (appelable depuis n'importe quel thread)."""
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

    # ----------------------------------------------------- priorité interactive
    def interactive_begin(self):
        with self._interactive_lock:
            self.interactive += 1
            pacing.set_interactive(self.interactive)
        self._publish_status()

    def interactive_end(self):
        with self._interactive_lock:
            self.interactive = max(0, self.interactive - 1)
            pacing.set_interactive(self.interactive)
        self._publish_status()
        self.wake()

    # ------------------------------------------------------------------ boucle
    def free_resources(self) -> set:
        now = time.monotonic()
        return {name for name, n in pacing.slots(self.slots).items()
                if self.busy[name] < n and self.cooldown.get(name, 0) <= now
                and not (self.interactive and name in GPU_BOUND)}

    async def _pace(self):
        """Alimentation et réglage relus hors de la boucle ; l'interface prévenue d'un
        passage sur batterie ou sur secteur."""
        before = pacing.current()
        if await asyncio.to_thread(pacing.refresh) != before:
            self._publish_status()

    async def _run(self):
        last_lease_check = 0.0
        while True:
            try:
                await self._pace()
                await self._fill()
                await self._halt_withdrawn()
                if time.monotonic() - last_lease_check > LEASE_CHECK_EVERY:
                    last_lease_check = time.monotonic()
                    for job_id, finished in await asyncio.to_thread(leases.expire):
                        await asyncio.to_thread(self.settle, job_id, finished)
            except Exception:
                log.exception("Erreur dans la boucle de l'exécuteur")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _fill(self):
        while True:
            unit = await asyncio.to_thread(queue.claim_next, self.free_resources(),
                                           self.node_id)
            if unit is None:
                return
            self.busy[unit["resource"]] += 1
            self._publish_status()
            # « En cours » se voit dès la première unité (sans attendre la fin de la limite de
            # 4 messages par seconde : la création du travail vient d'être annoncée)
            self.publish_job(unit["job_id"], force=unit.get("job_started", False))
            task = asyncio.create_task(self._execute(unit))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _halt_withdrawn(self):
        """Lève l'arrêt des unités en cours ici dont le travail a été annulé ou supprimé
        depuis : sans lui, elles iraient au bout, et leur résultat entrerait dans les
        bibliothèques (ou la galerie) d'un travail annulé."""
        watched = {job_id for job_id, halt in self._halts.values() if not halt.is_set()}
        if not watched:
            return
        withdrawn = await asyncio.to_thread(_withdrawn, watched)
        for unit_id, (job_id, halt) in list(self._halts.items()):
            if job_id in withdrawn and not halt.is_set():
                log.info("Unité %s arrêtée : son travail %s a été annulé ou supprimé",
                         unit_id, job_id)
                halt.set()

    async def _execute(self, unit):
        started = time.perf_counter()
        halt = threading.Event()
        if pacing.stopping().is_set():       # AIODOS s'arrête déjà : elle ne commencera pas
            halt.set()
        self._halts[unit["id"]] = (unit["job_id"], halt)
        try:
            # L'ordre complet d'une unité de moteur s'assemble ici (registry.payload_for)
            payload = await asyncio.to_thread(registry.payload_for, unit)
            result, waited = await priority.run(_run_unit, unit["task"], payload, halt)
            job_id, finished = await asyncio.to_thread(
                queue.finish_unit, unit["id"], result or {},
                max(0.0, time.perf_counter() - started - waited))
        except registry.Interrupted:
            # Arrêt d'AIODOS, travail annulé, attente du secteur : rien n'a échoué
            await asyncio.to_thread(queue.release_unit, unit["id"])
            job_id, finished = unit["job_id"], False
        except Exception as exc:
            if halt.is_set():
                # Après l'arrêt, l'erreur en vient (connexion coupée, programme arrêté) :
                # l'unité n'a pas échoué, et un travail annulé ne compte pas d'échec
                await asyncio.to_thread(queue.release_unit, unit["id"])
                job_id, finished = unit["job_id"], False
            elif engine_down(exc):
                log.info("Ollama injoignable : unité %s remise en file, nouvel essai dans %s s",
                         unit["id"], ENGINE_RETRY_DELAY)
                await asyncio.to_thread(queue.release_unit, unit["id"])
                self.cooldown[unit["resource"]] = time.monotonic() + ENGINE_RETRY_DELAY
                job_id, finished = unit["job_id"], False
            else:
                log.warning("Unité %s (%s) en échec : %s", unit["id"], unit["task"], exc)
                job_id, finished = await asyncio.to_thread(
                    queue.fail_unit, unit["id"], str(exc)[:500], time.perf_counter() - started)
        finally:
            self.busy[unit["resource"]] -= 1
            self._halts.pop(unit["id"], None)
        if finished:
            try:
                await asyncio.to_thread(queue.finalize_job, job_id)
            except Exception:
                log.exception("Finalisation du travail %s impossible", job_id)
        self.publish_job(job_id, force=finished)
        self._publish_status()
        self._wake.set()

    # ------------------------------------------- calcul pour d'autres machines
    def begin_remote(self, key, info: dict):
        """Une unité d'une autre machine occupe une ressource d'ici (boucle asyncio)."""
        self.busy[info["resource"]] += 1
        self.remote[key] = info
        self._publish_status()

    def end_remote(self, key):
        info = self.remote.pop(key, None)
        if info:
            self.busy[info["resource"]] -= 1
        self._publish_status()
        self.wake()

    def settle(self, job_id, finished):
        """Après une unité réglée hors de la boucle (résultat reçu d'une autre
        machine, bail échu…) : finalisation et nouvelles pour l'interface."""
        if finished:
            try:
                queue.finalize_job(job_id)
            except Exception:
                log.exception("Finalisation du travail %s impossible", job_id)
        self.publish_job(job_id, force=True)
        self.wake()

    # ---------------------------------------------------------------- événements
    def publish_job(self, job_id, force=False):
        """Progression d'un travail, limitée à ~4 messages/s par travail."""
        now = time.monotonic()
        if not force and now - self._last_publish.get(job_id, 0) < 0.25:
            return
        self._last_publish[job_id] = now
        summary = queue.job_summary(job_id)
        if summary:
            self.publish({"type": "job", "job": summary})

    def status(self) -> dict:
        now = time.monotonic()
        waiting = {name: round(until - now) for name, until in self.cooldown.items()
                   if until > now}
        return {"slots": pacing.slots(self.slots), "configured": self.slots,
                "busy": dict(self.busy), "interactive": self.interactive, "waiting": waiting,
                "remote": list(self.remote.values()), "pacing": pacing.describe()}

    def _publish_status(self):
        self.publish({"type": "executor", **self.status()})
