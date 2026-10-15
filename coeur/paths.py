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
    """Dossier des données (surchargeable par COEUR_DATA_DIR, utile pour les tests)."""
    d = Path(os.environ.get("COEUR_DATA_DIR") or (APP_ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "coeur.db"


def work_dir() -> Path:
    """Fichiers reçus d'autres machines le temps de calculer pour elles."""
    d = data_dir() / "tmp" / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    d = data_dir() / "logs"
    d.mkdir(exist_ok=True)
    return d / "coeur.log"
