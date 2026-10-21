"""Construire l'installeur Windows d'AIODOS (Inno Setup) : AIODOS avec son propre Python.

    python tools/build_installer.py [--out dist] [--companion FICHIER | --no-companion]
                                    [--no-engines] [--stage-only]

L'installeur pose, pour l'utilisateur seul et sans droits administrateur, dans
%LOCALAPPDATA%\\Programs\\AIODOS :
- runtime\\ : le Python 3.14 « embarquable » de python.org (version épinglée, empreinte
  vérifiée) et les paquets d'AIODOS, aux versions de la machine qui construit, chacun figé
  par son empreinte (tools/installer/paquets.txt, ci-dessous) ;
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

Les paquets ne sont pas pris tels que PyPI les sert ce jour-là : à version égale, PyPI
accepte qu'on ajoute un fichier (une roue d'étiquette plus précise, ou de numéro de
construction plus grand), que pip préférerait. Chaque fichier est donc figé par son
empreinte dans tools/installer/paquets.txt (pip --require-hashes), pour tous les paquets et
leurs dépendances, et ce fichier doit correspondre exactement aux versions installées sur la
machine qui construit (celles des essais). « --lock » relève ces empreintes : pip télécharge
les roues, et chacune doit contenir exactement les fichiers installés ici, comparés un à un
par leur contenu (pas par le RECORD de la roue, qu'une roue piégée recopierait) ; pour une
version qui n'a pas changé, le fichier téléchargé doit en plus être celui déjà figé (une
empreinte nouvelle ne s'accepte que pour une version nouvelle). Seule l'archive source de
proxy-tools (pas de roue sur PyPI, construite ici sans rien télécharger d'autre) garde son
empreinte figée, à relever à la main si sa version change. Une empreinte nouvelle se relit
avant d'être commitée.

Comme tools/publish.py, l'outil refuse, avant d'importer quoi que ce soit du dépôt
(_amorce, --lock compris), un dépôt que d'autres comptes de la machine peuvent modifier ou
qui porte des fichiers non suivis là où il cherche son code (private.require_private_repository),
et lance git sans ce que .git/config ou des objets de remplacement pourraient lui faire
exécuter ou livrer. Il travaille dans un dossier neuf et privé (build\\installateur,
fresh_work), et pip et Inno Setup y mettent leurs fichiers temporaires : %TEMP% peut être
ouvert à d'autres comptes. Inno Setup doit être dans un dossier à l'abri des autres comptes.

Sans Whisper (transcription audio et vidéo : 640 Mo de plus), à venir en option. Ollama
reste nécessaire pour converser : l'installeur propose de le télécharger depuis ollama.com
s'il manque. Les essais de cet outil : tests/test_build_installer.py.
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
    un dossier qui n'existe pas). Même amorce que tools/publish.py."""
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
            sys.exit(f"Installeur non construit : {exc}")
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
import base64  # noqa: E402
import csv  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import companion_build  # noqa: E402
import engine_release  # noqa: E402
from aiodos.updates import engines as engine_kinds  # noqa: E402
from aiodos.updates import install, private  # noqa: E402

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
# Paquets et dépendances, figés par empreinte (--lock les relève)
LOCK = ROOT / "tools" / "installer" / "paquets.txt"
# Sans roue sur PyPI : construits ici, depuis leur source figée, avec les outils de cette
# machine (pywebview → proxy-tools, un seul module en Python)
SOURCE_ONLY = ("proxy-tools",)
LOCK_HEADER = """\
# Paquets du Python embarqué de l'installeur Windows, figés par empreinte : pip n'installe
# que ces fichiers-là (--require-hashes), dépendances comprises (tools/build_installer.py).
# Relevé par « python tools/build_installer.py --lock » sur la machine qui construit ; une
# empreinte nouvelle, c'est un fichier jamais installé jusqu'ici : à relire avant de commiter.
"""
_LOCK_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)"
                        r"((?:\s+--hash=sha256:[0-9a-f]{64})+)\s*(?:#.*)?")
