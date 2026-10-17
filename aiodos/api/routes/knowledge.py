"""Bibliothèques de connaissance : collections, entrées, lancement d'indexations, ce qui
en est consultable par mes machines et mes amis, et la recherche dans ce qu'ils m'ouvrent."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos.jobs import queue
from aiodos.jobs.models import NORMAL
from aiodos.knowledge import store, visibility
from aiodos.network import remote_search

router = APIRouter(prefix="/api")
VIEW_PER_PEER = 5             # passages demandés à chaque machine depuis la vue Bibliothèques


class CollectionIn(BaseModel):
    name: str


class CollectionPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    local_only: bool | None = None
    scope: str | None = None              # local | mine | friends (cf. store.set_scope)
    open_to: str | None = None            # none | mine | friends (cf. visibility.set_open_to)


class ClosedIn(BaseModel):
    path: str
    folder: bool = False                  # un dossier entier, ou ce seul document
    closed: bool = True                   # fermer, ou rouvrir


class ConsultIn(BaseModel):
    question: str


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
    closed = visibility.closed_parts()
    return {"collections": [dict(c, closed=closed.get(c["id"], []))
                            for c in store.list_collections()],
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
    if body.local_only is not None:
        _guard(store.set_local_only, collection_id, body.local_only)
    if body.scope is not None:
        _guard(store.set_scope, collection_id, body.scope)
    if body.open_to is not None:
        _guard(visibility.set_open_to, collection_id, body.open_to)
    _notify(request)
    return {"ok": True}


@router.post("/collections/{collection_id}/closed")
def set_closed(collection_id: int, body: ClosedIn, request: Request):
    """Ferme (ou rouvre) à la consultation un dossier ou un document de la bibliothèque."""
    action = visibility.close if body.closed else visibility.reopen
    _guard(action, collection_id, body.path, body.folder)
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


@router.get("/consult")
def consult_targets():
    """Chez qui partirait une recherche : les machines qui ont quelque chose d'ouvert ici."""
    return {"machines": remote_search.describe()}


@router.post("/consult")
async def consult(body: ConsultIn):
    """Recherche dans les bibliothèques que mes machines et mes amis m'ouvrent : la question
    part chez chacune, seuls les passages trouvés reviennent."""
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Question vide.")
    passages, machines = await asyncio.to_thread(
        remote_search.search, question, VIEW_PER_PEER, remote_search.VIEW_TIMEOUT)
    return {"passages": passages, "machines": machines}


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
