"""Les applications vues par l'interface (Réglages → Applications et autres outils),
derrière le jeton de lancement : c'est seulement ici qu'un accès s'accorde (invariant I11),
se modifie ou se retire, jamais par la passerelle (docs/api-applications.md § 4).

- Demandes en attente (la carte d'accord) : accorder, en décochant si l'on veut des droits
  ou des cercles ; refuser ; refuser et bloquer le programme.
- Accès : liste, compteurs et journal sans contenu, modification (effet immédiat, même
  clé), retrait (ses réponses en cours s'arrêtent).
- Clés « à la main » pour un outil : un nom, des droits, des cercles ; la clé est montrée
  une seule fois.
- Programmes bloqués.
"""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aiodos.gateway import access_flow, apps, ownership, scopes, usage
from aiodos.gateway.errors import ApiError

router = APIRouter(prefix="/api/gateway")


class ApproveIn(BaseModel):
    scopes: list[str] | None = None
    where: list[str] | None = None
    priority: str = "interactive"
    duration: str = "forever"
    pin: bool = False
    replace: bool = True
    libraries: list[list[float]] = []


class DenyIn(BaseModel):
    block: bool = False


class AppIn(BaseModel):
    scopes: list[str] | None = None
    where: list[str] | None = None
    priority: str | None = None
    exe_pinned: bool | None = None
    libraries: list[list[float]] | None = None


class ManualIn(BaseModel):
    name: str
    scopes: list[str]
    where: list[str] = ["here", "mine"]
    container: bool = False             # outil dans un conteneur Docker : autre programme


class BlockedIn(BaseModel):
    exe_path: str


def _bad(exc: ApiError):
    raise HTTPException(exc.status if exc.status < 500 else 400, str(exc))


def _refresh(request: Request):
    request.app.state.bus.publish({"type": "gateway"})


# ------------------------------------------------------------------ demandes
@router.get("/requests")
async def requests_pending():
    return await asyncio.to_thread(access_flow.pending)


@router.post("/requests/{request_id}/approve")
async def approve(request_id: str, body: ApproveIn, request: Request):
    libraries = [(int(pair[0]), float(pair[1])) for pair in body.libraries if len(pair) == 2]
    try:
        view = await asyncio.to_thread(
            access_flow.approve, request_id, scopes_granted=body.scopes, where=body.where,
            priority=body.priority, duration=body.duration, pin=body.pin,
            replace=body.replace, libraries=libraries)
    except ApiError as exc:
        _bad(exc)
    _refresh(request)
    return view


@router.post("/requests/{request_id}/deny")
async def deny(request_id: str, body: DenyIn, request: Request):
    if not await asyncio.to_thread(access_flow.deny, request_id, body.block):
        raise HTTPException(404, "Demande inconnue, expirée ou déjà traitée.")
    _refresh(request)
    return {"denied": True, "blocked": body.block}


# --------------------------------------------------------------------- accès
def _with_usage(app: dict) -> dict:
    return {**app, "usage": usage.summary(app["id"])}


@router.get("/apps")
async def list_apps():
    found = await asyncio.to_thread(apps.list_apps)
    return await asyncio.to_thread(lambda: [_with_usage(a) for a in found])


@router.get("/apps/{app_id}/log")
async def app_log(app_id: str, limit: int = 50):
    if await asyncio.to_thread(apps.get, app_id) is None:
        raise HTTPException(404, "Accès inconnu.")
    return await asyncio.to_thread(usage.recent, app_id, limit)


@router.patch("/apps/{app_id}")
async def change_app(app_id: str, body: AppIn, request: Request):
    current = await asyncio.to_thread(apps.get, app_id)
    if current is None or current["revoked"]:
        raise HTTPException(404, "Accès inconnu ou retiré.")
    fields = {}
    try:
        if body.scopes is not None:
            fields["scopes"] = scopes.parse_scopes(body.scopes)
        if body.where is not None:
            fields["reach"] = scopes.parse_where(body.where)
            if current["kind"] == "app":        # un cercle ajouté ici est montré, donc accordé
                now = time.time()
                consents = current["consents"]
                for cap in fields.get("scopes", current["scopes"]):
                    served = scopes.SERVED.get(cap, frozenset())
                    consents[cap] = {c: now for c in fields["reach"] if c in served}
                fields["consents"] = consents
    except ApiError as exc:
        _bad(exc)
    if body.priority is not None:
        fields["priority"] = body.priority
    if body.exe_pinned is not None:
        fields["exe_pinned"] = body.exe_pinned and bool(current["exe_path"])
    if body.libraries is not None:
        fields["libraries"] = [(int(p[0]), float(p[1])) for p in body.libraries if len(p) == 2]
    await asyncio.to_thread(lambda: apps.update(app_id, **fields))
    _refresh(request)
    return await asyncio.to_thread(apps.get, app_id)


@router.delete("/apps/{app_id}")
async def revoke_app(app_id: str, request: Request):
    if not await asyncio.to_thread(apps.revoke, app_id):
        raise HTTPException(404, "Accès inconnu ou déjà retiré.")
    # Ses travaux en cours s'arrêtent, ses fichiers commencent leurs 7 jours de garde
    await asyncio.to_thread(ownership.release, app_id)
    request.app.state.executor.wake()
    _refresh(request)
    return {"revoked": True}


@router.post("/apps")
async def manual_key(body: ManualIn, request: Request):
    """Une clé « à la main » pour un outil (Open WebUI, un script…) : montrée une seule
    fois. Elle remplace avantageusement la clé générale : ses droits et ses cercles sont les
    siens."""
    try:
        name = access_flow.clean_text(body.name, 40, "name", required=True)
        granted = scopes.parse_scopes(body.scopes)
        where = scopes.parse_where(body.where)
    except ApiError as exc:
        _bad(exc)
    if not granted:
        raise HTTPException(400, "Choisis au moins un droit.")
    ident, key = await asyncio.to_thread(lambda: apps.create(
        kind="manual", name=name, scopes_granted=granted, reach=where,
        account_check=not body.container))
    _refresh(request)
    return {"id": ident, "key": key, "app": await asyncio.to_thread(apps.get, ident)}


# ------------------------------------------------------------- programmes bloqués
@router.get("/blocked")
async def blocked():
    return await asyncio.to_thread(access_flow.blocked)


@router.post("/blocked/remove")
async def unblock(body: BlockedIn, request: Request):
    if not await asyncio.to_thread(access_flow.unblock, body.exe_path):
        raise HTTPException(404, "Programme inconnu.")
    _refresh(request)
    return {"unblocked": True}
