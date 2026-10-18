"""Conversation en streaming (SSE).

Le chat est la tâche interactive : il ne passe pas par la file, il démarre
tout de suite et suspend les nouvelles unités GPU le temps de répondre (si le
modèle tourne ici ; servi par une machine appairée, c'est elle qui suspend les siennes).
En mode automatique (« auto:<modèle> »), la machine qui répond est choisie au moment
d'envoyer (network/placement.py) ; l'événement « route » dit laquelle.
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

from aiodos import render, settings
from aiodos.api.events import sse
from aiodos.chat import conversations, pipeline
from aiodos.jobs.kinds import index
from aiodos.network import placement
from aiodos.services import cloud

router = APIRouter(prefix="/api")
log = logging.getLogger("aiodos.chat")


class PreflightIn(BaseModel):
    text: str
    model: str
    conversation_id: int | None = None  # pour reconnaître la retouche d'une création


class ChatIn(BaseModel):
    text: str
    model: str
    conversation_id: int | None = None
    web: bool = False                   # recherche web validée par l'utilisateur
    cloud_confirmed: bool = False       # envoi cloud validé par l'utilisateur
    libraries: bool | None = None
    conversations: bool | None = None
    think: bool | None = None
    # Chercher aussi chez ces machines (identifiants que l'utilisateur a acceptés, parmi
    # celles que le preflight annonçait) : la question part chez elles. Seulement si la
    # demande les nomme, jamais d'après un réglage, ni chez une machine arrivée après
    # l'accord (une autre application cliente ne l'enverra pas sans le savoir)
    peer_libraries: list[str] = []


@router.get("/models")
def models():
    return pipeline.available_models()


@router.post("/chat/preflight")
def preflight(body: PreflightIn):
    return pipeline.preflight(body.text, body.model, body.conversation_id)


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
    try:                          # machines à essayer, la mieux placée d'abord
        plan = await asyncio.to_thread(placement.plan, body.model, executor)
    except placement.NoMachine as exc:
        raise HTTPException(400, str(exc))
    conv_id = body.conversation_id
    if conv_id is None or not await asyncio.to_thread(conversations.get, conv_id):
        conv_id = await asyncio.to_thread(conversations.create, model=body.model)
    # Ce que les autres machines m'ont ouvert ne va qu'aux modèles autorisés (ici, réparti,
    # ou prêté par une de mes machines) : ni les passages, ni les réponses qui s'en servent.
    # Jugé pour chaque machine du plan (mode automatique) ; si l'on cherche chez les autres,
    # celles qui peuvent recevoir leurs passages passent d'abord.
    allowed = {m: await asyncio.to_thread(pipeline.peers_allowed, m) for m in plan}
    if body.peer_libraries:
        plan.sort(key=lambda m: not allowed[m])
    # L'historique, lu avant d'ajouter la question ; chaque machine qui peut répondre n'en
    # reçoit que ce qu'elle peut recevoir (pipeline.history_filter)
    rows = await asyncio.to_thread(conversations.history_rows, conv_id, 30)
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
            # Arrêtée pendant la préparation (recherche web, bibliothèques des autres) : rien
            # ne part ensuite, ni au cloud, ni à une autre machine
            web = pipeline.web_context(text) if body.web and not stop.is_set() else None
            built = {}                    # messages selon la machine qui répond

            def attempt(model):
                if stop.is_set():
                    return
                level = placement.destination(model)
                ok = allowed[model]       # passages d'autres machines permis à celle-ci ?
                if (level, ok) not in built:
                    history = conversations.as_history(rows, pipeline.history_filter(level, ok))
                    built[level, ok] = pipeline.build_messages(
                        history, text, libraries=libraries, conversations=memory,
                        web_search=body.web, conversation_id=conv_id, destination=level,
                        web=web, peers=body.peer_libraries if ok else None)
                messages, sources = built[level, ok]
                yield "sources", sources
                if stop.is_set():
                    return
                yield from pipeline.generate(model, messages, think, stop)

            for kind, piece in placement.run(plan, attempt, executor, stop):
                if kind == "route":
                    piece = {"model": piece, "machine": placement.machine_of(piece)}
                put((kind, piece))
            put(("end", None))
        except Exception as exc:
            log.warning("Génération interrompue : %s", exc)
            put(("error", str(exc)))

    state = {"content": "", "thinking": "", "sources": [], "error": None, "saved": None,
             "model": plan[0]}
    saving = threading.Lock()

    def save():
        """Enregistre la réponse (même partielle), une seule fois : la fin normale et
        l'interruption (client parti pendant l'enregistrement) ne la doublent pas."""
        with saving:
            if state["saved"] is not None:
                return state["saved"]
            content = state["content"]
            if state["error"] and not content:
                content = f"{conversations.ERROR_MARK}{state['error']}"
            if not content and not state["thinking"]:
                state["saved"] = 0
                return 0
            state["saved"] = conversations.add_message(
                conv_id, "assistant", content, state["thinking"] or None,
                state["model"], state["sources"], conversation_model=body.model)
            if memory and not content.startswith(conversations.ERROR_MARK):
                job_id = index.queue_conversation(conv_id)
                if job_id is not None:
                    executor.wake()
                    executor.publish_job(job_id, force=True)
            bus.publish({"type": "conversations"})
            return state["saved"]

    async def events():
        # L'interactif d'abord : placement.run suspend les unités GPU de fond le temps
        # de la réponse, si elle est calculée ici
        threading.Thread(target=worker, daemon=True, name="aiodos-chat").start()
        last_render = 0.0
        try:
            yield sse({"type": "start", "conversation_id": conv_id,
                       "user_message_id": user_message_id, "title": title})
            while True:
                kind, data = await inbox.get()
                if kind == "route":
                    state["model"] = data["model"]
                    yield sse({"type": "route", **data})
                elif kind == "sources":
                    state["sources"] = data
                    yield sse({"type": "sources", "sources": data})
                elif kind == "status":    # mise en place d'un modèle réparti : montré, pas gardé
                    yield sse({"type": "status", "text": data})
                elif kind == "thinking":
                    state["thinking"] += data
                    yield sse({"type": "thinking", "text": data})
                elif kind == "content":
                    state["content"] += data
                    # Rendue moins souvent à mesure qu'elle s'allonge : tout le texte est
                    # refait à chaque fois, sur la boucle qui sert aussi les autres machines
                    size = len(state["content"])
                    every = 0.08 if size < 6000 else 0.25 if size < 30000 else 0.6
                    if time.monotonic() - last_render > every:
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
            if state["saved"] is None:    # interruption (bouton Stop) : hors de la boucle
                threading.Thread(target=save, daemon=True, name="aiodos-chat-save").start()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
