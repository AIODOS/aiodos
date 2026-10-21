"""Profil du nœud : ce que cette machine peut offrir au futur réseau. L'interface y lit
aussi ce que keep_private n'a pas pu fermer aux autres comptes (paths.left_open) : pour
cette machine seulement, jamais dans le profil envoyé aux autres."""
from fastapi import APIRouter

from aiodos import paths
from aiodos.node import identity

router = APIRouter(prefix="/api")


@router.get("/node")
def node():
    return {**identity.profile(), "left_open": paths.left_open()}
