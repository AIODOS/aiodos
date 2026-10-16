"""Mises à jour : état, vérification immédiate, redémarrage, message vu."""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api")


@router.get("/updates")
def read(request: Request):
    return request.app.state.updates.status()


@router.post("/updates/check")
def check(request: Request):
    request.app.state.updates.check_now()
    return request.app.state.updates.status()


@router.post("/updates/restart")
def restart_now(request: Request):
    if not request.app.state.updates.restart_now():
        raise HTTPException(409, "Aucune nouvelle version prête à démarrer.")
    return {"ok": True}


@router.post("/updates/notice/seen")
def notice_seen(request: Request):
    request.app.state.updates.notice_seen()
    return {"ok": True}
