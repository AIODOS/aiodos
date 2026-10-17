"""Manifeste d'une version : son numéro, ses fichiers et leurs empreintes, signé par la
clé de publication (Ed25519).

Le texte signé voyage et se garde tel quel, jamais re-sérialisé : on vérifie la
signature sur ces octets-là, puis seulement on le lit. Un manifeste peut annoncer
des fichiers d'un type inconnu : ils sont ignorés. Ainsi des programmes livrés avec
une version, un par plateforme (« aiodos-net-windows-x86_64 », cf. platforms.py et
binaries.py) : les versions 2.0.x n'en prennent aucun, les suivantes celui de leur
plateforme seulement. De même pour le moteur llama.cpp des gros modèles répartis
(« llama.cpp-windows-x86_64 », l'archive officielle, cf. cluster/delivery.py).
Le format (FORMAT) ne doit pas changer, sans quoi les machines déjà installées ne
pourraient plus se mettre à jour.

L'enveloppe signée peut porter en plus des approbations (« approvals ») : la signature
d'un appareil d'approbation (keys.APPROVER_KEYS, approval.py) sur le même texte. Les
versions qui ne les connaissent pas les ignorent ; les autres exigent l'une d'elles dès
qu'un appareil d'approbation est déclaré.
"""
import base64
import binascii
import hashlib
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from aiodos.updates import keys, platforms, version

FORMAT = 1
APP = "aiodos"
SOURCE = "source"                  # archive du code : aiodos/, assets/, lanceurs
COMPANION = "aiodos-net"           # compagnon Internet, un fichier par plateforme
ENGINE = "llama.cpp"               # moteur des gros modèles répartis, une archive par plateforme
MAX_FILE = 200 * 1024 ** 2
APPROVAL_CONTEXT = b"aiodos-approbation\n"
NOT_APPROVED = "Version pas encore approuvée sur l'appareil d'approbation : ignorée."
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class UpdateError(Exception):
    """Mise à jour refusée ou impossible (message lisible)."""


def key_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


def raw_public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)


def sign(manifest: dict, private: Ed25519PrivateKey) -> dict:
    """Manifeste signé, tel qu'il se garde et voyage (outil de publication)."""
    check(manifest)
    text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = private.sign(text.encode("utf-8"))
    return {"manifest": text, "key": key_id(raw_public(private)),
            "signature": base64.b64encode(signature).decode("ascii")}


def _b64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def declared_approvers() -> dict:
    """Appareils d'approbation déclarés (keys.APPROVER_KEYS ; absent d'un keys.py plus
    ancien : aucun)."""
    return getattr(keys, "APPROVER_KEYS", None) or {}


def approval_message(text: str) -> bytes:
    """Ce que signe un appareil d'approbation : le texte du manifeste, précédé d'un
    contexte propre (une approbation ne vaut jamais signature d'auteur, ni l'inverse)."""
    return APPROVAL_CONTEXT + text.encode("utf-8")


def approve(signed: dict, private: Ed25519PrivateKey) -> dict:
    """L'enveloppe signée, avec l'approbation de cet appareil (cf. approval.py)."""
    entry = {"key": key_id(raw_public(private)),
             "signature": base64.b64encode(
                 private.sign(approval_message(signed["manifest"]))).decode("ascii")}
    others = [a for a in signed.get("approvals") or []
              if isinstance(a, dict) and a.get("key") != entry["key"]]
    return dict(signed, approvals=others + [entry])


def approved(signed: dict, approvers: dict) -> bool:
    """L'enveloppe porte l'approbation valide d'un des appareils `approvers`."""
    text = signed.get("manifest")
    entries = signed.get("approvals")
    if not isinstance(text, str) or not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("signature"), str):
            continue
        public = approvers.get(entry.get("key")) if isinstance(entry.get("key"), str) else None
        if not public:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(_b64(public)).verify(
                _b64(entry["signature"]), approval_message(text))
            return True
        except (InvalidSignature, ValueError, binascii.Error, UnicodeEncodeError):
            continue
    return False


def verify(signed, trusted: dict | None = None, approvers: dict | None = None) -> dict:
    """Le manifeste lu, si la signature est celle d'une clé de confiance et, quand des
    appareils d'approbation sont déclarés (keys.APPROVER_KEYS, ou `approvers`), s'il porte
    l'approbation de l'un d'eux. UpdateError sinon. `approvers={}` : la seule signature de
    l'auteur (version publiée ici, en attente d'approbation)."""
    trusted = keys.TRUSTED_KEYS if trusted is None else trusted
    approvers = declared_approvers() if approvers is None else approvers
    if not isinstance(signed, dict):
        raise UpdateError("Manifeste illisible.")
    text, kid, signature = signed.get("manifest"), signed.get("key"), signed.get("signature")
    if not all(isinstance(x, str) for x in (text, kid, signature)):
        raise UpdateError("Manifeste illisible.")
    public = trusted.get(kid)
    if not public:
        raise UpdateError("Version signée par une clé inconnue : ignorée.")
    try:
        Ed25519PublicKey.from_public_bytes(_b64(public)).verify(_b64(signature),
                                                                text.encode("utf-8"))
    except (InvalidSignature, ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise UpdateError("Signature invalide : version ignorée.") from exc
    if approvers and not approved(signed, approvers):
        raise UpdateError(NOT_APPROVED)
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        raise UpdateError("Manifeste illisible.") from exc
    check(manifest)
    return manifest


def check(manifest):
    """Ce qu'un manifeste doit contenir (les fichiers deviennent des chemins : noms sûrs)."""
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT \
            or manifest.get("app") != APP:
        raise UpdateError("Manifeste d'un format inconnu.")
    if not version.valid(manifest.get("version")):
        raise UpdateError("Numéro de version illisible dans le manifeste.")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise UpdateError("Manifeste sans fichier.")
    names = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                or not _NAME.fullmatch(item["name"]) or item["name"] in names:
            raise UpdateError("Nom de fichier refusé dans le manifeste.")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_FILE:
            raise UpdateError("Taille de fichier refusée dans le manifeste.")
        if not isinstance(item.get("sha256"), str) or not _SHA256.fullmatch(item["sha256"]):
            raise UpdateError("Empreinte de fichier refusée dans le manifeste.")
        names.add(item["name"])
    if sum(1 for item in files if item.get("kind") == SOURCE) != 1:
        raise UpdateError("Le manifeste doit contenir une archive du code, et une seule.")


def source(manifest: dict) -> dict:
    """L'archive du code de cette version."""
    return next(item for item in manifest["files"] if item.get("kind") == SOURCE)


def binary(manifest: dict, program: str = COMPANION) -> dict | None:
    """Le programme `program` construit pour cette machine, s'il est livré avec la version."""
    wanted = platforms.kind(program)
    return next((item for item in manifest["files"]
                 if wanted and item.get("kind") == wanted), None)


def wanted(manifest: dict) -> list:
    """Fichiers que cette machine prend d'une version : l'archive du code, et le
    compagnon de sa plateforme s'il est livré. Les autres types (autres plateformes,
    programmes inconnus) sont ignorés."""
    companion = binary(manifest)
    return [source(manifest)] + ([companion] if companion else [])
