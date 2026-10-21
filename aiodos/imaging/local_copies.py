"""Fichiers de modèles d'images déjà sur ce PC, ailleurs que dans le dossier d'AIODOS.

ComfyUI (Comfy Desktop) range ses modèles dans un dossier partagé, que PANDORA utilise
aussi ; le projet minimax-h3-local garde les siens dans son dossier models ; un autre
AIODOS (une copie de développement, une ancienne installation) dans data/images/models.
Un fichier du catalogue qu'on y trouve, de même nom et de même taille, est repris au
lieu d'être téléchargé, une fois son empreinte vérifiée : par un lien physique sur le
même disque (instantané, aucune place en plus) quand personne d'autre ne peut le
modifier, par une copie sinon.

Un lien physique n'est pas une copie : c'est le même fichier sous deux noms, avec son
contenu et ses droits d'accès (ceux du fichier, pas de son dossier). Qui peut écrire dans
la source modifie donc le fichier d'AIODOS, puis remet sa date : AIODOS le croirait
toujours vérifié (seules sa taille et sa date sont revues, catalog.file_state) et
sd-server le chargerait. Le lien n'est donc gardé que s'il désigne un fichier privé
(_private) : personne d'autre que l'utilisateur, le système et les administrateurs ne
peut le modifier (updates/private.foreign_writers, qui lit les droits du fichier lui-même :
un modèle déplacé depuis C:\\models garde ceux de là-bas). Les droits sont relus sur le
lien fait, pas seulement sur la source avant : entre les deux, qui peut renommer dans le
dossier d'origine aurait pu y mettre son propre fichier. Une source d'un dossier commun
(C:\\models, un ComfyUI portable décompressé à la racine du disque, un dossier d'un autre
compte…) est copiée : la copie prend les droits du dossier d'AIODOS, et la première fois
il faut la place de ces quelques Go. Hors Windows, foreign_writers ne lit que les bits de
mode : un fichier qui porte en plus une liste de droits (ACL, setfacl) n'est pas jugé
privé, et là où on ne peut pas le savoir (macOS), la source est copiée, jamais reliée.

Avant chaque lancement de sd-server (detach), un fichier du modèle que d'autres comptes
peuvent modifier est remplacé par une copie vérifiée : un lien fait ainsi par une version
d'avant le 20/10/2026, même si l'autre nom a disparu depuis (son auteur a pu l'effacer
après avoir réécrit le fichier), ou un fichier déplacé depuis un dossier commun. Si la
copie est impossible, sd-server n'est pas lancé. Un dossier de modèles lui-même commun
(réglage images_models_dir) n'y gagnerait rien : c'est seulement dit au journal. Une copie
ne lit jamais plus que la taille attendue : qui peut encore écrire l'ancien fichier
pourrait sinon l'allonger sans fin pendant qu'elle se fait, et remplir le disque.

Dossiers en plus : réglage images_other_dirs, un par ligne (ou séparés par « ; »).

L'empreinte vérifiée est celle du fichier repris (après le lien ou la copie), pas de la
source avant : un fichier modifié pendant la reprise ne passe pas. Une copie à
l'empreinte différente n'est plus proposée ni rehachée tant qu'elle ne change pas ; une
source illisible (fichier verrouillé, disque plein, OneDrive pas encore téléchargé)
laisse le téléchargement continuer normalement.
"""
import errno
import hashlib
import logging
import os
import re
import stat
import sys
from pathlib import Path

from aiodos import settings
from aiodos.imaging import catalog
from aiodos.updates import private

log = logging.getLogger("aiodos.images")

DEPTH = 3                        # le dossier, puis deux niveaux : ComfyUI range models/vae/…
_rejected = {}                   # (chemin, taille, date) → empreinte attendue, qui ne correspondait pas
_common = set()                  # dossiers de modèles communs, déjà signalés au journal
# Windows : private.foreign_writers lit la liste des droits du fichier en entier ; ailleurs,
# seulement les bits de mode (_extended_acl)
_RIGHTS_READ_WHOLE = sys.platform == "win32"
_ACL_ATTRIBUTES = ("system.posix_acl_access", "system.nfs4_acl", "system.richacl")
_BLOCK = 8 << 20


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
        if hit and not _known_bad(hit[0], spec):
            found.append({"name": spec["name"], "size": spec["size"], "from": hit[1]})
    return found


def _stamp(path: Path) -> tuple:
    info = path.stat()
    return str(path), info.st_size, info.st_mtime_ns


def _known_bad(path: Path, spec: dict) -> bool:
    """Déjà hachée, avec une autre empreinte, et pas changée depuis."""
    try:
        return _rejected.get(_stamp(path)) == spec["sha256"]
    except OSError:
        return True


def _private(path: Path) -> bool:
    """Seul l'utilisateur (le système et les administrateurs mis à part) peut-il modifier
    ce fichier ? Alors seulement un lien physique, qui partage son contenu et ses droits,
    est gardé (_linkable). Droits illisibles, ou ACL vue hors Windows : non (le fichier est
    alors copié)."""
    try:
        others = private.foreign_writers(path)
    except Exception:                           # OSError, ou appel système refusé
        return False
    if not others and _extended_acl(path):
        others = ["liste de droits (ACL) du fichier"]
    if others:
        log.info("%s est modifiable par d'autres comptes de ce PC (%s)", path, ", ".join(others))
    return not others


