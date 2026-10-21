"""Approbation des versions sur un deuxième appareil (décision du 17/10/2026).

Une version signée par la clé de l'auteur ne part chez les autres machines qu'une fois
approuvée sur un appareil d'approbation, en général son téléphone. Une clé de
publication volée ne suffit donc plus à pousser une version. Les clés publiques de ces
appareils sont dans keys.APPROVER_KEYS ; tant que la liste est vide, rien ne change.

- **L'appareil d'approbation.** Sa clé se crée dans la vue Mises à jour, et elle ne
  quitte jamais le fichier data/updates/approbation.pem. Il demande à mes machines les
  versions qui attendent (update/pending). Il montre ce que contient chacune : numéro,
  notes, commit, fichiers et empreintes, et l'empreinte du texte du manifeste (« digest »).
  Sur un clic, il renvoie son approbation (update/approve), une signature sur le texte
  même du manifeste : celui qu'il a montré, et aucun autre. La machine est redemandée au
  moment du clic ; si le texte qu'elle rend n'est plus celui qu'on a vu (une machine
  compromise, ou une clé de publication volée, qui montrerait une version puis en ferait
  signer une autre sous le même numéro), rien n'est signé. Le clic désigne le texte par
  son empreinte ; sans elle (interface plus ancienne), seul un numéro qui n'a jamais
  montré qu'un texte s'approuve. Une machine qui en montre plusieurs sous un même numéro
  est signalée (« conflict ») : c'est anormal.
- **La machine qui a publié.** Elle garde la version sans la proposer : store.offer ne
  voit pas une version non approuvée. Elle répond à update/pending et ajoute à
  l'enveloppe l'approbation reçue, après l'avoir vérifiée. Dès lors, la version part.

Les deux actions ne répondent qu'à mes machines. L'approbation vaut par sa signature, pas
par la machine qui l'apporte.
"""
import base64
import hashlib
import logging
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos import paths
from aiodos.i18n import t
from aiodos.network import client, peers, sharing
from aiodos.network.client import PeerError
from aiodos.updates import manifest as manifests
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store

log = logging.getLogger("aiodos.updates")

KEY_FILE = "approbation.pem"
TIMEOUT = 15
MAX_SHOWN = 64                   # (machine, version) retenus
MAX_TEXTS = 8                    # empreintes retenues par (machine, version) : deux suffisent
MAX_PENDING = 32                 # versions en attente lues au plus par machine

_shown_lock = threading.Lock()
_shown = {}                      # (machine, version) -> {empreintes des textes montrés}
_suspect = set()                 # machines qui ont montré deux textes sous un même numéro


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
        raise UpdateError(t("approval.key_exists"))
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
        raise PeerError(t("approval.mine_only"))


def serve_pending(manager, sender, payload):
    _mine_only(sender)
    return {"pending": [{"version": v, "signed": manifests.canonical(signed)}
                        for v, signed in Store().pending()]}


def serve_approve(manager, sender, payload):
    _mine_only(sender)
    version, entry = str(payload.get("version") or ""), payload.get("approval")
    store = Store()
    try:
        signed = store.signed(version)
    except UpdateError:
        signed = None
    if not isinstance(signed, dict) or not isinstance(entry, dict):
        raise PeerError(t("approval.unknown_version"))
    others = [a for a in signed.get("approvals") or []
              if isinstance(a, dict) and a.get("key") != entry.get("key")]
    try:
        store.add_manifest(dict(signed, approvals=[entry] + others))   # tout revérifié
    except UpdateError as exc:
        raise PeerError(t("approval.refused", error=exc)) from exc
    log.info("Version %s approuvée (appareil %s) : elle peut partir", version, entry.get("key"))
    return {"ok": True}


HANDLERS = {
    "update/pending": serve_pending,
    "update/approve": serve_approve,
}


# --------------------------------------------------- appareil d'approbation
def digest(signed: dict) -> str:
    """Empreinte du texte du manifeste : ce qu'on montre est ce qu'on signera."""
    return hashlib.sha256(signed["manifest"].encode("utf-8")).hexdigest()


def _details(version: str, signed) -> dict | None:
    """Ce que l'utilisateur doit voir avant d'approuver, si l'auteur l'a bien signé."""
    try:
        found = manifests.verify(signed, approvers={})
    except UpdateError:
        return None
    if found["version"] != version:
        return None
    return {"version": version, "notes": str(found.get("notes") or ""),
            "commit": str(found.get("commit") or ""), "published": found.get("published"),
            "digest": digest(signed),
            "files": [{"kind": f.get("kind"), "name": f["name"], "size": f["size"],
                       "sha256": f["sha256"]} for f in found["files"]]}


