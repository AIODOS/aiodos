"""Conversation en streaming (SSE).

Le chat est la tâche interactive : il ne passe pas par la file, il démarre
tout de suite et suspend les nouvelles unités GPU le temps de répondre (si le
modèle tourne ici ; servi par une machine appairée, c'est elle qui suspend les siennes).
En mode automatique (« auto:<modèle> »), la machine qui répond est choisie au moment
d'envoyer (network/placement.py) ; l'événement « route » dit laquelle.
La recherche web n'a lieu que si l'envoi la dit validée : même si l'interface était
contournée, rien ne part sur le web sans accord.
"""
import asyncio
import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from aiodos import db, render, settings
from aiodos.api.events import sse
from aiodos.chat import attachments, conversations, creations, pending, pipeline
from aiodos.jobs.kinds import index
from aiodos.network import placement

router = APIRouter(prefix="/api")
log = logging.getLogger("aiodos.chat")


class PreflightIn(BaseModel):
    text: str
    model: str
    conversation_id: int | None = None  # pour reconnaître la retouche d'une création
    replace_from: int | None = None     # « Modifier » : le message remplacé (cf. ChatIn)


class ChatIn(BaseModel):
    text: str
    model: str
    conversation_id: int | None = None
    web: bool = False                   # recherche web validée par l'utilisateur
    libraries: bool | None = None
    conversations: bool | None = None
    think: bool | None = None
    # Chercher aussi chez ces machines (identifiants que l'utilisateur a acceptés, parmi
    # celles que le preflight annonçait) : la question part chez elles. Seulement si la
    # demande les nomme, jamais d'après un réglage, ni chez une machine arrivée après
    # l'accord (une autre application cliente ne l'enverra pas sans le savoir)
    peer_libraries: list[str] = []
    # Pièces jointes envoyées avant (api/routes/attachments.py) : liées à ce message
    attachments: list[str] = []
    # « Régénérer », « Modifier » : ce message de l'utilisateur (replace_text : son texte, tel
    # que l'interface le croit) et la suite sont retirés, une fois la demande acceptée
    replace_from: int | None = None
    replace_text: str | None = None


@router.get("/models")
def models():
    return pipeline.available_models()


@router.post("/chat/preflight")
def preflight(body: PreflightIn):
    return pipeline.preflight(body.text, body.model, body.conversation_id, body.replace_from)


def _flag(value, key):
    return settings.get(key) if value is None else value


@router.post("/chat")
async def chat(body: ChatIn, request: Request):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Message vide.")
    asked = list(dict.fromkeys(body.attachments))
    if len(asked) > attachments.MAX_PER_MESSAGE:
        raise HTTPException(400, f"{attachments.MAX_PER_MESSAGE} pièces jointes au plus par message.")
    joined = await asyncio.to_thread(attachments.pending, asked)
    if len(joined) != len(asked):
        raise HTTPException(400, "Pièce jointe introuvable ou déjà envoyée : joins-la de nouveau.")
    executor = request.app.state.executor
    bus = request.app.state.bus
    try:                          # machines à essayer, la mieux placée d'abord
        plan = await asyncio.to_thread(placement.plan, body.model, executor)
    except placement.NoMachine as exc:
        raise HTTPException(400, str(exc))
    conv_id = body.conversation_id
    if conv_id is not None:               # la réponse arrêtée juste avant : en place d'abord
        await asyncio.to_thread(pending.wait, conv_id)
        if not await asyncio.to_thread(conversations.get, conv_id):
            conv_id = None
    if conv_id is None and body.replace_from is not None:
        raise HTTPException(creations.CHANGED_STATUS, creations.CHANGED)
    # Jusqu'à ce que la question soit enregistrée (ou refusée), relire la conversation pour
    # se resynchroniser attend (pending) : l'écran voit ce que le nœud garde. Une
    # conversation neuve n'est créée qu'à l'acceptation (ask)
    marker = pending.begin(conv_id) if conv_id is not None else None
    try:
        return await _answer(body, request, text, plan, conv_id, executor, bus, joined)
    finally:
        if marker is not None:
            pending.end(conv_id, marker)


