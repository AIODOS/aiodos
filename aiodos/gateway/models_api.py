"""GET /aiodos/v1/models?kind=chat|embed : le catalogue par modèle logique, avec où il se
trouve, filtré selon les droits et le lieu de l'appli (docs/api-applications.md § 5.2).
Les fiches viennent de model_card.py, comme celles de /v1/models."""
import asyncio

from fastapi import APIRouter, Request

from aiodos.gateway import catalog, errors, model_card, scopes
from aiodos.gateway.errors import ApiError
from aiodos.i18n import t

router = APIRouter(prefix="/aiodos/v1")
KINDS = ("chat", "embed")


@router.get("/models")
async def models(request: Request, kind: str = "chat"):
    if kind not in KINDS:
        return errors.response(request.url.path, ApiError(
            "invalid_request", t("gateway_api.kind_invalid"), param="kind"),
            errors.lang_of(request.headers))
    if not scopes.has(request, kind):
        return {"object": "list", "models": []}
    try:
        where = scopes.for_request(request, kind)
    except ApiError as exc:
        return errors.response(request.url.path, exc, errors.lang_of(request.headers))
    entries = await asyncio.to_thread(catalog.entries)
    found = await asyncio.to_thread(model_card.logical, model_card.allowed(entries, where), kind)
    return {"object": "list", "models": found}
