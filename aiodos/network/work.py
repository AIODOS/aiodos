"""Travail confié aux machines appairées : côté propriétaire du travail.

Une machine qui a de la puissance libre demande du travail (« work/lease »).
On lui confie la première unité de la file qu'elle sait traiter : même modèle
d'embedding au bit près (sinon ses vecteurs seraient incompatibles avec ceux de
la bibliothèque), document dans un format qu'elle sait lire (un nœud minimal
peut manquer de python-pptx…), ressource libre chez elle, bibliothèque non
réservée à cette machine. Elle lit le fichier morceau par morceau (« work/blob »,
seulement celui de l'unité qui lui est confiée), calcule, puis rend sa sortie
(« work/result ») : elle est contrôlée puis intégrée ici, jamais là-bas.

Rien n'est confié si l'utilisateur a désactivé l'aide de ses machines.
Appelées dans un thread par le serveur réseau (cf. p2p_app.HANDLERS).
"""
import hashlib
import logging
import os
from pathlib import Path

from aiodos import settings
from aiodos.jobs import leases, queue, registry
from aiodos.knowledge import store
from aiodos.network import envelope, peers, wire
from aiodos.network.client import PeerError
from aiodos.services import ollama

log = logging.getLogger("aiodos.work")

WORK_API = 1                   # contrat de calcul (sorties, options) : à incrémenter s'il change
MAX_BLOB = 512 * 1024 ** 2     # au-delà, le fichier reste traité sur cette machine
BLOB_CHUNK = 2 * 1024 ** 2
NOTHING_RETRY = 5              # rien à confier : la machine peut redemander dans 5 s


def full_name(model: str) -> str:
    """« nomic-embed-text » et « nomic-embed-text:latest » désignent le même modèle."""
    return model if ":" in model else model + ":latest"


def local_models() -> dict:
    """Modèles Ollama de ce nœud : nom complet -> empreinte (digest)."""
    try:
        return {full_name(m["name"]): m.get("digest", "") for m in ollama.list_models()}
    except Exception:
        return {}


def can_serve(need: dict, caps: dict, mine: dict) -> bool:
    """La machine décrite par `caps` peut-elle calculer une unité qui demande `need` ?
    Une machine qui ne dit pas quels formats elle lit (« readers » absent : version
    précédente) est supposée tous les lire, comme avant."""
    theirs = caps.get("models") or {}
    embed = need.get("embed")
    if embed:
        digest = mine.get(full_name(embed))
        if not digest or theirs.get(full_name(embed)) != digest:
            return False
    vision = need.get("vision")
    if vision and full_name(vision) not in theirs:
        return False
    if need.get("whisper") and not caps.get("whisper"):
        return False
    readers = caps.get("readers")
    if need.get("format") and isinstance(readers, list) and need["format"] not in readers:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _peer_name(node_id) -> str:
    peer = peers.get(node_id)
    return peer["name"] if peer else "une autre machine"


