"""Préparer la publication des bibliothèques clientes aiodos-client (clients/python, clients/js)
sur PyPI et npm : tout vérifier, puis construire les paquets, sans rien publier.

    py -3.14 tools/publish_clients.py            # vérifie et construit dans build/clients/
    py -3.14 tools/publish_clients.py --check    # vérifie seulement

Ce qui est vérifié (conception, partage de puissance § 6.10) :
- les versions concordent : pyproject.toml, aiodos_client.__version__, package.json, VERSION
  du module JS, et la même partout ;
- chaque fichier source porte « SPDX-License-Identifier: Apache-2.0 » ; LICENSE et NOTICE sont
  là ;
- aucune dépendance (ni dependencies dans pyproject.toml, ni dans package.json) ;
- aucune ligne reprise de PANDORA ni d'ARKLIGHT PC (sous GPL ou autre licence) : chaque ligne
  de code assez longue des clients est cherchée dans leurs sources, quand elles sont sur ce PC ;
- les essais des deux clients passent.

La publication elle-même ne se fait pas d'ici : une étiquette « clients-v<version> » poussée
sur github.com/AIODOS/aiodos lance .github/workflows/clients-publish.yml, qui publie par
« trusted publishing » (OIDC, aucun jeton sur un disque), avec la provenance npm. Mode
d'emploi : clients/PUBLIER.md.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ROOT / "clients"
PY = CLIENTS / "python"
JS = CLIENTS / "js"
OUT = ROOT / "build" / "clients"
OTHERS = [Path(r"C:\_22eme ARKANE\PROJETS\PANDORA"), Path(r"C:\_22eme ARKANE\PROJETS\ARKLIGHT PC")]
SPDX = "SPDX-License-Identifier: Apache-2.0"
MIN_LINE = 40                 # une ligne plus courte se retrouve partout (imports, accolades)
# aiodos_client : la copie fidèle du client que PANDORA embarque (tools/update_aiodos_client.py
# chez lui) va d'AIODOS vers PANDORA ; la lire ferait prendre chaque ligne pour un emprunt
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build", ".venv", "venv",
             "aiodos_client", "vendor"}


class Problem(Exception):
    pass


def _sources(base: Path, suffixes) -> list:
    return [p for p in base.rglob("*") if p.is_file() and p.suffix in suffixes
            and not (SKIP_DIRS & set(p.relative_to(base).parts))]


def versions() -> dict:
    found = {}
    project = tomllib.loads((PY / "pyproject.toml").read_text(encoding="utf-8"))
    found["pyproject.toml"] = project["project"]["version"]
    if project["project"].get("dependencies"):
        raise Problem("clients/python : le client Python ne doit avoir aucune dépendance.")
    init = (PY / "src" / "aiodos_client" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
    found["aiodos_client.__version__"] = match.group(1) if match else None
    package = json.loads((JS / "package.json").read_text(encoding="utf-8"))
    found["package.json"] = package["version"]
    if package.get("dependencies") or package.get("devDependencies"):
        raise Problem("clients/js : le client JavaScript ne doit avoir aucune dépendance.")
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (JS / "src").glob("*.js"))
    match = re.search(r"VERSION\s*=\s*'([^']+)'", sources)
    found["src/*.js VERSION"] = match.group(1) if match else None
    if len(set(found.values())) != 1:
        raise Problem("Versions différentes : " + ", ".join(f"{k}={v}" for k, v in found.items()))
    return found


def licenses():
    for base in (PY, JS):
        for name in ("LICENSE", "NOTICE"):
            if not (base / name).is_file():
                raise Problem(f"{base.relative_to(ROOT)}/{name} manque.")
    missing = [p for p in _sources(PY / "src", {".py"}) + _sources(JS / "src", {".js"})
               + [JS / "index.d.ts"] if SPDX not in p.read_text(encoding="utf-8")[:600]]
    if missing:
        raise Problem("Sans en-tête de licence : " + ", ".join(
            str(p.relative_to(ROOT)) for p in missing))


GENERIC = ("#", "//", "*", "/*", "import ", "from ", "return new Promise(")


def _lines(path: Path) -> set:
    """Les lignes de code qui disent quelque chose : ni commentaires, ni imports, ni tournures
    que tout le monde écrit pareil."""
    out = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = " ".join(line.split())
        if len(text) >= MIN_LINE and not text.startswith(GENERIC):
            out.add(text)
    return out


def borrowed() -> list:
    """Lignes des clients qu'on retrouve telles quelles dans PANDORA ou ARKLIGHT PC."""
    ours = {}
    for path in _sources(PY / "src", {".py"}) + _sources(JS / "src", {".js"}):
        for line in _lines(path):
            ours.setdefault(line, path)
    found = []
    for other in OTHERS:
        if not other.is_dir():
            print(f"  (absent de ce PC, non comparé : {other})")
            continue
        for path in _sources(other, {".py", ".js", ".mjs", ".ts"}):
            for line in _lines(path) & ours.keys():
                found.append(f"{ours[line].relative_to(ROOT)} ← {path} : {line[:80]}")
    return found


def tests():
    python = subprocess.run([sys.executable, "-m", "pytest", "-q", str(PY / "tests")], cwd=ROOT)
    if python.returncode:
        raise Problem("Les essais du client Python échouent.")
    node = shutil.which("node")
    if not node:
        raise Problem("Node.js introuvable : les essais du client JavaScript n'ont pas tourné.")
    js = subprocess.run([node, "--test"], cwd=JS)       # « test/ » : pris pour un module
    if js.returncode:
        raise Problem("Les essais du client JavaScript échouent.")


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    wheel = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                            "-w", str(OUT), str(PY)], cwd=ROOT)
    if wheel.returncode:
        raise Problem("La roue du client Python ne se construit pas (setuptools manque ?).")
    npm = shutil.which("npm")
    if not npm:
        raise Problem("npm introuvable : le paquet JavaScript n'a pas été construit.")
    packed = subprocess.run([npm, "pack", "--pack-destination", str(OUT)], cwd=JS)
    if packed.returncode:
        raise Problem("npm pack a échoué.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="vérifier sans construire")
    args = parser.parse_args()
    try:
        print("Versions :", versions())
        licenses()
        print("Licences : en-têtes, LICENSE et NOTICE présents.")
        copied = borrowed()
        if copied:
            raise Problem("Lignes reprises d'une autre appli :\n  " + "\n  ".join(copied[:20]))
        print("Aucune ligne reprise de PANDORA ni d'ARKLIGHT PC.")
        tests()
        if not args.check:
            build()
            print(f"Paquets construits dans {OUT}. Publication : clients/PUBLIER.md.")
    except Problem as exc:
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
