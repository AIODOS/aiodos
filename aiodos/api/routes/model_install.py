"""Installer un modèle de conversation (services/model_install.py) : les modèles conseillés
pour cette machine, l'installation par Ollama et son avancement (événement
« model_install »)."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos.services import model_install

router = APIRouter(prefix="/api/models/install")


class InstallIn(BaseModel):
    """Un modèle (name) ou plusieurs (names), installés l'un après l'autre."""
    name: str = ""
    names: list[str] = []


@router.get("")
async def suggestions():
    return await asyncio.to_thread(model_install.suggestions)


@router.post("")
def install(body: InstallIn, request: Request):
    bus = request.app.state.bus

    def changed():
        bus.publish({"type": "model_install", **model_install.progress()})
    try:
        return model_install.start(body.names or [body.name], changed=changed)
    except model_install.InstallError as exc:
        raise HTTPException(409, str(exc))


@router.delete("")
def cancel():
    model_install.cancel()
    return model_install.progress()
