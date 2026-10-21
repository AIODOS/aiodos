"""Le genre d'un fichier d'après ses premiers octets, jamais d'après son nom ni ce qu'on en
dit : les entrées qu'une appli envoie (gateway/input_store.py) et les sorties d'un moteur
(engines/comfyui.py). Ce qui n'est pas reconnu ici n'entre pas comme entrée."""
from pathlib import Path

HEAD = 16
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"\x1aE\xdf\xa3", "video/webm"),
          (b"fLaC", "audio/flac"), (b"OggS", "audio/ogg"), (b"ID3", "audio/mpeg"),
          (b"glTF", "model/gltf-binary"))
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
              "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm",
              "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac",
              "audio/ogg": ".ogg", "model/gltf-binary": ".glb"}
# Les entrées qu'un moteur sait lire (LoadImage, LoadVideo, LoadAudio)
INPUT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "video/mp4",
                         "video/webm", "audio/wav", "audio/mpeg", "audio/flac", "audio/ogg"})


def sniff(head: bytes) -> str | None:
    """Le genre d'après les premiers octets (HEAD au moins), ou None."""
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if len(head) >= 2 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2):
        return "audio/mpeg"
    return None


def sniff_file(path: Path) -> str:
    with open(path, "rb") as f:
        return sniff(f.read(HEAD)) or "application/octet-stream"


def extension(kind: str) -> str:
    return EXTENSIONS.get(kind, ".bin")
