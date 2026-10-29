"""Le catalogue des recettes de ComfyUI (docs/conception/partage-puissance.md § 3.9, § 0.3 ;
comfyui-createur.md § 3.1) : les fichiers engines/recipe_catalogue/comfyui-<date>.json, livrés
avec AIODOS (et donc avec ses versions, y compris par les mises à jour pair à pair : pas de
serveur central), écrits par l'auteur avec tools/comfy_recipes.py.

La signature : la clé de publication (updates/keys.TRUSTED_KEYS), sous le contexte propre
« aiodos-catalogue\\n » (une signature de catalogue ne vaut jamais manifeste de version, ni
l'inverse : comme APPROVAL_CONTEXT, updates/manifest.py), sur les octets du fichier tels
quels, jamais re-sérialisés. Elle est à côté, dans <fichier>.sig : {"key": identifiant,
"signature": base64}. Seul l'auteur signe, avec sa clé, hors du dépôt
(tools/comfy_recipes.py sign) : AIODOS ne signe jamais rien ici.

- Un fichier signé par une clé de confiance : ses recettes servent ici, chez mes machines, et
  pourront servir au réseau public (recipe(…, public=True)).
- Un fichier sans signature (le premier jeu, tant que l'auteur ne l'a pas signé) : ses
  recettes ne sont acceptées QUE localement (ce PC et mes machines), jamais pour le réseau
  public : recipe(…, public=True) les refuse (recipe_unsigned).
- Une signature fausse, d'une clé inconnue, ou un fichier illisible : le fichier entier est
  écarté, même localement (noté dans errors et au journal).
Deux fichiers qui donnent la même recette : le plus récent (nom de fichier, donc date) l'emporte.
"""
import base64
import binascii
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from aiodos.engines import recipes
from aiodos.i18n import t
from aiodos.updates import keys, manifest

log = logging.getLogger("aiodos.engines")

CONTEXT = b"aiodos-catalogue\n"
FOLDER = Path(__file__).resolve().parent / "recipe_catalogue"
PATTERN = "comfyui-*.json"
KIND = "aiodos-comfyui-recipes"
FORMAT = 1
MAX_BYTES = 4 * 1024 ** 2
MAX_RECIPES = 64

_lock = threading.Lock()
_cache = {"stamp": None, "value": None}


class CatalogueError(Exception):
    """Recette absente ou refusée : code (recipe_unknown, recipe_unsigned, catalogue_invalid,
    catalogue_signature), message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Catalogue:
    recipes: dict = field(default_factory=dict)   # id -> recette (vérifiée par recipes.check)
    signed: frozenset = frozenset()               # identifiants des recettes signées
    keys: dict = field(default_factory=dict)      # id -> clé qui a signé son fichier
    files: dict = field(default_factory=dict)     # id -> nom du fichier
    errors: tuple = ()                            # (fichier, raison) écartés


# ---------------------------------------------------------------- la signature
def message(data: bytes) -> bytes:
    """Ce que signe la clé de publication : les octets du fichier, après le contexte."""
    return CONTEXT + data


def sign(data: bytes, private: Ed25519PrivateKey) -> dict:
    """La signature d'un fichier de catalogue (tools/comfy_recipes.py sign, chez l'auteur
    seulement : la clé n'est jamais dans le dépôt)."""
    return {"key": manifest.key_id(manifest.raw_public(private)),
            "signature": base64.b64encode(private.sign(message(data))).decode("ascii")}


def verify(data: bytes, envelope, trusted: dict | None = None) -> str:
    """L'identifiant de la clé de confiance qui a signé ces octets ; CatalogueError sinon."""
    trusted = keys.TRUSTED_KEYS if trusted is None else trusted
    if not isinstance(envelope, dict) or not isinstance(envelope.get("key"), str) \
            or not isinstance(envelope.get("signature"), str):
        raise CatalogueError("catalogue_signature", t("comfy_recipes.signature_unreadable"))
    public = trusted.get(envelope["key"])
    if not public:
        raise CatalogueError("catalogue_signature", t("comfy_recipes.signature_unknown_key"))
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True)).verify(
            base64.b64decode(envelope["signature"], validate=True), message(data))
    except (InvalidSignature, ValueError, binascii.Error) as exc:
        raise CatalogueError("catalogue_signature", t("comfy_recipes.signature_bad")) from exc
    return envelope["key"]