_RECORD_SKIP = ("INSTALLER", "REQUESTED", "direct_url.json", "RECORD")
# Parties d'une roue posées hors du paquet : les scripts vont dans bin\, que make_runtime
# efface ; les données et en-têtes (« data », « headers ») iraient à la racine des paquets,
# où un .pth s'exécuterait à chaque démarrage de Python : refusés
_WHEEL_DATA = {"purelib": True, "platlib": True, "scripts": False}
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
    done = subprocess.run(companion_build.git_command(ROOT, *args), capture_output=True,
                          text=True, encoding="utf-8")
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


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(packages=PACKAGES) -> dict:
    """{nom: version} des paquets et de toutes leurs dépendances, tels qu'installés sur la
    machine qui construit (dépendances conditionnelles évaluées pour elle : Windows et
    Python 3.14, comme le runtime)."""
    from importlib import metadata
    try:
        from packaging.requirements import Requirement
    except ImportError:                       # pas installé à part : celui de pip
        from pip._vendor.packaging.requirements import Requirement
    found, todo, done = {}, [(name, frozenset()) for name in packages], set()
    while todo:
        name, extras = todo.pop()
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise BuildError(f"{name} n'est pas installé sur cette machine : "
                             "pip install -r requirements.txt") from exc
        key = canonical_name(dist.metadata["Name"])
        if (key, extras) in done:
            continue
        done.add((key, extras))
        found[key] = dist.version
        for text in dist.requires or []:
            requirement = Requirement(text)
            if requirement.marker is not None and not any(
                    requirement.marker.evaluate({"extra": extra}) for extra in (extras or {""})):
                continue
            todo.append((requirement.name, frozenset(requirement.extras)))
    return dict(sorted(found.items()))


