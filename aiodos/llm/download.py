"""Télécharger un modèle du catalogue (llm/catalog.py) pour le moteur intégré, sans Ollama :
fichiers GGUF de Hugging Face, repris là où ils s'étaient arrêtés (en-tête Range), vérifiés
(taille et SHA-256 épinglés) avant d'être mis en place, puis décrits dans model.json :
llm/models.py les voit alors, et le routeur de llama.cpp repart avec eux.

Seulement à la demande de l'utilisateur (« Installer un modèle ») : jamais de
téléchargement d'office."""
import hashlib
import json
import logging
import shutil

import requests

from aiodos.i18n import t
from aiodos.llm import catalog, models as registry

log = logging.getLogger("aiodos.llm")

CHUNK = 1 << 20
TIMEOUT = (15, 120)


class DownloadError(RuntimeError):
    pass


def pull(name: str, progress=None, stop=None) -> bool:
    """Comme ollama.pull : progress(reçu, total, étape) ; vrai une fois installé, faux si
    `stop` est levé en route ; DownloadError sinon (modèle hors catalogue, fichier
    abîmé, réseau)."""
    spec = catalog.entry(name)
    if spec is None:
        raise DownloadError(t("llm.not_in_catalog", name=name))
    name = registry.full_name(name)
    folder = registry.models_dir() / registry.router_id(name)
    folder.mkdir(parents=True, exist_ok=True)
    total = catalog.total_size(spec)
    done_before = 0
    for role, (file, sha256, size) in spec["files"].items():
        target = folder / file
        if not (target.is_file() and target.stat().st_size == size):
            finished = _fetch(catalog.url(spec, file), target, size, sha256, stop,
                              lambda got, d=done_before: progress and progress(
                                  d + got, total, t("llm.downloading", file=file)))
            if not finished:
                return False
        done_before += size
    meta = {"name": name, "file": spec["files"]["model"][0], "kind": spec["kind"],
            "license": spec["license"], "family": spec["family"],
            "parameters": spec["parameters"], "quantization": spec["quantization"],
            "sha256": spec["files"]["model"][1], "source": f"{catalog.HF}/{spec['repo']}"}
    if "mmproj" in spec["files"]:
        meta["mmproj"] = spec["files"]["mmproj"][0]
    (folder / "model.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    registry.forget_cache()
    if progress:
        progress(total, total, "success")
    log.info("Modèle %s installé (%s, %s)", name, spec["repo"], spec["license"])
    return True


def _fetch(url, target, size, sha256, stop, progress) -> bool:
    part = target.with_name(target.name + ".part")
    have = part.stat().st_size if part.is_file() else 0
    if have > size:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=TIMEOUT) as r:
            if r.status_code == 200 and have:           # pas de reprise : depuis le début
                have = 0
            elif r.status_code not in (200, 206):
                raise DownloadError(t("llm.hf_status", status=r.status_code, file=target.name))
            with part.open("ab" if have else "wb") as out:
                for chunk in r.iter_content(CHUNK):
                    if stop is not None and stop.is_set():
                        return False
                    out.write(chunk)
                    have += len(chunk)
                    progress(have)
    except requests.RequestException as exc:
        raise DownloadError(t("llm.download_interrupted",
                              error=exc.__class__.__name__)) from exc
    if part.stat().st_size != size or _sha256(part) != sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(t("llm.checksum_mismatch", file=target.name))
    shutil.move(str(part), str(target))
    return True


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()
