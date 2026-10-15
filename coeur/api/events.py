"""Bus d'événements → flux SSE vers l'interface (progression des tâches…).

publish() est utilisable depuis n'importe quel thread : l'événement est remis
à la boucle asyncio du serveur, qui le distribue à chaque fenêtre abonnée.
"""
import asyncio
import json


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class EventBus:
    def __init__(self):
        self._subscribers = set()
        self._loop = None

    def bind(self, loop):
        self._loop = loop

    def publish(self, event: dict):
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._dispatch, event)

    def _dispatch(self, event):
        for q in list(self._subscribers):
            if q.qsize() < 500:          # abonné trop lent : on saute plutôt que d'enfler
                q.put_nowait(event)

    async def stream(self):
        q = asyncio.Queue()
        self._subscribers.add(q)
        try:
            yield sse({"type": "hello"})
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield sse(event)
                except TimeoutError:
                    yield ": ping\n\n"     # garde la connexion ouverte
        finally:
            self._subscribers.discard(q)
