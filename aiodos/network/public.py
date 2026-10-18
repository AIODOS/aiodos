"""Réseau public, phase 3.0 : un annuaire, sans rien confier (docs/phase3-reseau-public.md).

Désactivé par défaut (réglage « public_network », « Rejoindre le réseau public »). Activé,
et quand Internet tourne, le compagnon (aiodos-net) rejoint le réseau public avec une clé
à part, qui ne le relie pas aux machines appairées, et par les relais seulement : les
inconnus ne voient pas l'adresse IP de cette machine. Il y diffuse un profil sans rien de
personnel : la version d'AIODOS et les modèles que cette machine sait calculer. En retour,
la vue Réseau montre les nœuds publics en ligne.

En 3.0, rien n'est confié aux inconnus et rien n'est calculé pour eux : le profil dit
donc « ne prête pas » (« lends » : faux), quels que soient les réglages de prêt.
"""
import asyncio
import json
import logging

from aiodos import __version__, settings
from aiodos.network.companion import CompanionError
from aiodos.services import ollama

log = logging.getLogger("aiodos.net")

MAX_MODELS = 24               # comme le compagnon (public_profile.rs)
MAX_NODES = 200               # nœuds montrés dans la vue Réseau


def _models() -> list:
    try:
        names = sorted({m["name"] for m in ollama.list_models()})
    except Exception:                  # Ollama arrêté : on dira « aucun modèle »
        return []
    return names[:MAX_MODELS]


class Public:
    def __init__(self, manager):
        self.m = manager
        self._status = None        # dernier état du réseau public lu au compagnon
        self._sent = None          # (lancement du compagnon, demande envoyée)
        self._error = None

    @property
    def wanted(self) -> bool:
        return bool(settings.get("public_network"))

    def _request(self) -> dict:
        if not self.wanted:
            return {"enabled": False}
        return {"enabled": True, "version": __version__, "models": _models(), "lends": False}

    async def sync(self):
        """Dit au compagnon de rejoindre ou de quitter le réseau public (quand la demande
        change, et après chaque relance du compagnon), puis relit son état."""
        companion = self.m.internet.companion
        if companion is None or not companion.running:
            self._status, self._sent = None, None
            return
        request = await asyncio.to_thread(self._request)
        state = (companion.generation, json.dumps(request, sort_keys=True))
        try:
            if state != self._sent:
                self._status = await asyncio.to_thread(companion.api, "PUT", "/net/v1/public",
                                                       request)
                self._sent = state
            elif self.wanted:
                self._status = await asyncio.to_thread(companion.api, "GET", "/net/v1/public")
            self._error = None
        except CompanionError as exc:
            if "Chemin inconnu" in str(exc):
                self._error = ("Le compagnon Internet de cette version ne connaît pas encore le "
                               "réseau public : il arrivera avec la prochaine mise à jour.")
            else:
                self._error = str(exc)
            log.info("Réseau public : %s", self._error)

    def snapshot(self) -> dict:
        status = self._status or {}
        nodes = status.get("nodes") or []
        return {
            "wanted": self.wanted,
            "internet": self.m.internet.enabled,
            "joined": bool(status.get("enabled")),
            "key": status.get("key"),
            "neighbors": status.get("neighbors") or 0,
            "count": len(nodes),
            "nodes": nodes[:MAX_NODES],
            "rendezvous": status.get("rendezvous"),
            "error": self._error or status.get("error"),
        }
