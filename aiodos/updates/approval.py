"""Approbation des versions sur un deuxième appareil (décision du 17/10/2026).

Une version signée par la clé de l'auteur ne part chez les autres machines qu'une fois
approuvée sur un appareil d'approbation, en général son téléphone. Une clé de
publication volée ne suffit donc plus à pousser une version. Les clés publiques de ces
appareils sont dans keys.APPROVER_KEYS ; tant que la liste est vide, rien ne change.

- **L'appareil d'approbation.** Sa clé se crée dans la vue Mises à jour, et elle ne
  quitte jamais le fichier data/updates/approbation.pem. Il demande à mes machines les
  versions qui attendent (update/pending). Il montre ce que contient chacune : numéro,
  notes, commit, fichiers et empreintes. Sur un clic, il renvoie son approbation
  (update/approve), une signature sur le texte même du manifeste.
- **La machine qui a publié.** Elle garde la version sans la proposer : store.offer ne
  voit pas une version non approuvée. Elle répond à update/pending et ajoute à
  l'enveloppe l'approbation reçue, après l'avoir vérifiée. Dès lors, la version part.

Les deux actions ne répondent qu'à mes machines. L'approbation vaut par sa signature, pas
par la machine qui l'apporte.
"""
import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos import paths
from aiodos.network import client, peers, sharing
from aiodos.network.client import PeerError
from aiodos.updates import manifest as manifests
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

log = logging.getLogger("aiodos.updates")

KEY_FILE = "approbation.pem"
TIMEOUT = 15


# ------------------------------------------------------------ clé de cet appareil
def key_path() -> Path:
    return paths.data_dir() / "updates" / KEY_FILE


def load_key() -> Ed25519PrivateKey | None:
    try:
        return serialization.load_pem_private_key(key_path().read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None


def create_key() -> dict:
    """Fait de cet appareil un appareil d'approbation (une seule fois)."""
    path = key_path()
    if path.exists():
        raise UpdateError("Cet appareil a déjà sa clé d'approbation.")
    private = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0), 0o600)       # lisible par AIODOS seul
    with os.fdopen(descriptor, "wb") as f:
        f.write(pem)
    log.info("Clé d'approbation créée (%s)", manifests.key_id(manifests.raw_public(private)))
    return public_info()


def public_info() -> dict | None:
    """Clé publique de cet appareil, et la ligne à coller dans keys.APPROVER_KEYS."""
    private = load_key()
    if private is None:
        return None
    raw = manifests.raw_public(private)
    kid, public = manifests.key_id(raw), base64.b64encode(raw).decode("ascii")
    return {"id": kid, "public": public, "line": f'    "{kid}": "{public}",',
            "declared": kid in manifests.declared_approvers()}


# ------------------------------------------------------- machine qui a publié
def _mine_only(sender):
    if sharing.level(sender) != sharing.MINE:
        raise PeerError("Seules mes machines voient et approuvent les versions en attente.")


def serve_pending(manager, sender, payload):
    _mine_only(sender)
    return {"pending": [{"version": v, "signed": signed} for v, signed in Store().pending()]}


def serve_approve(manager, sender, payload):
    _mine_only(sender)
    version, entry = str(payload.get("version") or ""), payload.get("approval")
    store = Store()
    try:
        signed = store.signed(version)
    except UpdateError:
        signed = None
    if signed is None or not isinstance(entry, dict):
        raise PeerError("Version inconnue ici, ou approbation illisible.")
    others = [a for a in signed.get("approvals") or []
              if isinstance(a, dict) and a.get("key") != entry.get("key")]
    try:
        store.add_manifest(dict(signed, approvals=others + [entry]))   # tout revérifié
    except UpdateError as exc:
        raise PeerError(f"Approbation refusée : {exc}") from exc
    log.info("Version %s approuvée (appareil %s) : elle peut partir", version, entry.get("key"))
    return {"ok": True}


HANDLERS = {
    "update/pending": serve_pending,
    "update/approve": serve_approve,
}


# --------------------------------------------------- appareil d'approbation
def _details(version: str, signed: dict) -> dict | None:
    """Ce que l'utilisateur doit voir avant d'approuver, si l'auteur l'a bien signé."""
    try:
        found = manifests.verify(signed, approvers={})
    except UpdateError:
        return None
    if found["version"] != version:
        return None
    return {"version": version, "notes": str(found.get("notes") or ""),
            "commit": str(found.get("commit") or ""), "published": found.get("published"),
            "files": [{"kind": f.get("kind"), "name": f["name"], "size": f["size"],
                       "sha256": f["sha256"]} for f in found["files"]]}


def pending_from_peers(my_id: str) -> list:
    """Versions qui attendent une approbation chez mes machines en ligne."""
    found = []
    for peer in peers.list_peers():
        if not peer["online"] or sharing.level(peer["node_id"]) != sharing.MINE:
            continue
        try:
            reply = client.call(my_id, peers.get(peer["node_id"]), "update/pending", {},
                                timeout=TIMEOUT)
        except PeerError as exc:
            log.info("Versions en attente de « %s » illisibles : %s", peer["name"], exc)
            continue
        for item in reply.get("pending") or []:
            details = _details(str(item.get("version") or ""), item.get("signed"))
            if details:
                found.append({**details, "node_id": peer["node_id"], "machine": peer["name"]})
    return found


def approve(my_id: str, node_id: str, version: str) -> dict:
    """Approuve la version `version` qui attend chez la machine `node_id`."""
    private = load_key()
    if private is None:
        raise UpdateError("Cet appareil n'a pas de clé d'approbation.")
    peer = peers.get(node_id)
    if not peer or sharing.level(node_id) != sharing.MINE:
        raise UpdateError("Machine inconnue, ou qui n'est pas l'une des miennes.")
    reply = client.call(my_id, peer, "update/pending", {}, timeout=TIMEOUT)
    signed = next((item.get("signed") for item in reply.get("pending") or []
                   if item.get("version") == version), None)
    if signed is None or _details(version, signed) is None:
        raise UpdateError(f"La version {version} n'attend plus d'approbation chez « {peer['name']} ».")
    entry = manifests.approve(signed, private)["approvals"][-1]
    client.call(my_id, peer, "update/approve", {"version": version, "approval": entry},
                timeout=TIMEOUT)
    return {"ok": True, "version": version, "machine": peer["name"]}
