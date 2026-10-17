"""Lecture des livres EPUB, avec Python seul.

Un EPUB est une archive zip. META-INF/container.xml désigne le fichier OPF, qui liste
les documents du livre (manifest) et leur ordre de lecture (spine). On lit les
documents XHTML dans cet ordre, et on n'en garde que le texte (web.html_to_text).
Ce lecteur remplace EbookLib, sous licence AGPL, pour qu'AIODOS et ses dépendances
restent sous licences permissives (Apache-2.0, MIT, BSD).
"""
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from aiodos.services.web import html_to_text

MAX_BYTES = 100 * 1024 * 1024        # texte décompressé lu au plus : contre les archives piégées
_HTML = (".xhtml", ".html", ".htm")


def _local(tag) -> str:
    """Nom d'une balise sans son espace de noms : « {…/opf}item » → « item »."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _opf_path(z: zipfile.ZipFile) -> str | None:
    try:
        root = ET.fromstring(z.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError):
        return None
    for element in root.iter():
        if _local(element.tag) == "rootfile" and element.get("full-path"):
            return element.get("full-path")
    return None


def reading_order(z: zipfile.ZipFile) -> list:
    """Documents du livre dans l'ordre de lecture. Sans OPF lisible : tous les documents
    HTML de l'archive, dans l'ordre où elle les range."""
    opf = _opf_path(z)
    order = []
    if opf:
        try:
            root = ET.fromstring(z.read(opf))
        except (KeyError, ET.ParseError):
            root = None
        if root is not None:
            base = posixpath.dirname(opf)
            manifest = {}
            for element in root.iter():
                if _local(element.tag) == "item" and element.get("id") and element.get("href"):
                    path = posixpath.normpath(posixpath.join(base, unquote(element.get("href"))))
                    manifest[element.get("id")] = (path, element.get("media-type") or "")
            for element in root.iter():
                if _local(element.tag) == "itemref":
                    path, kind = manifest.get(element.get("idref"), (None, ""))
                    if path and ("html" in kind or path.lower().endswith(_HTML)):
                        order.append(path)
    if not order:
        order = [name for name in z.namelist() if name.lower().endswith(_HTML)]
    return order


def read_text(path, pace=None) -> str:
    """Le texte du livre, chapitre après chapitre ; pace(secondes) entre deux chapitres
    (cadence du travail de fond, cf. extract.paced)."""
    from aiodos.services.extract import paced
    parts, total = [], 0
    with zipfile.ZipFile(path) as z:
        present = set(z.namelist())
        for name in paced(reading_order(z), pace):
            if name not in present:
                continue
            total += z.getinfo(name).file_size
            if total > MAX_BYTES:
                break
            text = html_to_text(z.read(name).decode("utf-8", "ignore"))[1]
            if text.strip():
                parts.append(text)
    return "\n".join(parts)
