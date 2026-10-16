"""Conversations et dossiers : liste, lecture, renommage, rangement, suppression."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos import render
from aiodos.chat import conversations

router = APIRouter(prefix="/api")


class ConversationPatch(BaseModel):
    title: str | None = None
    folder_id: int | None = None


class FolderIn(BaseModel):
    name: str


def _notify(request: Request):
    request.app.state.bus.publish({"type": "conversations"})


@router.get("/conversations")
def tree():
    return conversations.list_tree()


@router.get("/conversations/{conv_id}")
def read(conv_id: int):
    conv = conversations.get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversation introuvable.")
    for m in conv["messages"]:
        if m["role"] == "assistant":
            m["html"] = render.to_html(m["content"])
    return conv


@router.patch("/conversations/{conv_id}")
def update(conv_id: int, body: ConversationPatch, request: Request):
    try:
        if body.title is not None:
            conversations.rename(conv_id, body.title)
        if "folder_id" in body.model_fields_set:          # null = remettre à la racine
            conversations.move(conv_id, body.folder_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _notify(request)
    return {"ok": True}


@router.delete("/conversations/{conv_id}")
def delete(conv_id: int, request: Request):
    conversations.delete(conv_id)
    _notify(request)
    return {"ok": True}


@router.post("/folders")
def create_folder(body: FolderIn, request: Request):
    try:
        folder_id = conversations.create_folder(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _notify(request)
    return {"id": folder_id}


@router.patch("/folders/{folder_id}")
def rename_folder(folder_id: int, body: FolderIn, request: Request):
    try:
        conversations.rename_folder(folder_id, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _notify(request)
    return {"ok": True}


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int, request: Request):
    conversations.delete_folder(folder_id)
    _notify(request)
    return {"ok": True}
