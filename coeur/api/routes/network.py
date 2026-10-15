"""Réseau local vu par l'interface : activation, machines, appairage."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from coeur import settings
from coeur.network.client import PeerError

router = APIRouter(prefix="/api/network")


class EnableIn(BaseModel):
    enabled: bool


class AddressIn(BaseModel):
    address: str


class NodeIn(BaseModel):
    node_id: str


class AnswerIn(BaseModel):
    node_id: str
    accept: bool


def _manager(request: Request):
    return request.app.state.network


@router.get("")
def status(request: Request):
    return _manager(request).snapshot()


@router.post("/enable")
async def enable(body: EnableIn, request: Request):
    manager = _manager(request)
    await asyncio.to_thread(settings.set_value, "network_enabled", body.enabled)
    await manager.apply()
    return manager.snapshot()


@router.post("/add")
async def add(body: AddressIn, request: Request):
    try:
        return await _manager(request).add_manual(body.address)
    except (PeerError, ValueError) as exc:
        raise HTTPException(400, f"Impossible de joindre {body.address} : {exc}")


@router.post("/pair")
async def pair(body: NodeIn, request: Request):
    manager = _manager(request)
    target = manager.visible.get(body.node_id)
    if target is None:
        raise HTTPException(404, "Machine introuvable sur le réseau.")
    try:
        pending = await asyncio.to_thread(manager.pairing.start, target["address"], target["port"])
    except PeerError as exc:
        raise HTTPException(400, str(exc))
    return {"node_id": pending.node_id, "name": pending.name, "code": pending.code}


@router.post("/pair/answer")
async def answer(body: AnswerIn, request: Request):
    try:
        await asyncio.to_thread(_manager(request).pairing.answer, body.node_id, body.accept)
    except PeerError as exc:
        raise HTTPException(400, str(exc))
    return _manager(request).snapshot()


@router.post("/peers/{node_id}/refresh")
async def refresh(node_id: str, request: Request):
    await _manager(request).refresh_now(node_id)
    return _manager(request).snapshot()


@router.delete("/peers/{node_id}")
async def unpair(node_id: str, request: Request):
    await _manager(request).unpair(node_id)
    return _manager(request).snapshot()
