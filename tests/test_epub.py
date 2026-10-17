"""Livres EPUB lus avec Python seul (services/epub.py) : ordre de lecture du livre,
liens encodés, archive sans sommaire, et rien d'autre que le texte."""
import zipfile

from aiodos.services import epub, extract

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="un" href="chapitre%201.xhtml" media-type="application/xhtml+xml"/>
    <item id="deux" href="texte/chapitre2.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="couv" href="couverture.jpg" media-type="image/jpeg"/>
  </manifest>
  <spine><itemref idref="deux"/><itemref idref="un"/></spine>
</package>"""


def chapter(title, text):
    return (f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title>'
            f'<style>p {{ color: red }}</style></head><body><h1>{title}</h1><p>{text}</p>'
            '<script>alert(1)</script></body></html>')


def book(path, opf=OPF, container=CONTAINER):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        if container is not None:
            z.writestr("META-INF/container.xml", container)
        if opf is not None:
            z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/chapitre 1.xhtml", chapter("Premier", "Le crachin breton."))
        z.writestr("OEBPS/texte/chapitre2.xhtml", chapter("Second", "Le café est chaud."))
        z.writestr("OEBPS/style.css", "p { color: blue }")
    return path


def test_a_book_is_read_in_its_reading_order(tmp_path):
    text = epub.read_text(book(tmp_path / "livre.epub"))
    assert text.index("Le café est chaud.") < text.index("Le crachin breton.")   # ordre du sommaire
    assert "color" not in text and "alert" not in text                          # ni style, ni script


def test_a_book_without_its_index_is_still_read(tmp_path):
    text = epub.read_text(book(tmp_path / "sans.epub", opf=None, container=None))
    assert "Le crachin breton." in text and "Le café est chaud." in text
    text = epub.read_text(book(tmp_path / "abime.epub", opf="<package><oups"))
    assert "Le crachin breton." in text


def test_epub_needs_no_extra_package(tmp_path):
    assert ".epub" in extract.readable_documents()
    assert "Le crachin breton." in extract.extract_text(book(tmp_path / "livre.epub"))