def _extended_acl(path: Path):
    """Hors Windows, une liste de droits (setfacl, ACL NFSv4) peut ouvrir l'écriture à un
    autre compte sans que les bits de mode, seuls lus par private.foreign_writers, le
    montrent. Vrai si le fichier en porte une (Linux, Android) ; faux s'il n'en porte pas,
    ou sous Windows (la liste des droits y est lue en entier) ; None si ce système ne le
    dit pas (macOS : pas d'os.listxattr)."""
    if _RIGHTS_READ_WHOLE:
        return False
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        return None
    try:
        names = listxattr(path)
    except OSError as exc:
        # Système de fichiers sans attributs étendus : pas d'ACL non plus
        return False if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP) else None
    return any(name in _ACL_ATTRIBUTES for name in names)


def _linkable(path: Path) -> bool:
    """Un lien physique vers ce fichier peut-il être gardé ? Il doit être privé, et ses
    droits lisibles en entier : là où une ACL ne se voit pas (macOS), il est copié."""
    return _private(path) and _extended_acl(path) is False


def _private_folder(folder: Path) -> bool:
    """Une copie faite dans ce dossier serait-elle privée ? Elle prend ses droits. Un
    dossier de modèles commun (réglage images_models_dir vers C:\\models…) est signalé au
    journal une fois : copier n'y changerait rien, et seules la taille et la date de ses
    fichiers sont revues avant de les charger."""
    if str(folder) in _common:
        return False
    try:
        others = private.foreign_writers(folder)
    except Exception:
        others = ["droits illisibles"]
    if others:
        _say_common(folder, ", ".join(others))
    return not others


def _say_common(folder: Path, who: str):
    _common.add(str(folder))
    log.warning("Le dossier des modèles d'images %s est modifiable par d'autres comptes de ce "
                "PC (%s) : ils peuvent y changer un modèle sans qu'AIODOS le voie. Choisis "
                "plutôt un dossier à toi (réglage « Dossier des modèles d'images »).",
                folder, who)


def _discard(part: Path):
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass


def _readable_size(size: int) -> str:
    if size < 1e9:
        return f"{max(1, round(size / 1e6))} Mo"
    return f"{size / 1e9:.1f}".replace(".", ",") + " Go"


class _Grown(Exception):
    """Le fichier est plus long que prévu : il change pendant qu'on le copie."""


def _copy(source: Path, part: Path, stop=None, digest=None, limit=None) -> bool:
    """Copie interruptible (faux : arrêt demandé) ; `digest` (hashlib) reçoit au passage
    ce qui est écrit. Au plus `limit` octets : _Grown au-delà, sans lire plus loin (qui peut
    écrire la source l'allongerait sans fin, et la copie remplirait le disque)."""
    copied = 0
    with open(source, "rb") as src, open(part, "wb") as dst:
        while True:
            block = src.read(_BLOCK if limit is None else min(_BLOCK, limit + 1 - copied))
            if not block:
                return True
            if stop is not None and stop.is_set():
                return False
            copied += len(block)
            if limit is not None and copied > limit:
                raise _Grown()
            if digest is not None:
                digest.update(block)
            dst.write(block)


def _no_space(exc: OSError) -> bool:
    return exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))


def _replace_read_only(part: Path, path: Path):
    """os.replace, même sur un fichier qui porte l'attribut lecture seule de Windows (porté
    par le fichier, donc par ses deux noms, et que l'autre compte peut poser) : il refuse
    d'être remplacé ; l'attribut est retiré, puis un seul nouvel essai."""
    try:
        os.replace(part, path)
        return
    except PermissionError:
        try:
            attributes = getattr(os.stat(path), "st_file_attributes", 0)
        except OSError:
            attributes = 0
        if not attributes & stat.FILE_ATTRIBUTE_READONLY:
            raise
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    try:
        os.replace(part, path)
    except OSError:                             # tenu ouvert en plus : remis tel quel
        try:
            os.chmod(path, stat.S_IREAD)
        except OSError:
            pass
        raise
    log.info("%s : attribut lecture seule retiré pour le remplacer par sa copie vérifiée",
             path.name)


