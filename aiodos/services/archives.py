"""Bornes des documents qui sont des archives zip (docx, pptx, xlsx, xlsm, odt, epub).

Un petit fichier zip peut se décompresser en plusieurs gigaoctets, ou annoncer des
millions d'entrées : la limite de taille du fichier (extract.MAX_TEXT_BYTES) ne dit rien
de ce qu'il devient une fois ouvert. Avant tout lecteur (python-docx, python-pptx,
openpyxl, OpenDocument, EPUB), on regarde donc ce que l'archive annonce :
- la taille de son répertoire central et son nombre d'entrées, lus dans l'enregistrement
  de fin d'archive, avant que zipfile ne crée un objet par entrée ;
- la somme des tailles décompressées annoncées. zipfile ne rend jamais plus que la
  taille annoncée d'une entrée (il coupe à file_size), donc borner les annonces borne ce
  que les lecteurs décompressent ;
- la taille de chaque partie qu'un analyseur XML accepterait de lire, et de toutes
  ensemble, bien plus bas que le total : python-docx et python-pptx en font un arbre lxml,
  qui occupe environ 33 fois la taille du XML quand il est fait de minuscules éléments
  (mesuré : 16 Mo de « <a/> » donnent 530 Mo). Ces parties se reconnaissent à leur début
  (« < », cf. _parsed_as_xml), et pas seulement à leur nom : les lecteurs les choisissent
  d'après [Content_Types].xml ou les relations (.rels), sous n'importe quel nom (relecture
  du 21/10/2026 : un document.xml renommé document.dat passait la borne par nom). Les
  images, sons et polices d'une présentation, eux, ne sont pas analysés : seul le total
  les borne.
Le document est alors refusé (rien d'exploitable), sans le décompresser.

Deux cas à part (relecture finale du 21/10/2026) :
- un EPUB : ses chapitres sont ce que le manifeste de son OPF déclare en HTML, sous
  n'importe quel nom, et html.parser lit un chapitre qui ne commence pas par « < » ; toute
  entrée que le manifeste déclare en HTML ou en XML compte donc comme balisage
  (epub.markup_entries) ;
- les feuilles d'un classeur (xlsx, xlsm) : openpyxl, en lecture seule, les lit en flux
  (iterparse) et vide chaque ligne une fois lue. Une feuille ordinaire de 60 000 lignes,
  soit 29 Mo de XML, se lit avec quelques Mo : la borne d'une partie XML refusait des
  tableurs ordinaires. Une feuille (_sheets : la cible d'une relation « worksheet » du
  classeur, que rien d'autre ne désigne) n'est donc bornée que par le total décompressé,
  mais par sa forme aussi, vérifiée en flux par expat sans rien garder (_sheet_refusal) :
  ce qu'openpyxl garde jusqu'à la fin (les éléments hors des lignes, MAX_SHEET_OUTSIDE)
  et jusqu'à la fin d'une ligne (MAX_ROW_ELEMENTS), en la lisant comme en y cherchant ses
  dimensions (il vide de même chaque élément lu). Mesuré : une feuille de 7,6 Mo faite
  d'une seule ligne de 2 millions de cellules vides occupait 583 Mo ; la feuille ordinaire
  de 60 000 lignes, 5 Mo, avec ou sans dimensions. Et un numéro de ligne au-delà de ceux
  d'Excel (EXCEL_ROWS) la fait refuser : openpyxl rendrait une ligne vide pour chaque
  numéro sauté, des milliards pour quelques octets.
"""
import logging
import posixpath
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from xml.parsers import expat

log = logging.getLogger("aiodos.extract")

