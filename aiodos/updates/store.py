"""Paquets reçus et versions installées, dans data/updates/ :

    packages/<version>/manifest.json     manifeste signé
    packages/<version>/<fichier>         fichiers complets, empreinte vérifiée
    downloads/<version>/<fichier>.part   téléchargements en cours, repris où ils en étaient
    versions/<version>/                  code installé (cf. install.py, launcher.py)
    bin/                                 compagnon livré, en service (cf. binaries.py)
    state.json                           cf. state.py

Un paquet complet (manifeste et archive du code) est gardé pour les machines appairées
qui ne l'ont pas encore : c'est ainsi qu'une version passe de machine en machine. Le
compagnon d'une plateforme s'y ajoute quand la machine est de cette plateforme ; une
machine transmet aussi les fichiers d'un paquet incomplet qu'elle a en entier.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

from aiodos import paths
from aiodos.updates import manifest as manifests
from aiodos.updates import version as versions
from aiodos.updates.manifest import UpdateError

MARKER = ".installed.json"         # dans versions/<v>/ : installation menée à son terme
KEEP_PACKAGES = 2


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else paths.data_dir() / "updates"

    # ------------------------------------------------------------ emplacements
    def _dir(self, name: str, version: str | None = None) -> Path:
        if version is not None and not versions.valid(version):
            raise UpdateError(f"Numéro de version illisible : {version!r}")
        folder = self.root / name
        return folder / version if version is not None else folder

    def version_dir(self, version: str) -> Path:
        return self._dir("versions", version)

    # ------------------------------------------------------------------ paquets
    def signed(self, version: str) -> dict | None:
        """Manifeste signé gardé pour cette version (non revérifié)."""
        try:
            path = self._dir("packages", version) / "manifest.json"
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UpdateError):
            return None

    def manifest(self, version: str, trusted=None) -> dict | None:
        """Manifeste vérifié de cette version, ou None."""
        signed = self.signed(version)
        if signed is None:
            return None
        try:
            found = manifests.verify(signed, trusted)
        except UpdateError:
            return None
        return found if found["version"] == version else None

    def add_manifest(self, signed: dict, trusted=None) -> dict:
        found = manifests.verify(signed, trusted)
        folder = self._dir("packages", found["version"])
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f"manifest.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(signed, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, folder / "manifest.json")
        return found

    def file_path(self, version: str, name: str, trusted=None) -> Path:
        """Fichier d'un paquet, seulement s'il figure dans son manifeste vérifié."""
        found = self.manifest(version, trusted)
        if found is None or name not in {f["name"] for f in found["files"]}:
            raise UpdateError("Fichier inconnu dans les paquets de cette machine.")
        return self._dir("packages", version) / name

    def package_path(self, version: str, item: dict) -> Path:
        return self._dir("packages", version) / item["name"]

    def has(self, version: str, item: dict) -> bool:
        """Ce fichier du paquet est-il là, entier ? (empreinte vérifiée à l'arrivée)"""
        path = self.package_path(version, item)
        return path.is_file() and path.stat().st_size == item["size"]

    def held(self, version: str, trusted=None) -> list:
        """Fichiers de ce paquet que cette machine a en entier (manifeste vérifié)."""
        found = self.manifest(version, trusted)
        return [f for f in found["files"] if self.has(version, f)] if found else []

    def complete(self, version: str, trusted=None) -> bool:
        """Paquet installable et transmissible : manifeste vérifié, archive du code là.
        (Le compagnon d'une plateforme est en plus : chaque machine ne prend que le sien.)"""
        found = self.manifest(version, trusted)
        return found is not None and self.has(version, manifests.source(found))

    def package_versions(self) -> list:
        try:
            names = [p.name for p in self._dir("packages").iterdir() if p.is_dir()]
        except OSError:
            return []
        return sorted((n for n in names if versions.valid(n)), key=versions.key)

    def offer(self, trusted=None) -> str | None:
        """La version la plus récente que cette machine peut transmettre."""
        for candidate in reversed(self.package_versions()):
            if self.complete(candidate, trusted):
                return candidate
        return None

    def available(self, trusted=None) -> dict:
        """{version: [types de fichiers présents]} : ce que cette machine peut transmettre,
        compagnons compris (annoncé dans son profil, cf. identity.profile)."""
        found = {}
        for version in self.package_versions():
            kinds = sorted({f["kind"] for f in self.held(version, trusted)
                            if isinstance(f.get("kind"), str)})
            if kinds:
                found[version] = kinds
        return found

    def read_chunk(self, version: str, name: str, offset: int, size: int) -> bytes:
        """Un morceau d'un fichier nommé dans le manifeste vérifié, et présent en entier."""
        item = next((f for f in self.held(version) if f["name"] == name), None)
        if item is None:
            raise UpdateError("Fichier absent des paquets de cette machine.")
        with open(self.package_path(version, item), "rb") as f:
            f.seek(max(0, offset))
            return f.read(max(0, size))

    # ----------------------------------------------------------- téléchargements
    def partial(self, version: str, name: str) -> Path:
        folder = self._dir("downloads", version)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{name}.part"

    def finish_download(self, version: str, item: dict) -> Path:
        """Téléchargement terminé : taille et empreinte vérifiées, puis rangé dans le
        paquet. Fichier non conforme : effacé (on repartira de zéro)."""
        part = self.partial(version, item["name"])
        if part.stat().st_size != item["size"] or sha256_of(part) != item["sha256"]:
            part.unlink(missing_ok=True)
            raise UpdateError("Fichier reçu incomplet ou altéré : il sera téléchargé de nouveau.")
        target = self._dir("packages", version) / item["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)
        try:
            part.parent.rmdir()           # plus rien en cours pour cette version
        except OSError:
            pass
        return target

    def reuse(self, version: str, item: dict) -> bool:
        """Le même fichier (même empreinte) est déjà dans le paquet d'une autre version :
        copié plutôt que téléchargé de nouveau. Le moteur llama.cpp, épinglé, reste le même
        d'une version à l'autre. Faux s'il n'y en a pas, ou si la copie échoue."""
        for other in self.package_versions():
            if other == version:
                continue
            found = self.manifest(other)
            twin = next((f for f in (found or {}).get("files") or []
                         if f.get("sha256") == item["sha256"] and f.get("size") == item["size"]),
                        None)
            if twin is None or not self.has(other, twin):
                continue
            try:
                shutil.copyfile(self.package_path(other, twin), self.partial(version, item["name"]))
                self.finish_download(version, item)       # empreinte revérifiée
                return True
            except (OSError, UpdateError):
                continue
        return False

    def save_file(self, version: str, item: dict, source: Path):
        """Fichier fourni par l'outil de publication (déjà sur le disque)."""
        target = self._dir("packages", version) / item["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    # ------------------------------------------------------- versions installées
    def installed(self) -> list:
        try:
            folders = [p for p in self._dir("versions").iterdir() if p.is_dir()]
        except OSError:
            return []
        found = [p.name for p in folders if versions.valid(p.name) and (p / MARKER).is_file()]
        return sorted(found, key=versions.key)

    def prune(self, keep: set):
        """Ménage : versions installées hors de `keep`, installations interrompues,
        paquets anciens (on garde les KEEP_PACKAGES plus récents et ceux de `keep`),
        téléchargements d'une version déjà dépassée (hors `keep`)."""
        try:
            folders = list(self._dir("versions").iterdir())
        except OSError:
            folders = []
        for folder in folders:
            if folder.is_dir() and folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)
        complete = [v for v in self.package_versions() if self.complete(v)]
        newest = complete[-1] if complete else None
        for old in self.package_versions():
            if old in complete[-KEEP_PACKAGES:] or old in keep:
                continue
            if versions.newer(old, newest):
                continue                  # manifeste d'un paquet en cours de téléchargement
            shutil.rmtree(self._dir("packages", old), ignore_errors=True)
        try:
            pending = list(self._dir("downloads").iterdir())
        except OSError:
            pending = []
        for folder in pending:
            if not versions.newer(folder.name, newest) and folder.name not in keep:
                shutil.rmtree(folder, ignore_errors=True)
