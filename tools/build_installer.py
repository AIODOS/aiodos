"""Construire l'installeur Windows d'AIODOS (Inno Setup) : AIODOS avec son propre Python.

    python tools/build_installer.py [--out dist] [--companion FICHIER | --no-companion]
                                    [--no-engines] [--stage-only]

L'installeur pose, pour l'utilisateur seul et sans droits administrateur, dans
%LOCALAPPDATA%\\Programs\\AIODOS :
- runtime\\ : le Python 3.14 « embarquable » de python.org (version épinglée, empreinte
  vérifiée) et les paquets d'AIODOS, aux versions de la machine qui construit ;
- app\\ : le code du commit courant (git archive : les mêmes fichiers qu'une version
  publiée), et installation.json, qui envoie les données dans %LOCALAPPDATA%\\AIODOS
  (paths.installed_data_dir) ;
- app\\compagnon\\target\\release\\aiodos-net.exe : le compagnon Internet, construit et
  vérifié à partir du même commit (companion_build.py) ;
- app\\engines\\ : llama.cpp et stable-diffusion.cpp, les archives épinglées par
  engine_release.py, dépliées (updates/engines.bundled).

Le Python embarqué garde les mises à jour signées telles quelles : une version reçue d'une
machine appairée se déplie dans les données et se lance avec ce même Python, depuis son
dossier (updates/launcher.py ; d'où le retrait du fichier python314._pth, qui empêcherait
de trouver son code). Les paquets Python de l'utilisateur (site « user ») sont écartés par
un sitecustomize propre à ce Python.

Sans Whisper (transcription audio et vidéo : 640 Mo de plus), à venir en option. Ollama
reste nécessaire pour converser : l'installeur propose de le télécharger depuis ollama.com
s'il manque. Les essais de cet outil : tests/test_build_installer.py.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import companion_build  # noqa: E402
import engine_release  # noqa: E402
from aiodos.updates import engines as engine_kinds  # noqa: E402
from aiodos.updates import install  # noqa: E402

# Python 3.14.4, distribution « embarquable » 64 bits de python.org (API des versions)
PYTHON = {
    "version": "3.14.4",
    "name": "python-3.14.4-embed-amd64.zip",
    "url": "https://www.python.org/ftp/python/3.14.4/python-3.14.4-embed-amd64.zip",
    "size": 12034740,
    "sha256": "cda80a9b1e75c0f1b4f9872ca1b417f0d19bce32facc811aea9180e70fad5fb9",
}
# Paquets d'AIODOS (requirements.txt sans les essais ni Whisper, plus cryptography, que
# requirements.txt n'a jamais nommé : il arrivait par d'autres paquets)
PACKAGES = ["fastapi", "uvicorn", "pywebview", "requests", "numpy", "cryptography",
            "markdown-it-py", "pygments", "pypdf", "python-docx", "python-pptx", "openpyxl",
            "striprtf"]
# Le code d'une version publiée (tools/publish.py : RUNTIME)
SOURCE = ["aiodos", "assets", "lancer.bat", "lancer-noeud.sh", "requirements.txt",
          "requirements-node.txt", "LICENSE", "NOTICE", "README.md"]
CACHE = Path.home() / ".aiodos" / "installeur"
WINDOWS = "windows-x86_64"
SITECUSTOMIZE = '''"""Python d'AIODOS : les paquets installés par l'utilisateur pour son propre Python
(site « user ») ne passent pas devant ceux d'AIODOS."""
import site
import sys

_user = getattr(site, "USER_SITE", None)
if _user:
    sys.path[:] = [p for p in sys.path if not p.startswith(_user)]