ZIP_DOCUMENTS = {".docx", ".pptx", ".xlsx", ".xlsm", ".odt", ".epub"}
MAX_ENTRIES = 10_000                 # fichiers dans l'archive (un gros EPUB en compte ~2 000)
MAX_DIRECTORY = 4 * 1024 * 1024      # répertoire central (~130 octets par entrée)
# Décompressé au plus : le double de la limite d'un fichier, car les images d'une
# présentation, déjà compressées, gardent leur taille
MAX_UNZIPPED = 200 * 1024 * 1024
# Parties lisibles comme du XML ou du HTML : 16 Mo chacune (un document.xml de 500 pages
# en fait 5 à 8), 40 Mo en tout (plus d'un Go en mémoire au pire, pas plusieurs)
MAX_XML_PART = 16 * 1024 * 1024
MAX_XML_TOTAL = 40 * 1024 * 1024
HEAD_BYTES = 1024                    # début de chaque partie lu pour la reconnaître
# Noms des parties de balisage : comptées aussi, quel que soit leur début (un chapitre
# d'EPUB est lu par html.parser, qui n'exige pas de « < » au début)
XML_PARTS = (".xml", ".rels", ".opf", ".ncx", ".xhtml", ".html", ".htm")
# Méthodes de compression d'un document : aucune, ou deflate. bzip2, lzma, zstandard…
# n'en sont jamais (Word, LibreOffice ne les ouvrent pas)
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

# Classeurs : ce qu'openpyxl garde en lisant une feuille en flux (cf. l'en-tête du module).
# Hors des lignes, chaque élément reste en mémoire jusqu'à la fin de la feuille (une ligne
# lue y reste aussi, vidée) : Excel a au plus 1 048 576 lignes. Dans une ligne, tout reste
# jusqu'à sa fin : 16 384 colonnes, de quelques éléments chacune (c, v, f, is…).
SPREADSHEETS = (".xlsx", ".xlsm")
MAX_SHEET_OUTSIDE = 1_200_000
MAX_ROW_ELEMENTS = 200_000
EXCEL_ROWS = 1_048_576                       # dernière ligne d'une feuille d'Excel
_SHEET_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_ROW = f"{_SHEET_MAIN} row"                  # nom d'une ligne pour expat (espace de noms, nom)
_WORKSHEET_REL = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
                  "worksheet")
_WORKSHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_CONTENT_TYPES = "[Content_Types].xml"
# Le classeur, tel qu'openpyxl le cherche dans [Content_Types].xml (reader/excel.py :
# _find_workbook_part), dans cet ordre ; sinon xl/workbook.xml si un type par défaut le dit
_WORKBOOK_TYPES = tuple(f"application/vnd.{kind}.main+xml" for kind in (
    "ms-excel.template.macroEnabled", "openxmlformats-officedocument.spreadsheetml.template",
    "ms-excel.sheet.macroEnabled", "openxmlformats-officedocument.spreadsheetml.sheet"))
# Lus d'office par openpyxl, en arbre, sous ces noms : jamais lus comme une feuille en flux
_READ_WHOLE = frozenset({_CONTENT_TYPES, "docProps/core.xml", "docProps/custom.xml",
                         "docProps/app.xml", "xl/styles.xml", "xl/workbook.xml"})
_PIECE = 64 * 1024
_ENCRYPTED = 0x1                     # bit 0 des drapeaux d'une entrée zip

# Enregistrement de fin d'archive, tel que le décrit la norme zip (APPNOTE 4.3.16)
_END = b"PK\x05\x06"
_END_SIZE = 22
_END_ZIP64_LOCATOR = b"PK\x06\x07"
_LOCATOR_SIZE = 20
_MAX_COMMENT = 0xFFFF

# Avant le premier « < » d'un XML : marques d'ordre des octets (UTF-8, UTF-16, UTF-32),
# blancs, et octets nuls (un « < » en UTF-16 ou en UTF-32 en est entouré)
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")
_BLANKS = b" \t\r\n\x00"
_EBCDIC_DECLARATION = b"\x4c\x6f\xa7\x94"       # « <?xm » en EBCDIC, que libxml2 reconnaît


class ArchiveRefused(Exception):
    """Archive refusée, avec la raison."""


def _size(n) -> str:
    """Une taille lisible : « 850 Ko », « 16,5 Mo »."""
    if n < 1024:
        return f"{n} octets"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} Ko"
    return f"{n / (1024 * 1024):.1f}".replace(".", ",").removesuffix(",0") + " Mo"


def _with_zipfile(f):
    """(entrées, taille du répertoire) lues comme zipfile les lit, par sa propre fonction :
    la borne porte ainsi sur ce que zipfile parcourra. None si ce n'est pas une archive."""
    end = zipfile._EndRecData(f)
    if not end:
        return None
    return end[zipfile._ECD_ENTRIES_TOTAL], end[zipfile._ECD_SIZE]


