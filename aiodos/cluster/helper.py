"""Prêter sa carte graphique à ses machines pour un gros modèle (la machine qui prête).

Une de MES machines demande « cluster/lend ». Jamais un ami pour l'instant : le serveur
RPC de llama.cpp est encore un prototype, sans défense contre un client malveillant.
Si le réglage « Prêter ma carte graphique » est activé ici, et si llama.cpp a la même
version des deux côtés, un ggml-rpc-server démarre sur 127.0.0.1 avec la meilleure carte.
Le compagnon l'ouvre à cette seule machine (tunnel chiffré), et le **tient** : ce serveur
n'a pas de mot de passe, et n'importe quel programme de cette machine (un autre compte du
PC) pourrait s'y connecter ; il ne sert qu'un client à la fois, dans l'ordre d'arrivée, et
le compagnon y garde toujours ses propres connexions, servie et suivante (compagnon/src/
hold.rs). S'il voit un autre programme connecté au serveur, il ne le laisse jamais passer
et le dit (« blocked ») : le prêt est arrêté aussitôt.

Le serveur s'arrête aussi quand la machine qui emprunte le rend (« cluster/release »),
quand elle ne donne plus de nouvelles (« cluster/keep ») depuis LEASE_S secondes, dès que
la connexion de son llama-server par le tunnel est tombée (DROP_LOADED_S une fois le modèle
chargé, DROP_S avant : au démarrage, llama-server ouvre quelques connexions brèves), ou dès
que le compagnon ne le tient plus (arrêté, relancé, remplacé par un plus ancien) : un
serveur RPC sans garde ne reste pas ouvert aux autres programmes de cette machine. Pendant
le prêt, les unités de fond de cette machine laissent la carte à la conversation, comme
pour une conversation locale.

Reste ouvert, chez la machine qui prête : l'instant entre le démarrage du serveur et la
première connexion du compagnon (un intrus servi alors et toujours connecté est vu, et le
prêt arrêté ; un intrus qui frappe et repart aussitôt ne l'est pas). C'est pourquoi le prêt
reste réservé à mes machines.

Seule une machine dont le tunnel est gardé emprunte (« tunnel »: « guarded », cf.
cluster/group.py) : chez une machine plus ancienne, n'importe quel programme joindrait ce
serveur par son tunnel ; il lui est demandé de mettre AIODOS à jour.
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
from aiodos.network.companion import SERVICE_HOLD, SERVICE_USAGE
from aiodos.node import android_app

log = logging.getLogger("aiodos.cluster")

LEASE_S = 90                  # sans nouvelles de la machine qui emprunte : carte rendue
SWEEP_S = 5
START_TIMEOUT = 60
DROP_S = 30                   # plus de connexion par le tunnel, modèle pas encore chargé
DROP_LOADED_S = 2             # … modèle chargé : sa connexion ne tombe qu'à la fin
GUARDED = "guarded"           # tunnel de la machine qui emprunte (cf. group.GUARDED)
# Cache des poids reçus (fichiers nommés par une empreinte FNV, non cryptographique, rangés
# par ggml-rpc-server dans <cache>/rpc/) : le client du serveur RPC y écrit ce qu'il veut. On
# le ramène sous ce plafond (les plus anciens fichiers partent) au début de chaque prêt, puis
# toutes les CACHE_TRIM_S pendant le prêt. Assez large pour garder un gros modèle d'un prêt à
# l'autre (son intérêt).
CACHE_MAX_BYTES = 48 * 1024 ** 3
CACHE_TRIM_S = 60

_lock = threading.Lock()
_loans = {}                   # service -> {"owner", "net_id", "process", "device", "expires",
                              #             "loaded", "published"}
_sweeper = {"thread": None, "trimmed": 0.0}


def cache_dir() -> Path:
    """Poids reçus, gardés pour la prochaine fois (cache du serveur RPC)."""
    return paths.data_dir() / "cluster" / "cache"


def trim_cache(cap: int | None = None) -> int:
    """Garde le cache des poids sous `cap` octets : au-delà, retire les fichiers les plus
    anciens, à tous les niveaux (ggml-rpc-server range les siens dans <cache>/rpc/). Borne
    le disque que le client du serveur RPC peut remplir. Un fichier en cours d'écriture
    (verrouillé sous Windows) reste : il partira au passage suivant. → octets restants."""
    cap = CACHE_MAX_BYTES if cap is None else cap
    files = []
    total = 0
    try:
        for entry in cache_dir().rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    stat = entry.stat()
                    files.append((stat.st_mtime, stat.st_size, entry))
                    total += stat.st_size
            except OSError:
                continue                              # parti entre-temps
    except OSError:
        return total                                  # cache absent ou illisible : rien à faire
    for _mtime, size, entry in sorted(files, key=lambda f: f[0]):   # du plus ancien au plus récent
        if total <= cap:
            break
        try:
            entry.unlink()
            total -= size
        except OSError:
            pass
    return total


def _companion():
    companion = route.companion()
    if companion is None or not companion.running:
        raise PeerError("Le compagnon aiodos-net ne tourne pas sur cette machine "
                        "(réglage « Relier mes machines par Internet »).")
    return companion


def _publish():
    """Le compagnon n'ouvre que les prêts en cours, chacun à sa seule machine, et les tient
    (« hold ») : aucun autre programme de cette machine n'est servi par leur serveur RPC."""
    with _lock:
        services = [{"name": name, "port": loan["process"].port, "peers": [loan["net_id"]],
                     "hold": True}
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
    if payload.get("tunnel") != GUARDED:
        raise PeerError(f"Mets AIODOS à jour sur « {peer.get('name') or 'ta machine'} » : sa "
                        "version ouvrirait le tunnel vers cette carte graphique à tous les "
                        "programmes de la machine.")
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
    companion = _companion()
    if not (companion.supports(SERVICE_USAGE) and companion.supports(SERVICE_HOLD)):
        # Indispensable, vérifié avant tout lancement : sans lui, le serveur RPC servirait
        # n'importe quel programme de cette machine
        raise PeerError(f"Le compagnon aiodos-net de « {manager.name()} » est trop ancien pour "
                        f"prêter sa carte, ou ne peut pas la garder sur ce système : mets AIODOS "
                        f"à jour sur « {manager.name()} ».")
    trim_cache()                                   # le cache ne grossit pas sans fin
    _sweeper["trimmed"] = time.monotonic()
    process = engine.rpc_server(Path(found["folder"]), device["name"], cache_dir())
    try:
        process.start(timeout=START_TIMEOUT)
    except engine.EngineError as exc:
        raise PeerError(f"Serveur RPC impossible sur « {manager.name()} » : {exc}") from exc
    name = f"rpc-{secrets.token_hex(6)}"
    with _lock:
        _loans[name] = {"owner": sender, "net_id": peer["net_id"], "process": process,
                        "device": device["name"], "expires": time.monotonic() + LEASE_S,
                        "loaded": False, "published": False}
    try:
        _publish()
    except Exception as exc:
        _end(name)
        raise PeerError(f"Le compagnon refuse d'ouvrir la carte : {exc}") from exc
    with _lock:
        if name in _loans:
            _loans[name]["published"] = True       # désormais tenu (cf. _unguarded)
    if manager.executor:
        manager.executor.interactive_begin()      # la carte va à la conversation
    _start_sweeper(manager)
    log.info("Carte %s prêtée à %s (%s)", device["name"], peer.get("name") or sender, name)
    return {"service": name, "device": device["name"], "label": device["label"],
            "free_mib": device["free_mib"]}


def keep(manager, sender, payload):
    """La machine qui emprunte est toujours là : le prêt continue, sauf si le réglage a
    été coupé entre-temps (la carte est alors reprise). « loaded » : son modèle est
    chargé, la connexion de son llama-server ne doit plus tomber (cf. _dropped)."""
    name = str(payload.get("service") or "")
    with _lock:
        loan = _loans.get(name)
        if not loan or loan["owner"] != sender:
            return {"ok": False}
        lent = bool(settings.get("lend_gpu"))
        if lent:
            loan["expires"] = time.monotonic() + LEASE_S
            if payload.get("loaded") is True:
                loan["loaded"] = True
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


def _unguarded(loan, usage) -> str | None:
    """Pourquoi le serveur RPC de ce prêt n'est plus gardé par le compagnon, ou None. Avant
    la publication (lend en cours), rien à dire."""
    if not loan.get("published"):
        return None
    if usage is None:
        return "le compagnon ne connaît plus ce prêt (relancé ?)"
    if usage.get("held") is not True:
        return "le compagnon ne tient pas ce serveur (trop ancien ?)"
    if usage.get("blocked"):
        return f"le compagnon a vu un autre programme de cette machine : {usage['blocked']}"
    return None


def _dropped(loan, usage) -> bool:
    """La connexion de llama-server par le tunnel est-elle tombée ? Il faut qu'il y en
    ait eu une (avant, le prêt attend llama-server, borné par LEASE_S), et plus aucune
    depuis DROP_LOADED_S (modèle chargé) ou DROP_S (au démarrage, connexions brèves)."""
    if not usage or not usage.get("total") or usage.get("active"):
        return False
    idle = usage.get("idle_s")
    if not isinstance(idle, (int, float)):
        return False
    return idle >= (DROP_LOADED_S if loan.get("loaded") else DROP_S)


def _guard_state():
    """(flux des services du compagnon, raison de tout arrêter) : sans compagnon qui tient
    les serveurs, aucun prêt ne continue ; flux None si l'état est illisible un instant."""
    companion = route.companion()
    if companion is None or not companion.running:
        return None, "le compagnon aiodos-net ne tourne plus"
    if not companion.supports(SERVICE_HOLD):
        return None, "le compagnon aiodos-net ne sait plus tenir les serveurs (remplacé ?)"
    try:
        return companion.service_usage(), None
    except Exception as exc:                      # compagnon qui répond mal un instant : tour suivant
        log.debug("Flux des prêts illisibles : %s", exc)
        return None, None


def sweep_once(manager=None) -> int:
    """Un passage : rend les cartes dont l'emprunteur ne donne plus de nouvelles, dont le
    serveur s'est arrêté, dont la connexion par le tunnel est tombée, ou que le compagnon ne
    garde plus. Ramène aussi le cache sous son plafond, de temps en temps. → prêts restants."""
    now = time.monotonic()
    usage, stop_all_loans = _guard_state()
    with _lock:
        ended = []
        for name, loan in _loans.items():
            if loan["expires"] < now or not loan["process"].alive:
                ended.append(name)
            elif stop_all_loans and loan.get("published"):
                log.warning("Prêt %s arrêté : %s", name, stop_all_loans)
                ended.append(name)
            elif usage is not None and (why := _unguarded(loan, usage.get(name))):
                log.warning("Prêt %s arrêté : %s", name, why)
                ended.append(name)
            elif usage is not None and _dropped(loan, usage.get(name)):
                log.info("Prêt %s : la connexion de la machine qui emprunte est tombée", name)
                ended.append(name)
        left = len(_loans) - len(ended)
        trim = left and now - _sweeper["trimmed"] >= CACHE_TRIM_S
        if trim:
            _sweeper["trimmed"] = now
    for name in ended:
        _end(name, manager)
    if trim:
        trim_cache()                              # pendant le prêt aussi (cf. CACHE_MAX_BYTES)
    return left


def _sweep(manager):
    """Passe toutes les SWEEP_S secondes (cf. sweep_once) ; s'arrête quand il n'y a plus
    de prêt."""
    while True:
        time.sleep(SWEEP_S)
        if not sweep_once(manager):
            with _lock:
                if not _loans:
                    _sweeper["thread"] = None
                    return


HANDLERS = {
    "cluster/lend": lend,
    "cluster/keep": keep,
    "cluster/release": release,
}
