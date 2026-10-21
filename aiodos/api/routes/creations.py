"""Images et vidéos créées dans la conversation (chat/creations.py) : le message de
l'utilisateur et la réponse qui montrera les images s'ajoutent à la conversation, et le
travail part dans la file, calculé ici ou par mes machines qui ont le modèle."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aiodos.chat import creations, pending

router = APIRouter(prefix="/api/chat")


class CreateIn(BaseModel):
    text: str                           # le message, tel qu'écrit
    kind: str | None = None             # « image » ou « video » ; vide : d'après le message
    model: str | None = None            # modèle préféré ; vide (ou indisponible) : le premier prêt
    prompt: str | None = None           # la description ; vide : tirée du message
    conversation_id: int | None = None
    chat_model: str | None = None       # modèle de conversation d'une conversation neuve
    count: int | None = None
    width: int | None = None
    height: int | None = None
    shape: str | None = None            # « portrait », « landscape », « square »
    frames: int | None = None
    seconds: float | None = None
    seed: int | None = None
    negative: str | None = None
    followup: str | None = None         # « edit » ou « variation » de la dernière création
    auto: bool = False                  # reconnue dans le message (et non demandée au bouton)
    replace_from: int | None = None     # « Modifier » : ce message et la suite sont remplacés
    replace_text: str | None = None     # son texte, tel que l'interface le croit
    attachments: list[str] = []         # une image jointe : l'image de départ (référence)


@router.post("/create/{message_id}/undo")
async def undo(message_id: int, request: Request):
    """« Répondre en texte » : la création reconnue au vol n'en était pas une."""
    try:
        undone = await asyncio.to_thread(creations.undo, message_id)
    except creations.CreationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)
    if undone.get("job_id"):
        request.app.state.executor.publish_job(undone["job_id"], force=True)
    request.app.state.bus.publish({"type": "conversations"})
    return undone


@router.post("/create")
async def create(body: CreateIn, request: Request):
    conv_id = body.conversation_id
    if conv_id is not None:               # une réponse arrêtée juste avant : en place d'abord
        await asyncio.to_thread(pending.wait, conv_id)
    try:
        # La préparation (une retouche réécrit la description) n'écrit rien : seule
        # l'acceptation fait attendre qui relit la conversation pour se resynchroniser
        plan = await asyncio.to_thread(creations.prepare, body.text, body.model_dump(),
                                       conv_id, body.chat_model, body.replace_from)
        conv_id = plan["conv_id"]
        marker = pending.begin(conv_id) if conv_id is not None else None
        try:
            if await request.is_disconnected():     # parti avant l'acceptation : rien ne change
                return JSONResponse({"detail": "Demande abandonnée."}, status_code=400)
            made = await asyncio.to_thread(creations.accept, plan, body.replace_from,
                                           body.replace_text)
        finally:
            if marker is not None:
                pending.end(conv_id, marker)
    except creations.CreationError as exc:     # avec les amis qui pourraient calculer
        return JSONResponse({"detail": str(exc), "friends": exc.friends}, status_code=exc.status)
    executor = request.app.state.executor
    executor.wake()
    for job_id in [made["job_id"], *made.pop("stopped", [])]:
        executor.publish_job(job_id, force=True)
    request.app.state.bus.publish({"type": "conversations"})
    return made