def _by_hand(f):
    """La même lecture, faite ici, si une version de Python retire ces fonctions privées :
    l'enregistrement sans commentaire en dernier, sinon le dernier trouvé dans les 64 Ko de
    la fin, comme zipfile. Une archive zip64 (plus de 65 535 entrées ou 4 Go) n'est jamais
    un document : refusée, plutôt que de lire ses compteurs."""
    f.seek(0, 2)
    size = f.tell()
    if size < _END_SIZE:
        return None
    f.seek(size - _END_SIZE)
    record = f.read(_END_SIZE)
    start = size - _END_SIZE
    if not (record[:4] == _END and record[-2:] == b"\0\0"):
        tail_start = max(0, size - _MAX_COMMENT - _END_SIZE)
        f.seek(tail_start)
        tail = f.read()
        found = tail.rfind(_END)
        if found < 0 or len(tail) - found < _END_SIZE:
            return None
        record, start = tail[found:found + _END_SIZE], tail_start + found
    if start >= _LOCATOR_SIZE:
        f.seek(start - _LOCATOR_SIZE)
        if f.read(4) == _END_ZIP64_LOCATOR:
            raise ArchiveRefused("archive zip64 : bien trop grande pour un document")
    _, _, _, _, entries, directory, _, _ = struct.unpack("<4s4H2LH", record)
    return entries, directory


def _end_record(path):
    """(entrées, taille du répertoire central) annoncées par l'archive, None si ce n'est
    pas une archive zip."""
    with open(path, "rb") as f:
        try:
            return _with_zipfile(f)
        except (AttributeError, TypeError, IndexError):
            return _by_hand(f)               # fonctions privées de zipfile changées


def _parsed_as_xml(head: bytes) -> bool:
    """Un analyseur XML (lxml, expat) accepterait-il de lire une partie qui commence ainsi ?
    Seulement si, passé les marques d'ordre des octets, les blancs et les octets nuls,
    elle commence par « < » (UTF-8, UTF-16, UTF-32), ou par la déclaration XML en EBCDIC.
    Une image, un son, une police, un objet incorporé ou une archive ne commencent pas
    ainsi. Un début tout blanc compte comme XML, par prudence."""
    if head.startswith(_EBCDIC_DECLARATION):
        return True
    rest = head
    while True:
        stripped = rest.lstrip(_BLANKS)
        for bom in _BOMS:
            if stripped.startswith(bom):
                stripped = stripped[len(bom):]
                break
        if stripped == rest:
            break
        rest = stripped
    return not rest or rest[:1] == b"<"


def _xml_parts(z, infos) -> list:
    """Les entrées qu'un lecteur pourrait analyser comme du XML ou du HTML : reconnues à
    leur début, quel que soit leur nom ou le type déclaré (cf. l'en-tête du module), et
    celles dont le nom le dit (XML_PARTS). Lire HEAD_BYTES d'une entrée ne décompresse que
    ce qu'il faut pour les rendre."""
    found = []
    for info in infos:
        if info.flag_bits & _ENCRYPTED:
            raise ArchiveRefused("archive chiffrée")
        if info.compress_type not in _METHODS:
            raise ArchiveRefused("méthode de compression inattendue pour un document")
        if info.file_size <= 0 or info.is_dir():
            continue
        if info.filename.lower().endswith(XML_PARTS):
            found.append(info)
            continue
        try:
            with z.open(info) as f:
                head = f.read(HEAD_BYTES)
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError, ValueError,
                NotImplementedError, RuntimeError) as exc:
            raise ArchiveRefused(f"archive zip abîmée ({exc.__class__.__name__})") from exc
        if _parsed_as_xml(head):
            found.append(info)
    return found


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _small_tree(z, names: set, name: str):
    """La racine XML d'une petite partie (au plus MAX_XML_PART), None sinon ou illisible."""
    if name not in names:
        return None
    try:
        if z.getinfo(name).file_size > MAX_XML_PART:
            return None
        return ET.fromstring(z.read(name))
    except (KeyError, ET.ParseError, zipfile.BadZipFile, zlib.error, OSError, EOFError,
            ValueError, NotImplementedError, RuntimeError):
        return None


