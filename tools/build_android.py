"""Construire l'appli Android d'AIODOS (APK) : le nœud tourne dans l'appli, sans Termux.

    python tools/build_android.py [--out dist] [--install]

L'appli (android/, docs/android.md) : une activité qui montre l'interface du nœud, un service
au premier plan qui fait tourner le nœud Python (Chaquopy, Python 3.13), le démarrage avec
le téléphone. Étapes :
1. le code d'AIODOS et le logo (build/android/python) ; les paquets Python
   (android/requirements.txt), dont pydantic-core, compilé ici (tools/android_wheels.py) ;
2. les programmes, rangés en bibliothèques (build/android/jniLibs) : llama.cpp, l'archive
   officielle pour Android de la version des PC (tools/android_deps.py), et le compagnon
   Internet compilé pour Android (tools/companion_build.py, gardé d'une fois sur l'autre
   tant que compagnon/ ne change pas) ;
3. les icônes tirées du logo (assets/, tools/make_logo.py ; Pillow) ;
4. Gradle (android/gradlew : greffon Android 8.9.1 et Chaquopy 17.1) : l'appli signée avec
   la clé de développement gardée hors du dépôt (~/.aiodos/android/, créée au besoin), la
   même d'une version à l'autre.
--install : « adb install -r » sur le téléphone branché (débogage USB autorisé).

Ce qu'il faut sur la machine : le SDK Android (ANDROID_HOME) avec son NDK, un JDK 17 ou plus
récent, Rust avec la cible aarch64-linux-android, et Python 3.13 (Chaquopy installe les
paquets avec la même version que l'appli : « uv python install 3.13 »).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import android_deps  # noqa: E402
import android_wheels  # noqa: E402
import companion_build  # noqa: E402
from aiodos.node import android_app  # noqa: E402

PROJECT = ROOT / "android"
STAGE = ROOT / "build" / "android"
CACHE = ROOT / "build" / "android-cache"
ICONS = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
ADAPTIVE = 108 / 48                  # premier plan d'une icône adaptative : 108 dp pour 48
KEYS = Path.home() / ".aiodos" / "android"
EXE = ".exe" if os.name == "nt" else ""
BAT = ".bat" if os.name == "nt" else ""
ABI = "arm64-v8a"
# Ce que llama-server et ggml-rpc-server chargent (le reste de l'archive : d'autres outils)
LLAMA_LIBRARIES = ("libllama-server-impl.so", "libllama-common.so", "libmtmd.so",
                   "libllama.so", "libggml.so", "libggml-base.so", "libggml-rpc.so")
LLAMA_CPU = re.compile(r"libggml-cpu-android_[\w.]+\.so")      # une par famille de processeur


class BuildError(Exception):
    pass


def sdk() -> Path:
    for candidate in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                      r"C:\dev\android-sdk",
                      os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk"),
                      str(Path.home() / "Android" / "Sdk")):
        if candidate and (Path(candidate) / "platforms").is_dir():
            return Path(candidate)
    raise BuildError("SDK Android introuvable : ANDROID_HOME")


def jdk_tool(name: str) -> str:
    home = os.environ.get("JAVA_HOME")
    if home and (Path(home) / "bin" / f"{name}{EXE}").is_file():
        return str(Path(home) / "bin" / f"{name}{EXE}")
    found = shutil.which(name)
    if not found:
        raise BuildError(f"{name} introuvable : installer un JDK 17 ou plus récent")
    return found


def python313() -> str:
    """Le Python 3.13 du PC, avec lequel Chaquopy installe les paquets de l'appli."""
    for command in (["uv", "python", "find", android_deps.PYTHON],
                    ["py", f"-{android_deps.PYTHON}", "-c", "import sys; print(sys.executable)"]):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        found = done.stdout.strip().splitlines()[-1:] if done.returncode == 0 else []
        if found and Path(found[0]).is_file():
            return found[0]
    raise BuildError(f"Python {android_deps.PYTHON} introuvable sur ce PC "
                     f"(« uv python install {android_deps.PYTHON} »)")


def version() -> tuple:
    """(versionName, versionCode) : « 2.0.2-dev » → 20002."""
    text = (ROOT / "aiodos" / "__init__.py").read_text(encoding="utf-8")
    name = re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)
    major, minor, patch = (int(n) for n in re.findall(r"\d+", name)[:3])
    return name, major * 10000 + minor * 100 + patch


