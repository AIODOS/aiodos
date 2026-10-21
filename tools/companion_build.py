"""Construire le compagnon aiodos-net d'une version à publier (tools/publish.py).

Pour chaque plateforme, le dossier compagnon/ du commit publié est exporté dans un
dossier temporaire (git archive : ni modification en cours, ni ancien binaire), puis
construit par cargo, profil « distribution » (cf. compagnon/Cargo.toml), avec le
commit donné au programme (AIODOS_NET_COMMIT). Le binaire est ensuite vérifié :
format de la plateforme, commit retrouvé dans ses octets, et, s'il peut tourner sur
cette machine, « aiodos-net --version » au numéro du Cargo.toml de ce commit.

Les dépendances compilées se gardent d'une fois sur l'autre dans un dossier de
construction privé, hors du dépôt (build_dir : %LOCALAPPDATA%\\aiodos-build sous Windows,
~/.cache/aiodos-build ailleurs, ou AIODOS_BUILD_DIR) : une nouvelle publication ne
recompile que le compagnon (quelques minutes tout de même, à cause de l'optimisation de tout
le programme). Privé, car une dépendance compilée qu'un autre compte remplacerait serait
liée telle quelle dans le compagnon signé ; l'export des sources l'est aussi, et le
programme construit est copié avant d'être vérifié et pesé. Un .cargo/config posé dans un
dossier parent (C:\\.cargo, que tout compte peut créer) est refusé : cargo le lirait.

git est lancé sans ce que .git/config pourrait faire exécuter ni ses objets de
remplacement (GIT_SAFE : fsmonitor, crochets, signature, refs/replace). Cela ne suffit pas
(un filtre déclaré dans .git/config tourne aussi pendant git archive) : les outils de
publication refusent en plus un dépôt que d'autres comptes de la machine peuvent modifier,
qui porte des fichiers non suivis là où Python cherche leur code, ou dont la configuration
git est piégée (require_private_repository, cf. aiodos/updates/private.py, qu'ils
appellent avant d'importer quoi que ce soit du dépôt), et des outils de construction
(cargo, NDK) dans un dossier ouvert à d'autres (require_private_tools). Les fichiers
temporaires de cargo, rustc et de l'éditeur de liens vont dans un dossier privé
(private_temp) : %TEMP% peut être ouvert à d'autres comptes. Une configuration de cargo
apparue pendant la construction dans un dossier parent rejette le programme construit, et
un dossier parent où d'autres comptes pourraient créer .cargo est signalé (cargo_exposed).

Ce qu'il faut sur la machine qui publie :
- pour sa propre plateforme (windows-x86_64 sur le PC de l'auteur) : Rust, et sous
  Windows les outils C++ de Visual Studio ;
- pour android-aarch64 : en plus, la cible Rust (rustup target add
  aarch64-linux-android) et le NDK Android (ANDROID_NDK_HOME, ANDROID_NDK_ROOT, ou
  <SDK>/ndk/<version> d'après ANDROID_HOME ou ANDROID_SDK_ROOT).
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiodos.updates import platforms, private  # noqa: E402

PROGRAM = "aiodos-net"
PROFILE = "distribution"
ANDROID = "android-aarch64"
ANDROID_API = 24                   # Android 7 et plus, comme le nœud sous Termux
BUILD_TIMEOUT = 3600
_PE_MACHINES = {"x86_64": 0x8664, "aarch64": 0xAA64}
_ELF_MACHINES = {"x86_64": 62, "aarch64": 183}
_MACHO_CPUS = {"x86_64": 0x01000007, "aarch64": 0x0100000C}
# Ce que .git/config ou le dépôt pourraient faire exécuter ou substituer aux commandes des
# outils (cf. aiodos/updates/private.py)
GIT_SAFE = private.GIT_SAFE


class BuildError(Exception):
    """Compagnon impossible à construire, ou qui ne correspond pas (message lisible)."""


def git_command(repo: Path, *args) -> list:
    """La commande git sur le dépôt `repo`, sans ses crochets, son fsmonitor ni ses objets
    de remplacement (GIT_SAFE)."""
    return private.git_command(repo, *args)


def require_private_repository(repo: Path = ROOT, extra=()):
    """BuildError si les outils ne doivent rien signer ni livrer depuis le dépôt : d'autres
    comptes de cette machine peuvent le modifier (ou l'un des dossiers `extra`, de travail),
    des fichiers non suivis s'y trouvent là où Python cherche le code des outils, ou sa
    configuration git ferait exécuter ou publier autre chose (private.repository_problems).
    Ses droits ne sont jamais changés ici : c'est à l'auteur de les corriger."""
    try:
        private.require_private_repository(repo, extra)
    except private.NotPrivate as exc:
        raise BuildError(str(exc)) from exc


def build_dir() -> Path:
    """Dossier de construction privé, hors du dépôt (AIODOS_BUILD_DIR : un autre, essais)."""
    chosen = os.environ.get("AIODOS_BUILD_DIR")
    if chosen:
        return Path(chosen)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "aiodos-build"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "aiodos-build"


def target_dir() -> Path:
    """CARGO_TARGET_DIR du compagnon : privé, créé tel s'il manque. BuildError s'il est
    ouvert à d'autres comptes (ses droits ne sont jamais changés ici)."""
    try:
        return private.private_dir(private.private_dir(build_dir()) / "compagnon")
    except (private.NotPrivate, OSError) as exc:
        raise BuildError(f"{exc}. Choisis un dossier à toi seul (AIODOS_BUILD_DIR), ou "
                         "rends celui-ci privé.") from exc


def private_temp(env: dict, folder: Path) -> dict:
    """`env`, où les fichiers temporaires de l'outil lancé (TMP, TEMP, TMPDIR) vont dans
    `folder`, créé privé : %TEMP% peut être ouvert à d'autres comptes (bacs à sable), et un
    dossier que cargo, rustc, l'éditeur de liens, pip ou Inno Setup y créent en hérite les
    droits. BuildError si `folder` existait déjà ouvert à d'autres."""
    try:
        folder = private.private_dir(folder)
    except (private.NotPrivate, OSError) as exc:
        raise BuildError(f"Dossier temporaire de la construction : {exc}") from exc
    return dict(env, TMP=str(folder), TEMP=str(folder), TMPDIR=str(folder))


def cargo_configs(work: Path) -> list:
    """Fichiers de configuration de cargo dans `work` et ses dossiers parents, hors de
    CARGO_HOME : cargo les lirait (rustc-wrapper, éditeur de liens…)."""
    home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo").resolve()
    found = []
    work = Path(work).resolve()
    for parent in (work, *work.parents):
        folder = parent / ".cargo"
        if folder.resolve() == home:
            continue
        found += [str(folder / n) for n in ("config", "config.toml") if (folder / n).is_file()]
    return found


def cargo_exposed(work: Path) -> list:
    """Dossiers parents de `work` sans .cargo où d'autres comptes peuvent en créer un (C:\\
    le permet par défaut) : cargo lirait une configuration qu'ils y poseraient pendant la
    construction. Un .cargo vide et privé, créé une fois, ferme la porte."""
    home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo").resolve()
    found = []
    work = Path(work).resolve()
    for parent in (work, *work.parents):
        folder = parent / ".cargo"
        if folder.resolve() == home:
            continue
        try:
            if folder.exists():
                private.require_private(folder, "le dossier .cargo")
            elif private.foreign_writers(parent):
                found.append(folder)
        except private.NotPrivate as exc:
            raise BuildError(f"{exc} : cargo y lirait une configuration posée par un autre "
                             "compte. Rends-le privé, puis recommence.") from exc
        except OSError:
            found.append(folder)
    return found


def tool_folders(target: str) -> list:
    """[(ce que c'est, dossier)] des outils qui construisent le compagnon de `target` :
    cargo et, pour Android, le NDK (son éditeur de liens)."""
    found = [("le dossier de cargo", Path(cargo()).resolve().parent)] if cargo() else []
    if target == ANDROID and ndk() is not None:
        found.append(("le NDK Android", ndk()))
    return found


def require_private_tools(target: str):
    """BuildError si d'autres comptes peuvent modifier les outils de construction : un
    cargo ou un éditeur de liens remplacé piégerait le compagnon signé."""
    for what, folder in tool_folders(target):
        try:
            private.require_private(folder, what)
        except private.NotPrivate as exc:
            raise BuildError(f"{exc}. Installe ces outils dans ton dossier personnel, ou "
                             "rends ce dossier privé.") from exc


def default_targets() -> list:
    """Plateformes construites par défaut : celle de cette machine, et Android."""
    return [t for t in dict.fromkeys((platforms.tag(), ANDROID)) if t]


def cargo() -> str | None:
    """cargo (AIODOS_CARGO : un autre programme, pour les essais de l'outil)."""
    return shutil.which(os.environ.get("AIODOS_CARGO") or "cargo")


def _version_key(path: Path) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", path.name))


def ndk() -> Path | None:
    """Le NDK Android de cette machine (le plus récent), ou None."""
    for name in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT"):
        value = os.environ.get(name)
        if value and (Path(value) / "toolchains").is_dir():
            return Path(value)
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(name)
        found = sorted((p for p in (Path(value) / "ndk").glob("*") if (p / "toolchains").is_dir()),
                       key=_version_key) if value else []
        if found:
            return found[-1]
    return None


def android_tools(ndk_dir: Path) -> tuple:
    """(éditeur de liens, archiveur) du NDK pour aarch64."""
    host = {"win32": "windows-x86_64", "darwin": "darwin-x86_64"}.get(sys.platform,
                                                                       "linux-x86_64")
    folder = ndk_dir / "toolchains" / "llvm" / "prebuilt" / host / "bin"
    windows = sys.platform == "win32"
    return (folder / f"aarch64-linux-android{ANDROID_API}-clang{'.cmd' if windows else ''}",
            folder / f"llvm-ar{'.exe' if windows else ''}")


def _rust_target_installed(triple: str) -> bool:
    rustc = shutil.which("rustc")
    if not rustc:
        return False
    done = subprocess.run([rustc, "--print", "target-libdir", "--target", triple],
                          capture_output=True, text=True)
    return done.returncode == 0 and Path(done.stdout.strip()).is_dir()


def missing(target: str) -> str | None:
    """Pourquoi `target` ne peut pas être construite ici (outil absent), ou None."""
    if target not in platforms.RUST_TARGETS:
        return f"plateforme inconnue (connues : {', '.join(platforms.RUST_TARGETS)})"
    if cargo() is None:
        return "cargo est introuvable (installer Rust : https://rustup.rs)"
    if target == platforms.tag():
        return None
    if target != ANDROID:
        return "construction croisée prévue seulement pour Android"
    found = ndk()
    if found is None:
        return ("NDK Android introuvable (ANDROID_NDK_HOME, ou ANDROID_HOME avec un dossier "
                "ndk/<version>)")
    linker = android_tools(found)[0]
    if not linker.is_file():
        return f"NDK Android incomplet ({linker} absent)"
    triple = platforms.RUST_TARGETS[target]
    if not _rust_target_installed(triple):
        return f"cible Rust absente (rustup target add {triple})"
    return None


def has_sources(repo: Path, commit: str) -> bool:
    done = subprocess.run(git_command(repo, "ls-tree", "--name-only", commit, "compagnon/"),
                          capture_output=True, text=True, encoding="utf-8")
    return done.returncode == 0 and bool(done.stdout.strip())


def _export(repo: Path, commit: str, work: Path) -> Path:
    """Le dossier compagnon/ du commit, tel qu'il est dans le dépôt."""
    archive = work / "compagnon.zip"
    done = subprocess.run(git_command(repo, "archive", "--format=zip", f"--output={archive}",
                                      commit, "--", "compagnon"),
                          capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise BuildError(f"Export du compagnon impossible : {done.stderr.strip()}")
    with zipfile.ZipFile(archive) as z:
        z.extractall(work)
    return work / "compagnon"


def crate_version(source: Path) -> str | None:
    found = re.search(r'^version\s*=\s*"([^"]+)"',
                      (source / "Cargo.toml").read_text(encoding="utf-8"), re.M)
    return found.group(1) if found else None


def file_name(number: str, target: str) -> str:
    """Nom du fichier dans le paquet : aiodos-net-2.0.3-windows-x86_64.exe."""
    return f"{PROGRAM}-{number}-{target}{'.exe' if target.startswith('windows') else ''}"


def build(target: str, commit: str, number: str, out: Path, repo: Path = ROOT) -> Path:
    """Construit le compagnon de `target` à partir du commit, le vérifie, et le copie
    dans `out` (nom : file_name ; un dossier privé, celui de la publication). BuildError
    sinon. Les messages de cargo s'affichent."""
    triple = platforms.RUST_TARGETS[target]
    require_private_tools(target)
    shared = target_dir()
    env = dict(os.environ, AIODOS_NET_COMMIT=commit, CARGO_TARGET_DIR=str(shared))
    if target == ANDROID:
        linker, archiver = android_tools(ndk())
        env.update({"CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": str(linker),
                    "CC_aarch64_linux_android": str(linker),
                    "AR_aarch64_linux_android": str(archiver)})
    started = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    result = out / file_name(number, target)
    with private.private_tempdir("aiodos-compagnon-", build_dir()) as work:
        foreign = cargo_configs(work)
        if foreign:
            raise BuildError("Configuration de cargo hors de CARGO_HOME, qu'il lirait : "
                             f"{', '.join(foreign)}. Vérifie-la et retire-la avant de publier.")
        for folder in cargo_exposed(work):
            print(f"  ATTENTION : d'autres comptes de cette machine peuvent créer {folder}, que "
                  "cargo lirait. Crée-le une fois, vide et privé (toi, Système et "
                  "Administrateurs seulement).", flush=True)
        source = _export(repo, commit, work)
        command = [cargo(), "build", "--profile", PROFILE, "--target", triple, "--locked"]
        try:
            done = subprocess.run(command, cwd=source, env=private_temp(env, work / "temp"),
                                  timeout=BUILD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError(f"cargo ne s'est pas terminé ({exc}).") from exc
        appeared = cargo_configs(work)
        if appeared:                              # posée pendant la construction : lue
            raise BuildError("Configuration de cargo apparue pendant la construction : "
                             f"{', '.join(appeared)}. Le programme construit est écarté ; "
                             "vérifie qui l'a posée avant de recommencer.")
        if done.returncode != 0:
            raise BuildError(f"cargo a échoué (code {done.returncode}) : voir ses messages "
                             "ci-dessus.")
        built = shared / triple / PROFILE / platforms.executable(PROGRAM, target)
        if not built.is_file():
            raise BuildError(f"Programme introuvable après la construction : {built}")
        shutil.copyfile(built, result)          # vérifié, puis pesé, sur cette copie seule
        try:
            check(result, target, commit, crate_version(source))
        except BuildError:
            result.unlink(missing_ok=True)
            raise
    print(f"  {target} : {result.stat().st_size / 1e6:.1f} Mo, construit et vérifié en "
          f"{time.monotonic() - started:.0f} s", flush=True)
    return result


def _format_ok(data: bytes, target: str) -> bool:
    system, machine = target.split("-", 1)
    try:
        if system == "windows":
            start = struct.unpack_from("<I", data, 0x3C)[0]
            return (data[:2] == b"MZ" and data[start:start + 4] == b"PE\0\0"
                    and struct.unpack_from("<H", data, start + 4)[0] == _PE_MACHINES[machine])
        if system in ("android", "linux"):
            ok = (data[:4] == b"\x7fELF" and data[4] == 2 and data[5] == 1
                  and struct.unpack_from("<H", data, 18)[0] == _ELF_MACHINES[machine])
            android = b"/system/bin/linker64" in data
            return ok and android == (system == "android")
        if system == "macos":
            return (data[:4] == b"\xcf\xfa\xed\xfe"
                    and struct.unpack_from("<I", data, 4)[0] == _MACHO_CPUS[machine])
    except (struct.error, KeyError, IndexError):
        return False
    return False


def check(path: Path, target: str, commit: str, version: str | None = None):
    """Le binaire correspond-il ? Format de la plateforme, commit publié dans ses
    octets, et « --version » s'il peut tourner ici. BuildError sinon."""
    data = path.read_bytes()
    if not _format_ok(data, target):
        raise BuildError(f"{path.name} n'est pas un programme pour {target}.")
    if commit.encode("ascii") not in data:
        raise BuildError(f"{path.name} ne porte pas le commit publié ({commit[:10]}) : il "
                         "n'a pas été construit à partir de ce commit.")
    if version and target == platforms.tag():
        try:
            done = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                                  timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError(f"{path.name} ne démarre pas sur cette machine ({exc}).") from exc
        if done.stdout.strip() != f"{PROGRAM} {version}":
            raise BuildError(f"{path.name} --version répond « {done.stdout.strip()} », "
                             f"attendu « {PROGRAM} {version} ».")
