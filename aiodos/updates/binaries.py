"""Programmes livrés avec une version : le compagnon Internet aiodos-net, construit pour
chaque plateforme par l'outil de publication (tools/publish.py).

Le manifeste signé les liste comme des fichiers de type « aiodos-net-<plateforme> »
(cf. platforms.py). Une machine ne prend que celui de sa plateforme, empreinte vérifiée
(service.py, p2p.fetch_file), et le garde dans le paquet pour ses machines appairées de
même plateforme.

Pour lancer le compagnon (network/companion.py), on prend celui de la version qui tourne,
à défaut celui de la plus récente des versions plus anciennes dont la machine garde le
paquet ; jamais celui d'une version plus récente, écrit pour un autre code. Il est d'abord
copié à un emplacement fixe, data/updates/bin/, son empreinte revérifiée pendant la
copie : le pare-feu de Windows retient l'autorisation donnée à un chemin, et ne la
redemande donc pas à chaque version. Sous Android et Linux, la copie reçoit le droit
d'exécution.
"""
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from aiodos import __version__
from aiodos.updates import manifest as manifests
from aiodos.updates import platforms
from aiodos.updates import version as versions
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store, sha256_of

log = logging.getLogger("aiodos.updates")

ACTIVE = "bin"                     # dans data/updates/ : le programme en service


def candidates(store: Store, program: str = manifests.COMPANION, running: str | None = None):
    """(version, fichier du manifeste) des programmes livrés utilisables ici, du préféré au
    dernier recours : celui de la version qui tourne, puis ceux des plus anciennes."""
    running = running or __version__
    order = [running] + [v for v in reversed(store.package_versions())
                         if versions.newer(running, v)]
    for version in order:
        found = store.manifest(version)
        item = manifests.binary(found, program) if found else None
        if item and store.has(version, item):
            yield version, item


def delivered(store: Store | None = None, program: str = manifests.COMPANION,
              running: str | None = None):
    """(chemin dans le paquet, version) des programmes livrés, sans rien copier."""
    store = store or Store()
    for version, item in candidates(store, program, running):
        yield store.package_path(version, item), version


def find(store: Store | None = None, program: str = manifests.COMPANION,
         running: str | None = None):
    """(chemin à lancer, version) du programme livré préféré, mis en place (cf.
    activate), ou None. Le choix complet, avec les autres programmes possibles, est
    celui de network/companion.py."""
    store = store or Store()
    for version, item in list(candidates(store, program, running)):
        try:
            return activate(store, version, item, program), version
        except UpdateError as exc:
            log.warning("%s", exc)
    return None


def active(store: Store, program: str = manifests.COMPANION) -> dict:
    """Le programme en place dans bin/ : {"version", "kind", "sha256"}, vide si aucun."""
    try:
        data = json.loads(_meta(store, program).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _meta(store: Store, program: str) -> Path:
    return store.root / ACTIVE / f"{program}.json"


def _intact(path: Path, item: dict) -> bool:
    try:
        return path.stat().st_size == item["size"] and sha256_of(path) == item["sha256"]
    except OSError:
        return False


def _copy(source: Path, target: Path) -> str:
    """Copie `source` vers `target` ; renvoie l'empreinte SHA-256 de ce qui a été lu."""
    digest = hashlib.sha256()
    with open(source, "rb") as src, open(target, "wb") as out:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
            out.write(block)
    return digest.hexdigest()


def make_executable(path: Path):
    """Droit d'exécution partout où il y a droit de lecture (Android, Linux, macOS)."""
    if sys.platform == "win32":
        return
    mode = path.stat().st_mode
    path.chmod(mode | (mode & 0o444) >> 2)


def activate(store: Store, version: str, item: dict,
             program: str = manifests.COMPANION) -> Path:
    """Chemin du programme livré à lancer : bin/<programme>, copié du paquet s'il n'y est
    pas déjà. Si la copie ne peut pas prendre sa place (l'ancien programme tourne encore,
    sous Windows), on le lance depuis le paquet. UpdateError si le fichier du paquet est
    illisible ou altéré ; altéré, il est effacé : il sera téléchargé de nouveau."""
    target = store.root / ACTIVE / platforms.executable(program)
    if active(store, program).get("sha256") == item["sha256"] and _intact(target, item):
        return target
    source = store.package_path(version, item)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        digest = _copy(source, temporary)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Compagnon de la version {version} illisible ({exc}).") from exc
    if digest != item["sha256"]:
        temporary.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
        raise UpdateError(f"Compagnon de la version {version} altéré : il sera téléchargé "
                          "de nouveau.")
    make_executable(temporary)
    try:
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        log.info("Compagnon de la version %s lancé depuis son paquet (%s)", version, exc)
        make_executable(source)
        return source
    meta = _meta(store, program)
    try:
        written = meta.with_name(f".{meta.name}.{os.getpid()}.tmp")
        written.write_text(json.dumps({"version": version, "kind": item["kind"],
                                       "sha256": item["sha256"]}), encoding="utf-8")
        os.replace(written, meta)
    except OSError as exc:              # le programme est en place : recopié la fois suivante
        log.warning("Compagnon en place, mais sa description n'a pas pu être écrite (%s)", exc)
    log.info("Compagnon livré avec la version %s mis en service (%s)", version, target)
    return target