def _workbook_part(types) -> str | None:
    """Le classeur, comme le cherche openpyxl : le premier type de classeur trouvé parmi
    les « Override » de [Content_Types].xml, sinon xl/workbook.xml si un type par défaut
    est celui d'un classeur."""
    overrides = [(e.get("PartName") or "", e.get("ContentType") or "")
                 for e in types.iter() if _local(e.tag) == "Override"]
    for kind in _WORKBOOK_TYPES:
        for part, declared in overrides:
            if declared == kind and part.startswith("/"):
                return part[1:]
    defaults = {e.get("ContentType") for e in types.iter() if _local(e.tag) == "Default"}
    return "xl/workbook.xml" if defaults & set(_WORKBOOK_TYPES) else None


def _sheets(z, names: set) -> set:
    """Les feuilles d'un classeur qu'openpyxl lit en flux : la cible d'une relation
    « worksheet » du classeur (résolue comme openpyxl, reader/excel.py et
    packaging/relationship.get_dependents), qu'aucune autre relation ne désigne, que
    [Content_Types].xml ne déclare pas sous un autre type (openpyxl y cherche ses chaînes
    partagées, qu'il garde en mémoire), et qui n'est pas une des parties qu'il lit d'office
    en arbre (_READ_WHOLE, .rels). Toute autre partie reste bornée comme du XML."""
    types = _small_tree(z, names, _CONTENT_TYPES)
    if types is None:
        return set()
    workbook = _workbook_part(types)
    if not workbook:
        return set()
    folder, base = posixpath.split(workbook)
    rels_path = posixpath.join(folder, "_rels", base + ".rels")
    rels = _small_tree(z, names, rels_path)
    if rels is None:
        return set()
    parent = posixpath.split(posixpath.dirname(rels_path))[0]
    targets = {}                               # cible -> types des relations qui la visent
    for element in rels.iter():
        if _local(element.tag) != "Relationship" or element.get("TargetMode") == "External":
            continue
        target = element.get("Target") or ""
        target = target[1:] if target.startswith("/") \
            else posixpath.normpath(posixpath.join(parent, target))
        targets.setdefault(target, []).append(element.get("Type") or "")
    declared = {}                              # partie -> types que lui donne [Content_Types]
    for element in types.iter():
        if _local(element.tag) == "Override":
            declared.setdefault((element.get("PartName") or "")[1:], set()).add(
                element.get("ContentType") or "")
    return {target for target, kinds in targets.items()
            if kinds == [_WORKSHEET_REL] and target in names
            and target not in _READ_WHOLE and target != workbook
            and not target.lower().endswith(".rels")
            and declared.get(target, {_WORKSHEET_TYPE}) == {_WORKSHEET_TYPE}}


class _Shape(Exception):
    """Une feuille dont la forme ferait garder trop d'éléments à openpyxl."""


def _row_number_ok(text) -> bool:
    """Un numéro de ligne (attribut r) qu'Excel connaît ; openpyxl lit aussi « 5.0 »."""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return False
    return 0 < number <= EXCEL_ROWS