def run(*command, cwd=None, quiet=True, env=None):
    done = subprocess.run([str(c) for c in command], cwd=cwd, env=env,
                          capture_output=quiet, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[-3000:] if quiet else "voir ci-dessus"
        raise BuildError(f"{Path(str(command[0])).name} : {detail}")
    return done.stdout


def python_code(stage: Path):
    """Le paquet aiodos (sans fichiers compilés) et le logo que sert l'interface."""
    target = stage / "python"
    shutil.copytree(ROOT / "aiodos", target / "aiodos",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / "assets").mkdir(parents=True)
    for name in ("logo.png", "logo_128.png"):
        shutil.copyfile(ROOT / "assets" / name, target / "assets" / name)


def requirements(stage: Path):
    """android/requirements.txt, avec le dossier de la roue de pydantic-core."""
    wheel = android_wheels.build()
    wheels = stage / "wheels"
    wheels.mkdir()
    shutil.copyfile(wheel, wheels / wheel.name)
    text = (PROJECT / "requirements.txt").read_text(encoding="utf-8")
    (stage / "requirements-android.txt").write_text(
        f"--find-links {wheels.as_uri()}\n{text}", encoding="utf-8")   # adresse : sans espace


def llama_programs(libs: Path) -> str:
    """Les programmes et bibliothèques de llama.cpp pour Android, sous des noms que le
    gestionnaire de paquets d'Android déplie (« lib….so »)."""
    archive = android_deps.fetch("llama.cpp")
    renamed = {name: library for programs in android_app.PROGRAMS.values()
               for name, library in programs.items()}
    found = set()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            name = Path(member.name).name
            wanted = renamed.get(name) or (name if name in LLAMA_LIBRARIES
                                           or LLAMA_CPU.fullmatch(name) else None)
            if not member.isfile() or wanted is None:
                continue
            with tar.extractfile(member) as source, open(libs / wanted, "wb") as out:
                shutil.copyfileobj(source, out)
            found.add(name)
    missing = (set(renamed) | set(LLAMA_LIBRARIES)) - found
    if missing or not any(LLAMA_CPU.fullmatch(n) for n in found):
        raise BuildError(f"archive de llama.cpp incomplète : {', '.join(sorted(missing))}")
    return android_deps.LLAMA_BUILD


def companion(libs: Path) -> str:
    """Le compagnon Internet pour Android, compilé une fois par état de compagnon/."""
    commit = run("git", "log", "-1", "--format=%H", "--", "compagnon", cwd=ROOT).strip()
    if not commit:
        raise BuildError("compagnon/ introuvable dans le dépôt")
    out = CACHE / "compagnon" / commit[:12]
    name, _ = version()
    built = out / companion_build.file_name(name, companion_build.ANDROID)
    if not built.is_file():
        why = companion_build.missing(companion_build.ANDROID)
        if why:
            raise BuildError(f"compagnon pour Android impossible à construire : {why}")
        shutil.rmtree(out, ignore_errors=True)
        try:
            built = companion_build.build(companion_build.ANDROID, commit, name, out)
        except companion_build.BuildError as exc:
            raise BuildError(str(exc)) from exc
    shutil.copyfile(built, libs / android_app.COMPANION)
    return commit[:10]


def icons(res: Path):
    """Icônes tirées du logo (tools/make_logo.py) : l'icône classique aux coins arrondis, et
    le premier plan de l'icône adaptative (res/mipmap-anydpi-v26), dont le lanceur découpe
    la forme : sans lui, Android 8 et plus réduit l'icône classique dans un cadre."""
    from PIL import Image
    logo = Image.open(ROOT / "assets" / "logo.png").convert("RGBA")
    adaptive = Image.open(ROOT / "assets" / "logo-adaptive.png").convert("RGBA")
    for density, size in ICONS.items():
        folder = res / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        logo.resize((size, size), Image.LANCZOS).save(folder / "ic_launcher.png")
        side = round(size * ADAPTIVE)
        adaptive.resize((side, side), Image.LANCZOS).save(folder / "ic_launcher_foreground.png")


def keystore(create: bool = False) -> Path:
    """Clé de signature (hors du dépôt), la même d'une version à l'autre : avec une autre
    clé, Android refuse la mise à jour, et il faudrait désinstaller l'appli, ce qui efface
    l'identité du nœud, ses appairages et ses modèles. Elle n'est donc jamais recréée en
    silence : sur un autre PC, recopier ~/.aiodos/android/aiodos-debug.keystore (et la
    sauvegarder) ; --nouvelle-cle seulement pour un tout premier téléphone. Son mot de passe
    (« android ») ne protège rien : c'est le fichier qu'il faut garder pour soi."""
    store = KEYS / "aiodos-debug.keystore"
    if store.is_file():
        return store
    if not create:
        raise BuildError(f"clé de signature introuvable ({store}) : la recopier depuis le PC "
                         "qui a construit l'appli installée, ou --nouvelle-cle pour en créer "
                         "une (l'appli déjà installée devra alors être désinstallée)")
    KEYS.mkdir(parents=True, exist_ok=True)
    run(jdk_tool("keytool"), "-genkeypair", "-keystore", store, "-storepass", "android",
        "-keypass", "android", "-alias", "aiodos", "-keyalg", "RSA", "-keysize", "2048",
        "-validity", "10000", "-dname", "CN=AIODOS, O=22eme Arkane, C=FR")
    print(f"  nouvelle clé de signature : {store} (à sauvegarder)", flush=True)
    return store


def stage_all() -> Path:
    shutil.rmtree(STAGE, ignore_errors=True)
    libs = STAGE / "jniLibs" / ABI
    libs.mkdir(parents=True)
    python_code(STAGE)
    requirements(STAGE)
    print(f"  llama.cpp {llama_programs(libs)} pour Android", flush=True)
    print(f"  compagnon Internet pour Android (compagnon/ au commit {companion(libs)})",
          flush=True)
    icons(STAGE / "res")
    return STAGE


def build(out: Path, new_key: bool = False) -> Path:
    root = sdk()
    stage = stage_all()
    name, code = version()
    gradle = PROJECT / f"gradlew{BAT}"
    env = dict(os.environ, ANDROID_HOME=str(root))
    if not env.get("JAVA_HOME"):
        env["JAVA_HOME"] = str(Path(jdk_tool("javac")).parent.parent)
    ndk = companion_build.ndk()
    run(gradle, "--console=plain", "assembleRelease", f"-PaiodosStage={stage}",
        f"-PaiodosVersionName={name}", f"-PaiodosVersionCode={code}",
        f"-PaiodosBuildPython={python313()}", f"-PaiodosKeystore={keystore(new_key)}",
        *([f"-PaiodosNdk={ndk.name}"] if ndk else []), cwd=PROJECT, quiet=False, env=env)
    built = PROJECT / "build" / "outputs" / "apk" / "release" / "aiodos-release.apk"
    if not built.is_file():
        raise BuildError(f"appli introuvable après Gradle : {built}")
    out.mkdir(parents=True, exist_ok=True)
    apk = out / f"AIODOS-{name}.apk"
    shutil.copyfile(built, apk)
    tools = sorted((root / "build-tools").iterdir(), key=lambda d: [int(x) for x in re.findall(r"\d+", d.name)])
    run(tools[-1] / f"apksigner{BAT}", "verify", apk)
    return apk


def install(apk: Path):
    adb = sdk() / "platform-tools" / f"adb{EXE}"
    devices = [line.split()[0] for line in run(adb, "devices").splitlines()[1:]
               if line.strip().endswith("device")]
    if not devices:
        raise BuildError("aucun téléphone branché avec le débogage USB autorisé (adb devices)")
    print(run(adb, "install", "-r", apk).strip())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--nouvelle-cle", action="store_true",
                        help="créer la clé de signature si elle manque (premier téléphone)")
    parser.add_argument("--install", action="store_true",
                        help="installer sur le téléphone branché (adb)")
    args = parser.parse_args(argv)
    try:
        apk = build(Path(args.out), new_key=args.nouvelle_cle)
        print(f"Application : {apk} ({apk.stat().st_size / 1e6:.1f} Mo)")
        if args.install:
            install(apk)
    except (BuildError, android_deps.DepError, android_wheels.WheelError, OSError) as exc:
        sys.exit(f"Application non construite : {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
