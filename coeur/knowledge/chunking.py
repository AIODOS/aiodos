"""Découpage d'un texte en passages qui se chevauchent légèrement.

On coupe de préférence à une frontière naturelle (paragraphe, ligne, phrase,
mot) plutôt qu'au milieu d'un mot comme le faisait la v1.
"""
CHUNK_SIZE = 900
CHUNK_OVERLAP = 150
_BREAKS = ("\n\n", "\n", ". ", " ")


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    text = (text or "").strip()
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            for sep in _BREAKS:
                cut = window.rfind(sep, size // 2)
                if cut != -1:
                    end = start + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks
