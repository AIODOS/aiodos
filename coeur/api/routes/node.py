"""Profil du nœud : ce que cette machine peut offrir au futur réseau."""
from fastapi import APIRouter

from coeur.node import identity

router = APIRouter(prefix="/api")


@router.get("/node")
def node():
    return identity.profile()