def _sheet_refusal(z, info) -> str | None:
    """Pourquoi openpyxl ne doit pas lire cette feuille en flux, None si elle a la forme
    d'une feuille. Parcourue par expat, en flux, sans rien garder : combien d'éléments
    resteraient en mémoire jusqu'à la fin de la feuille (hors des lignes), et jusqu'à la fin
    de chaque ligne, et ses numéros de ligne (cf. l'en-tête du module)."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.ordered_attributes = True
    state = {"depth": 0, "row": None, "outside": 0, "in_row": 0}

    def start(name, attributes):
        state["depth"] += 1
        if state["row"] is not None:
            state["in_row"] += 1
            if state["in_row"] > MAX_ROW_ELEMENTS:
                raise _Shape(f"une ligne de plus de {MAX_ROW_ELEMENTS} éléments")
            return
        state["outside"] += 1
        if state["outside"] > MAX_SHEET_OUTSIDE:
            raise _Shape(f"plus de {MAX_SHEET_OUTSIDE} éléments hors des lignes")
        if name == _ROW:
            given = dict(zip(attributes[::2], attributes[1::2])).get("r")
            if given is not None and not _row_number_ok(given):
                raise _Shape(f"numéro de ligne hors de ceux d'Excel ({str(given)[:20]})")
            state["row"], state["in_row"] = state["depth"], 0

    def end(_name):
        if state["row"] == state["depth"]:
            state["row"] = None
        state["depth"] -= 1

    parser.StartElementHandler, parser.EndElementHandler = start, end
    try:
        with z.open(info) as f:
            while piece := f.read(_PIECE):
                parser.Parse(piece, False)
        parser.Parse(b"", True)
    except _Shape as exc:
        return f"feuille « {info.filename[:80]} » : {exc}"
    except expat.ExpatError:
        return f"feuille « {info.filename[:80]} » illisible"
    except (zipfile.BadZipFile, zlib.error, OSError, EOFError, ValueError,
            NotImplementedError, RuntimeError) as exc:
        raise ArchiveRefused(f"archive zip abîmée ({exc.__class__.__name__})") from exc
    return None


def _epub_markup(z, infos, xml) -> list:
    """Les parties de balisage d'un EPUB : en plus de celles reconnues à leur nom ou à leur
    début, toute entrée que son manifeste déclare en HTML ou en XML (le conteneur et l'OPF,
    lus pour cela, sont bornés à MAX_XML_PART, cf. epub._parsed)."""
    from aiodos.services import epub
    declared = epub.markup_entries(z)
    counted = {info.filename for info in xml}
    return xml + [info for info in infos if info.filename in declared
                  and info.filename not in counted and info.file_size > 0]


def check(path):
    """ArchiveRefused si l'archive est illisible, ou trop grosse une fois décompressée."""
    try:
        end = _end_record(path)
    except (OSError, zipfile.BadZipFile, struct.error) as exc:
        raise ArchiveRefused(f"archive illisible ({exc.__class__.__name__})") from exc
    if not end:
        raise ArchiveRefused("ce n'est pas une archive zip valide")
    entries, directory = end
    if entries > MAX_ENTRIES:
        raise ArchiveRefused(f"plus de {MAX_ENTRIES} fichiers dans l'archive")
    if directory > MAX_DIRECTORY:
        raise ArchiveRefused("répertoire de l'archive trop grand")
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ArchiveRefused(f"plus de {MAX_ENTRIES} fichiers dans l'archive")
            total = sum(max(0, info.file_size) for info in infos)
            if total > MAX_UNZIPPED:
                raise ArchiveRefused(f"{_size(total)} une fois décompressée "
                                     f"(au plus {_size(MAX_UNZIPPED)})")
            xml = _xml_parts(z, infos)
            suffix = str(path).lower()
            if suffix.endswith(".epub"):
                xml = _epub_markup(z, infos, xml)
            elif suffix.endswith(SPREADSHEETS):
                sheets = _sheets(z, {info.filename for info in infos})
                for info in infos:
                    if info.filename in sheets:
                        reason = _sheet_refusal(z, info)
                        if reason is not None:
                            raise ArchiveRefused(reason)
                xml = [info for info in xml if info.filename not in sheets]
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise ArchiveRefused("archive zip abîmée") from exc
    biggest = max(xml, key=lambda info: info.file_size, default=None)
    if biggest is not None and biggest.file_size > MAX_XML_PART:
        raise ArchiveRefused(f"partie « {biggest.filename[:80]} » de "
                             f"{_size(biggest.file_size)} une fois décompressée "
                             f"(au plus {_size(MAX_XML_PART)})")
    xml_total = sum(max(0, info.file_size) for info in xml)
    if xml_total > MAX_XML_TOTAL:
        raise ArchiveRefused(f"{_size(xml_total)} de texte XML une fois décompressée "
                             f"(au plus {_size(MAX_XML_TOTAL)})")


def refusal(path) -> str | None:
    """La raison pour laquelle cette archive n'est pas ouverte, None si elle peut l'être."""
    try:
        check(path)
        return None
    except ArchiveRefused as exc:
        return str(exc)


def readable(path) -> bool:
    """Vrai si l'archive peut être ouverte par un lecteur ; sinon la raison va au journal."""
    reason = refusal(path)
    if reason is not None:
        log.warning("Document non lu, %s : %s", reason, path)
    return reason is None
