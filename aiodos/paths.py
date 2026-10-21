"""Emplacements des fichiers : code, données utilisateur, ressources.

Hors Windows (Linux, macOS, Android), les données ne sont lisibles que par l'utilisateur :
dossier en 0700, fichiers en 0600 (keep_private). La base y garde en clair les clés des
machines appairées (node/vault.py), les conversations et les bibliothèques ;
un autre compte de la machine ne doit pas pouvoir la lire. Ce qu'AIODOS écrit pour
l'utilisateur hors de ses données reprend les droits habituels de celui-ci (for_user). Sous
Windows, ce sont les droits du profil (%LOCALAPPDATA%) et DPAPI qui s'en chargent ; une copie
lancée depuis les sources hors du profil garde les droits du dossier où elle est posée.
"""
import logging
import os
import stat
from pathlib import Path

from aiodos.i18n import t

log = logging.getLogger("aiodos")

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
PRIVATE_UMASK = 0o077              # fichiers en 0600, dossiers en 0700
# L'umask de l'utilisateur d'avant keep_private, transmis aux versions qu'AIODOS relance
# (lanceur des mises à jour) : elles héritent de 077 et ne le connaîtraient plus
USER_UMASK_ENV = "AIODOS_USER_UMASK"
_closed = set()                    # dossiers de données déjà fermés aux autres comptes ici
_user_umask = None                 # pour ce qu'AIODOS écrit à l'utilisateur (for_user)
_left_open = []                    # ce que keep_private n'a pas pu fermer (left_open)


def _posix() -> bool:
    """Droits Unix (Linux, macOS, Android) ? Sous Windows, ce sont les ACL du profil."""
    return os.name != "nt"


def _make_dir(d: Path, parents=False):
    """Crée un dossier de données : en 0700 hors Windows. Sous Windows, il garde les droits
    hérités de son parent (le profil, pour une copie installée) : avec Python 3.13 et plus,
    mode=0o700 y poserait une ACL protégée réservée au propriétaire du dossier, qu'une
    session sans élévation ne pourrait plus ouvrir si une session élevée l'avait créé."""
    if _posix():
        d.mkdir(parents=parents, exist_ok=True, mode=0o700)
    else:
        d.mkdir(parents=parents, exist_ok=True)


def _close_to_others(path: Path, quiet=False) -> bool:
    """Retire les droits du groupe et des autres (0755 → 0700, 0644 → 0600), puis vérifie
    qu'ils sont bien partis : un partage CIFS sans extensions Unix, un disque Windows vu par
    WSL sans « metadata », une carte FAT acceptent chmod sans rien changer. Ce qui reste
    ouvert (fichier d'un autre compte, disque sans droits Unix) est dit au journal (sauf
    quiet : l'appelant le dit une fois pour tous) et rendu faux. Un lien est suivi : c'est
    sa cible qui change."""
    if not _posix():
        return True
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            os.chmod(path, mode & ~0o077)
            if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                raise PermissionError("le disque ne garde pas les droits Unix")  # i18n: tel quel (journal)
        return True
    except OSError as exc:
        if not quiet:
            log.warning("%s reste lisible par d'autres comptes de la machine : %s", path, exc)
        return False


def _mine(info: os.stat_result) -> bool:
    """Le fichier est-il à l'utilisateur ? (un AIODOS lancé en root ne touche pas aux
    fichiers des autres comptes)"""
    getuid = getattr(os, "getuid", None)
    return getuid is None or info.st_uid == getuid()


def _close_entry(entry: Path) -> str | None:
    """Ferme un élément du dossier de données ; rend ce qui reste ouvert aux autres comptes
    (pour l'avertissement), sinon None. Un lien est suivi : un fichier lié (la base ou un
    journal déplacé sur un autre disque) est fermé s'il est à l'utilisateur. Un dossier lié
    (des modèles sur un autre disque) peut être partagé exprès : ses droits restent tels
    quels, il est seulement signalé s'il est ouvert."""
    if not entry.is_symlink():
        return None if _close_to_others(entry, quiet=True) else ""
    try:
        target = os.stat(entry)
    except OSError:                              # lien cassé : rien à protéger
        return None
    if not target.st_mode & 0o077:
        return None
    if stat.S_ISDIR(target.st_mode):
        return " " + t("paths.linked_folder")
    if not _mine(target):
        return " " + t("paths.foreign_link")
    return None if _close_to_others(entry, quiet=True) else " " + t("paths.link")


