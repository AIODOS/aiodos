"""llama.cpp livré avec les versions signées : personne n'a à l'installer à la main
(updates/engines.py, commun aux moteurs livrés). Dossier : data/engines/llama.cpp/."""
from pathlib import Path

from aiodos.updates import engines
from aiodos.updates.store import Store

PROGRAMS = engines.PROGRAMS[engines.LLAMA]


def root() -> Path:
    return engines.root(engines.LLAMA)


def complete(folder: Path) -> bool:
    """Le dossier contient les deux programmes dont AIODOS se sert."""
    return engines.complete(folder, engines.LLAMA)


def find(store: Store | None = None) -> Path | None:
    """Dossier du llama.cpp livré, déplié s'il le faut ; None sinon."""
    return engines.find(engines.LLAMA, store)


def unpacked(store: Store, version: str, item: dict) -> Path:
    return engines.unpacked(engines.LLAMA, store, version, item)
