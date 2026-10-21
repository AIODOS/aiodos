"""Lecture des livres EPUB, avec Python seul.

Un EPUB est une archive zip. META-INF/container.xml désigne le fichier OPF, qui liste
les documents du livre (manifest) et leur ordre de lecture (spine). On lit les
documents XHTML dans cet ordre, et on n'en garde que le texte (web.html_to_text).
Ce lecteur remplace EbookLib, sous licence AGPL, pour qu'AIODOS et ses dépendances
restent sous licences permissives (Apache-2.0, MIT, BSD).

Bornes (audit du 20/10/2026, relecture du 21/10/2026). Un chapitre est ce que le manifeste
déclare en HTML, sous n'importe quel nom (« c1.dat »), et html.parser le lit même s'il ne
commence pas par « < » : archives.check compte donc comme balisage toute entrée que le
manifeste déclare en HTML ou en XML (markup_entries), et la lecture elle-même saute un
chapitre de plus de archives.MAX_XML_PART, s'arrête à archives.MAX_XML_TOTAL pour tout le
livre, et ne relit pas un chapitre que l'ordre de lecture cite plusieurs fois. Mesuré
avant : un EPUB de 0,06 Mo dont le chapitre « c1.dat » faisait 60 Mio passait, puis sa
lecture prenait 130 s et 1,1 Gio de mémoire.
"""
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from aiodos.services.web import html_to_text

_HTML = (".xhtml", ".html", ".htm")
CONTAINER = "META-INF/container.xml"


def _local(tag) -> str:
    """Nom d'une balise sans son espace de noms : « {…/opf}item » → « item »."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parsed(z: zipfile.ZipFile, name: str):
    """La racine XML de cette entrée, None si elle manque, est illisible, ou dépasse
    archives.MAX_XML_PART (lue avant tout contrôle de l'archive, par archives.check)."""
    from aiodos.services import archives
    try:
        if z.getinfo(name).file_size > archives.MAX_XML_PART:
            return None
        return ET.fromstring(z.read(name))
    except (KeyError, ET.ParseError):
        return None


def _opf_path(z: zipfile.ZipFile) -> str | None:
    root = _parsed(z, CONTAINER)
    if root is None:
        return None
    for element in root.iter():
        if _local(element.tag) == "rootfile" and element.get("full-path"):
            return element.get("full-path")
    return None


def _manifest(z: zipfile.ZipFile):
    """(manifeste {id: (chemin, type)}, racine de l'OPF), ou (None, None) sans OPF lisible."""
    opf = _opf_path(z)
    root = _parsed(z, opf) if opf else None
    if root is None:
        return None, None
    base = posixpath.dirname(opf)
    manifest = {}
    for element in root.iter():
        if _local(element.tag) == "item" and element.get("id") and element.get("href"):
            path = posixpath.normpath(posixpath.join(base, unquote(element.get("href"))))
            manifest[element.get("id")] = (path, element.get("media-type") or "")
    return manifest, root


def markup_entries(z: zipfile.ZipFile) -> set:
    """Les entrées que le manifeste déclare en HTML ou en XML : un lecteur les analyse,
    quel que soit leur nom ou leur début (cf. archives.check)."""
    manifest, _root = _manifest(z)
    return {path for path, kind in (manifest or {}).values()
            if "html" in kind.lower() or "xml" in kind.lower()}


def reading_order(z: zipfile.ZipFile) -> list:
    """Documents du livre dans l'ordre de lecture, chacun une seule fois. Sans OPF lisible :
    tous les documents HTML de l'archive, dans l'ordre où elle les range."""
    manifest, root = _manifest(z)
    order = []
    if root is not None:
        for element in root.iter():
            if _local(element.tag) == "itemref":
                path, kind = manifest.get(element.get("idref"), (None, ""))
                if path and ("html" in kind or path.lower().endswith(_HTML)):
                    order.append(path)
    if not order:
        order = [name for name in z.namelist() if name.lower().endswith(_HTML)]
    return list(dict.fromkeys(order))


def read_text(path, pace=None) -> str:
    """Le texte du livre, chapitre après chapitre ; pace(secondes) entre deux chapitres
    (cadence du travail de fond, cf. extract.paced). Un chapitre de plus de
    archives.MAX_XML_PART est sauté, et la lecture s'arrête à archives.MAX_XML_TOTAL."""
    from aiodos.services import archives
    from aiodos.services.extract import paced
    parts, total = [], 0
    with zipfile.ZipFile(path) as z:
        present = set(z.namelist())
        for name in paced(reading_order(z), pace):
            if name not in present:
                continue
            size = z.getinfo(name).file_size
            if size > archives.MAX_XML_PART:
                continue
            total += size
            if total > archives.MAX_XML_TOTAL:
                break
            text = html_to_text(z.read(name).decode("utf-8", "ignore"))[1]
            if text.strip():
                parts.append(text)
    return "\n".join(parts)
