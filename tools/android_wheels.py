"""pydantic-core pour l'appli Android, compilé ici : Chaquopy ne le fournit pas.

    python tools/android_wheels.py [--force]

FastAPI dépend de pydantic, et pydantic de pydantic-core, écrit en Rust : ni PyPI ni le
dépôt de Chaquopy n'en ont de version pour Android. On le compile avec cargo et le NDK,
comme le compagnon (tools/companion_build.py), contre le libpython 3.13 de Chaquopy
(tools/android_deps.py), et on en fait une roue « cp313-cp313-android_24_arm64_v8a » que
Chaquopy installe comme les siennes (tools/build_android.py). Elle est gardée dans
build/android-cache/wheels/ et refaite seulement si elle manque (ou avec --force).

Vérifiée avant d'être rangée : bibliothèque ELF pour aarch64 et Android, liée au libpython
3.13 (sans quoi Android refuse de la charger), pages de 16 Ko.
"""
import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import android_deps  # noqa: E402
import companion_build  # noqa: E402

CACHE = ROOT / "build" / "android-cache"
WHEELS = CACHE / "wheels"
TRIPLE = "aarch64-linux-android"
PY = android_deps.PYTHON.replace(".", "")                 # « 313 »
TAG = f"cp{PY}-cp{PY}-android_{companion_build.ANDROID_API}_arm64_v8a"
SUFFIX = f".cpython-{PY}-aarch64-linux-android.so"     # celui des modules de Chaquopy
BUILD_TIMEOUT = 3600


class WheelError(Exception):
    pass


def wheel_path() -> Path:
    version = android_deps.DEPS["pydantic-core"]["version"]
    return WHEELS / f"pydantic_core-{version}-{TAG}.whl"


def _unpack_source() -> Path:
    """Les sources de pydantic-core, dépliées une fois (archive épinglée)."""
    dep = android_deps.DEPS["pydantic-core"]
    folder = CACHE / "src" / f"pydantic_core-{dep['version']}"
    if (folder / "Cargo.toml").is_file():
        return folder
    archive = android_deps.fetch("pydantic-core")
    shutil.rmtree(folder, ignore_errors=True)
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(folder.parent, filter="data")
    if not (folder / "Cargo.toml").is_file():
        raise WheelError(f"sources de pydantic-core inattendues dans {archive.name}")
    return folder


def _libpython() -> Path:
    """Dossier du libpython de Chaquopy pour arm64, de quoi lier pydantic-core."""
    version = android_deps.DEPS["python-target"]["version"]
    folder = CACHE / f"python-{version}-arm64-v8a"
    library = folder / f"libpython{android_deps.PYTHON}.so"
    if library.is_file():
        return folder
    archive = android_deps.fetch("python-target")
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        data = z.read(f"jniLibs/arm64-v8a/{library.name}")
    library.write_bytes(data)
    return folder


def _pyo3_config(folder: Path) -> Path:
    """Le Python visé, décrit à PyO3 (PYO3_CONFIG_FILE) : sans lui, PyO3 chercherait un
    _sysconfigdata que le paquet de Chaquopy n'a pas. Bibliothèque partagée, liée par son
    nom : un module chargé par Android doit déclarer libpython."""
    config = folder / "pyo3-android.txt"
    config.write_text("\n".join([
        "implementation=CPython", f"version={android_deps.PYTHON}", "shared=true",
        "abi3=false", f"lib_name=python{android_deps.PYTHON}", f"lib_dir={folder}",
        "pointer_width=64", "build_flags=", "suppress_build_script_link_lines=false"]) + "\n",
        encoding="utf-8")
    return config


def _readelf() -> Path:
    found = companion_build.ndk()
    tools = companion_build.android_tools(found)[1].parent if found else None
    exe = tools / f"llvm-readelf{'.exe' if os.name == 'nt' else ''}" if tools else None
    if exe is None or not exe.is_file():
        raise WheelError("NDK Android introuvable (llvm-readelf)")
    return exe