site.ENABLE_USER_SITE = False
'''


class BuildError(Exception):
    pass


def version() -> str:
    text = (ROOT / "aiodos" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not found:
        raise BuildError("numéro de version introuvable dans aiodos/__init__.py")
    return found.group(1)


def git(*args) -> str:
    done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          encoding="utf-8")
    if done.returncode != 0:
        raise BuildError(f"git {' '.join(args)} : {done.stderr.strip()}")
    return done.stdout.strip()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_python(cache: Path = CACHE) -> Path:
    """L'archive du Python embarquable, taille et empreinte vérifiées (téléchargée une fois)."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / PYTHON["name"]
    if path.is_file() and path.stat().st_size == PYTHON["size"] and \
            sha256_of(path) == PYTHON["sha256"]:
        return path
    part = path.with_name(path.name + ".part")
    try:
        with urllib.request.urlopen(PYTHON["url"], timeout=60) as reply, open(part, "wb") as out:
            shutil.copyfileobj(reply, out, 1 << 20)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise BuildError(f"Python embarquable non téléchargé ({exc})") from exc
    if part.stat().st_size != PYTHON["size"] or sha256_of(part) != PYTHON["sha256"]:
        part.unlink(missing_ok=True)
        raise BuildError("Python embarquable différent de celui épinglé (taille ou empreinte)")
    os.replace(part, path)
    return path


def pinned(packages=PACKAGES) -> list:
    """Les paquets aux versions installées sur la machine qui construit (celles des essais) :
    « fastapi==0.143.2 »… ; une contrainte pour chaque paquet installé fige aussi leurs
    dépendances (constraints())."""
    from importlib import metadata
    found = []
    for name in packages:
        try:
            found.append(f"{name}=={metadata.version(name)}")
        except metadata.PackageNotFoundError as exc:
            raise BuildError(f"{name} n'est pas installé sur cette machine : "
                             "pip install -r requirements.txt") from exc
    return found


def constraints() -> str:
    from importlib import metadata
    lines = sorted({f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()
                    if d.metadata["Name"]})
    return "\n".join(lines) + "\n"


def make_runtime(stage: Path, archive: Path):
    """runtime\\ : Python embarquable, sans python314._pth (le dossier de chaque version
    doit compter, cf. updates/launcher.py), ses paquets, et le sitecustomize."""
    runtime = stage / "runtime"
    with zipfile.ZipFile(archive) as z:
        z.extractall(runtime)
    for pth in runtime.glob("python*._pth"):
        pth.unlink()
    packages = runtime / "Lib" / "site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    work = stage.parent / "pip"
    work.mkdir(parents=True, exist_ok=True)
    (work / "constraints.txt").write_text(constraints(), encoding="utf-8")
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
               "--no-warn-script-location", "--prefer-binary", "--target", str(packages),
               "-c", str(work / "constraints.txt"), *pinned()]
    done = subprocess.run(command)
    if done.returncode != 0:
        raise BuildError("pip n'a pas installé les paquets (voir ses messages ci-dessus)")
    shutil.rmtree(packages / "bin", ignore_errors=True)          # scripts inutiles ici
    (packages / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    (runtime / "aiodos-runtime.json").write_text(
        json.dumps({"python": PYTHON["version"], "packages": pinned()}, indent=1),
        encoding="utf-8")


def make_app(stage: Path, commit: str, number: str):
    """app\\ : le code du commit, et installation.json (données dans %LOCALAPPDATA%)."""
    app = stage / "app"
    archive = stage.parent / f"aiodos-{number}.zip"
    git("archive", "--format=zip", f"--output={archive}", commit, "--", *SOURCE)
    with zipfile.ZipFile(archive) as z:
        z.extractall(app)
    (app / "installation.json").write_text(json.dumps(
        {"version": number, "commit": commit, "built": int(time.time()),
         "data": "%LOCALAPPDATA%\\AIODOS\\data"}, indent=1), encoding="utf-8")


def precompile(stage: Path):
    """Le code d'AIODOS compilé d'avance, par le Python embarqué : le premier lancement n'a
    plus à le faire, ce qui se sentait sur un PC modeste. Bytecode vérifié par l'empreinte de
    sa source, pas par sa date : un fichier remplacé est recompilé quand même."""
    python = stage / "runtime" / "python.exe"
    done = subprocess.run([str(python), "-m", "compileall", "-q", "-j", "0",
                           "--invalidation-mode", "checked-hash", str(stage / "app" / "aiodos")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise BuildError(f"compilation du code : {(done.stderr or done.stdout).strip()[:1000]}")


def add_companion(stage: Path, commit: str, number: str, given: Path | None):
    target = stage / "app" / "compagnon" / "target" / "release" / "aiodos-net.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    if given is None:
        print("Construction du compagnon à partir du commit (quelques minutes)…", flush=True)
        try:
            given = companion_build.build(WINDOWS, commit, number, stage.parent / "compagnon")
        except companion_build.BuildError as exc:
            raise BuildError(f"compagnon : {exc}") from exc
    shutil.copyfile(given, target)


def add_engines(stage: Path):
    """app\\engines\\<moteur>\\ : chaque moteur épinglé, déplié (programmes vérifiés)."""
    for name in engine_release.ENGINES:
        archive = engine_release.fetch(WINDOWS, engine=name)
        work = stage.parent / f"moteur-{name}"
        shutil.rmtree(work, ignore_errors=True)
        install.unpack(archive, work)
        found = work if engine_kinds.complete(work, name) else next(
            (d for d in work.iterdir() if d.is_dir() and engine_kinds.complete(d, name)), None)
        if found is None:
            raise BuildError(f"{name} : programmes absents de l'archive {archive.name}")
        shutil.copytree(found, stage / "app" / "engines" / name)


def iscc() -> Path:
    for candidate in (os.environ.get("AIODOS_ISCC"), shutil.which("ISCC"),
                      r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                      r"C:\Program Files\Inno Setup 6\ISCC.exe",
                      os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise BuildError("Inno Setup 6 introuvable (ISCC.exe) : https://jrsoftware.org/isdl.php, "
                     "ou AIODOS_ISCC")


def shortcut_icon(stage: Path) -> str:
    """L'icône des raccourcis, sous un nom tiré de son contenu : Windows garde en cache
    l'icône d'un raccourci d'après le chemin du fichier, si bien qu'un nouveau logo sous le
    même nom laissait l'ancien dans la barre des tâches et le menu Démarrer."""
    icon = stage / "app" / "assets" / "logo.ico"
    named = icon.with_name(f"aiodos-{hashlib.sha256(icon.read_bytes()).hexdigest()[:10]}.ico")
    shutil.copyfile(icon, named)
    return named.name


def compile_installer(stage: Path, number: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    script = ROOT / "tools" / "installer" / "aiodos.iss"
    done = subprocess.run([str(iscc()), "/Qp", f"/DAppVersion={number}",
                           f"/DStage={stage}", f"/DOutDir={out}", f"/DRepo={ROOT}",
                           f"/DIcon={shortcut_icon(stage)}", str(script)])
    if done.returncode != 0:
        raise BuildError(f"Inno Setup a échoué (code {done.returncode})")
    return out / f"AIODOS-Setup-{number}.exe"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "dist"), help="dossier de l'installeur")
    parser.add_argument("--companion", help="compagnon déjà construit pour ce commit")
    parser.add_argument("--no-companion", action="store_true", help="sans compagnon Internet")
    parser.add_argument("--no-engines", action="store_true",
                        help="sans llama.cpp ni stable-diffusion.cpp (ils arriveront avec "
                             "les mises à jour)")
    parser.add_argument("--stage-only", action="store_true",
                        help="préparer les fichiers sans lancer Inno Setup")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.exit("L'installeur Windows se construit sous Windows.")
    number, commit = version(), git("rev-parse", "HEAD")
    if git("status", "--porcelain", "--untracked-files=no"):
        print("ATTENTION : des modifications ne sont pas commitées ; l'installeur prend le "
              f"code du commit {commit[:10]}.", flush=True)
    work = ROOT / "build" / "installateur"
    shutil.rmtree(work, ignore_errors=True)
    stage = work / "stage"
    stage.mkdir(parents=True)
    began = time.monotonic()
    try:
        print(f"AIODOS {number} (commit {commit[:10]}) : Python {PYTHON['version']}…", flush=True)
        make_runtime(stage, fetch_python())
        make_app(stage, commit, number)
        precompile(stage)
        if not args.no_companion:
            add_companion(stage, commit, number,
                          Path(args.companion) if args.companion else None)
        if not args.no_engines:
            add_engines(stage)
        if args.stage_only:
            print(f"Fichiers prêts dans {stage}")
            return 0
        setup = compile_installer(stage, number, Path(args.out))
    except (BuildError, engine_release.EngineError, OSError) as exc:
        sys.exit(f"Installeur non construit : {exc}")
    print(f"Installeur : {setup} ({setup.stat().st_size / 1e6:.0f} Mo, empreinte "
          f"{sha256_of(setup)}), en {time.monotonic() - began:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
