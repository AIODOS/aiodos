"""Les entrées d'une appli (docs/conception/partage-puissance.md § 5.1, 6.6) :
HEAD /aiodos/v1/inputs/{sha256} dit si cette appli l'a déjà envoyée (200, ou 404) ;
PUT /aiodos/v1/inputs/{sha256}, corps brut, la range (201 créée, 200 déjà là), avec
{sha256, size, type}. Le travail les cite ensuite par empreinte (« inputs »)."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from aiodos.gateway import errors, input_store
from aiodos.gateway.errors import ApiError

router = APIRouter(prefix="/aiodos/v1")


@router.head("/inputs/{sha256}")
async def has_input(sha256: str, request: Request):
    context = request.state.aiodos_app
    known = await asyncio.to_thread(input_store.info, context.id, sha256)
    if known is None:
        return Response(status_code=404)
    return Response(status_code=200, headers={"X-Aiodos-Size": str(known["size"]),
                                              "X-Aiodos-Type": known["type"]})


@router.put("/inputs/{sha256}")
async def put_input(sha256: str, request: Request):
    context = request.state.aiodos_app
    lang = errors.lang_of(request.headers)
    try:
        if context is None or context.kind == "general":
            raise ApiError("scope_denied", "Les entrées s'envoient avec la clé d'une "
                           "application.", detail={"scope": "engines:comfyui"})
        found, created = await input_store.receive(
            context.id, sha256, request.stream(), request.headers.get("content-type"))
    except ApiError as exc:
        return errors.response(request.url.path, exc, lang)
    return JSONResponse(found, status_code=201 if created else 200)