def detach(spec: dict, path: Path, stop=None) -> bool:
    """Avant le lancement de sd-server (imaging/engine.acquire) : un fichier du modèle que
    d'autres comptes peuvent modifier est remplacé par une copie, vérifiée pendant qu'elle
    se fait ; elle prend les droits du dossier d'AIODOS. Vrai s'il l'a été.

    La décision suit les droits du fichier, pas son nombre de noms : un lien d'une version
    d'avant le 20/10/2026 dont l'autre nom a été effacé (après avoir réécrit le fichier et
    remis sa date) n'a plus qu'un nom, mais il garde les droits ouverts de son dossier
    d'origine, et un modèle déplacé depuis un dossier commun aussi.

    ModelError, et sd-server n'est pas lancé : si le fichier ne correspond plus à son
    empreinte ou à sa taille (il est noté à refaire, rien n'est effacé), ou si la copie
    ou le remplacement est impossible (disque plein, que l'autre compte peut remplir ;
    fichier tenu ouvert) : le hacher puis le charger laisserait le temps de le réécrire
    entre les deux. Arrêt demandé : faux, rien n'est lancé. Dossier des modèles lui-même
    commun : rien à faire ici (_private_folder)."""
    if _private(path) or not _private_folder(path.parent):
        return False
    part = path.with_name(path.name + ".copie")
    digest = hashlib.sha256()
    try:
        part.unlink(missing_ok=True)            # copie d'un lancement interrompu
        if not _copy(path, part, stop, digest, limit=spec["size"]):
            _discard(part)
            return False
        found = digest.hexdigest()
    except _Grown:
        found = "allongé pendant la copie"      # pas une empreinte : « à refaire »
    except OSError as exc:
        _discard(part)
        log.warning("%s : copie impossible (%s), sd-server n'est pas lancé", spec["name"], exc)
        if _no_space(exc):
            advice = "la place manque. Libère de la place sur le disque d'AIODOS, puis réessaie."
        else:
            advice = ("la copie a échoué. Réessaie dans un moment ; si cela dure, supprime ce "
                      "fichier du dossier des modèles d'images, puis télécharge de nouveau le "
                      "modèle (vue Images).")
        raise catalog.ModelError(
            f"Le fichier « {spec['name']} » ({_readable_size(spec['size'])}) est modifiable par "
            f"d'autres comptes de ce PC : il doit être copié avant d'être chargé, et {advice}"
        ) from None
    if found != spec["sha256"]:
        _discard(part)
        catalog.record(path, found)             # « à refaire » dans la vue Images
        raise catalog.ModelError(
            f"Le fichier « {spec['name']} » a changé depuis sa vérification : d'autres "
            "comptes de ce PC peuvent le modifier. Télécharge-le de nouveau (vue Images).")
    if not _private(part):
        # Les copies faites là restent ouvertes (droits par défaut d'un système Unix dont
        # le groupe a d'autres membres…) : la refaire à chaque lancement ne servirait à rien
        _discard(part)
        _say_common(path.parent, "les fichiers qu'on y crée")
        return False
    try:
        _replace_read_only(part, path)          # un lien est rompu, l'autre nom reste
    except OSError as exc:                      # tenu ouvert par un autre programme
        _discard(part)
        log.warning("%s : remplacement impossible (%s), sd-server n'est pas lancé",
                    spec["name"], exc)
        raise catalog.ModelError(
            f"Le fichier « {spec['name']} » est modifiable par d'autres comptes de ce PC, et "
            "un autre programme le tient ouvert : il n'a pas pu être remplacé par sa copie "
            "vérifiée. Ferme ce programme puis réessaie ; si cela dure, supprime ce fichier "
            "du dossier des modèles d'images, puis télécharge de nouveau le modèle (vue "
            "Images).") from None
    catalog.record(path, found)
    log.info("%s : fichier modifiable par d'autres comptes remplacé par une copie vérifiée",
             spec["name"])
    return True


def bring(spec: dict, target: Path, stop=None):
    """Reprend la copie trouvée sur ce PC si son empreinte est la bonne : rend le nom de son
    dossier, ou None (rien trouvé, empreinte différente, ou arrêt demandé)."""
    hit = find(spec)
    if hit is None or _known_bad(hit[0], spec):
        return None
    source, name = hit
    part = target.with_name(target.name + ".part")
    try:
        stamp = _stamp(source)
        part.unlink(missing_ok=True)            # téléchargement entamé : inutile désormais
        linked = False
        if _linkable(source):
            try:
                os.link(source, part)           # même disque : aucune place en plus
                linked = True
            except OSError:
                pass
            # Le lien désigne désormais un fichier fixe : ses droits relus là, plus de
            # course. Entre les deux, qui peut renommer dans le dossier d'origine a pu
            # mettre le sien à la place de la source.
            if linked and not _linkable(part):
                part.unlink()
                linked = False
        else:
            log.info("%s copié depuis %s plutôt que relié : d'autres comptes de ce PC "
                     "peuvent le modifier, ou ses droits ne se lisent pas en entier ici",
                     spec["name"], name)
        try:
            if not linked and not _copy(source, part, stop, limit=spec["size"]):
                part.unlink(missing_ok=True)
                return None
        except _Grown:                          # la source change en ce moment : téléchargé
            part.unlink(missing_ok=True)
            return None
        digest = catalog.sha256_of(part, stop)  # le fichier repris lui-même
        if digest != spec["sha256"]:
            part.unlink(missing_ok=True)
            if digest is not None:              # pas un arrêt : une autre version du fichier
                _rejected[stamp] = spec["sha256"]
            return None
        os.replace(part, target)
        return name
    except OSError as exc:
        log.info("Copie de %s sur ce PC inutilisable (%s) : téléchargement", spec["name"], exc)
        part.unlink(missing_ok=True)
        return None
