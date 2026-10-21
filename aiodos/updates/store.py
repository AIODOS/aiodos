"""Paquets reçus et versions installées, dans data/updates/ :

    packages/<version>/manifest.json     manifeste signé
    packages/<version>/<fichier>         fichiers complets, empreinte vérifiée
    downloads/<version>/<fichier>.part.<machine>
                                         téléchargements en cours, un par machine qui
                                         l'envoie, repris où ils en étaient quand elle revient
    versions/<version>/                  code installé (cf. install.py, launcher.py)
    bin/                                 compagnon livré, en service (cf. binaries.py)
    state.json                           cf. state.py

Un paquet complet (manifeste et archive du code) est gardé pour les machines appairées
qui ne l'ont pas encore : c'est ainsi qu'une version passe de machine en machine. Le
compagnon d'une plateforme s'y ajoute quand la machine est de cette plateforme ; une
machine transmet aussi les fichiers d'un paquet incomplet qu'elle a en entier.
"""
import glob
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from aiodos import paths
from aiodos.updates import manifest as manifests
from aiodos.updates import version as versions
from aiodos.updates.manifest import UpdateError

MARKER = ".installed.json"         # dans versions/<v>/ : installation menée à son terme
KEEP_PACKAGES = 2
KEEP_PARTIALS = 3                  # débuts d'un même fichier gardés, chez autant de machines
MAX_KEPT = 16 * 1024 ** 2          # manifest.json : sous forme canonique, quelques Ko


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_tag(source: str) -> str:
    """La machine qui envoie, dans un nom de fichier : son identifiant, sans rien d'autre."""
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(source or ""))[:64] or "inconnue"


def _glob_escape(name: str) -> str:
    return glob.escape(name)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


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

    def add_manifest(self, signed: dict, trusted=None, approvers=None) -> dict:
        """Garde l'enveloppe signée, vérifiée, sous sa forme canonique (manifest.canonical :
        rien de ce que la machine qui l'a transmise y aurait ajouté), avec les approbations
        déjà gardées pour le même texte (manifest.merged) ; approvers={} : sans exiger
        d'approbation (version publiée ici, qui attend la sienne, cf. pending)."""
        found = manifests.verify(signed, trusted, approvers)
        kept = manifests.canonical(manifests.merged(signed, self.signed(found["version"])),
                                   approvers or None)
        folder = self._dir("packages", found["version"])
        folder.mkdir(parents=True, exist_ok=True)
        self._write_manifest(folder, kept)
        return found

    @staticmethod
    def _write_manifest(folder: Path, kept: dict):
        temporary = folder / f"manifest.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, folder / "manifest.json")

    def tidy_manifests(self) -> int:
        """Remet sous sa forme canonique (manifest.canonical) chaque manifest.json gardé
        autrement : une version plus ancienne le gardait tel que la machine qui l'a transmis
        l'avait gonflé (champs en plus, milliers d'approbations), et il est relu à chaque
        manifest(). Au-delà de MAX_KEPT, il n'est même pas lu : retiré, le paquet se
        redemandera. Rend le nombre de manifestes réécrits ou retirés."""
        changed = 0
        for version in self.package_versions():
            folder = self._dir("packages", version)
            path = folder / "manifest.json"
            try:
                if path.stat().st_size > MAX_KEPT:
                    path.unlink()
                    changed += 1
                    continue
                signed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(signed, dict):
                continue
            kept = manifests.canonical(signed)
            if kept != signed:
                try:
                    self._write_manifest(folder, kept)
                except OSError:
                    continue
                changed += 1
        return changed

    def pending(self, trusted=None, approvers=None) -> list:
        """[(version, enveloppe)] des paquets signés par l'auteur mais pas encore approuvés
        par un appareil d'approbation : ni installés, ni proposés (approval.py)."""
        approvers = manifests.declared_approvers() if approvers is None else approvers
        if not approvers:
            return []
        waiting = []
        for version in self.package_versions():
            signed = self.signed(version)
            try:
                manifests.verify(signed, trusted, approvers={})
            except UpdateError:
                continue
            if not manifests.approved(signed, approvers):
                waiting.append((version, signed))
        return waiting

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
    def partial(self, version: str, name: str, source: str | None = None) -> Path:
        """Le fichier en cours de téléchargement : un par machine qui l'envoie
        (<fichier>.part.<machine>), ou <fichier>.part pour une copie faite ici (reuse)."""
        folder = self._dir("downloads", version)
        folder.mkdir(parents=True, exist_ok=True)
        if source is None:
            return folder / f"{name}.part"
        return folder / f"{name}.part.{_source_tag(source)}"

    def partial_from(self, version: str, name: str, source: str) -> Path:
        """Le téléchargement en cours de ce fichier chez la machine `source`, repris là où
        il en était quand elle revient, même si d'autres ont envoyé une partie du fichier
        entre-temps : chacune a le sien (relecture finale du 21/10/2026 ; un seul fichier
        par nom, effacé à chaque changement de machine, ne s'achevait jamais tant qu'aucune
        machine instable, téléphone en 4G ou relais, ne tenait d'un bout à l'autre). Les
        morceaux d'une machine ne se mêlent jamais à ceux d'une autre : si l'empreinte
        échoue, c'est bien celle qui a tout envoyé qui est laissée de côté. KEEP_PARTIALS
        au plus par fichier : les plus anciens des autres machines sont effacés."""
        part = self.partial(version, name, source)
        others = [p for p in part.parent.glob(f"{_glob_escape(name)}.part*") if p != part]
        others.sort(key=_mtime, reverse=True)
        for old in others[KEEP_PARTIALS - 1:]:
            old.unlink(missing_ok=True)
        return part

    def finish_download(self, version: str, item: dict, source: str | None = None) -> Path:
        """Téléchargement terminé chez `source` : taille et empreinte vérifiées, puis rangé
        dans le paquet ; les débuts reçus d'autres machines ne servent plus. Fichier non
        conforme : effacé (cette machine repartira de zéro), les autres débuts restent."""
        part = self.partial(version, item["name"], source)
        if part.stat().st_size != item["size"] or sha256_of(part) != item["sha256"]:
            part.unlink(missing_ok=True)
            raise UpdateError("Fichier reçu incomplet ou altéré : il sera téléchargé de nouveau.")
        target = self._dir("packages", version) / item["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)
        for old in part.parent.glob(f"{_glob_escape(item['name'])}.part*"):
            old.unlink(missing_ok=True)       # y compris les repères d'une version d'avant
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
