"""Gros modèles répartis : préparer un groupe d'avance, dès que le modèle est choisi dans
la conversation (cluster/group.py). Les nouvelles partent sur le flux d'événements
(« cluster ») ; une conversation qui arrive avant la fin reprend la mise en place en cours."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos.cluster import group

router = APIRouter(prefix="/api/cluster")


class PrepareIn(BaseModel):
    model: str


@router.post("/prepare")
async def prepare(body: PrepareIn, request: Request):
    bus = request.app.state.bus

    def changed():
        bus.publish({"type": "cluster", "model": body.model, **group.preparation(body.model)})
    try:
        return await asyncio.to_thread(group.prepare, body.model, changed)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@router.get("/prepare")
def preparation(model: str):
    return group.preparation(model)
