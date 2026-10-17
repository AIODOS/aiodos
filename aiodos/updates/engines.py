"""Moteurs livrés avec les versions signées : llama.cpp (gros modèles répartis) et
stable-diffusion.cpp (images). Personne n'a à les installer à la main.

L'outil de publication joint l'archive officielle de chaque moteur, d'une version épinglée
(tools/engine_release.py), comme fichier de type « <moteur>-<plateforme> » du manifeste,
empreinte signée. Le service de mise à jour la reçoit avec la version et la garde dans le
paquet (updates/service.py) ; identique d'une version à l'autre, elle n'est pas
retéléchargée (store.reuse). Une version plus ancienne d'AIODOS ignore le type de fichier
d'un moteur qu'elle ne connaît pas.

Ici, on prend celle de la version qui tourne, à défaut celle d'une version plus ancienne
gardée (jamais d'une plus récente, cf. updates/binaries.py), et on la déplie une fois dans
data/engines/<moteur>/<empreinte>/, avec l'extraction sûre des mises à jour.
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
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import Store, sha256_of

log = logging.getLogger("aiodos.updates")

LLAMA = "llama.cpp"
IMAGES = "stable-diffusion.cpp"
# Les programmes dont AIODOS se sert : une archive sans eux est inutilisable
PROGRAMS = {LLAMA: ("llama-server", "ggml-rpc-server"), IMAGES: ("sd-server",)}
FIND_TTL = 60                     # un moteur arrivé avec une nouvelle version est vu en 1 min
_FOLDER = re.compile(r"[0-9a-f]{16}")

_lock = threading.Lock()
_found = {}                       # moteur -> (instant, dossier)


def root(engine: str) -> Path:
    return paths.data_dir() / "engines" / engine


def complete(folder: Path, engine: str) -> bool:
    """Le dossier contient les programmes dont AIODOS se sert."""
    return all((folder / platforms.executable(p)).is_file() for p in PROGRAMS[engine])


def find(engine: str, store: Store | None = None) -> Path | None:
    """Dossier du moteur livré, déplié s'il le faut ; None si aucune version gardée ici ne
    le livre pour cette plateforme. Résultat gardé FIND_TTL secondes."""
    with _lock:
        at, folder = _found.get(engine, (0.0, None))
        if store is None and at and time.monotonic() - at < FIND_TTL:
            return folder
        folder = None
        for version, item in binaries.candidates(store or Store(), engine):
            try:
                folder = unpacked(engine, store or Store(), version, item)
                break
            except (UpdateError, OSError) as exc:
                log.warning("%s de la version %s inutilisable : %s", engine, version, exc)
        if store is None:
            _found[engine] = (time.monotonic(), folder)
        return folder


def unpacked(engine: str, store: Store, version: str, item: dict) -> Path:
    """L'archive du paquet, dépliée dans data/engines/<moteur>/<empreinte>/."""
    base = root(engine)
    target = base / item["sha256"][:16]
    if complete(target, engine):
        return target
    archive = store.package_path(version, item)
    if sha256_of(archive) != item["sha256"]:
        raise UpdateError("archive altérée dans le paquet")
    work = base / f".{item['sha256'][:16]}-en-cours"
    shutil.rmtree(work, ignore_errors=True)
    install.unpack(archive, work)
    found = work if complete(work, engine) else next(
        (d for d in work.iterdir() if d.is_dir() and complete(d, engine)), None)
    if found is None:
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateError(f"{', '.join(PROGRAMS[engine])} absent(s) de l'archive")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(found, target)
    shutil.rmtree(work, ignore_errors=True)
    _tidy(base, target)
    log.info("%s de la version %s prêt dans %s", engine, version, target)
    return target


def _tidy(base: Path, keep: Path):
    """Moteurs livrés plus anciens : effacés (un programme encore en marche garde ses
    fichiers sous Windows : ils partiront la fois suivante)."""
    for folder in base.iterdir():
        if folder != keep and folder.is_dir() and _FOLDER.fullmatch(folder.name):
            shutil.rmtree(folder, ignore_errors=True)
