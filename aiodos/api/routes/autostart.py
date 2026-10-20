"""Démarrer AIODOS avec l'appareil, en arrière-plan (node/autostart.py)."""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aiodos.node import autostart

router = APIRouter(prefix="/api/node")


class AutostartIn(BaseModel):
    enabled: bool


@router.get("/autostart")
async def autostart_state():
    return await asyncio.to_thread(autostart.state)


@router.post("/autostart")
async def autostart_set(body: AutostartIn):
    try:
        return await asyncio.to_thread(autostart.set_enabled, body.enabled)
    except OSError as exc:
        raise HTTPException(400, str(exc))