def _pending(my_id: str, peer: dict) -> list:
    """[(version, enveloppe)] que `peer` dit en attente (réponse lue sans rien supposer,
    MAX_PENDING au plus : chacune coûte une vérification de signature)."""
    reply = client.call(my_id, peer, "update/pending", {}, timeout=TIMEOUT)
    items = reply.get("pending") if isinstance(reply, dict) else None
    return [(str(item.get("version") or ""), item.get("signed"))
            for item in (items if isinstance(items, list) else [])[:MAX_PENDING]
            if isinstance(item, dict)]


def _remember(node_id: str, version: str, shown: str) -> set:
    """Retient que le texte d'empreinte `shown` a été montré pour (machine, version), en plus
    de ceux qui l'ont déjà été : un numéro qui en a montré plusieurs reste suspect. Rend
    l'ensemble des empreintes montrées."""
    with _shown_lock:
        seen = _shown.pop((node_id, version), set())
        if len(seen) < MAX_TEXTS:
            seen.add(shown)
        if len(seen) > 1:
            _suspect.add(node_id)                   # même si ce souvenir s'efface ensuite
        _shown[(node_id, version)] = seen          # en dernier : le plus récent
        while len(_shown) > MAX_SHOWN:
            _shown.pop(next(iter(_shown)))
        return set(seen)


def forget():
    """Oublie ce qui a été montré, et les machines suspectes (essais)."""
    with _shown_lock:
        _shown.clear()
        _suspect.clear()


def pending_from_peers(my_id: str) -> list:
    """Versions qui attendent une approbation chez mes machines en ligne. Ce qui est
    montré de chacune (l'empreinte de son texte, « digest ») est retenu : approve() ne
    signera que lui. Un numéro sous lequel une machine a montré plusieurs textes est
    signalé (« conflict », « warning ») sur chacune de ses lignes."""
    found = []
    for peer in peers.list_peers():
        if not peer["online"] or sharing.level(peer["node_id"]) != sharing.MINE:
            continue
        try:
            waiting = _pending(my_id, peers.get(peer["node_id"]))
        except PeerError as exc:
            log.info("Versions en attente de « %s » illisibles : %s", peer["name"], exc)
            continue
        lines, texts = [], {}
        for version, signed in waiting:
            details = _details(version, signed)
            if details:
                texts[version] = _remember(peer["node_id"], version, details["digest"])
                lines.append(details)
        for details in lines:
            conflict = len(texts[details["version"]]) > 1
            if conflict:
                log.warning("« %s » montre plusieurs textes sous le numéro %s", peer["name"],
                            details["version"])
            found.append({**details, "node_id": peer["node_id"], "machine": peer["name"],
                          "conflict": conflict,
                          "warning": t("approval.conflict") if conflict else ""})
    return found


def approve(my_id: str, node_id: str, version: str, shown: str | None = None) -> dict:
    """Approuve la version `version` qui attend chez la machine `node_id`, telle qu'elle a
    été montrée : `shown`, l'empreinte (« digest ») de la ligne choisie. Sans elle
    (interface plus ancienne), seulement si ce numéro n'a montré qu'un texte et que la
    machine n'en a jamais montré deux sous un même numéro (_suspect : la mémoire de ce qui
    a été montré est bornée, et une machine compromise pourrait l'effacer en listant
    beaucoup de versions) ; sinon, on ne saurait pas lequel l'utilisateur a lu. La machine
    est redemandée ; si son texte n'est plus celui-là, rien n'est signé."""
    private = load_key()
    if private is None:
        raise UpdateError(t("approval.no_key"))
    peer = peers.get(node_id)
    if not peer or sharing.level(node_id) != sharing.MINE:
        raise UpdateError(t("approval.unknown_machine"))
    with _shown_lock:
        seen = set(_shown.get((node_id, version)) or ())
        suspect = node_id in _suspect
    expected = shown or (next(iter(seen)) if len(seen) == 1 and not suspect else None)
    if not expected and (seen or suspect):
        if len(seen) > 1:
            raise UpdateError(t("approval.several_texts_version", name=peer["name"],
                                version=version))
        raise UpdateError(t("approval.several_texts", name=peer["name"]))
    if not expected:
        raise UpdateError(t("approval.show_first"))
    candidates = [signed for v, signed in _pending(my_id, peer)
                  if v == version and _details(version, signed) is not None]
    if not candidates:
        raise UpdateError(t("approval.no_longer_waiting", version=version, name=peer["name"]))
    signed = next((s for s in candidates if digest(s) == expected), None)
    if signed is None:
        raise UpdateError(t("approval.changed", version=version, name=peer["name"]))
    entry = manifests.approval_entry(signed["manifest"], private)    # ce texte-là, tel quel
    client.call(my_id, peer, "update/approve", {"version": version, "approval": entry},
                timeout=TIMEOUT)
    return {"ok": True, "version": version, "machine": peer["name"]}
