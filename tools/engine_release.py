"""Les moteurs joints à une version (tools/publish.py) : llama.cpp pour les gros modèles
répartis, stable-diffusion.cpp pour les images. Chaque fois l'archive officielle, d'une
version épinglée ici. Elle est téléchargée une fois, gardée dans ~/.aiodos/moteurs/, et sa
taille et son empreinte sont revérifiées à chaque publication.

Changer de version d'un moteur :
- relever la version, les noms, les tailles et les empreintes (champ « digest » de l'API
  des versions sur GitHub) ;
- refaire les mesures (docs/gros-modeles.md, docs/generation-repartie.md), puis publier.

Toutes les machines d'un groupe doivent avoir la même version de llama.cpp : elle voyage
avec AIODOS.

Pour les essais (tests/test_updates_publish.py), AIODOS_ENGINE_ASSETS désigne un fichier
JSON qui remplace les tables épinglées : {plateforme : {"name", "size", "sha256",
"source"}} pour llama.cpp seul, ou {"engines" : {moteur : {plateforme : {…}}}}, où
« source » est un fichier local ; {} : aucun moteur. AIODOS_ENGINE_CACHE remplace le
dossier ~/.aiodos/moteurs/.
"""
import hashlib
import json
import os
import shutil
import urllib.request
from pathlib import Path

LLAMA = "llama.cpp"
IMAGES = "stable-diffusion.cpp"
ENGINES = {
    LLAMA: {
        "build": "b11707",        # 17/10/2026 : celle des mesures de docs/gros-modeles.md
        "url": "https://github.com/ggml-org/llama.cpp/releases/download/{build}/{name}",
        "assets": {
            # Version Vulkan : toutes les cartes graphiques (NVIDIA, AMD, Intel), 30 Mo
            "windows-x86_64": {
                "name": "llama-b11707-bin-win-vulkan-x64.zip",
                "size": 32260710,
                "sha256": "9c7d17c12053bf95b03a179f9bcdf15fdeafe680a4f2845d23bc951bc18c0fa5",
            },
        },
    },
    IMAGES: {
        # 29/10/2026 : la dernière publiée (FLUX.3 Image Klein, Qwen Image 2.1, LTX-2.5,
        # Wan 2.2, Ming-Image, HiDream-O1 ; docs/conception/modeles-auto.md). Avant :
        # master-973-f0aac52 (17/10/2026), celle des mesures de docs/generation-repartie.md
        "build": "master-1027-e7b7ec6",
        "url": "https://github.com/leejet/stable-diffusion.cpp/releases/download/{build}/{name}",
        "assets": {
            # Version Vulkan : toutes les cartes graphiques, 30 Mo (sans le RPC de ggml)
            "windows-x86_64": {
                "name": "sd-master-e7b7ec6-bin-win-vulkan-x64.zip",
                "size": 29745686,
                "sha256": "2d0d65f51f059588740a82fc983b9d4afdf28839729ab0db4af2701c4f17a701",
            },
        },
    },
}
CACHE = Path.home() / ".aiodos" / "moteurs"
TIMEOUT = 60


class EngineError(Exception):
    pass


def assets(engine: str = LLAMA) -> dict:
    """{plateforme : {"name", "size", "sha256", "source"}} des archives du moteur à joindre."""
    override = os.environ.get("AIODOS_ENGINE_ASSETS")
    if override:
        table = json.loads(Path(override).read_text(encoding="utf-8"))
        if "engines" in table:
            return table["engines"].get(engine, {})
        return table if engine == LLAMA else {}
    pinned = ENGINES[engine]
    return {where: dict(asset, source=pinned["url"].format(build=pinned["build"],
                                                           name=asset["name"]))
            for where, asset in pinned["assets"].items()}


def targets(engine: str = LLAMA) -> list:
    return sorted(assets(engine))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _matches(path: Path, asset: dict) -> bool:
    return (path.is_file() and path.stat().st_size == asset["size"]
            and _sha256(path) == asset["sha256"])


def fetch(where: str, cache: Path | None = None, engine: str = LLAMA) -> Path:
    """L'archive du moteur pour la plateforme `where`, taille et empreinte vérifiées."""
    asset = assets(engine).get(where)
    if not asset:
        raise EngineError(f"pas de {engine} épinglé pour {where}")
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
