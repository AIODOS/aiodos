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
# Posé par l'installeur Windows (tools/build_installer.py) à côté du code installé : les
# données vont alors dans le dossier de l'utilisateur, pas à côté du code, que
# l'installeur remplace à chaque nouvelle version
INSTALLED_MARK = APP_ROOT / "installation.json"


def installed_data_dir() -> Path | None:
    """%LOCALAPPDATA%\\AIODOS\\data pour une copie posée par l'installeur, sinon None."""
    if not INSTALLED_MARK.is_file():
        return None
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), "AppData", "Local")
    return Path(base) / "AIODOS" / "data"


def data_dir() -> Path:
    """Dossier des données : AIODOS_DATA_DIR (essais, --data-dir, versions lancées par le
    lanceur des mises à jour ; COEUR_DATA_DIR, l'ancien nom, reste compris), sinon celui de
    l'utilisateur pour une copie installée, sinon data/ à côté du code."""
    d = Path(os.environ.get("AIODOS_DATA_DIR") or os.environ.get("COEUR_DATA_DIR")
             or installed_data_dir() or (APP_ROOT / "data"))
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
