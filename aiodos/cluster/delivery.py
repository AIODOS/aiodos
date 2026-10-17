"""llama.cpp livré avec les versions signées : personne n'a à l'installer à la main.

L'outil de publication joint l'archive officielle de llama.cpp, d'une version épinglée
(fichier de type « llama.cpp-<plateforme> » du manifeste, empreinte signée). Le service de
mise à jour la reçoit avec la version et la garde dans le paquet (updates/service.py) ;
identique d'une version à l'autre, elle n'est pas retéléchargée (store.reuse).

Ici, on prend celle de la version qui tourne, à défaut celle d'une version plus ancienne
gardée (jamais d'une plus récente, cf. updates/binaries.py), et on la déplie une fois
dans data/engines/llama.cpp/<empreinte>/, avec l'extraction sûre des mises à jour.
"""
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from aiodos import paths
from aiodos.updates import binaries, install, platforms
from aiodos.updates import manifest as manifests
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store, sha256_of

log = logging.getLogger("aiodos.cluster")

FIND_TTL = 60                     # un moteur arrivé avec une nouvelle version est vu en 1 min
PROGRAMS = ("llama-server", "ggml-rpc-server")
_FOLDER = re.compile(r"[0-9a-f]{16}")

_lock = threading.Lock()
_found = {"at": 0.0, "folder": None}


def root() -> Path:
    return paths.data_dir() / "engines" / "llama.cpp"


def complete(folder: Path) -> bool:
    """Le dossier contient les deux programmes dont AIODOS se sert."""
    return all((folder / platforms.executable(p)).is_file() for p in PROGRAMS)


def find(store: Store | None = None) -> Path | None:
    """Dossier du llama.cpp livré, déplié s'il le faut ; None si aucune version gardée ici
    ne le livre pour cette plateforme. Résultat gardé FIND_TTL secondes."""
    with _lock:
        if store is None and _found["at"] and time.monotonic() - _found["at"] < FIND_TTL:
            return _found["folder"]
        folder = None
        for version, item in binaries.candidates(store or Store(), manifests.ENGINE):
            try:
                folder = unpacked(store or Store(), version, item)
                break
            except (UpdateError, OSError) as exc:
                log.warning("llama.cpp de la version %s inutilisable : %s", version, exc)
        if store is None:
            _found.update(at=time.monotonic(), folder=folder)
        return folder


def unpacked(store: Store, version: str, item: dict) -> Path:
    """L'archive du paquet, dépliée dans data/engines/llama.cpp/<empreinte>/."""
    target = root() / item["sha256"][:16]
    if complete(target):
        return target
    archive = store.package_path(version, item)
    if sha256_of(archive) != item["sha256"]:
        raise UpdateError("archive altérée dans le paquet")
    work = root() / f".{item['sha256'][:16]}-en-cours"
    shutil.rmtree(work, ignore_errors=True)
    install.unpack(archive, work)
    found = work if complete(work) else next(
        (d for d in work.iterdir() if d.is_dir() and complete(d)), None)
    if found is None:
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateError("llama-server ou ggml-rpc-server absent de l'archive")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(found, target)
    shutil.rmtree(work, ignore_errors=True)
    _tidy(target)
    log.info("llama.cpp de la version %s prêt dans %s", version, target)
    return target


def _tidy(keep: Path):
    """Moteurs livrés plus anciens : effacés (un programme encore en marche garde ses
    fichiers sous Windows : ils partiront la fois suivante)."""
    for folder in root().iterdir():
        if folder != keep and folder.is_dir() and _FOLDER.fullmatch(folder.name):
            shutil.rmtree(folder, ignore_errors=True)
