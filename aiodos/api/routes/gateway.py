"""Passerelle vers les autres outils, vue par l'interface (Réglages → Autres outils) :
ouverture, port, clé d'accès à copier ou à renouveler (cf. aiodos/gateway)."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos import settings
from aiodos.gateway import access

router = APIRouter(prefix="/api/gateway")


class GatewayIn(BaseModel):
    enabled: bool | None = None
    port: int | None = None


def _gateway(request: Request):
    return request.app.state.gateway


@router.get("")
async def status(request: Request):
    return await asyncio.to_thread(_gateway(request).snapshot)


@router.post("")
async def change(body: GatewayIn, request: Request):
    values = {}
    if body.enabled is not None:
        values["gateway_enabled"] = body.enabled
    if body.port is not None:
        if not 1024 <= body.port <= 65535:
            raise HTTPException(400, "Choisis un port entre 1024 et 65535.")
        values["gateway_port"] = body.port
    if values:
        await asyncio.to_thread(settings.set_many, values)
        request.app.state.bus.publish({"type": "settings"})
    await _gateway(request).apply()
    return await asyncio.to_thread(_gateway(request).snapshot)


@router.post("/key")
async def renew_key(request: Request):
    """Nouvelle clé : l'ancienne cesse aussitôt de marcher dans les outils."""
    await asyncio.to_thread(access.renew)
    request.app.state.bus.publish({"type": "gateway"})
    return await asyncio.to_thread(_gateway(request).snapshot)
