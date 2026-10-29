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
choisit ses machines (seen, network/placement.py). Une conversation publique qui commence ou
finit chez moi redit le profil sans attendre la synchronisation suivante (Public.push).

Depuis le 29/10/2026, « models » dit aussi les modèles d'images et de vidéos que cette machine
prête aux inconnus, marqués « image: » (network/public_imaging.py, network/public_video.py) :
le profil signé du compagnon n'a pas d'autre champ pour eux.

La première minute (essai réel du 28/10/2026) : tant que le réseau public n'est pas rejoint,
ou rejoint sans qu'aucun nœud n'ait encore été vu, et depuis moins de SEARCH_S secondes (le
démarrage, ou l'arrivée sur le réseau), on cherche encore (searching) : l'interface d'une
machine sans modèle dit « Recherche des machines du réseau public… » plutôt qu'« aucune ne
peut répondre » (chat/public_consent.py, entry).

Depuis le 29/10/2026 au soir (contre la collusion, network/sightings.py) : chaque lecture de
l'annuaire au compagnon (la synchronisation toutes les 15 s, la relecture du choix des
machines) note la première vue de chaque nœud (_sight, sightings.sighted), à 15 s près, au
lieu d'attendre une réponse publique ; le compagnon dit aussi depuis quand il connaît chaque
nœud (« known_s », gardé ici : placement._known). Et, la première fois, pendant que le
compagnon cherche la clé du nœud public (une preuve de travail : une dizaine de secondes sur un
téléphone), l'état le dit (« preparing ») : la vue Réseau affiche « Préparation de l'identité
publique… » au lieu de « connexion… ».
"""
import asyncio
import json
import logging
import math
import threading
import time

from aiodos import __version__, settings
from aiodos.i18n import t
from aiodos.network import public_imaging, public_lending, public_video, route, sightings
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
# Recherche des machines du réseau public : les profils partent chaque minute (et dans les
# 5 s quand ils changent), plus la recherche de voisins par la DHT
SEARCH_S = 90


def _models() -> list:
    """Les modèles du profil : ceux de conversation, puis les modèles d'images et de vidéos
    prêtés aux inconnus, marqués (public_imaging.offered, public_video.offered :
    « image:z-image-turbo », « image:wan2.1-t2v-1.3b »), en dernier, et toujours là
    (MAX_MODELS au plus en tout). Un nom d'Ollama qui commencerait par la marque n'est pas
    annoncé : il passerait pour un modèle d'images."""
    images = (public_imaging.offered() + public_video.offered())[:MAX_MODELS]
    try:
        names = sorted({m["name"] for m in ollama.list_models()
                        if not str(m["name"]).startswith(public_imaging.MARKER)})
    except Exception:                  # Ollama arrêté : on dira « aucun modèle »
        names = []
    return names[:MAX_MODELS - len(images)] + images


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _node(raw) -> dict | None:
    """Un nœud vu, tel que le compagnon le dit, typé : sa clé, sa version, ses modèles, s'il
    prête, depuis quand il a été vu, et, s'il les publie, « free » (places libres) et
    « tps » (jetons par seconde) ; « known_s », si le compagnon le dit (depuis le 29/10/2026) :
    depuis combien de secondes il connaît ce nœud (ce que lui-même a vu, pas ce que le nœud
    annonce)."""
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
    if _number(raw.get("known_s")):
        node["known_s"] = max(0, int(raw["known_s"]))
    return node


def _typed(status) -> dict:
    """L'état lu au compagnon, ses nœuds typés (_node)."""
    if not isinstance(status, dict):
        return {}
    nodes = status.get("nodes") if isinstance(status.get("nodes"), list) else []
    return dict(status, nodes=[n for n in map(_node, nodes[:MAX_KEPT]) if n is not None])


# Le dernier état lu, pour le choix des machines (placement.py), qui n'a pas le gestionnaire ;
# « since » : depuis quand on cherche (le démarrage, puis l'arrivée sur le réseau), « found » :
# un nœud a été vu depuis (searching)
_lock = threading.Lock()
_latest = {"key": None, "nodes": [], "at": 0.0, "since": time.monotonic(), "found": False}


def _remember(status):
    with _lock:
        status = status if isinstance(status, dict) and status.get("enabled") else {}
        key, nodes, now = status.get("key"), list(status.get("nodes") or []), time.monotonic()
        if key is not None and _latest["key"] is None:      # le réseau (re)joint : on cherche
            _latest.update(since=now, found=False)
        _latest.update(key=key, nodes=nodes, at=now, found=_latest["found"] or bool(nodes))