async def _answer(body, request, text, plan, conv_id, executor, bus, joined=()):
    """La suite de chat(), une fois la conversation connue. joined : les pièces jointes de
    la question (attachments.pending)."""
    # Ce que les autres machines m'ont ouvert ne va qu'aux modèles autorisés (ici, réparti,
    # ou prêté par une de mes machines) : ni les passages, ni les réponses qui s'en servent.
    # Jugé pour chaque machine du plan (mode automatique) ; si l'on cherche chez les autres,
    # celles qui peuvent recevoir leurs passages passent d'abord.
    allowed = {m: await asyncio.to_thread(pipeline.peers_allowed, m) for m in plan}
    if body.peer_libraries:
        plan.sort(key=lambda m: not allowed[m])
    def ask():
        """Acceptée : l'ancien échange (replace_from) laisse la place, l'historique est lu,
        la question ajoutée, d'un seul geste (une erreur entre deux ne perd pas l'ancien
        échange sans la nouvelle question). Chaque machine qui peut répondre ne reçoit de
        l'historique que ce qu'elle peut recevoir (pipeline.history_filter)."""
        with db.transaction():
            cid = conv_id if conv_id is not None else conversations.create(model=body.model)
            stopped = [] if body.replace_from is None else creations.replace(
                cid, body.replace_from, body.replace_text)
            rows = conversations.history_rows(cid, 30)
            asked = conversations.add_message(cid, "user", text)
            attachments.link(joined, asked)
            named = conversations.auto_title(cid, text)
        # Ce qu'on a déjà lu des pièces jointes des messages précédents les accompagne
        earlier = attachments.of_messages([r["id"] for r in rows if r["role"] == "user"])
        extra = {mid: attachments.history_blocks(found) for mid, found in earlier.items()}
        return cid, stopped, rows, asked, named, extra
    if await request.is_disconnected():   # parti (Stop) avant l'acceptation : rien ne change
        raise HTTPException(400, "Demande abandonnée.")
    try:
        conv_id, stopped, rows, user_message_id, title, extra = await asyncio.to_thread(ask)
    except creations.CreationError as exc:
        raise HTTPException(exc.status, str(exc))
    for job_id in stopped:                # créations retirées : la vue des tâches le voit
        executor.publish_job(job_id, force=True)
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
            # ne part ensuite vers aucune autre machine. Et l'arrêt va à la
            # recherche web elle-même : Stop (ou le client parti) interrompt la page en cours
            # de lecture, et les suivantes ne sont pas lues
            web = pipeline.web_context(text, stop) if body.web and not stop.is_set() else None
            built = {}                    # messages selon la machine qui répond
            read = {}                     # pièces jointes lues, selon que le modèle voit les images

            def attached(model, messages):
                """La question avec ses pièces jointes : une image va telle quelle à un modèle
                de cette machine qui voit les images, décrite (texte) à tous les autres."""
                images = [r for r in joined if r["kind"] == "image"]
                native = bool(images) and pipeline.sees_images(model)
                if native not in read:
                    seen = images if native else []
                    read[native] = (attachments.question_blocks(
                        joined, stop, {r["id"] for r in seen}, status=lambda t: put(("status", t))),
                        attachments.images_b64(seen))
                blocks, pictures = read[native]
                return pipeline.with_attachments(messages, blocks, pictures)

            def attempt(model):
                if stop.is_set():
                    return
                level = placement.destination(model)
                ok = allowed[model]       # passages d'autres machines permis à celle-ci ?
                if (level, ok) not in built:
                    keep = pipeline.history_filter(level, ok)
                    history = conversations.as_history(rows, keep, extra)
                    shown = {}            # noms du sommaire montrés à cette machine
                    built[level, ok] = (*pipeline.build_messages(
                        history, text, libraries=libraries, conversations=memory,
                        web_search=body.web, conversation_id=conv_id, destination=level,
                        web=web, peers=body.peer_libraries if ok else None,
                        summary=shown), shown, pipeline.inherited(rows, keep))
                messages, sources, shown, marks = built[level, ok]
                if joined:
                    messages = attached(model, messages)
                    if stop.is_set():
                        return
                yield "summary", shown
                yield "inherit", marks
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
             "model": plan[0], "shown": {}, "inherit": {}}
    saving = threading.Lock()

    def save():
        """Enregistre la réponse (même partielle), une seule fois : la fin normale et
        l'interruption (client parti pendant l'enregistrement) ne la doublent pas."""
        with saving:
            if state["saved"] is not None:
                return state["saved"]
            content = state["content"]
            failed = bool(state["error"] and not content)    # rien qu'une erreur
            if failed:
                content = f"{conversations.ERROR_MARK}{state['error']}"
            if not content and not state["thinking"]:
                state["saved"] = 0
                return 0
            # Une réponse qui cite des noms du sommaire des bibliothèques s'en est servie :
            # elle ne suivra pas, dans l'historique, vers une machine qui ne doit pas les voir
            sources = list(state["sources"])
            cited = pipeline.summary_mentioned(state["shown"], content)
            if cited:
                sources.append({"type": "summary", "collection_ids": cited})
            if state["inherit"]:          # ce que lui imposent les réponses qu'elle a reçues
                sources.append({"type": "inherited", **state["inherit"]})
            if failed:
                sources.append(dict(conversations.ERROR_SOURCE))
            state["saved"] = conversations.add_message(
                conv_id, "assistant", content, state["thinking"] or None,
                state["model"], sources, conversation_model=body.model)
            if memory and not failed:
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
                elif kind == "summary":   # gardé pour l'enregistrement, pas montré
                    state["shown"] = data
                elif kind == "inherit":
                    state["inherit"] = data
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
                done = pending.begin(conv_id)

                def save_stopped():
                    try:
                        save()
                    finally:
                        pending.end(conv_id, done)
                threading.Thread(target=save_stopped, daemon=True,
                                 name="aiodos-chat-save").start()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
