"""Le moteur llama.cpp joint à une version (tools/publish.py), pour les gros modèles
répartis : l'archive officielle de ggml-org, d'une version épinglée ici. Elle est
téléchargée une fois, gardée dans ~/.aiodos/moteurs/, et sa taille et son empreinte sont
revérifiées à chaque publication.

Changer de version de llama.cpp :
- relever BUILD, les noms, les tailles et les empreintes (champ « digest » de l'API des
  versions de github.com/ggml-org/llama.cpp) ;
- refaire les mesures de docs/gros-modeles.md, puis publier.

Toutes les machines d'un groupe doivent avoir la même version de llama.cpp : elle voyage
avec AIODOS.

Pour les essais (tests/test_updates_publish.py), AIODOS_ENGINE_ASSETS désigne un fichier
JSON qui remplace la table épinglée : {plateforme : {"name", "size", "sha256", "source"}},
où « source » est un fichier local ; {} : aucun moteur. AIODOS_ENGINE_CACHE remplace
le dossier ~/.aiodos/moteurs/.
"""
import hashlib
import json
import os
import shutil
import urllib.request
from pathlib import Path

BUILD = "b11707"                  # 17/10/2026 : celle des mesures de docs/gros-modeles.md
URL = "https://github.com/ggml-org/llama.cpp/releases/download/{build}/{name}"
ASSETS = {
    # Version Vulkan : toutes les cartes graphiques (NVIDIA, AMD, Intel), 30 Mo
    "windows-x86_64": {
        "name": "llama-b11707-bin-win-vulkan-x64.zip",
        "size": 32260710,
        "sha256": "9c7d17c12053bf95b03a179f9bcdf15fdeafe680a4f2845d23bc951bc18c0fa5",
    },
}
CACHE = Path.home() / ".aiodos" / "moteurs"
TIMEOUT = 60


class EngineError(Exception):
    pass


def assets() -> dict:
    """{plateforme : {"name", "size", "sha256", "source"}} des archives à joindre."""
    override = os.environ.get("AIODOS_ENGINE_ASSETS")
    if override:
        return json.loads(Path(override).read_text(encoding="utf-8"))
    return {where: dict(asset, source=URL.format(build=BUILD, name=asset["name"]))
            for where, asset in ASSETS.items()}


def targets() -> list:
    return sorted(assets())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _matches(path: Path, asset: dict) -> bool:
    return (path.is_file() and path.stat().st_size == asset["size"]
            and _sha256(path) == asset["sha256"])


def fetch(where: str, cache: Path | None = None) -> Path:
    """L'archive du moteur pour la plateforme `where`, taille et empreinte vérifiées."""
    asset = assets().get(where)
    if not asset:
        raise EngineError(f"pas de llama.cpp épinglé pour {where}")
    cache = cache or Path(os.environ.get("AIODOS_ENGINE_CACHE") or CACHE)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / asset["name"]
    if _matches(path, asset):
        return path
    part = path.with_name(path.name + ".part")
    try:
        if "://" in asset["source"]:
            with urllib.request.urlopen(asset["source"], timeout=TIMEOUT) as reply, \
                    open(part, "wb") as out:
                shutil.copyfileobj(reply, out, 1 << 20)
        else:
            shutil.copyfile(asset["source"], part)
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        raise EngineError(f"téléchargement impossible ({exc})") from exc
    if not _matches(part, asset):
        part.unlink(missing_ok=True)
        raise EngineError("archive reçue différente de celle épinglée (taille ou empreinte)")
    os.replace(part, path)
    return path