# ------------------------------------------------------------------- la lecture
def parse(data: bytes) -> list:
    """Les recettes d'un fichier de catalogue, chacune vérifiée ; CatalogueError sinon."""
    if len(data) > MAX_BYTES:
        raise CatalogueError("catalogue_invalid", t("comfy_recipes.catalogue_too_big"))
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CatalogueError("catalogue_invalid", t("comfy_recipes.catalogue_unreadable")) from exc
    if not isinstance(doc, dict) or doc.get("kind") != KIND or doc.get("format") != FORMAT \
            or not isinstance(doc.get("recipes"), list) or len(doc["recipes"]) > MAX_RECIPES:
        raise CatalogueError("catalogue_invalid", t("comfy_recipes.catalogue_unreadable"))
    found, seen = [], set()
    for recipe in doc["recipes"]:
        try:
            recipes.check(recipe)
        except recipes.RecipeError as exc:
            raise CatalogueError("catalogue_invalid", str(exc)) from exc
        if recipe["id"] in seen:
            raise CatalogueError("catalogue_invalid", t("comfy_recipes.duplicate",
                                                        id=recipe["id"]))
        seen.add(recipe["id"])
        found.append(recipe)
    return found


def _stamp(folder: Path) -> tuple:
    stamp = []
    try:
        for path in sorted(folder.glob(PATTERN)) + sorted(folder.glob(PATTERN + ".sig")):
            info = path.stat()
            stamp.append((path.name, info.st_size, info.st_mtime_ns))
    except OSError:
        pass
    return tuple(stamp)


def read(folder: Path | None = None, trusted: dict | None = None) -> Catalogue:
    """Le catalogue de ce dossier, relu seulement quand ses fichiers changent."""
    folder = Path(folder) if folder is not None else FOLDER
    stamp = (str(folder), _stamp(folder), id(trusted) if trusted is not None else None)
    with _lock:
        if _cache["stamp"] == stamp and _cache["value"] is not None:
            return _cache["value"]
    found, signed, signers, files, errors = {}, set(), {}, {}, []
    for path in sorted(folder.glob(PATTERN)):
        try:
            data = path.read_bytes()
            listed = parse(data)
            sig = path.with_name(path.name + ".sig")
            key = None
            if sig.exists():
                try:
                    envelope = json.loads(sig.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise CatalogueError("catalogue_signature",
                                         t("comfy_recipes.signature_unreadable")) from exc
                key = verify(data, envelope, trusted)
        except (OSError, CatalogueError) as exc:
            errors.append((path.name, str(exc)))
            log.warning("Catalogue de recettes %s écarté : %s", path.name, exc)
            continue
        for recipe in listed:
            found[recipe["id"]] = recipe
            files[recipe["id"]] = path.name
            if key:
                signed.add(recipe["id"])
                signers[recipe["id"]] = key
            else:
                signed.discard(recipe["id"])
                signers.pop(recipe["id"], None)
    value = Catalogue(found, frozenset(signed), signers, files, tuple(errors))
    with _lock:
        _cache.update(stamp=stamp, value=value)
    return value


def forget():
    with _lock:
        _cache.update(stamp=None, value=None)


def recipe(recipe_id: str, public: bool = False, catalogue: Catalogue | None = None) -> dict:
    """La recette de ce catalogue ; CatalogueError si elle n'y est pas (recipe_unknown), ou
    si elle n'est pas signée et qu'elle doit servir au réseau public (recipe_unsigned : une
    recette non signée n'est acceptée que localement)."""
    catalogue = catalogue or read()
    found = catalogue.recipes.get(str(recipe_id))
    if found is None:
        raise CatalogueError("recipe_unknown", t("comfy_recipes.unknown", id=str(recipe_id)[:80]))
    if public and found["id"] not in catalogue.signed:
        raise CatalogueError("recipe_unsigned", t("comfy_recipes.unsigned_public",
                                                  id=found["id"]))
    return found


def public_ids(catalogue: Catalogue | None = None) -> list:
    """Les recettes que ce PC peut annoncer au réseau public (signées seulement), au plus 32
    (profil public v2, § 3.5)."""
    catalogue = catalogue or read()
    return sorted(catalogue.signed)[:32]


def digest_of(path: Path) -> str:
    """L'empreinte d'un fichier de catalogue (journal, outil)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