def _sight(status):
    """La première vue de chaque nœud de cet état lu au compagnon (sightings.sighted : une clé
    déjà notée ne l'est de nouveau qu'une fois par jour, sans écrire en base), à chaque lecture
    de l'annuaire (cf. le début du module). Une écriture en base : hors de la boucle du
    gestionnaire. Jamais d'échec : une machine non notée passe pour vue à l'instant, ce qui ne
    lui donne rien."""
    nodes = status.get("nodes") if isinstance(status, dict) and status.get("enabled") else None
    if not nodes:
        return
    try:
        sightings.sighted(nodes)
    except Exception:  # noqa: BLE001 — base occupée… : à la lecture suivante
        log.debug("Réseau public : premières vues non notées", exc_info=True)  # i18n: tel quel


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
    _sight(status)


def seen(fresh: bool = False) -> tuple:
    """(clé de cette machine sur le réseau public, nœuds vus) d'après le dernier état lu au
    compagnon ; « seen_s » et « known_s » de chaque nœud y comptent le temps écoulé depuis.
    Rien si le réseau public n'est pas rejoint. fresh : relu d'abord au compagnon s'il date de
    plus de FRESH_S secondes (_refresh)."""
    if fresh:
        _refresh()
    with _lock:
        key, nodes, at = _latest["key"], list(_latest["nodes"]), _latest["at"]
    if not nodes:
        return key, []
    age = int(max(0.0, time.monotonic() - at))
    return key, [dict(node, seen_s=node["seen_s"] + age,
                      **({"known_s": node["known_s"] + age} if "known_s" in node else {}))
                 for node in nodes]


def searching() -> bool:
    """Cherche-t-on encore les machines du réseau public ? Aucun nœud vu depuis le démarrage
    ou l'arrivée sur le réseau, il y a moins de SEARCH_S secondes (cf. le début du module)."""
    with _lock:
        return not _latest["found"] and time.monotonic() - _latest["since"] < SEARCH_S


def forget():
    """Rien de vu, et la première minute passée (essais)."""
    _remember(None)
    with _lock:
        _latest.update(since=float("-inf"), found=False)


class Public:
    def __init__(self, manager):
        self.m = manager
        self._status = None        # dernier état du réseau public lu au compagnon
        self._sent = None          # (lancement du compagnon, demande envoyée)
        self._error = None
        self._syncing = asyncio.Lock()     # une synchronisation à la fois (push, la boucle)
        self._pushed = threading.Event()   # une poussée attend déjà son tour : pas deux

    def push(self) -> bool:
        """Le profil redit au compagnon sans attendre la synchronisation suivante (une
        conversation publique commence ou finit ici, public_lending._announce) : planifiée sur
        la boucle du gestionnaire, sans attendre ; une seule à la fois en attente. → vrai si
        elle est partie."""
        loop = getattr(self.m, "_loop", None)
        if loop is None or loop.is_closed() or self._pushed.is_set():
            return False
        self._pushed.set()
        try:
            asyncio.run_coroutine_threadsafe(self._push(), loop)
        except RuntimeError:               # la boucle s'arrête
            self._pushed.clear()
            return False
        return True

    async def _push(self):
        self._pushed.clear()               # la suivante lira l'état d'après celle-ci
        try:
            await self.sync()
        except Exception:  # noqa: BLE001 — la boucle de synchronisation redira tout
            log.debug("Profil public : envoi anticipé en échec", exc_info=True)  # i18n: tel quel

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
        change, et après chaque relance du compagnon), puis relit son état. Une à la fois :
        la boucle du gestionnaire, la vue Réseau et les poussées (push) ne se croisent pas."""
        async with self._syncing:
            await self._sync()

    async def _sync(self):
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
                # Le compagnon n'y est plus : redit au prochain tour ; sauf s'il cherche encore
                # la clé du nœud public (« preparing ») : il rejoindra le réseau de lui-même
                if not self._status.get("enabled") and not self._status.get("preparing"):
                    self._sent = None
            self._error = None
            _remember(self._status if self.wanted else None)
            if self.wanted:
                await asyncio.to_thread(_sight, self._status)
        except CompanionError as exc:
            if "Chemin inconnu" in str(exc):   # i18n: tel quel (réponse du compagnon, en Rust)
                self._error = t("net.public_unsupported")
            else:
                self._error = str(exc)
            log.info("Réseau public : %s", self._error)

    def snapshot(self) -> dict:
        status = self._status or {}
        nodes = status.get("nodes") or []
        joined = bool(status.get("enabled"))
        return {
            "wanted": self.wanted,
            "internet": self.m.internet.enabled,
            "joined": joined,
            # La clé du nœud public se cherche (la première fois, cf. le début du module)
            "preparing": self.wanted and not joined and status.get("preparing") is True,
            "key": status.get("key"),
            "neighbors": status.get("neighbors") or 0,
            "count": len(nodes),
            "nodes": nodes[:MAX_NODES],
            "rendezvous": status.get("rendezvous"),
            "error": self._error or status.get("error"),
        }
