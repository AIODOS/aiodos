"""Mises à jour : état, vérification immédiate, redémarrage, message vu ; approbation des
versions sur un deuxième appareil (updates/approval.py). Le clic « Approuver » porte
l'empreinte (« digest ») de la ligne montrée : seul ce texte-là est signé, même si la
machine en montre un autre sous le même numéro."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos.i18n import t
from aiodos.network.client import PeerError
from aiodos.node import identity
from aiodos.updates import approval
from aiodos.updates import manifest as manifests
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

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
        raise HTTPException(409, t("updates.nothing_to_restart"))
    return {"ok": True}


@router.post("/updates/notice/seen")
def notice_seen(request: Request):
    request.app.state.updates.notice_seen()
    return {"ok": True}


# ------------------------------------------------- approbation (deuxième appareil)
class Approve(BaseModel):
    node_id: str
    version: str
    # Empreinte du texte de la ligne choisie (approval.pending_from_peers, « digest ») ;
    # absente (interface plus ancienne) : approval.approve ne signe qu'un numéro qui n'a
    # jamais montré qu'un texte
    digest: str | None = None


@router.get("/updates/approval")
def approval_state():
    """Clé d'approbation de cet appareil, si elle existe ; versions publiées ici qui
    attendent leur approbation."""
    return {"key": approval.public_info(), "required": bool(manifests.declared_approvers()),
            "waiting_here": [version for version, _ in Store().pending()]}


@router.get("/updates/approval/pending")
def approval_pending():
    """Versions qui attendent une approbation chez mes machines en ligne."""
    return {"pending": approval.pending_from_peers(identity.node_id())}


@router.post("/updates/approval/key")
def approval_key():
    try:
        return approval.create_key()
    except UpdateError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/updates/approval/approve")
def approval_approve(body: Approve):
    try:
        return approval.approve(identity.node_id(), body.node_id, body.version,
                                shown=body.digest)
    except (UpdateError, PeerError) as exc:
        raise HTTPException(400, str(exc)) from exc
