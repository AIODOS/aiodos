"""Bibliothèques de connaissance : collections, entrées, lancement d'indexations."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from coeur.jobs import queue
from coeur.jobs.models import NORMAL
from coeur.knowledge import store

router = APIRouter(prefix="/api")


class CollectionIn(BaseModel):
    name: str


class CollectionPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None


class IndexPathsIn(BaseModel):
    paths: list[str]
    collection_id: int
    priority: int = NORMAL


class IndexUrlsIn(BaseModel):
    urls: list[str]
    collection_id: int
    priority: int = NORMAL


class PrefixIn(BaseModel):
    collection_id: int
    prefix: str


def _notify(request: Request):
    request.app.state.bus.publish({"type": "knowledge"})


def _guard(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/collections")
def collections():
    memory = [c for c in store.list_collections(include_system=True) if c["system"]]
    return {"collections": store.list_collections(),
            "memory": memory[0] if memory else None}


@router.post("/collections")
def create_collection(body: CollectionIn, request: Request):
    collection_id = _guard(store.create_collection, body.name)
    _notify(request)
    return {"id": collection_id}


@router.patch("/collections/{collection_id}")
def update_collection(collection_id: int, body: CollectionPatch, request: Request):
    if body.name is not None:
        _guard(store.rename_collection, collection_id, body.name)
    if body.enabled is not None:
        store.set_enabled(collection_id, body.enabled)
    _notify(request)
    return {"ok": True}


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: int, request: Request):
    _guard(store.delete_collection, collection_id)
    _notify(request)
    return {"ok": True}


@router.get("/entries")
def entries(collection_id: int | None = None):
    return store.list_entries(collection_id)


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: int, request: Request):
    store.remove_entry(entry_id)
    _notify(request)
    return {"ok": True}


@router.post("/entries/remove_prefix")
def delete_folder(body: PrefixIn, request: Request):
    removed = store.remove_prefix(body.collection_id, body.prefix)
    _notify(request)
    return {"removed": removed}


@router.post("/memory/clear")
def clear_memory(request: Request):
    """Vide la mémoire inter-conversations (les conversations restent)."""
    collection_id = store.conversation_collection_id()
    for entry in store.list_entries(collection_id, include_system=True):
        store.remove_entry(entry["id"])
    _notify(request)
    return {"ok": True}


async def _launch(request: Request, kind: str, params: dict, priority: int):
    try:
        job_id = await asyncio.to_thread(queue.create_job, kind, params, priority)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    executor = request.app.state.executor
    executor.wake()
    executor.publish_job(job_id, force=True)
    return {"job_id": job_id}


@router.post("/index")
async def index_paths(body: IndexPathsIn, request: Request):
    if not body.paths:
        raise HTTPException(400, "Aucun chemin fourni.")
    return await _launch(request, "index_paths",
                         {"paths": body.paths, "collection_id": body.collection_id},
                         body.priority)


@router.post("/index_urls")
async def index_urls(body: IndexUrlsIn, request: Request):
    urls = [u.strip() for u in body.urls if u.strip().startswith(("http://", "https://"))]
    if not urls:
        raise HTTPException(400, "Aucune URL valide (http:// ou https://).")
    return await _launch(request, "index_urls",
                         {"urls": urls, "collection_id": body.collection_id}, body.priority)
