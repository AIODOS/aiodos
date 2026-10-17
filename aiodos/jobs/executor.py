"""Exécuteur local : vide la file en respectant les ressources de la machine.

Tourne dans la boucle asyncio du nœud ; chaque unité s'exécute dans un thread de
fond, à priorité abaissée (node/priority.py : les tâches sont bloquantes : réseau,
fichiers, modèles). Pendant une conversation, aucune nouvelle unité GPU ne démarre,
et celles en cours attendent entre deux lots : l'interactif d'abord. Sur batterie,
moins d'unités à la fois, ou aucune, selon le réglage (jobs/pacing.py).

Les ressources sont partagées avec le calcul fait ici pour d'autres machines
(begin_remote / end_remote, cf. network.worker) ; l'exécuteur surveille aussi
les baux des unités que ce nœud a confiées ailleurs (cf. jobs.leases).
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


def _run_unit(task, payload) -> tuple:
    """Une unité de la file (thread de fond), et le temps passé à attendre ou à se reposer
    (jobs/pacing.py) : il ne compte pas dans le temps de calcul."""
    before = pacing.waited()
    result = registry.run_local(task, payload)
    return result, pacing.waited() - before


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

    async def _execute(self, unit):
        started = time.perf_counter()
        try:
            result, waited = await priority.run(_run_unit, unit["task"], unit["payload"])
            job_id, finished = await asyncio.to_thread(
                queue.finish_unit, unit["id"], result or {},
                max(0.0, time.perf_counter() - started - waited))
        except registry.Interrupted:         # arrêt d'AIODOS pendant une attente : rien n'a échoué
            await asyncio.to_thread(queue.release_unit, unit["id"])
            job_id, finished = unit["job_id"], False
        except Exception as exc:
            if engine_down(exc):
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
