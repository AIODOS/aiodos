"""Réseau public : un annuaire (phase 3.0, docs/phase3-reseau-public.md), et des inconnus
qui font calculer leurs conversations par cette machine (network/public_lending.py,
docs/conception/reseau-public-calcul.md).

Activé par défaut depuis le 27/10/2026 (réglage « public_network », « Rejoindre le réseau
public ») : quand Internet tourne, le compagnon (aiodos-net) rejoint le réseau public avec une clé
à part, qui ne le relie pas aux machines appairées, et par les relais : l'adresse IP de
cette machine n'est pas donnée aux inconnus, mais un inconnu qui fait tourner son propre
relais pourrait la voir (Iroh joint le relais qu'un autre nœud annonce : limite connue). Il y diffuse un profil sans rien de
personnel : la version d'AIODOS et les modèles que cette machine sait calculer. En retour,
la vue Réseau montre les nœuds publics en ligne.

Depuis le 28/10/2026, le profil dit aussi si cette machine prête aux inconnus (« lends » :
le réglage lend_public, et le partage actif, cf. public_lending.lending_now), si une
conversation peut y être prise maintenant (« free » : 1 ou 0) et, si elle est connue, sa
vitesse (« tps », jetons par seconde, node/speed.py). Le compagnon les publie signés. Les
nœuds vus sont gardés typés (_node), « free » et « tps » compris : le côté demandeur y
choisit ses machines (seen, network/placement.py).
"""
import asyncio
import json
import logging
import math
import threading
import time

from aiodos import __version__, settings
from aiodos.i18n import t
from aiodos.network import public_lending, route
from aiodos.network.companion import CompanionError
from aiodos.services import ollama

log = logging.getLogger("aiodos.net")

MAX_MODELS = 24               # comme le compagnon (public_profile.rs)
MAX_TEXT = 64                 # de même, pour une version ou un nom de modèle
MAX_NODES = 200               # nœuds montrés dans la vue Réseau
MAX_KEPT = 2000               # nœuds gardés du dernier état lu au compagnon
MAX_TPS = 100_000             # vitesse au-delà de laquelle un profil ment
MAX_FREE = 255                # places libres dites, au plus (comme le compagnon)
# Le choix des machines (placement.py) relit l'annuaire au compagnon s'il date de plus de
# FRESH_S secondes, au lieu d'attendre la prochaine synchronisation (toutes les 15 s) : un
# prêteur redevenu libre est vu plus tôt (essai réel du 28/10/2026)
FRESH_S = 3
FRESH_TIMEOUT = 2             # le compagnon est sur 127.0.0.1


def _models() -> list:
    try:
        names = sorted({m["name"] for m in ollama.list_models()})
    except Exception:                  # Ollama arrêté : on dira « aucun modèle »
        return []
    return names[:MAX_MODELS]


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _node(raw) -> dict | None:
    """Un nœud vu, tel que le compagnon le dit, typé : sa clé, sa version, ses modèles, s'il
    prête, depuis quand il a été vu, et, s'il les publie, « free » (places libres) et
    « tps » (jetons par seconde)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw["key"]:
        return None
    models = raw.get("models") if isinstance(raw.get("models"), list) else []
    node = {"key": raw["key"][:128],
            "version": raw["version"][:MAX_TEXT] if isinstance(raw.get("version"), str) else "",
            "models": [m[:MAX_TEXT] for m in models if isinstance(m, str)][:MAX_MODELS],
            "lends": raw.get("lends") is True,
            "seen_s": max(0, int(raw["seen_s"])) if _number(raw.get("seen_s")) else 0}
    if _number(raw.get("free")):                  # places libres (0 ou 1 aujourd'hui)
        node["free"] = max(0, min(MAX_FREE, round(raw["free"])))
    if _number(raw.get("tps")) and 0 < raw["tps"] < MAX_TPS:
        node["tps"] = round(float(raw["tps"]), 1)
    return node


def _typed(status) -> dict:
    """L'état lu au compagnon, ses nœuds typés (_node)."""
    if not isinstance(status, dict):
        return {}
    nodes = status.get("nodes") if isinstance(status.get("nodes"), list) else []
    return dict(status, nodes=[n for n in map(_node, nodes[:MAX_KEPT]) if n is not None])


# Le dernier état lu, pour le choix des machines (placement.py), qui n'a pas le gestionnaire
_lock = threading.Lock()
_latest = {"key": None, "nodes": [], "at": 0.0}


def _remember(status):
    with _lock:
        status = status if isinstance(status, dict) and status.get("enabled") else {}
        _latest.update(key=status.get("key"), nodes=list(status.get("nodes") or []),
                       at=time.monotonic())


def _refresh():
    """L'annuaire relu au compagnon s'il date de plus de FRESH_S secondes, et seulement si le
    réseau public est rejoint (dernière synchronisation, Public.sync) ; sinon, ou si le
    compagnon ne répond pas, l'état gardé sert tel quel."""
    with _lock:
        joined, at = _latest["key"] is not None, _latest["at"]
    if not joined or time.monotonic() - at < FRESH_S:
        return
    companion = route.companion()
    if companion is None or not getattr(companion, "running", False):
        return
    try:
        status = _typed(companion.api("GET", "/net/v1/public", timeout=FRESH_TIMEOUT))
    except Exception:  # noqa: BLE001 — compagnon arrêté, lent ou ancien : l'état gardé
        return
    _remember(status)


def seen(fresh: bool = False) -> tuple:
    """(clé de cette machine sur le réseau public, nœuds vus) d'après le dernier état lu au
    compagnon ; « seen_s » de chaque nœud y compte le temps écoulé depuis. Rien si le
    réseau public n'est pas rejoint. fresh : relu d'abord au compagnon s'il date de plus de
    FRESH_S secondes (_refresh)."""
    if fresh:
        _refresh()
    with _lock:
        key, nodes, at = _latest["key"], list(_latest["nodes"]), _latest["at"]
    if not nodes:
        return key, []
    age = int(max(0.0, time.monotonic() - at))
    return key, [dict(node, seen_s=node["seen_s"] + age) for node in nodes]


def forget():
    """Rien de vu (essais)."""
    _remember(None)


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
        return {"enabled": True, "version": __version__, "models": _models(),
                **public_lending.profile_fields(self.m)}

    async def sync(self):
        """Dit au compagnon de rejoindre ou de quitter le réseau public (quand la demande
        change, et après chaque relance du compagnon), puis relit son état."""
        companion = self.m.internet.companion
        if companion is None or not companion.running:
            self._status, self._sent = None, None
            _remember(None)
            return
        request = await asyncio.to_thread(self._request)
        # Un nouveau compagnon (réseau ou Internet relancés) repart de zéro : il faut tout
        # lui redire, même si son compteur de lancements vaut celui de l'ancien
        state = (self.m.internet.launches, companion.generation,
                 json.dumps(request, sort_keys=True))
        try:
            if state != self._sent:
                self._status = _typed(await asyncio.to_thread(companion.api, "PUT",
                                                              "/net/v1/public", request))
                self._sent = state
            elif self.wanted:
                self._status = _typed(await asyncio.to_thread(companion.api, "GET",
                                                              "/net/v1/public"))
                if not self._status.get("enabled"):
                    self._sent = None      # le compagnon n'y est plus : redit au prochain tour
            self._error = None
            _remember(self._status if self.wanted else None)
        except CompanionError as exc:
            if "Chemin inconnu" in str(exc):   # i18n: tel quel (réponse du compagnon, en Rust)
                self._error = t("net.public_unsupported")
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