def read_lock(path: Path = LOCK) -> dict:
    """{nom: (version, [empreintes])} des paquets figés. BuildError s'il manque ou si une
    ligne est illisible."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"Paquets figés introuvables ({path}) : "
                         "python tools/build_installer.py --lock") from exc
    found = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE.fullmatch(line)
        if not match:
            raise BuildError(f"{path.name}, ligne {number} illisible : {line[:80]}")
        found[canonical_name(match.group(1))] = (match.group(2),
                                                 re.findall(r"[0-9a-f]{64}", match.group(3)))
    return found


def check_lock(lock: dict, wanted: dict):
    """BuildError si les paquets figés ne sont pas exactement ceux installés ici (closure) :
    l'installeur livrerait des versions que les essais n'ont pas vues."""
    missing = sorted(set(wanted) - set(lock))
    extra = sorted(set(lock) - set(wanted))
    changed = sorted(f"{n} {lock[n][0]} → {wanted[n]}" for n in set(wanted) & set(lock)
                     if lock[n][0] != wanted[n])
    parts = ([f"absents : {', '.join(missing)}"] if missing else []) + \
        ([f"en trop : {', '.join(extra)}"] if extra else []) + \
        ([f"versions changées : {', '.join(changed)}"] if changed else [])
    if parts:
        raise BuildError(f"Paquets figés ({LOCK.name}) différents de ceux de cette machine "
                         f"({' ; '.join(parts)}). Relève leurs empreintes (python "
                         "tools/build_installer.py --lock), relis le fichier, puis commite-le.")


def install_options() -> list:
    """Options de pip pour le runtime : chaque fichier vérifié par son empreinte, rien
    d'autre que les paquets figés (pas de dépendance prise en passant), des roues seulement,
    sauf SOURCE_ONLY, construits avec les outils de cette machine (sans isolement : pip
    téléchargerait sinon de quoi les construire, sans en vérifier l'empreinte)."""
    return ["--require-hashes", "--no-deps", "--only-binary=:all:",
            f"--no-binary={','.join(SOURCE_ONLY)}", "--no-build-isolation"]


def distribution_of(filename: str) -> tuple:
    """(nom, version) d'après le nom d'une roue ou d'une archive source."""
    if filename.endswith(".whl"):
        name, number = filename[:-4].split("-")[:2]
    else:
        stem = re.sub(r"\.(tar\.gz|zip)$", "", filename)
        name, number = stem.rsplit("-", 1)
    return canonical_name(name), number


def _record_entries(text: str) -> set:
    """(chemin, empreinte, taille) des fichiers d'un paquet installé, d'après son RECORD
    (écrit par pip à l'installation, d'après ce qu'il a posé) : ceux que pip pose tels
    quels. Sont écartés les scripts déplacés (« ../ »), les fichiers compilés et ceux que pip
    ajoute (INSTALLER, REQUESTED, direct_url.json, RECORD)."""
    found = set()
    for row in csv.reader(text.splitlines()):
        if len(row) < 3 or not row[1]:
            continue
        path = row[0]
        top = path.partition("/")[0]
        if path.startswith("../") or "__pycache__" in path or (
                top.endswith(".dist-info") and path.rsplit("/", 1)[-1] in _RECORD_SKIP):
            continue
        found.add((path, row[1], row[2]))
    return found


def _wheel_entries(z: zipfile.ZipFile, wheel_name: str) -> set:
    """(chemin une fois installé, empreinte, taille) de chaque fichier de la roue, calculés
    sur son contenu même : son RECORD, une roue piégée le recopierait tel quel. Le RECORD
    (pip en écrit un autre) et les scripts (_WHEEL_DATA) sont écartés ; une autre partie
    posée hors du paquet est refusée."""
    found = set()
    for info in z.infolist():
        if info.is_dir():
            continue
        path = info.filename
        top, _, rest = path.partition("/")
        if top.endswith(".dist-info") and rest == "RECORD":
            continue
        if top.endswith(".data"):
            kind, _, inner = rest.partition("/")
            if kind not in _WHEEL_DATA:
                raise BuildError(f"{wheel_name} pose des fichiers hors du paquet ({path}) : "
                                 "jamais figé sans les avoir vérifiés à la main.")
            if not _WHEEL_DATA[kind]:
                continue
            path = inner
        digest = hashlib.sha256()
        with z.open(info) as member:
            for block in iter(lambda: member.read(1 << 20), b""):
                digest.update(block)
        text = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
        found.add((path, f"sha256={text}", str(info.file_size)))
    return found


def same_as_installed(wheel: Path, name: str):
    """BuildError si la roue `wheel` ne contient pas exactement les fichiers du paquet
    `name` installé ici, contenu compris : une roue ajoutée sur PyPI après coup, à la même
    version."""
    from importlib import metadata
    with zipfile.ZipFile(wheel) as z:
        records = [n for n in z.namelist() if re.fullmatch(r"[^/]+\.dist-info/RECORD", n)]
        if len(records) != 1:
            raise BuildError(f"{wheel.name} : roue illisible (RECORD)")
        inside = _wheel_entries(z, wheel.name)
    installed = _record_entries(metadata.distribution(name).read_text("RECORD") or "")
    if inside != installed:
        different = sorted({p for p, _, _ in inside ^ installed})
        raise BuildError(f"{wheel.name} ne contient pas les fichiers de {name} installés sur "
                         "cette machine : PyPI sert un autre fichier que celui installé ici "
                         f"({', '.join(different[:5])}{'…' if len(different) > 5 else ''}). "
                         "Vérifie-le avant de le figer.")


def relock(path: Path = LOCK, packages=PACKAGES) -> Path:
    """Relève dans `path` les empreintes des paquets installés ici (closure) : pip
    télécharge les roues dans un dossier privé, et chacune doit contenir les fichiers
    installés ici (same_as_installed). Pour une version déjà figée, le fichier téléchargé
    doit être celui-là (même empreinte) : une empreinte nouvelle ne vaut que pour une
    version qui change. Une archive source (SOURCE_ONLY) n'est pas téléchargée : pip
    exécuterait son setup.py pour la lire, avant toute vérification. Son empreinte déjà
    figée est reprise si sa version n'a pas changé ; sinon, elle se relève à la main, après
    relecture. Le fichier est à relire avant d'être commité."""
    wanted = closure(packages)
    try:
        before = read_lock(path)
    except BuildError:
        before = {}
    seen = {}
    for name in SOURCE_ONLY:
        if name not in wanted:
            continue
        number, hashes = before.get(name, (None, []))
        if number != wanted[name] or not hashes:
            raise BuildError(f"{name} est passé à la version {wanted[name]} : relève à la main "
                             f"l'empreinte de son archive source (pip download --no-deps "
                             f"--no-binary=:all: {name}=={wanted[name]}, relue, puis pip hash) "
                             f"dans {path.name}, puis relance --lock.")
        seen[name] = f"{name}=={number} " + " ".join(f"--hash=sha256:{h}" for h in hashes)
    lines = []
    with private.private_tempdir("aiodos-roues-") as work:
        dest = private.private_dir(work / "roues")
        wheels = [f"{n}=={v}" for n, v in wanted.items() if n not in SOURCE_ONLY]
        done = subprocess.run([sys.executable, "-m", "pip", "download",
                               "--disable-pip-version-check", "--no-deps", "--only-binary=:all:",
                               "--dest", str(dest), *wheels], cwd=ROOT,
                              env=companion_build.private_temp(os.environ, work / "temp"))
        if done.returncode != 0:
            raise BuildError("pip n'a pas téléchargé les paquets (voir ses messages ci-dessus)")
        for file in sorted(dest.iterdir()):
            name, number = distribution_of(file.name)
            if file.suffix != ".whl" or wanted.get(name) != number or name in SOURCE_ONLY:
                raise BuildError(f"Fichier inattendu parmi les paquets téléchargés : {file.name}")
            same_as_installed(file, name)
            digest = sha256_of(file)
            pinned_number, pinned_hashes = before.get(name, (None, []))
            if pinned_number == number and digest not in pinned_hashes:
                raise BuildError(
                    f"PyPI sert pour {name} {number} un autre fichier que celui figé dans "
                    f"{path.name} ({file.name}, empreinte {digest[:16]}…) : à version égale, "
                    "c'est un fichier ajouté après coup. Rien n'a changé. Vérifie-le ; s'il est "
                    f"bon, retire la ligne de {name} du fichier, puis relance --lock.")
            hashes = pinned_hashes if pinned_number == number else [digest]
            seen[name] = f"{name}=={number} " + " ".join(f"--hash=sha256:{h}" for h in hashes)
        missing = sorted(set(wanted) - set(seen))
        if missing:
            raise BuildError(f"Paquets non téléchargés : {', '.join(missing)}")
        lines = [seen[name] for name in sorted(seen)]
    path.write_text(LOCK_HEADER + "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


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
    check_lock(read_lock(), closure())
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
               "--no-warn-script-location", *install_options(), "--target", str(packages),
               "-r", str(LOCK)]
    done = subprocess.run(command, cwd=ROOT, env=companion_build.private_temp(
        os.environ, stage.parent / "temp"))
    if done.returncode != 0:
        raise BuildError("pip n'a pas installé les paquets (voir ses messages ci-dessus)")
    shutil.rmtree(packages / "bin", ignore_errors=True)          # scripts inutiles ici
    (packages / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    (runtime / "aiodos-runtime.json").write_text(
        json.dumps({"python": PYTHON["version"], "packages": pinned(),
                    "lock_sha256": sha256_of(LOCK)}, indent=1), encoding="utf-8")


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
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
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
    """ISCC.exe d'Inno Setup 6, dans un dossier que les autres comptes ne peuvent pas
    modifier : un ISCC remplacé piégerait l'installeur (shutil.which regarde aussi le
    dossier courant). BuildError sinon."""
    for candidate in (os.environ.get("AIODOS_ISCC"), shutil.which("ISCC"),
                      r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                      r"C:\Program Files\Inno Setup 6\ISCC.exe",
                      os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")):
        if candidate and Path(candidate).is_file():
            found = Path(candidate).resolve()
            try:
                private.require_private(found.parent, "le dossier d'Inno Setup")
                private.require_private(found, "ISCC.exe")
            except private.NotPrivate as exc:
                raise BuildError(f"{exc}. Installe Inno Setup dans Program Files ou dans ton "
                                 "dossier personnel.") from exc
            return found
    raise BuildError("Inno Setup 6 introuvable (ISCC.exe) : https://jrsoftware.org/isdl.php, "
                     "ou AIODOS_ISCC")


def fresh_work(path: Path) -> Path:
    """Le dossier de travail `path` (build\\installateur), neuf et privé : l'ancien est
    effacé, puis recréé privé (private.private_dir), et tout ce qui s'y crée en hérite. Ses
    droits ne sont jamais changés : s'il n'a pas pu être effacé et qu'il est ouvert à
    d'autres comptes, BuildError."""
    shutil.rmtree(path, ignore_errors=True)
    try:
        return private.private_dir(path)
    except (private.NotPrivate, OSError) as exc:
        raise BuildError(f"Dossier de travail {path} : {exc}") from exc


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
                           f"/DIcon={shortcut_icon(stage)}", str(script)],
                          env=companion_build.private_temp(os.environ, stage.parent / "temp"))
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
    parser.add_argument("--lock", action="store_true",
                        help=f"relever les empreintes des paquets ({LOCK.name}), puis s'arrêter")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.exit("L'installeur Windows se construit sous Windows.")
    try:                                  # déjà fait par l'amorce, lancé comme outil
        companion_build.require_private_repository(ROOT, extra=[ROOT / "build"])
    except companion_build.BuildError as exc:
        sys.exit(f"Installeur non construit : {exc}")
    if args.lock:
        try:
            path = relock()
        except (BuildError, OSError) as exc:
            sys.exit(f"Empreintes non relevées : {exc}")
        print(f"Empreintes relevées dans {path} : relis-les, puis commite le fichier.")
        return 0
    try:
        number, commit = version(), git("rev-parse", "HEAD")
        changed = git("status", "--porcelain", "--untracked-files=no")
    except BuildError as exc:
        sys.exit(f"Installeur non construit : {exc}")
    if changed:
        print("ATTENTION : des modifications ne sont pas commitées ; l'installeur prend le "
              f"code du commit {commit[:10]}.", flush=True)
    began = time.monotonic()
    try:
        stage = fresh_work(ROOT / "build" / "installateur") / "stage"
        stage.mkdir()
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