def _unit_id(payload) -> int:
    try:
        return int(payload["unit"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PeerError("Unité non précisée.") from exc


# --------------------------------------------------------------------- bail
def lease(manager, sender, payload):
    if not settings.get("offload_enabled"):
        return {"unit": None, "retry_after": 60}
    caps = payload.get("caps") or {}
    if caps.get("work_api") != WORK_API:
        return {"unit": None, "retry_after": 600,
                "reason": "Versions d'AIODOS différentes : mets à jour les deux machines."}
    mine = local_models()
    local_only = store.local_only_ids()
    options = {}

    def acceptable(unit):
        spec = registry.TASKS.get(unit["task"])
        if spec is None or not spec.portable:
            return False
        if unit["payload"].get("collection_id") in local_only:
            return False
        if unit["task"] not in options:
            options[unit["task"]] = spec.options() if spec.options else {}
        if spec.requires and not can_serve(spec.requires(unit, options[unit["task"]]), caps, mine):
            return False
        if spec.file_key:
            try:
                return os.path.getsize(unit["payload"][spec.file_key]) <= MAX_BLOB
            except OSError:
                return False      # fichier disparu : l'exécution locale le signalera
        return True

    unit = leases.claim_remote(payload.get("resources"), sender, acceptable)
    if unit is None:
        return {"unit": None, "retry_after": NOTHING_RETRY}
    spec = registry.TASKS[unit["task"]]
    order = {"id": unit["id"], "task": unit["task"], "resource": unit["resource"],
             "payload": {k: v for k, v in unit["payload"].items() if k != spec.file_key},
             "options": options[unit["task"]], "lease_s": leases.LEASE_S, "blob": None}
    if spec.file_key:
        path = Path(unit["payload"][spec.file_key])
        try:
            order["blob"] = {"name": path.name, "size": path.stat().st_size,
                             "sha256": _sha256(path)}
        except OSError:
            leases.release(unit["id"], sender)
            return {"unit": None, "retry_after": NOTHING_RETRY}
    log.info("Unité %s (%s) confiée à %s", unit["id"], unit["task"], _peer_name(sender))
    manager.executor.publish_job(unit["job_id"], force=True)
    return {"unit": order}


def renew(manager, sender, payload):
    return {"ok": leases.renew(_unit_id(payload), sender)}


def blob(manager, sender, payload):
    """Un morceau du fichier de l'unité confiée à cette machine (et d'aucun autre)."""
    unit = leases.held(_unit_id(payload), sender)
    spec = registry.TASKS.get(unit["task"]) if unit else None
    if spec is None or not spec.file_key:
        raise PeerError("Cette unité ne t'est plus confiée.")
    offset = max(0, int(payload.get("offset") or 0))
    size = min(BLOB_CHUNK, max(1, int(payload.get("size") or BLOB_CHUNK)))
    with open(unit["payload"][spec.file_key], "rb") as f:
        f.seek(offset)
        data = f.read(size)
    return {"data": envelope.b64e(data)}


# ---------------------------------------------------------------- résultats
def result(manager, sender, payload):
    unit_id = _unit_id(payload)
    compute_s = max(0.0, min(float(payload.get("compute_s") or 0), 86400.0))
    unit = leases.take_back(unit_id, sender)
    if unit is None:
        return {"accepted": False, "reason": "Unité reprise ou annulée entre-temps."}
    spec = registry.TASKS[unit["task"]]
    try:
        output = wire.unpack(payload.get("output") or {})
        if spec.validate:
            spec.validate(unit["payload"], output)
        outcome = spec.assimilate(unit["payload"], output) or {}
    except Exception as exc:
        log.warning("Résultat de %s refusé pour l'unité %s : %s", _peer_name(sender), unit_id, exc)
        job_id, finished = queue.fail_unit(
            unit_id, f"Résultat de « {_peer_name(sender)} » refusé : {exc}"[:500], compute_s)
        manager.executor.settle(job_id, finished)
        return {"accepted": False, "reason": str(exc)[:200]}
    job_id, finished = queue.finish_unit(unit_id, outcome, compute_s)
    peers.account(sender, received_s=compute_s, received_units=1)
    manager.executor.settle(job_id, finished)
    manager.changed()
    return {"accepted": True}


def fail(manager, sender, payload):
    error = str(payload.get("error") or "erreur inconnue")[:400]
    settled = leases.fail(_unit_id(payload), sender, f"Sur « {_peer_name(sender)} » : {error}",
                          max(0.0, float(payload.get("compute_s") or 0)))
    if settled:
        manager.executor.settle(*settled)
    return {"ok": bool(settled)}


def release(manager, sender, payload):
    job_id = leases.release(_unit_id(payload), sender)
    if job_id is not None:
        manager.executor.publish_job(job_id, force=True)
        manager.executor.wake()
    return {"ok": job_id is not None}


HANDLERS = {
    "work/lease": lease,
    "work/renew": renew,
    "work/blob": blob,
    "work/result": result,
    "work/fail": fail,
    "work/release": release,
}
