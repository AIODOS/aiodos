"""Le menu des modèles (chat/model_menu.py ; docs/conception/modeles-auto.md § 4) : pour le
texte, les images, les vidéos et l'audio, « Automatique » et ce qu'il prendrait, la suggestion
pour cette machine, et les modèles, du plus puissant au plus modeste. mine_only : « Sur mes
machines seulement » (rien du réseau public)."""
import asyncio

from fastapi import APIRouter, Request

from aiodos.chat import model_menu

router = APIRouter(prefix="/api/models")


@router.get("/menu")
async def menu(request: Request, mine_only: bool = False):
    executor = getattr(request.app.state, "executor", None)
    return await asyncio.to_thread(model_menu.menu, executor, mine_only)