def check_library(path: Path):
    """ELF aarch64 pour Android, lié au libpython 3.13, segments alignés sur 16 Ko."""
    data = path.read_bytes()
    if not (data[:4] == b"\x7fELF" and data[4] == 2 and int.from_bytes(data[18:20], "little") == 183):
        raise WheelError(f"{path.name} n'est pas une bibliothèque aarch64")
    readelf = str(_readelf())
    dynamic = subprocess.run([readelf, "-d", str(path)], capture_output=True, text=True).stdout
    needed = re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", dynamic)
    if f"libpython{android_deps.PYTHON}.so" not in needed:
        raise WheelError(f"{path.name} n'est pas lié à libpython{android_deps.PYTHON}.so "
                         f"({', '.join(needed)}) : Android refuserait de le charger")
    segments = subprocess.run([readelf, "-l", "-W", str(path)], capture_output=True,
                              text=True).stdout
    aligns = [int(line.split()[-1], 16) for line in segments.splitlines()
              if line.strip().startswith("LOAD")]
    if not aligns or min(aligns) < 0x4000:
        raise WheelError(f"{path.name} : segments non alignés sur 16 Ko ({aligns})")


def compile_library(source: Path) -> Path:
    """cargo pour aarch64-linux-android (NDK, API de companion_build), contre libpython."""
    found = companion_build.ndk()
    if found is None:
        raise WheelError("NDK Android introuvable (ANDROID_HOME avec un dossier ndk/<version>)")
    linker, archiver = companion_build.android_tools(found)
    target_dir = CACHE / "cargo-pydantic-core"
    env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir),
               CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER=str(linker),
               CC_aarch64_linux_android=str(linker), AR_aarch64_linux_android=str(archiver),
               PYO3_CONFIG_FILE=str(_pyo3_config(_libpython())),
               PYO3_BUILD_EXTENSION_MODULE="1")
    for name in ("PYO3_CROSS", "PYO3_CROSS_LIB_DIR", "PYO3_CROSS_PYTHON_VERSION", "PYO3_PYTHON"):
        env.pop(name, None)
    command = [companion_build.cargo(), "build", "--release", "--lib", "--locked",
               "--target", TRIPLE]
    try:
        done = subprocess.run(command, cwd=source, env=env, timeout=BUILD_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WheelError(f"cargo ne s'est pas terminé ({exc})") from exc
    if done.returncode != 0:
        raise WheelError(f"cargo a échoué (code {done.returncode}) : voir ses messages")
    built = target_dir / TRIPLE / "release" / "lib_pydantic_core.so"
    if not built.is_file():
        raise WheelError(f"bibliothèque introuvable après la construction : {built}")
    check_library(built)
    return built


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def make_wheel(source: Path, library: Path, out: Path) -> Path:
    """La roue : le paquet Python des sources, la bibliothèque compilée, et dist-info
    (METADATA tiré de PKG-INFO, licence, WHEEL, RECORD)."""
    version = android_deps.DEPS["pydantic-core"]["version"]
    info = f"pydantic_core-{version}.dist-info"
    files = {}
    package = source / "python" / "pydantic_core"
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".so":
            files[f"pydantic_core/{path.relative_to(package).as_posix()}"] = path.read_bytes()
    files[f"pydantic_core/_pydantic_core{SUFFIX}"] = library.read_bytes()
    files[f"{info}/METADATA"] = (source / "PKG-INFO").read_bytes()
    files[f"{info}/licenses/LICENSE"] = (source / "LICENSE").read_bytes()
    files[f"{info}/WHEEL"] = ("Wheel-Version: 1.0\nGenerator: AIODOS (tools/android_wheels.py)\n"
                              f"Root-Is-Purelib: false\nTag: {TAG}\n").encode()
    record = [_record_line(name, data) for name, data in files.items()]
    record.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    part.replace(out)
    return out


def build(force: bool = False) -> Path:
    """La roue de pydantic-core pour Android, construite si elle manque."""
    out = wheel_path()
    if out.is_file() and not force:
        return out
    started = time.monotonic()
    source = _unpack_source()
    library = compile_library(source)
    make_wheel(source, library, out)
    print(f"  {out.name} : {out.stat().st_size / 1e6:.1f} Mo, construite en "
          f"{time.monotonic() - started:.0f} s", flush=True)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="reconstruire même si elle existe")
    args = parser.parse_args(argv)
    try:
        print(build(args.force))
    except (WheelError, android_deps.DepError) as exc:
        sys.exit(f"Roue non construite : {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
