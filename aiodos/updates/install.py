"""Installation d'une version reçue, à côté de celle qui tourne :

1. l'archive est revérifiée (empreinte donnée par le manifeste signé) ;
2. elle est extraite sans danger : ni chemin absolu, ni « .. », ni lien, taille bornée ;
3. le numéro écrit dans le code doit être celui du manifeste ;
4. essai à blanc : le code se charge-t-il avec le Python et les paquets de cette
   machine ? Sinon (un paquet manque…), on le dit et rien ne change ;
5. elle est rangée dans versions/<version>/, prête pour le prochain démarrage.
"""
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from aiodos.i18n import t
from aiodos.updates import manifest as manifests
from aiodos.updates.manifest import UpdateError
from aiodos.updates.store import MARKER, Store, sha256_of

MAX_TOTAL = 400 * 1024 ** 2
MAX_ENTRIES = 20_000
SMOKE_TIMEOUT = 300                # un vieux téléphone charge numpy et FastAPI en une minute
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
             *(f"LPT{i}" for i in range(10))}

# Lancé dans le dossier de la version, avec un dossier de données jetable
# i18n: tel quel (programme Python lancé à part)
SMOKE = """
import os, sys
import aiodos
here = os.path.realpath(sys.argv[1])
if not os.path.realpath(aiodos.__file__).startswith(here + os.sep):
    sys.exit("autre code AIODOS chargé : " + aiodos.__file__)
import aiodos.__main__, aiodos.api.server
print("version", aiodos.__version__)
"""


def interpreter() -> str:
    """Le Python qui fait tourner AIODOS ; python.exe plutôt que pythonw.exe, dont les
    sorties sont perdues."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return sys.executable


def install(store: Store, found: dict) -> Path:
    """Installe la version décrite par le manifeste vérifié `found`. UpdateError si
    elle ne peut pas l'être sur cette machine (raison lisible)."""
    wanted = found["version"]
    target = store.version_dir(wanted)
    if (target / MARKER).is_file():
        return target
    _check_python(found)
    item = manifests.source(found)
    archive = store.file_path(wanted, item["name"])
    if not archive.is_file() or sha256_of(archive) != item["sha256"]:
        archive.unlink(missing_ok=True)
        raise UpdateError(t("updates_install.archive_missing"))
    work = target.with_name(f".{wanted}-{secrets.token_hex(4)}")
    try:
        extract(archive, work)
        declared = declared_version(work)
        if declared != wanted:
            raise UpdateError(t("updates_install.version_mismatch", declared=declared,
                                wanted=wanted))
        smoke_test(work, wanted)
        (work / MARKER).write_text(json.dumps({"version": wanted, "sha256": item["sha256"],
                                               "at": time.time()}), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)         # installation interrompue d'une fois précédente
        _move(work, target)
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
    return target


def _move(source: Path, target: Path):
    """Renommage, réessayé un instant : un antivirus examine souvent les fichiers neufs."""
    for attempt in range(10):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


def _check_python(found: dict):
    needed = found.get("min_python")
    if not needed:
        return
    try:
        wanted = tuple(int(part) for part in str(needed).split(".")[:2])
    except ValueError as exc:
        raise UpdateError(t("updates_install.python_unreadable", needed=repr(needed))) from exc
    if sys.version_info[:2] < wanted:
        raise UpdateError(t("updates_install.python_too_old", needed=needed,
                            here=platform.python_version()))


def _safe_parts(entry: zipfile.ZipInfo) -> tuple:
    name = entry.filename
    if "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise UpdateError(t("updates_install.path_refused", name=repr(name)))
    parts = tuple(p for p in PurePosixPath(name).parts if p not in ("", "."))
    if not parts or ".." in parts:
        raise UpdateError(t("updates_install.path_refused", name=repr(name)))
    if any(p.split(".")[0].upper() in _RESERVED for p in parts):
        raise UpdateError(t("updates_install.reserved_name", name=repr(name)))
    if stat.S_ISLNK(entry.external_attr >> 16):
        raise UpdateError(t("updates_install.link_refused", name=repr(name)))
    return parts


def extract(archive: Path, dest: Path):
    """Archive du code d'une version : dépliée, puis le paquet aiodos doit y être."""
    unpack(archive, dest)
    if not (dest / "aiodos" / "__init__.py").is_file():
        raise UpdateError(t("updates_install.no_code"))


def unpack(archive: Path, dest: Path):
    """Déplie une archive zip dans `dest` (qui ne doit pas exister) : chemins vérifiés un
    à un, liens refusés, taille et nombre de fichiers bornés (cf. cluster/delivery.py pour
    le moteur llama.cpp)."""
    dest.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            if len(entries) > MAX_ENTRIES:
                raise UpdateError(t("updates_install.too_many_files"))
            if sum(e.file_size for e in entries) > MAX_TOTAL:
                raise UpdateError(t("updates_install.too_large"))
            for entry in entries:
                target = dest.joinpath(*_safe_parts(entry))
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with z.open(entry) as src, open(target, "wb") as out:
                    for block in iter(lambda: src.read(1024 * 1024), b""):
                        written += len(block)
                        if written > entry.file_size:
                            raise UpdateError(t("updates_install.longer_than_announced"))
                        out.write(block)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise UpdateError(t("updates_install.damaged", error=exc)) from exc


def declared_version(folder: Path) -> str | None:
    try:
        text = (folder / "aiodos" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    found = _VERSION.search(text)
    return found.group(1) if found else None


def smoke_test(folder: Path, expected: str):
    """Le code de la version se charge-t-il ici ? (dans un processus à part)"""
    with tempfile.TemporaryDirectory(prefix="aiodos-essai-") as data:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AIODOS_UPDATE")}
        env.update(AIODOS_DATA_DIR=data, PYTHONIOENCODING="utf-8")
        try:
            done = subprocess.run([interpreter(), "-c", SMOKE, str(folder)], cwd=folder,
                                  env=env, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=SMOKE_TIMEOUT,
                                  creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(t("updates_install.smoke_timeout")) from exc
        except OSError as exc:
            raise UpdateError(t("updates_install.smoke_failed", error=exc)) from exc
    if done.returncode == 0 and f"version {expected}" in done.stdout:
        return
    missing = re.search(r"No module named '([\w.]+)'", done.stderr)    # i18n: tel quel (motif)
    if missing:
        raise UpdateError(t("updates_install.missing_package",
                            package=missing.group(1).split(".")[0]))
    last = (done.stderr.strip() or done.stdout.strip() or "?").splitlines()[-1]
    raise UpdateError(t("updates_install.does_not_load", error=last[:200]))
