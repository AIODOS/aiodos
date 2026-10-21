"""Les routes de la demande d'accès sur la passerelle (docs/api-applications.md § 4.3) :
demande, relève de la clé, retrait par l'appli elle-même. La logique est dans
access_flow.py ; l'accord, lui, ne se donne que dans l'interface d'AIODOS
(api/routes/gateway_apps.py)."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aiodos.gateway import access_flow, apps, errors
from aiodos.gateway.errors import ApiError

router = APIRouter(prefix="/aiodos/v1/access")


async def _json(request: Request):
    try:
        return await request.json()
    except ValueError as exc:
        raise ApiError("invalid_request", "Corps JSON illisible.") from exc


def _failure(request: Request, exc: ApiError):
    return errors.response(request.url.path, exc, errors.lang_of(request.headers))


@router.post("/requests")
async def request_access(request: Request):
    """Sans clé : une nouvelle appli. Avec une clé d'appli valide : des droits en plus pour
    la même clé."""
    try:
        body = await _json(request)
        upgrade_of = None
        context = getattr(request.state, "aiodos_app", None)
        if context is not None and context.kind == "app":
            upgrade_of = context.id
        caller = request.state.aiodos_caller
        reply = await asyncio.to_thread(access_flow.start, body, caller, upgrade_of=upgrade_of)
    except ApiError as exc:
        return _failure(request, exc)
    return reply


@router.post("/token")
async def token(request: Request):
    try:
        body = await _json(request)
    except ApiError as exc:
        return _failure(request, exc)
    if not isinstance(body, dict) or body.get("grant_type") != \
            "urn:ietf:params:oauth:grant-type:device_code":
        return JSONResponse({"error": "unsupported_grant_type",
                             "error_description": "grant_type attendu : "
                             "urn:ietf:params:oauth:grant-type:device_code."}, status_code=400)
    status, reply = await asyncio.to_thread(access_flow.poll, body.get("device_code"))
    return JSONResponse(reply, status_code=status, headers={"Cache-Control": "no-store"})


@router.post("/revoke")
async def revoke(request: Request):
    """L'appli se retire elle-même (bouton « Se déconnecter d'AIODOS ») : esprit de la
    RFC 7009. La clé générale ne se retire pas ainsi (elle se renouvelle dans AIODOS)."""
    context = request.state.aiodos_app
    if context.kind == "general":
        return _failure(request, ApiError("invalid_request", "La clé générale se renouvelle "
                                          "dans AIODOS (Réglages → Applications et autres "
                                          "outils)."))
    await asyncio.to_thread(apps.revoke, context.id)
    access_flow.publish({"type": "gateway"})
    return {"revoked": True}
