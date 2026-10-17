"""Construire l'application Android d'AIODOS (APK) avec les outils du SDK, sans Gradle.

    python tools/build_android.py [--out dist] [--install]

android/ : le manifeste, les ressources et le code Java (une activité, sans bibliothèque).
Étapes : icônes tirées du logo (assets/, tools/make_logo.py ; Pillow) ; aapt2 (ressources et manifeste,
numéro de version d'AIODOS) ; javac contre android.jar ; d8 (dex) ; zipalign ; apksigner,
avec une clé de développement gardée hors du dépôt (~/.aiodos/android/, créée au besoin).
--install : « adb install -r » sur le téléphone branché (débogage USB autorisé).

L'application démarre le nœud AIODOS du téléphone, qui tourne dans Termux, et affiche son
interface (android/src/fr/arkane/aiodos/MainActivity.java, docs/android.md).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "android"
PACKAGE = "fr.arkane.aiodos"
PLATFORM = "android-34"
ICONS = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
ADAPTIVE = 108 / 48                  # premier plan d'une icône adaptative : 108 dp pour 48
KEYS = Path.home() / ".aiodos" / "android"
EXE = ".exe" if os.name == "nt" else ""
BAT = ".bat" if os.name == "nt" else ""


class BuildError(Exception):
    pass


def sdk() -> Path:
    for candidate in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                      r"C:\dev\android-sdk",
                      os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk"),
                      str(Path.home() / "Android" / "Sdk")):
        if candidate and (Path(candidate) / "platforms" / PLATFORM / "android.jar").is_file():
            return Path(candidate)
    raise BuildError(f"SDK Android introuvable (avec {PLATFORM}) : ANDROID_HOME")


def build_tools(root: Path) -> Path:
    found = sorted((d for d in (root / "build-tools").iterdir() if (d / f"aapt2{EXE}").is_file()),
                   key=lambda d: [int(x) for x in re.findall(r"\d+", d.name)])
    if not found:
        raise BuildError("outils de construction Android (build-tools) introuvables")
    return found[-1]


def jdk_tool(name: str) -> str:
    home = os.environ.get("JAVA_HOME")
    if home and (Path(home) / "bin" / f"{name}{EXE}").is_file():
        return str(Path(home) / "bin" / f"{name}{EXE}")
    found = shutil.which(name)
    if not found:
        raise BuildError(f"{name} introuvable : installer un JDK 17 ou plus récent")
    return found


def version() -> tuple:
    """(versionName, versionCode) : « 2.0.2-dev » → 20002."""
    text = (ROOT / "aiodos" / "__init__.py").read_text(encoding="utf-8")
    name = re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)
    major, minor, patch = (int(n) for n in re.findall(r"\d+", name)[:3])
    return name, major * 10000 + minor * 100 + patch


def run(*command, cwd=None):
    done = subprocess.run([str(c) for c in command], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise BuildError(f"{Path(str(command[0])).name} : {(done.stderr or done.stdout).strip()[:2000]}")
    return done.stdout


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


def keystore(tools: Path) -> Path:
    """Clé de développement (hors du dépôt) : une même clé d'une version à l'autre, sans quoi
    Android refuse la mise à jour de l'application."""
    KEYS.mkdir(parents=True, exist_ok=True)
    store = KEYS / "aiodos-debug.keystore"
    if not store.is_file():
        run(jdk_tool("keytool"), "-genkeypair", "-keystore", store, "-storepass", "android",
            "-keypass", "android", "-alias", "aiodos", "-keyalg", "RSA", "-keysize", "2048",
            "-validity", "10000", "-dname", "CN=AIODOS, O=22eme Arkane, C=FR")
    return store


def build(out: Path) -> Path:
    root = sdk()
    tools = build_tools(root)
    android_jar = root / "platforms" / PLATFORM / "android.jar"
    name, code = version()
    work = ROOT / "build" / "android"
    shutil.rmtree(work, ignore_errors=True)
    res = work / "res"
    shutil.copytree(SOURCE / "res", res)
    icons(res)
    compiled = work / "compiled.zip"
    run(tools / f"aapt2{EXE}", "compile", "--dir", res, "-o", compiled)
    gen, base = work / "gen", work / "base.apk"
    gen.mkdir()
    run(tools / f"aapt2{EXE}", "link", "-I", android_jar, "--manifest",
        SOURCE / "AndroidManifest.xml", "--java", gen, "--version-name", name,
        "--version-code", str(code), "--min-sdk-version", "24", "--target-sdk-version", "34",
        "-o", base, compiled)
    classes = work / "classes"
    classes.mkdir()
    sources = [str(p) for p in (SOURCE / "src").rglob("*.java")] + \
              [str(p) for p in gen.rglob("*.java")]
    run(jdk_tool("javac"), "--release", "11", "-encoding", "UTF-8", "-classpath", android_jar,
        "-d", classes, *sources)
    dex = work / "dex"
    dex.mkdir()
    run(tools / f"d8{BAT}", "--release", "--min-api", "24", "--lib", android_jar,
        "--output", dex, *[str(p) for p in classes.rglob("*.class")])
    unsigned = work / "unsigned.apk"
    shutil.copyfile(base, unsigned)
    with zipfile.ZipFile(unsigned, "a") as z:
        z.write(dex / "classes.dex", "classes.dex")
    aligned = work / "aligned.apk"
    run(tools / f"zipalign{EXE}", "-f", "-p", "4", unsigned, aligned)
    out.mkdir(parents=True, exist_ok=True)
    apk = out / f"AIODOS-{name}.apk"
    run(tools / f"apksigner{BAT}", "sign", "--ks", keystore(tools), "--ks-pass", "pass:android",
        "--key-pass", "pass:android", "--out", apk, aligned)
    run(tools / f"apksigner{BAT}", "verify", apk)
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
    parser.add_argument("--install", action="store_true",
                        help="installer sur le téléphone branché (adb)")
    args = parser.parse_args(argv)
    try:
        apk = build(Path(args.out))
        print(f"Application : {apk} ({apk.stat().st_size / 1e3:.0f} Ko)")
        if args.install:
            install(apk)
    except BuildError as exc:
        sys.exit(f"Application non construite : {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