def keep_private() -> list[str]:
    """Au démarrage, avant d'écrire quoi que ce soit (__main__._setup_logging) : ce
    qu'AIODOS crée ensuite n'est lisible que par l'utilisateur (umask 077 : la base et ses
    fichiers -wal et -shm, les journaux, instance.json…), et ce qui existait déjà le
    devient (créé en 0755 et 0644 par les versions d'avant le 20/10/2026). L'umask d'avant
    est gardé pour ce qu'AIODOS écrit à l'utilisateur (for_user), et passé aux versions
    qu'il relance (USER_UMASK_ENV). Rend ce qui reste ouvert aux autres comptes (disque
    sans droits Unix, fichier d'un autre compte) : la journalisation n'est pas encore
    prête, l'appelant l'écrit ensuite. Sous Windows, rien ne change (droits du profil et
    DPAPI) et la liste est vide."""
    if not _posix():
        return []
    global _user_umask
    _user_umask = _original_umask(os.umask(PRIVATE_UMASK))
    os.environ[USER_UMASK_ENV] = f"{_user_umask:03o}"
    folder = _data_path()
    _make_dir(folder, parents=True)
    _closed.add(folder)                          # data_dir() ne le refera pas
    left = [] if _close_to_others(folder, quiet=True) else [f"{folder} {t('paths.the_folder')}"]
    for parent in (folder, folder / "logs"):
        try:
            entries = sorted(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            note = _close_entry(entry)
            if note is not None:
                left.append(entry.relative_to(folder).as_posix() + note)
    _left_open[:] = left
    return left


def _original_umask(previous: int) -> int:
    """L'umask de l'utilisateur. Une version relancée par AIODOS reçoit 077 de la précédente
    (previous vaut alors PRIVATE_UMASK) : le vrai lui vient par USER_UMASK_ENV. Une valeur
    illisible est ignorée."""
    if previous == PRIVATE_UMASK:
        try:
            return int(os.environ.get(USER_UMASK_ENV, ""), 8) & 0o777
        except ValueError:
            pass
    return previous


def left_open() -> list[str]:
    """Ce que keep_private n'a pas pu fermer aux autres comptes, pour l'interface :
    l'avertissement du journal ne se voit ni depuis la fenêtre ni depuis l'appli."""
    return list(_left_open)


def rights() -> str:
    """Les droits réels du dossier de données et de la base (« dossier 0700, base 0600 »),
    écrits au journal à chaque démarrage (__main__._setup_logging) : ce qui a vraiment pris
    sur cette machine, pas ce qui a été demandé. Vide sous Windows."""
    if not _posix():
        return ""
    folder = _data_path()
    shown = []
    for label, path in (("dossier", folder), ("base", folder / "aiodos.db")):
        try:
            shown.append(f"{label} {stat.S_IMODE(os.stat(path).st_mode):04o}")
        except OSError:                          # pas encore de base : premier démarrage
            pass
    return ", ".join(shown)


def for_user(path) -> None:
    """Ce qu'AIODOS écrit pour l'utilisateur hors de ses données (« Enregistrer sous… »,
    une galerie dans un dossier qu'il a choisi) reprend les droits qu'il aurait eus sans
    AIODOS (0644 et 0755 avec l'umask 022 courant) : l'umask 077 de keep_private vaut pour
    tout le processus, et une image posée dans un dossier partagé y deviendrait illisible
    pour les autres comptes ou services. Sans effet sous Windows, avant keep_private, dans
    le dossier de données, et sur ce qui appartient à un autre compte."""
    if not _posix() or _user_umask is None:
        return
    try:
        target = Path(path).resolve()
        if target.is_relative_to(_data_path().resolve()):
            return
        info = os.stat(target)
        if _mine(info):
            full = 0o777 if stat.S_ISDIR(info.st_mode) else 0o666
            os.chmod(target, full & ~_user_umask)
    except OSError:
        pass


def installed_data_dir() -> Path | None:
    """%LOCALAPPDATA%\\AIODOS\\data pour une copie posée par l'installeur, sinon None."""
    if not INSTALLED_MARK.is_file():
        return None
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), "AppData", "Local")
    return Path(base) / "AIODOS" / "data"


def _data_path() -> Path:
    return Path(os.environ.get("AIODOS_DATA_DIR") or os.environ.get("COEUR_DATA_DIR")
                or installed_data_dir() or (APP_ROOT / "data"))


def data_path() -> Path:
    """Le dossier des données, sans le créer ni le fermer aux autres (cf. data_dir)."""
    return _data_path()


def data_dir() -> Path:
    """Dossier des données : AIODOS_DATA_DIR (essais, --data-dir, versions lancées par le
    lanceur des mises à jour ; COEUR_DATA_DIR, l'ancien nom, reste compris), sinon celui de
    l'utilisateur pour une copie installée, sinon data/ à côté du code. Hors Windows, il
    n'est ouvert qu'à l'utilisateur (0700), même créé avant par une version plus ancienne."""
    d = _data_path()
    _make_dir(d, parents=True)
    if d not in _closed:
        _closed.add(d)
        _close_to_others(d)
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
    _make_dir(d, parents=True)
    return d


def gateway_dir(part: str) -> Path:
    """Les fichiers des applications (gateway/input_store.py, file_store.py) : « inputs »
    (ce qu'elles envoient avant un travail), « out » (ce que leurs travaux produisent). Hors
    du dossier Images, qui peut être synchronisé."""
    d = data_dir() / "gateway" / part
    _make_dir(d, parents=True)
    return d


def engine_runs_dir() -> Path:
    """Le dossier de calcul des unités de moteur (ComfyUI) : effacé au démarrage."""
    d = data_dir() / "tmp" / "engine_runs"
    _make_dir(d, parents=True)
    return d


def log_path() -> Path:
    d = data_dir() / "logs"
    _make_dir(d)
    return d / "aiodos.log"
