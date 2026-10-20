"""Ce que l'appli Android d'AIODOS prend chez d'autres projets, épinglé ici : nom, adresse,
taille et empreinte SHA-256 de chaque archive. Téléchargée une fois dans
~/.aiodos/android/telechargements/, revérifiée à chaque construction (tools/build_android.py,
tools/android_wheels.py).

- llama.cpp : l'archive officielle pour Android (arm64) de la même version que le moteur des
  PC (tools/engine_release.py) ; ses programmes deviennent des bibliothèques de l'appli, seul
  endroit d'où Android permet d'exécuter un programme (docs/android.md).
- pydantic-core : ses sources, que Chaquopy ne fournit pas compilées (tools/android_wheels.py).
- Python 3.13 de Chaquopy pour arm64 (Maven Central) : son libpython, pour compiler
  pydantic-core contre lui. Gradle prend le même pour l'appli.

Changer de version : relever nom, taille et empreinte (champ « digest » de l'API des versions
de GitHub, « sha256 » de PyPI, fichier .sha1 de Maven Central puis sha256sum), puis refaire
l'essai sur un téléphone.
"""
import hashlib
import shutil
import urllib.request
from pathlib import Path

CACHE = Path.home() / ".aiodos" / "android" / "telechargements"
TIMEOUT = 60

LLAMA_BUILD = "b11707"            # celle du moteur des PC (tools/engine_release.py)
PYTHON = "3.13"                   # celle des paquets Android de Chaquopy (docs/android.md)
DEPS = {
    "llama.cpp": {
        "name": f"llama-{LLAMA_BUILD}-bin-android-arm64.tar.gz",
        "url": f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/"
               f"llama-{LLAMA_BUILD}-bin-android-arm64.tar.gz",
        "size": 76064057,
        "sha256": "4369cd572e5207c27af1b6a2ff8c6d1215d616337f260f9313b3795612dcaf14",
    },
    "pydantic-core": {
        "version": "2.46.3",      # celle de pydantic 2.13.3, comme sur les PC
        "name": "pydantic_core-2.46.3.tar.gz",
        "url": "https://files.pythonhosted.org/packages/2a/ef/"
               "f7abb56c49382a246fd2ce9c799691e3c3e7175ec74b14d99e798bcddb1a/"
               "pydantic_core-2.46.3.tar.gz",
        "size": 471412,
        "sha256": "41c178f65b8c29807239d47e6050262eb6bf84eb695e41101e62e38df4a5bc2c",
    },
    "python-target": {
        "version": "3.13.16-0",
        "name": "target-3.13.16-0-arm64-v8a.zip",
        "url": "https://repo.maven.apache.org/maven2/com/chaquo/python/target/3.13.16-0/"
               "target-3.13.16-0-arm64-v8a.zip",
        "size": 7309656,
        "sha256": "fae8cfd4d3910b6f978b22279fe820c0c6ba49d0ea20f7b106aa6130f1c8f355",
    },
}


class DepError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _matches(path: Path, dep: dict) -> bool:
    return (path.is_file() and path.stat().st_size == dep["size"]
            and sha256(path) == dep["sha256"])


def fetch(key: str, cache: Path = CACHE) -> Path:
    """L'archive `key` de DEPS, taille et empreinte vérifiées (téléchargée au besoin)."""
    dep = DEPS[key]
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / dep["name"]
    if _matches(path, dep):
        return path
    print(f"  téléchargement : {dep['name']} ({dep['size'] / 1e6:.1f} Mo)", flush=True)
    part = path.with_name(path.name + ".part")
    try:
        with urllib.request.urlopen(dep["url"], timeout=TIMEOUT) as reply, \
                open(part, "wb") as out:
            shutil.copyfileobj(reply, out, 1 << 20)
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        raise DepError(f"{dep['name']} : téléchargement impossible ({exc})") from exc
    if not _matches(part, dep):
        part.unlink(missing_ok=True)
        raise DepError(f"{dep['name']} : archive reçue différente de celle épinglée")
    part.replace(path)
    return path
