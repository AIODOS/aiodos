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

L'appli embarque le dossier aiodos/ tel quel et le compagnon compilé ici, signés avec sa clé,
et l'outil fait tourner Gradle et cargo sous le compte de l'auteur. Comme tools/publish.py et
tools/build_installer.py, il refuse donc, avant d'importer quoi que ce soit du dépôt
(_amorce), un dépôt que d'autres comptes de la machine peuvent modifier ou qui porte des
fichiers non suivis là où il cherche son code (private.require_private_repository : un
module planté s'exécuterait, et partirait sur le téléphone). Avant de rien construire, il
refuse aussi des dossiers de travail ouverts à d'autres comptes, dont ce qu'il réutilise
d'une fois sur l'autre, compagnon compilé compris (work_paths), et des outils de
construction dans un dossier ouvert à d'autres : SDK Android, JDK, Python de Chaquopy
(require_private_tools ; cargo et le NDK : companion_build.require_private_tools).
"""
# os et sys sont gelés dans Python : jamais pris dans le dépôt, même avant le contrôle
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _amorce(root: str):
    """Lancé comme outil : le dépôt est contrôlé (private.require_private_repository) avant
    que rien n'en soit importé. Ni un module ni un bytecode plantés ne passent : le dossier
    du dépôt et celui des outils sont retirés de sys.path le temps du contrôle, private.py
    est chargé par son chemin, et aucun __pycache__ n'est lu ni écrit (pycache_prefix vers
    un dossier qui n'existe pas). Même amorce que tools/publish.py et tools/build_installer.py."""
    sys.dont_write_bytecode = True
    sys.pycache_prefix = os.path.join(os.path.expanduser("~"), ".aiodos",
                                      f"pycache-{os.urandom(16).hex()}")
    mine = {os.path.normcase(os.path.realpath(p)) for p in (root, os.path.join(root, "tools"))}
    saved = list(sys.path)
    sys.path[:] = [p for p in sys.path if p and os.path.normcase(os.path.realpath(p)) not in mine]
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "aiodos_amorce_private", os.path.join(root, "aiodos", "updates", "private.py"))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        try:
            guard.require_private_repository(root, extra=[os.path.join(root, "build")])
        except guard.NotPrivate as exc:
            for stream in (sys.stdout, sys.stderr):
                stream.reconfigure(encoding="utf-8", errors="replace")
            sys.exit(f"Application non construite : {exc}")
    finally:
        sys.path[:] = saved
    # Le dossier courant n'est pas forcément le dépôt contrôlé ici : Windows y cherche un
    # programme (git.exe…) avant le PATH, et « python -m » y prend ses modules (pip.py).
    # Jamais un programme du dossier courant, et les « python -m » des outils partent du
    # dépôt (cwd=ROOT ; relecture finale du 21/10/2026)
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"


if __name__ == "__main__":
    _amorce(os.path.dirname(_HERE))

import argparse  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import android_deps  # noqa: E402
import android_wheels  # noqa: E402
import companion_build  # noqa: E402
from aiodos.node import android_app  # noqa: E402
from aiodos.updates import private  # noqa: E402

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


# Les versions d'essai d'avant la 1.0.0 portaient des versionCode jusqu'à 20002 : Android
# refuse un code plus petit que celui de l'appli en place, sauf à la désinstaller et perdre
# les données du nœud. Le décalage garde chaque code au-dessus.
CODE_OFFSET = 100000


def version() -> tuple:
    """(versionName, versionCode) : « 1.0.0 » → 110000 (CODE_OFFSET)."""
    text = (ROOT / "aiodos" / "__init__.py").read_text(encoding="utf-8")
    name = re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)
    major, minor, patch = (int(n) for n in re.findall(r"\d+", name)[:3])
    return name, CODE_OFFSET + major * 10000 + minor * 100 + patch


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
    """Le compagnon Internet pour Android, compilé une fois par état de compagnon/. git est
    lancé sans ce que .git/config pourrait lui faire exécuter (companion_build.git_command)."""
    commit = run(*companion_build.git_command(ROOT, "log", "-1", "--format=%H", "--",
                                              "compagnon")).strip()
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


