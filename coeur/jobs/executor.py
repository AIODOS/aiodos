"""Exécuteur local : vide la file en respectant les ressources de la machine.

Tourne dans la boucle asyncio du nœud ; chaque unité s'exécute dans un thread
(les tâches sont bloquantes : réseau, fichiers, modèles). Pendant une
conversation, aucune nouvelle unité GPU ne démarre : l'interactif d'abord.
En phase 1, un « exécuteur distant » réservera des unités pour d'autres nœuds.
"""
import asyncio
import logging
import time

from coeur.jobs import queue, registry
from coeur.jobs.models import DEFAULT_SLOTS, GPU_BOUND

log = logging.getLogger("coeur.executor")


class Executor:
    def __init__(self, node_id: str, publish):
        self.node_id = node_id
        self.publish = publish            # publish(event: dict), sûr depuis tout thread
        self.slots = dict(DEFAULT_SLOTS)
        self.busy = {name: 0 for name in self.slots}
        self.interactive = 0
        self._loop = None
        self._wake = None
        self._task = None
        self._running = set()
        self._last_publish = {}

    # ------------------------------------------------------------ cycle de vie
    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        await asyncio.to_thread(queue.recover)
        self._task = asyncio.create_task(self._run(), name="coeur-executor")

    async def stop(self):
        if self._task:
            self._task.cancel()

    def wake(self):
        """Réveille l'exécuteur (appelable depuis n'importe quel thread)."""
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

    # ----------------------------------------------------- priorité interactive
    def interactive_begin(self):
        self.interactive += 1
        self._publish_status()

    def interactive_end(self):
        self.interactive = max(0, self.interactive - 1)
        self._publish_status()
        self.wake()

    # ------------------------------------------------------------------ boucle
    def free_resources(self) -> set:
        return {name for name, n in self.slots.items()
                if self.busy[name] < n and not (self.interactive and name in GPU_BOUND)}

    async def _run(self):
        while True:
            try:
                await self._fill()
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
            task = asyncio.create_task(self._execute(unit))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _execute(self, unit):
        started = time.perf_counter()
        try:
            handler = registry.TASKS[unit["task"]]
            result = await asyncio.to_thread(handler, unit["payload"])
            job_id, finished = await asyncio.to_thread(
                queue.finish_unit, unit["id"], result or {}, time.perf_counter() - started)
        except Exception as exc:
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
        return {"slots": self.slots, "busy": dict(self.busy),
                "interactive": self.interactive}

    def _publish_status(self):
        self.publish({"type": "executor", **self.status()})
