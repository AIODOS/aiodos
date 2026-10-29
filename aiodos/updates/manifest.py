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

La signature ne couvre que le texte : le reste de l'enveloppe vient de la machine qui l'a
transmise. Elle se garde et se retransmet donc réduite à sa forme canonique (canonical :
texte, clé, signature, quelques approbations bien formées), et le texte est borné
(MAX_TEXT) : une machine ne peut pas faire garder et propager une enveloppe géante ni
des milliers d'approbations à vérifier.

Pour qu'une machine qui ne déclare aucun appareil d'approbation (une version plus ancienne,
ou toutes tant que keys.APPROVER_KEYS est vide) relaie quand même la bonne approbation, le
texte signé nomme les appareils d'approbation du commit publié (« approvers », écrit par
tools/publish.py ; les versions qui ne le connaissent pas l'ignorent). Elle vérifie avec eux
ce qu'elle garde, et rien d'autre : une fausse approbation placée devant la vraie ne la fait
plus retirer. Ces clés servent seulement à trier ce qu'on relaie : une version ne s'installe
qu'approuvée par un appareil que la machine déclare elle-même (approved).
"""
import base64
import binascii
import hashlib
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from aiodos.i18n import t
from aiodos.updates import keys, platforms, version

FORMAT = 1
APP = "aiodos"
SOURCE = "source"                  # archive du code : aiodos/, assets/, lanceurs
COMPANION = "aiodos-net"           # compagnon Internet, un fichier par plateforme
ENGINE = "llama.cpp"               # moteur des gros modèles répartis, une archive par plateforme
IMAGE_ENGINE = "stable-diffusion.cpp"   # moteur des images, une archive par plateforme
AUDIO_ENGINE = "audio.cpp"         # moteur de l'audio, de même ; i18n: tel quel (pas une clé)
ENGINES = (ENGINE, IMAGE_ENGINE, AUDIO_ENGINE)
MAX_FILE = 200 * 1024 ** 2
MAX_TEXT = 256 * 1024              # un manifeste fait quelques Ko
MAX_APPROVALS = 8                  # gardées et retransmises, une par appareil
MAX_SCAN = 32                      # approbations examinées au plus (vérification bornée)
APPROVAL_CONTEXT = b"aiodos-approbation\n"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"[0-9a-f]{16}")


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


def approval_entry(text: str, private: Ed25519PrivateKey) -> dict:
    """L'approbation de cet appareil pour le texte de manifeste `text`, tel quel."""
    return {"key": key_id(raw_public(private)),
            "signature": base64.b64encode(private.sign(approval_message(text))).decode("ascii")}


def approve(signed: dict, private: Ed25519PrivateKey) -> dict:
    """L'enveloppe signée, avec l'approbation de cet appareil en tête (cf. approval.py)."""
    entry = approval_entry(signed["manifest"], private)
    others = [a for a in signed.get("approvals") or []
              if isinstance(a, dict) and a.get("key") != entry["key"]]
    return dict(signed, approvals=[entry] + others)


def _well_formed(entry) -> bool:
    """Une approbation qui a la forme d'une vraie : identifiant de clé, signature Ed25519."""
    if not isinstance(entry, dict):
        return False
    kid, signature = entry.get("key"), entry.get("signature")
    if not isinstance(kid, str) or not _KEY_ID.fullmatch(kid) or not isinstance(signature, str) \
            or len(signature) != 88:
        return False
    try:
        return len(_b64(signature)) == 64
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return False


def _approval_valid(text: str, entry: dict, approvers: dict) -> bool:
    public = approvers.get(entry.get("key")) if isinstance(entry.get("key"), str) else None
    if not public or not isinstance(entry.get("signature"), str):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(_b64(public)).verify(
            _b64(entry["signature"]), approval_message(text))
        return True
    except (InvalidSignature, ValueError, binascii.Error, UnicodeEncodeError):
        return False


def approved(signed: dict, approvers: dict) -> bool:
    """L'enveloppe porte l'approbation valide d'un des appareils `approvers`. Seules les
    MAX_SCAN premières entrées sont examinées : le coût reste borné, quelle que soit la
    liste reçue (une enveloppe gardée ici n'en a jamais plus de MAX_APPROVALS)."""
    text = signed.get("manifest")
    entries = signed.get("approvals")
    if not isinstance(text, str) or not isinstance(entries, list):
        return False
    return any(isinstance(entry, dict) and _approval_valid(text, entry, approvers)
               for entry in entries[:MAX_SCAN])


def listed_approvers(text) -> dict | None:
    """Les appareils d'approbation que nomme le texte signé (« approvers ») : {identifiant :
    clé publique}, seulement ceux dont l'identifiant est bien celui de la clé. None : le
    texte n'en nomme pas (publié par un outil plus ancien)."""
    try:
        listed = json.loads(text).get("approvers") if isinstance(text, str) else None
    except (ValueError, AttributeError, RecursionError):
        return None
    if not isinstance(listed, dict):
        return None
    found = {}
    for kid, public in list(listed.items())[:MAX_APPROVALS]:
        if not isinstance(kid, str) or not _KEY_ID.fullmatch(kid) or not isinstance(public, str):
            continue
        try:
            raw = _b64(public)
        except (ValueError, binascii.Error, UnicodeEncodeError):
            continue
        if len(raw) == 32 and key_id(raw) == kid:
            found[kid] = public
    return found


