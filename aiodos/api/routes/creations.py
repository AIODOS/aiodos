"""Images et vidéos créées dans la conversation (chat/creations.py) : le message de
l'utilisateur et la réponse qui montrera les images s'ajoutent à la conversation, et le
travail part dans la file, calculé ici ou par mes machines qui ont le modèle."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aiodos.chat import creations

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


@router.post("/create")
async def create(body: CreateIn, request: Request):
    try:
        made = await asyncio.to_thread(creations.start, body.text, body.model_dump(),
                                       body.conversation_id, body.chat_model)
    except creations.CreationError as exc:     # avec les amis qui pourraient calculer
        return JSONResponse({"detail": str(exc), "friends": exc.friends}, status_code=exc.status)
    executor = request.app.state.executor
    executor.wake()
    executor.publish_job(made["job_id"], force=True)
    request.app.state.bus.publish({"type": "conversations"})
    return made
