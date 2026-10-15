"""Conversation en streaming (SSE).

Le chat est la tâche interactive : il ne passe pas par la file, il démarre
tout de suite et suspend les nouvelles unités GPU le temps de répondre.
La validation des envois sortants (web, cloud) est revérifiée ici : même si
l'interface était contournée, rien ne part dans le cloud sans accord.
"""
import asyncio
import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from coeur import render, settings
from coeur.api.events import sse
from coeur.chat import conversations, pipeline
from coeur.jobs import queue
from coeur.jobs.models import BACKGROUND
from coeur.services import cloud

router = APIRouter(prefix="/api")
log = logging.getLogger("coeur.chat")


class PreflightIn(BaseModel):
    text: str
    model: str


class ChatIn(BaseModel):
    text: str
    model: str
    conversation_id: int | None = None
    web: bool = False                   # recherche web validée par l'utilisateur
    cloud_confirmed: bool = False       # envoi cloud validé par l'utilisateur
    libraries: bool | None = None
    conversations: bool | None = None
    think: bool | None = None


@router.get("/models")
def models():
    return pipeline.available_models()


@router.post("/chat/preflight")
def preflight(body: PreflightIn):
    return pipeline.preflight(body.text, body.model)


def _flag(value, key):
    return settings.get(key) if value is None else value


@router.post("/chat")
async def chat(body: ChatIn, request: Request):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Message vide.")
    provider = cloud.provider_of(body.model)
    if provider:
        if not cloud.has_key(provider):
            raise HTTPException(400, f"Aucune clé API pour {cloud.PROVIDERS[provider]['label']}.")
        if settings.get("cloud_confirm") and not body.cloud_confirmed:
            raise HTTPException(409, "L'envoi vers le cloud doit être validé.")

    executor = request.app.state.executor
    bus = request.app.state.bus
    conv_id = body.conversation_id
    if conv_id is None or not await asyncio.to_thread(conversations.get, conv_id):
        conv_id = await asyncio.to_thread(conversations.create, model=body.model)
    history = await asyncio.to_thread(conversations.history, conv_id)
    user_message_id = await asyncio.to_thread(conversations.add_message, conv_id, "user", text)
    title = await asyncio.to_thread(conversations.auto_title, conv_id, text)
    if title:
        bus.publish({"type": "conversations"})

    libraries = _flag(body.libraries, "knowledge_enabled")
    memory = _flag(body.conversations, "conversations_enabled")
    think = _flag(body.think, "think")

    loop = asyncio.get_running_loop()
    inbox = asyncio.Queue()
    stop = threading.Event()

    def worker():
        def put(item):
            loop.call_soon_threadsafe(inbox.put_nowait, item)
        try:
            messages, sources = pipeline.build_messages(
                history, text, libraries=libraries, conversations=memory,
                web_search=body.web)
            put(("sources", sources))
            for kind, piece in pipeline.generate(body.model, messages, think, stop):
                put((kind, piece))
            put(("end", None))
        except Exception as exc:
            log.warning("Génération interrompue : %s", exc)
            put(("error", str(exc)))

    state = {"content": "", "thinking": "", "sources": [], "error": None, "saved": None}

    def save():
        """Enregistre la réponse (même partielle). Appelé une seule fois."""
        if state["saved"] is not None:
            return state["saved"]
        content = state["content"]
        if state["error"] and not content:
            content = f"⚠ {state['error']}"
        if not content and not state["thinking"]:
            state["saved"] = 0
            return 0
        state["saved"] = conversations.add_message(
            conv_id, "assistant", content, state["thinking"] or None,
            body.model, state["sources"])
        if memory:
            job_id = queue.create_job("index_conversation", {"conversation_id": conv_id},
                                      priority=BACKGROUND)
            executor.wake()
            executor.publish_job(job_id, force=True)
        bus.publish({"type": "conversations"})
        return state["saved"]

    async def events():
        executor.interactive_begin()
        threading.Thread(target=worker, daemon=True, name="coeur-chat").start()
        last_render = 0.0
        try:
            yield sse({"type": "start", "conversation_id": conv_id,
                       "user_message_id": user_message_id, "title": title})
            while True:
                kind, data = await inbox.get()
                if kind == "sources":
                    state["sources"] = data
                    yield sse({"type": "sources", "sources": data})
                elif kind == "thinking":
                    state["thinking"] += data
                    yield sse({"type": "thinking", "text": data})
                elif kind == "content":
                    state["content"] += data
                    if time.monotonic() - last_render > 0.08:
                        last_render = time.monotonic()
                        yield sse({"type": "render", "html": render.to_html(state["content"])})
                elif kind == "error":
                    state["error"] = data
                    break
                else:
                    break
            message_id = await asyncio.to_thread(save)
            yield sse({"type": "done", "message_id": message_id, "error": state["error"],
                       "html": render.to_html(state["content"])})
        finally:
            stop.set()                    # client parti ou fin : on coupe le modèle
            executor.interactive_end()
            if state["saved"] is None:    # interruption (bouton Stop)
                save()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
