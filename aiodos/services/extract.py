"""Extraction de texte depuis tout type de fichier (repris et étendu de la v1).

classify() range un fichier dans une famille ; extract_text() en tire le texte.
Images et audio/vidéo passent par services.media (vision / Whisper), seulement
si l'appelant l'autorise : ce sont des traitements lents.
"""
import re
import zipfile
from pathlib import Path

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".json", ".js", ".mjs", ".ts",
    ".tsx", ".jsx", ".css", ".scss", ".csv", ".tsv", ".log", ".xml", ".yaml",
    ".yml", ".ini", ".cfg", ".toml", ".c", ".cpp", ".h", ".hpp", ".java",
    ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql", ".env",
    ".tex", ".srt", ".vtt", ".lua", ".kt", ".swift", ".r", ".m", ".vue",
}
DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".rtf", ".epub",
           ".html", ".htm", ".odt"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
             ".webp", ".heic"}
MEDIA_EXT = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus",
             ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv"}
BINARY_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".exe",
              ".dll", ".msi", ".bin", ".so", ".dylib", ".db", ".sqlite",
              ".psd", ".ai", ".eps", ".class", ".pyc", ".pyd", ".iso", ".ico",
              ".ttf", ".otf", ".woff", ".woff2", ".blend", ".fbx", ".obj",
              ".drp", ".dra", ".lnk"}

MAX_TEXT_BYTES = 100 * 1024 * 1024     # au-delà, un document n'est pas lu


def classify(path) -> str:
    ext = Path(path).suffix.lower()
    if ext in TEXT_EXT:
        return "text"
    if ext in DOC_EXT:
        return "document"
    if ext in IMAGE_EXT:
        return "image"
    if ext in MEDIA_EXT:
        return "media"
    if ext in BINARY_EXT:
        return "binary"
    return "unknown"


def extract_text(path, vision=None, transcribe=None):
    """Texte du fichier, ou None s'il n'y a rien d'exploitable.

    vision     : callable(path) -> str pour les images (None = ignorer)
    transcribe : callable(path) -> str pour l'audio/vidéo (None = ignorer)
    """
    p = Path(path)
    family = classify(p)
    if family == "image":
        return vision(p) if vision else None
    if family == "media":
        return transcribe(p) if transcribe else None
    if family == "binary":
        return None
    if p.stat().st_size > MAX_TEXT_BYTES:
        return None
    if family == "document":
        return _extract_document(p)
    if family == "text":
        return p.read_text(encoding="utf-8", errors="ignore")
    return _text_fallback(p)


def _extract_document(p: Path):
    ext = p.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == ".docx":
        import docx
        doc = docx.Document(str(p))
        parts = [par.text for par in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    if ext == ".pptx":
        from pptx import Presentation
        parts = []
        for number, slide in enumerate(Presentation(str(p)).slides, 1):
            parts.append(f"# Diapositive {number}")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
        return "\n".join(parts)
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append(f"# Feuille : {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    if ext == ".rtf":
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(p.read_text(encoding="utf-8", errors="ignore"))
    if ext == ".epub":
        import ebooklib
        from ebooklib import epub
        from aiodos.services.web import html_to_text
        book = epub.read_epub(str(p))
        return "\n".join(
            html_to_text(item.get_content().decode("utf-8", "ignore"))[1]
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    if ext in (".html", ".htm"):
        from aiodos.services.web import html_to_text
        return html_to_text(p.read_text(encoding="utf-8", errors="ignore"))[1]
    if ext == ".odt":
        with zipfile.ZipFile(str(p)) as z:
            xml = z.read("content.xml").decode("utf-8", "ignore")
        return re.sub(r"<[^>]+>", " ", xml)
    return None


def _text_fallback(p: Path):
    """Extension inconnue : on la lit comme du texte si c'en est vraiment."""
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:                   # octet nul : fichier binaire
        return None
    for encoding in ("utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        sample = text[:4000]
        printable = sum(c.isprintable() or c in "\n\r\t " for c in sample)
        if sample and printable / len(sample) > 0.85:
            return text
    return None
