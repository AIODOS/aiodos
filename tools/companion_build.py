"""Construire le compagnon aiodos-net d'une version à publier (tools/publish.py).

Pour chaque plateforme, le dossier compagnon/ du commit publié est exporté dans un
dossier temporaire (git archive : ni modification en cours, ni ancien binaire), puis
construit par cargo, profil « distribution » (cf. compagnon/Cargo.toml), avec le
commit donné au programme (AIODOS_NET_COMMIT). Le binaire est ensuite vérifié :
format de la plateforme, commit retrouvé dans ses octets, et, s'il peut tourner sur
cette machine, « aiodos-net --version » au numéro du Cargo.toml de ce commit.

Les dépendances compilées se gardent d'une fois sur l'autre dans compagnon/target :
une nouvelle publication ne recompile que le compagnon (quelques minutes tout de même,
à cause de l'optimisation de tout le programme).

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
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiodos.updates import platforms  # noqa: E402

PROGRAM = "aiodos-net"
PROFILE = "distribution"
ANDROID = "android-aarch64"
ANDROID_API = 24                   # Android 7 et plus, comme le nœud sous Termux
BUILD_TIMEOUT = 3600
_PE_MACHINES = {"x86_64": 0x8664, "aarch64": 0xAA64}
_ELF_MACHINES = {"x86_64": 62, "aarch64": 183}
_MACHO_CPUS = {"x86_64": 0x01000007, "aarch64": 0x0100000C}


class BuildError(Exception):
    """Compagnon impossible à construire, ou qui ne correspond pas (message lisible)."""


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
    done = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", commit, "compagnon/"],
                          capture_output=True, text=True, encoding="utf-8")
    return done.returncode == 0 and bool(done.stdout.strip())


def _export(repo: Path, commit: str, work: Path) -> Path:
    """Le dossier compagnon/ du commit, tel qu'il est dans le dépôt."""
    archive = work / "compagnon.zip"
    done = subprocess.run(["git", "-C", str(repo), "archive", "--format=zip",
                           f"--output={archive}", commit, "--", "compagnon"],
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
    dans `out` (nom : file_name). BuildError sinon. Les messages de cargo s'affichent."""
    triple = platforms.RUST_TARGETS[target]
    shared = repo / "compagnon" / "target"
    env = dict(os.environ, AIODOS_NET_COMMIT=commit, CARGO_TARGET_DIR=str(shared))
    if target == ANDROID:
        linker, archiver = android_tools(ndk())
        env.update({"CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": str(linker),
                    "CC_aarch64_linux_android": str(linker),
                    "AR_aarch64_linux_android": str(archiver)})
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aiodos-compagnon-") as work:
        source = _export(repo, commit, Path(work))
        command = [cargo(), "build", "--profile", PROFILE, "--target", triple, "--locked"]
        try:
            done = subprocess.run(command, cwd=source, env=env, timeout=BUILD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BuildError(f"cargo ne s'est pas terminé ({exc}).") from exc
        if done.returncode != 0:
            raise BuildError(f"cargo a échoué (code {done.returncode}) : voir ses messages "
                             "ci-dessus.")
        built = shared / triple / PROFILE / platforms.executable(PROGRAM, target)
        if not built.is_file():
            raise BuildError(f"Programme introuvable après la construction : {built}")
        check(built, target, commit, crate_version(source))
    out.mkdir(parents=True, exist_ok=True)
    result = out / file_name(number, target)
    shutil.copyfile(built, result)
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