def canonical(signed: dict, approvers: dict | None = None) -> dict:
    """L'enveloppe telle qu'elle se garde et se retransmet : texte, clé, signature, et au
    plus MAX_APPROVALS approbations bien formées. Celles d'un appareil déclaré ici ou nommé
    dans le texte signé (listed_approvers) ne restent que valides, une par appareil ; si le
    texte nomme ses appareils, rien d'autre ne reste. Sinon (manifeste plus ancien), les
    approbations qu'on ne sait pas vérifier restent telles quelles, sans en retirer une
    parce qu'une autre porte le même identifiant : une machine qui n'en déclare aucun relaie
    celle qu'exigent les autres. Tout autre champ est retiré."""
    approvers = declared_approvers() if approvers is None else approvers
    text = signed.get("manifest")
    listed = listed_approvers(text)
    checkable = {**(listed or {}), **approvers}
    found, others, seen = [], [], set()
    entries = signed.get("approvals")
    for entry in (entries[:MAX_SCAN] if isinstance(entries, list) else []):
        if not _well_formed(entry):
            continue
        clean = {"key": entry["key"], "signature": entry["signature"]}
        if entry["key"] in checkable:
            if entry["key"] not in seen and isinstance(text, str) \
                    and _approval_valid(text, clean, checkable):
                seen.add(entry["key"])
                found.append(clean)
        elif listed is None and clean not in others:
            others.append(clean)
    envelope = {name: signed.get(name) for name in ("manifest", "key", "signature")}
    kept = (found + others)[:MAX_APPROVALS]
    if kept:
        envelope["approvals"] = kept
    return envelope


def merged(signed: dict, kept: dict | None) -> dict:
    """`signed`, avec en plus les approbations de `kept` (la même version gardée ici) si
    c'est le même texte : une approbation reçue d'une machine ne se perd pas parce qu'une
    autre transmet la version sans elle. Les approbations déjà gardées passent devant."""
    if not isinstance(kept, dict) or kept.get("manifest") != signed.get("manifest"):
        return signed
    before, after = kept.get("approvals"), signed.get("approvals")
    return dict(signed, approvals=[*(before[:MAX_APPROVALS] if isinstance(before, list) else []),
                                   *(after[:MAX_SCAN] if isinstance(after, list) else [])])


def verify(signed, trusted: dict | None = None, approvers: dict | None = None) -> dict:
    """Le manifeste lu, si la signature est celle d'une clé de confiance et, quand des
    appareils d'approbation sont déclarés (keys.APPROVER_KEYS, ou `approvers`), s'il porte
    l'approbation de l'un d'eux. UpdateError sinon. `approvers={}` : la seule signature de
    l'auteur (version publiée ici, en attente d'approbation)."""
    trusted = keys.TRUSTED_KEYS if trusted is None else trusted
    approvers = declared_approvers() if approvers is None else approvers
    if not isinstance(signed, dict):
        raise UpdateError(t("updates_package.unreadable"))
    text, kid, signature = signed.get("manifest"), signed.get("key"), signed.get("signature")
    if not all(isinstance(x, str) for x in (text, kid, signature)):
        raise UpdateError(t("updates_package.unreadable"))
    if len(text) > MAX_TEXT:
        raise UpdateError(t("updates_package.too_large"))
    public = trusted.get(kid)
    if not public:
        raise UpdateError(t("updates_package.unknown_key"))
    try:
        Ed25519PublicKey.from_public_bytes(_b64(public)).verify(_b64(signature),
                                                                text.encode("utf-8"))
    except (InvalidSignature, ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise UpdateError(t("updates_package.bad_signature")) from exc
    if approvers and not approved(signed, approvers):
        raise UpdateError(t("updates_package.not_approved"))
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        raise UpdateError(t("updates_package.unreadable")) from exc
    check(manifest)
    return manifest


def check(manifest):
    """Ce qu'un manifeste doit contenir (les fichiers deviennent des chemins : noms sûrs)."""
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT \
            or manifest.get("app") != APP:
        raise UpdateError(t("updates_package.unknown_format"))
    if not version.valid(manifest.get("version")):
        raise UpdateError(t("updates_package.bad_version"))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise UpdateError(t("updates_package.no_files"))
    names = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                or not _NAME.fullmatch(item["name"]) or item["name"] in names:
            raise UpdateError(t("updates_package.bad_file_name"))
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_FILE:
            raise UpdateError(t("updates_package.bad_file_size"))
        if not isinstance(item.get("sha256"), str) or not _SHA256.fullmatch(item["sha256"]):
            raise UpdateError(t("updates_package.bad_file_digest"))
        names.add(item["name"])
    if sum(1 for item in files if item.get("kind") == SOURCE) != 1:
        raise UpdateError(t("updates_package.one_source"))


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
