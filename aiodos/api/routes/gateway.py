"""Passerelle, vue par l'interface (Réglages → Applications et autres outils) :
ouverture aux applications et aux autres outils, port, clé générale à copier ou à
renouveler (cf. aiodos/gateway ; les applications elles-mêmes : gateway_apps.py)."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos import settings
from aiodos.gateway import access

router = APIRouter(prefix="/api/gateway")


class GatewayIn(BaseModel):
    enabled: bool | None = None         # clé générale des autres outils
    port: int | None = None
    apps: bool | None = None            # ouvert aux applications de ce PC
    requests: bool | None = None        # accepte les nouvelles demandes d'accès
    same_account: bool | None = None    # refuse les applis d'un autre compte Windows


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
    for field, setting in (("apps", "gateway_apps"), ("requests", "gateway_requests"),
                           ("same_account", "gateway_same_account")):
        if getattr(body, field) is not None:
            values[setting] = getattr(body, field)
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
