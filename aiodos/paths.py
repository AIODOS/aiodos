"""Emplacements des fichiers : code, données utilisateur, ressources."""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
APP_ROOT = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "ui" / "static"
ASSETS_DIR = APP_ROOT / "assets"
LOGO_PNG = ASSETS_DIR / "logo.png"
LOGO_ICO = ASSETS_DIR / "logo.ico"
LEGACY_DATA_DIR = APP_ROOT / "legacy" / "data"


def data_dir() -> Path:
    """Dossier des données (surchargeable par AIODOS_DATA_DIR, utile pour les tests ;
    COEUR_DATA_DIR, l'ancien nom, reste compris)."""
    d = Path(os.environ.get("AIODOS_DATA_DIR") or os.environ.get("COEUR_DATA_DIR")
             or (APP_ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    path = data_dir() / "aiodos.db"
    _adopt_old_database(path)
    return path


def _adopt_old_database(path: Path):
    """Le projet s'appelait Cœur IA : sa base « coeur.db » devient « aiodos.db ».
    Ses fichiers -wal et -shm suivent, sans quoi les dernières écritures seraient
    perdues. Sans effet si la nouvelle base existe déjà."""
    old = path.with_name("coeur.db")
    if path.exists() or not old.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        source = old.with_name(old.name + suffix)
        if source.exists():
            os.replace(source, path.with_name(path.name + suffix))


def work_dir() -> Path:
    """Fichiers reçus d'autres machines le temps de calculer pour elles."""
    d = data_dir() / "tmp" / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    d = data_dir() / "logs"
    d.mkdir(exist_ok=True)
    return d / "aiodos.log"