def uncommitted() -> list:
    """Fichiers d'aiodos/ ou de compagnon/ modifiés depuis le dernier commit, ou non suivis
    (git status, sans les fichiers ignorés). L'appli embarque aiodos/ tel qu'il est dans le
    dossier, mais compile le compagnon depuis le dernier commit de compagnon/ (companion,
    gardé d'une fois sur l'autre) : construite avant le commit, elle associerait le nouvel
    AIODOS au compagnon d'avant, sans ses bornes ni ce qu'il sait faire, et rien ne le
    dirait (relecture finale du 21/10/2026)."""
    out = run(*companion_build.git_command(ROOT, "status", "--porcelain",
                                           "--untracked-files=all", "--", "aiodos",
                                           "compagnon"))
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


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


def work_paths() -> list:
    """Les dossiers de travail, et tout ce que la construction garde d'une fois sur l'autre
    pour le livrer tel quel : build/android-cache (le compagnon compilé pour chaque état de
    compagnon/, la roue de pydantic-core, ses sources, son libpython, ses dépendances
    compilées), et les sorties de Gradle qu'il reprend sans les refaire (android/build,
    android/.gradle). Un fichier qu'un autre compte de la machine y aurait posé ou remplacé
    partirait sur le téléphone dans l'appli signée : chacun est contrôlé avec le dépôt
    (main), pas seulement le dossier qui le contient (un fichier créé par un autre compte
    lui appartient, quels que soient les droits du dossier aujourd'hui)."""
    reused = (CACHE, PROJECT / "build", PROJECT / ".gradle")
    found = [ROOT / "build", STAGE, *reused]
    for top in reused:
        for folder, dirs, files in os.walk(top):
            here = Path(folder)
            found += [here / name for name in (*dirs, *files)]
    return found


def require_private_tools(sdk_root: Path, jdk_home: Path, python: Path):
    """BuildError si d'autres comptes de cette machine peuvent modifier ce qui construit
    l'appli : le SDK Android (ses outils, d8, aapt2, apksigner, et la plateforme, passent
    dans l'appli ; le NDK du compagnon y est rangé), le JDK de Gradle et de la signature, ou
    le Python 3.13 avec lequel Chaquopy installe les paquets de l'appli (son dossier).
    Comme cargo et le NDK pour le compagnon (companion_build.require_private_tools) : un
    outil remplacé piégerait l'appli signée. Leurs droits ne sont jamais changés ici."""
    for what, folder in (("le SDK Android", sdk_root), ("le JDK", jdk_home),
                         ("le Python 3.13 de Chaquopy", python)):
        try:
            private.require_private(folder, what)
        except private.NotPrivate as exc:
            raise BuildError(f"{exc}. Installe-le dans ton dossier personnel, ou rends ce "
                             "dossier privé (toi, Système et Administrateurs seulement).") \
                from exc


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
    env = dict(os.environ, ANDROID_HOME=str(root))
    if not env.get("JAVA_HOME"):
        env["JAVA_HOME"] = str(Path(jdk_tool("javac")).parent.parent)
    python = python313()
    require_private_tools(root, Path(env["JAVA_HOME"]), Path(python).parent)   # avant tout
    stage = stage_all()
    name, code = version()
    gradle = PROJECT / f"gradlew{BAT}"
    ndk = companion_build.ndk()
    run(gradle, "--console=plain", "assembleRelease", f"-PaiodosStage={stage}",
        f"-PaiodosVersionName={name}", f"-PaiodosVersionCode={code}",
        f"-PaiodosBuildPython={python}", f"-PaiodosKeystore={keystore(new_key)}",
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
    # Le dépôt : déjà contrôlé par l'amorce quand l'outil est lancé, de nouveau ici (main
    # appelé d'ailleurs), avec les dossiers de travail et ce qu'ils gardent d'une fois sur
    # l'autre ; avant le compagnon et tout le reste
    try:
        companion_build.require_private_repository(ROOT, extra=work_paths())
        changed = uncommitted()
    except (companion_build.BuildError, BuildError) as exc:
        sys.exit(f"Application non construite : {exc}")
    if changed:
        shown = ", ".join(changed[:6]) + ("…" if len(changed) > 6 else "")
        sys.exit("Application non construite : des fichiers d'aiodos/ ou de compagnon/ ne sont "
                 f"pas commités ({shown}). L'appli embarquerait aiodos/ tel quel, mais le "
                 "compagnon de leur dernier commit : commite-les d'abord.")
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
