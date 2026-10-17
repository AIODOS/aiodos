"""Fichiers de modèles d'images déjà sur ce PC, ailleurs que dans le dossier d'AIODOS.

ComfyUI (Comfy Desktop) range ses modèles dans un dossier partagé, que PANDORA utilise
aussi ; le projet minimax-h3-local garde les siens dans son dossier models ; un autre
AIODOS (une copie de développement, une ancienne installation) dans data/images/models.
Un fichier du catalogue qu'on y trouve, de même nom et de même taille, est repris au
lieu d'être téléchargé, une fois son empreinte vérifiée : par un lien physique sur le
même disque (instantané, aucune place en plus), par une copie sinon.

Dossiers en plus : réglage images_other_dirs, un par ligne (ou séparés par « ; »).
"""
import os
import re
import shutil
from pathlib import Path

from aiodos import settings
from aiodos.imaging import catalog

DEPTH = 3                        # le dossier, puis deux niveaux : ComfyUI range models/vae/…


def _known() -> list:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return [(base / "Comfy-Desktop" / "ComfyUI-Shared" / "models", "ComfyUI"),
            (Path.home() / "Documents" / "ComfyUI" / "models", "ComfyUI"),
            (base / "PANDORA" / "externals" / "minimax-h3-local" / "models", "minimax-h3-local")]


def folders() -> list:
    """[(dossier, nom en clair)] où chercher : ceux du réglage d'abord, puis ceux qu'on
    connaît, sans le dossier de modèles d'AIODOS lui-même."""
    extra = [(Path(p.strip()), p.strip())
             for p in re.split(r"[;\n]", str(settings.get("images_other_dirs") or "")) if p.strip()]
    own = os.path.normcase(str(catalog.folder().resolve()))
    found, seen = [], {own}
    for folder, name in extra + _known():
        try:
            key = os.path.normcase(str(folder.resolve()))
            if key in seen or not folder.is_dir():
                continue
        except OSError:
            continue
        seen.add(key)
        found.append((folder, name))
    return found


def find(spec: dict):
    """(chemin, nom du dossier) d'un fichier du nom et de la taille de `spec`, ou None."""
    for root, name in folders():
        for depth in range(DEPTH):
            for path in root.glob("/".join(["*"] * depth + [spec["name"]])):
                try:
                    if path.is_file() and path.stat().st_size == spec["size"]:
                        return path, name
                except OSError:
                    continue
    return None


def available(model_id: str) -> list:
    """Les fichiers d'un modèle qui manquent ici et qu'on a trouvés sur ce PC (pas encore
    vérifiés : leur empreinte l'est au moment de les reprendre)."""
    found = []
    for spec in catalog.model(model_id)["files"].values():
        if catalog.file_state(spec, check=False) == "ok":
            continue
        hit = find(spec)
        if hit:
            found.append({"name": spec["name"], "size": spec["size"], "from": hit[1]})
    return found


def bring(spec: dict, target: Path, stop=None):
    """Reprend la copie trouvée sur ce PC si son empreinte est la bonne : rend le nom de son
    dossier, ou None (rien trouvé, empreinte différente, ou arrêt demandé)."""
    hit = find(spec)
    if hit is None:
        return None
    source, name = hit
    if catalog.sha256_of(source, stop) != spec["sha256"]:
        return None
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)                # téléchargement entamé : inutile désormais
    try:
        os.link(source, part)                   # même disque : aucune place en plus
    except OSError:
        shutil.copyfile(source, part)
    os.replace(part, target)
    return name
