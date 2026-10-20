"""Prêter sa carte graphique à ses machines pour un gros modèle (la machine qui prête).

Une de MES machines demande « cluster/lend ». Jamais un ami pour l'instant : le serveur
RPC de llama.cpp est encore un prototype, sans défense contre un client malveillant.
Si le réglage « Prêter ma carte graphique » est activé ici, et si llama.cpp a la même
version des deux côtés, un ggml-rpc-server démarre sur 127.0.0.1 avec la meilleure carte.
Le compagnon l'ouvre à cette seule machine (tunnel chiffré). Le serveur s'arrête quand
elle le rend (« cluster/release »), ou quand elle ne donne plus de nouvelles
(« cluster/keep ») depuis LEASE_S secondes. Pendant le prêt, les unités de fond de cette
machine laissent la carte à la conversation, comme pour une conversation locale.
"""
import logging
import secrets
import threading
import time
from pathlib import Path

from aiodos import paths, settings
from aiodos.cluster import engine
from aiodos.network import peers, route, sharing
from aiodos.network.client import PeerError
from aiodos.node import android_app

log = logging.getLogger("aiodos.cluster")

LEASE_S = 90                  # sans nouvelles de la machine qui emprunte : carte rendue
SWEEP_S = 10
START_TIMEOUT = 60

_lock = threading.Lock()
_loans = {}                   # service -> {"owner", "net_id", "process", "device", "expires"}
_sweeper = {"thread": None}


def cache_dir() -> Path:
    """Poids reçus, gardés pour la prochaine fois (cache du serveur RPC)."""
    return paths.data_dir() / "cluster" / "cache"


def _companion():
    companion = route.companion()
    if companion is None or not companion.running:
        raise PeerError("Le compagnon aiodos-net ne tourne pas sur cette machine "
                        "(réglage « Relier mes machines par Internet »).")
    return companion


def _publish():
    """Le compagnon n'ouvre que les prêts en cours, chacun à sa seule machine."""
    with _lock:
        services = [{"name": name, "port": loan["process"].port, "peers": [loan["net_id"]]}
                    for name, loan in _loans.items()]
    _companion().expose(services)


def lend(manager, sender, payload):
    if not settings.get("lend_gpu"):
        raise PeerError(f"« {manager.name()} » ne prête pas sa carte graphique (réglage "
                        "« Prêter ma carte graphique pour les gros modèles »).")
    if sharing.level(sender) != sharing.MINE:
        raise PeerError("Seules mes machines peuvent emprunter cette carte graphique.")
    if android_app.in_app():
        # ggml-rpc-server n'a ni mot de passe ni chiffrement : sur un téléphone, n'importe
        # quelle appli joindrait son port de 127.0.0.1
        raise PeerError(f"« {manager.name()} » est un téléphone : il ne prête pas sa "
                        "puissance aux gros modèles répartis.")
    peer = peers.get(sender) or {}
    if not peer.get("net_id"):
        raise PeerError("Clé Internet de ta machine inconnue ici : il faut le compagnon "
                        "aiodos-net des deux côtés.")
    found = engine.info(max_age=0)                 # mémoire libre de maintenant
    if not found:
        raise PeerError(f"llama.cpp n'est pas installé sur « {manager.name()} ».")
    if payload.get("version") != found["version"]:
        raise PeerError(f"Versions de llama.cpp différentes : {found['version']} sur "
                        f"« {manager.name()} », {payload.get('version')} chez toi.")
    device = engine.lent_device(found["devices"])
    if not device:
        raise PeerError(f"Aucune carte graphique à prêter sur « {manager.name()} ».")
    _companion()                                  # indispensable : vérifié avant tout lancement
    process = engine.rpc_server(Path(found["folder"]), device["name"], cache_dir())
    try:
        process.start(timeout=START_TIMEOUT)
    except engine.EngineError as exc:
        raise PeerError(f"Serveur RPC impossible sur « {manager.name()} » : {exc}") from exc
    name = f"rpc-{secrets.token_hex(6)}"
    with _lock:
        _loans[name] = {"owner": sender, "net_id": peer["net_id"], "process": process,
                        "device": device["name"], "expires": time.monotonic() + LEASE_S}
    try:
        _publish()
    except Exception as exc:
        _end(name)
        raise PeerError(f"Le compagnon refuse d'ouvrir la carte : {exc}") from exc
    if manager.executor:
        manager.executor.interactive_begin()      # la carte va à la conversation
    _start_sweeper(manager)
    log.info("Carte %s prêtée à %s (%s)", device["name"], peer.get("name") or sender, name)
    return {"service": name, "device": device["name"], "label": device["label"],
            "free_mib": device["free_mib"]}


def keep(manager, sender, payload):
    """La machine qui emprunte est toujours là : le prêt continue, sauf si le réglage a
    été coupé entre-temps (la carte est alors reprise)."""
    name = str(payload.get("service") or "")
    with _lock:
        loan = _loans.get(name)
        if not loan or loan["owner"] != sender:
            return {"ok": False}
        lent = bool(settings.get("lend_gpu"))
        if lent:
            loan["expires"] = time.monotonic() + LEASE_S
    if not lent:
        _end(name, manager)
        return {"ok": False}
    return {"ok": True}


def release(manager, sender, payload):
    name = str(payload.get("service") or "")
    with _lock:
        loan = _loans.get(name)
        if not loan or loan["owner"] != sender:
            return {"ok": False}
    _end(name, manager)
    return {"ok": True}


def _end(name, manager=None):
    with _lock:
        loan = _loans.pop(name, None)
    if not loan:
        return
    loan["process"].stop()
    try:
        _publish()
    except Exception as exc:                      # compagnon arrêté : plus rien d'ouvert
        log.info("Services du compagnon non mis à jour : %s", exc)
    if manager and manager.executor:
        manager.executor.interactive_end()
    log.info("Prêt %s terminé", name)


def stop_all(manager=None):
    for name in list(_loans):
        _end(name, manager)


def _start_sweeper(manager):
    with _lock:
        if _sweeper["thread"] and _sweeper["thread"].is_alive():
            return
        thread = threading.Thread(target=_sweep, args=(manager,), daemon=True,
                                  name="aiodos-cluster-prets")
        _sweeper["thread"] = thread
    thread.start()


def _sweep(manager):
    """Rend les cartes dont l'emprunteur ne donne plus de nouvelles, ou dont le serveur
    s'est arrêté ; s'arrête quand il n'y a plus de prêt."""
    while True:
        time.sleep(SWEEP_S)
        now = time.monotonic()
        with _lock:
            ended = [name for name, loan in _loans.items()
                     if loan["expires"] < now or not loan["process"].alive]
            left = len(_loans) - len(ended)
        for name in ended:
            _end(name, manager)
        if not left:
            with _lock:
                if not _loans:
                    _sweeper["thread"] = None
                    return


HANDLERS = {
    "cluster/lend": lend,
    "cluster/keep": keep,
    "cluster/release": release,
}
