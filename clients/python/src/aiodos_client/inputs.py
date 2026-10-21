# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""`ai.inputs` : les entrées d'un travail, envoyées à part et une seule fois, par leur
empreinte (docs/api-applications.md § 5.7, « Les entrées »).

- `put(data, type=None)` : des octets, ou le chemin d'un fichier ; rend son SHA-256.
  `HEAD /aiodos/v1/inputs/{sha256}` d'abord : déjà envoyée par cette appli, rien ne repart ;
  sinon `PUT`, corps brut (un fichier part par blocs, sans être lu d'avance en mémoire).
- 100 Mo au plus par entrée, vérifiés avant tout envoi (InputTooLarge).
- Le genre (Content-Type) se lit dans les premiers octets si l'appli ne le donne pas ; AIODOS
  le relit de toute façon et refuse ce qu'il ne reconnaît pas (UnsupportedMediaType).
- Une réponse à HEAD n'a pas de corps : en cas de refus (clé, droit…), la même adresse en GET
  dit le code de l'erreur, la passerelle jugeant la clé et les droits avant la route.

Tant que `features` n'annonce pas « inputs », put() lève NotSupported sans rien essayer.
"""

import contextlib
import hashlib
import io
import json
import os
import re
from pathlib import Path

from . import errors

FEATURE = "inputs"
MAX_INPUT = 100 * 1024 * 1024           # celui d'AIODOS (gateway/input_store.py)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
READ_BLOCK = 1024 * 1024
OCTETS = "application/octet-stream"
# (décalage, signature, genre) : ce qu'AIODOS reconnaît comme entrée
_SIGNS = ((0, b"\x89PNG\r\n\x1a\n", "image/png"), (0, b"\xff\xd8\xff", "image/jpeg"),
          (0, b"GIF87a", "image/gif"), (0, b"GIF89a", "image/gif"),
          (0, b"\x1aE\xdf\xa3", "video/webm"), (4, b"ftyp", "video/mp4"),
          (0, b"fLaC", "audio/flac"), (0, b"OggS", "audio/ogg"), (0, b"ID3", "audio/mpeg"))


def sniff(head: bytes) -> str:
    """Le genre d'un fichier d'après ses premiers octets (16 suffisent), sinon
    application/octet-stream."""
    for offset, sign, kind in _SIGNS:
        if head[offset:offset + len(sign)] == sign:
            return kind
    if head[:4] == b"RIFF":
        return {b"WEBP": "image/webp", b"WAVE": "audio/wav"}.get(head[8:12], OCTETS)
    if len(head) > 1 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2):
        return "audio/mpeg"                     # MP3 sans étiquette ID3
    return OCTETS


def is_sha256(value) -> bool:
    return isinstance(value, str) and bool(SHA256.match(value))


class Source:
    """Une entrée à envoyer : des octets, ou un fichier (lu par blocs) ; sa taille, ses
    premiers octets, son empreinte."""

    def __init__(self, data):
        if isinstance(data, (bytes, bytearray, memoryview)):
            self.data = bytes(data)
            self.path = None
            self.size = len(self.data)
        elif isinstance(data, (str, os.PathLike)):
            self.data = None
            self.path = Path(data)
            self.size = self.path.stat().st_size        # FileNotFoundError si absent
        else:
            raise TypeError("une entrée : des octets, ou le chemin d'un fichier")
        self._digest = None

    def head(self, count=16) -> bytes:
        if self.data is not None:
            return self.data[:count]
        with open(self.path, "rb") as handle:
            return handle.read(count)

    def sha256(self) -> str:
        if self._digest is None:
            digest = hashlib.sha256()
            if self.data is not None:
                digest.update(self.data)
            else:
                with open(self.path, "rb") as handle:
                    for block in iter(lambda: handle.read(READ_BLOCK), b""):
                        digest.update(block)
            self._digest = digest.hexdigest()
        return self._digest

    @contextlib.contextmanager
    def body(self):
        """Le corps à envoyer : les octets, ou le fichier ouvert (envoyé par blocs)."""
        if self.data is not None:
            yield self.data
            return
        with open(self.path, "rb") as handle:
            yield handle


def _has_code(body) -> bool:
    try:
        data = json.loads(bytes(body or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    error = data.get("error") if isinstance(data, dict) else None
    return isinstance(error, dict) and isinstance(error.get("code"), str)


class Inputs:
    """Les entrées par empreinte : `ai.inputs.put(data)`."""

    def __init__(self, client):
        self._client = client

    def put(self, data, type=None, cancel=None):
        """Envoie une entrée (octets, ou chemin d'un fichier) si cette appli ne l'a pas déjà
        envoyée ; rend son SHA-256 (64 caractères hexadécimaux). `type` : son genre
        (« image/png »…), lu dans ses premiers octets sinon. Lève InputTooLarge (avant tout
        envoi), InputHashMismatch (le fichier a changé pendant l'envoi),
        UnsupportedMediaType, QuotaExceeded…"""
        client = self._client
        client._gate(FEATURE)
        source = data if isinstance(data, Source) else Source(data)
        if source.size > MAX_INPUT:
            raise errors.make("input_too_large", client.lang,
                              detail={"bytes": source.size, "max_bytes": MAX_INPUT})
        sha = source.sha256()
        path = f"/aiodos/v1/inputs/{sha}"
        resp, endpoint = client._request("HEAD", path, feature=FEATURE, cancel=cancel)
        if resp.status == 200:
            return sha                                  # déjà là : rien ne repart
        if resp.status != 404:
            self._refused(path, resp, endpoint)
        headers = {"Content-Type": type or sniff(source.head()),
                   "Content-Length": str(source.size)}
        with source.body() as body:
            resp, endpoint = client._request("PUT", path, data=body, headers=headers,
                                             feature=FEATURE, cancel=cancel)
        client._check(resp.status, resp.headers, resp.body, endpoint)
        answer = resp.json(client.lang)
        if not isinstance(answer, dict) or answer.get("sha256") != sha:
            raise errors.make("protocol", client.lang)
        return sha

    def _refused(self, path, resp, endpoint):
        """Un refus de HEAD, sans corps : la même adresse en GET en dit le code."""
        client = self._client
        try:
            again, where = client._request("GET", path, feature=FEATURE)
        except errors.AiodosError:
            again = None
        if again is not None and again.status >= 400 and _has_code(again.body):
            client._check(again.status, again.headers, again.body, where)
        client._check(resp.status, resp.headers, resp.body, endpoint)
        raise errors.make("protocol", client.lang)         # un statut 2xx inattendu


def check_input(value):
    """Vérifie la forme d'une entrée telle qu'une appli la donne : des octets, le chemin
    d'un fichier, ou {"sha256": …} (déjà envoyée). TypeError ou ValueError sinon."""
    if isinstance(value, dict):
        if not is_sha256(value.get("sha256")):
            raise ValueError("entrée : {\"sha256\": 64 caractères hexadécimaux en minuscules}")
        return
    if isinstance(value, io.IOBase):
        raise TypeError("entrée : des octets ou un chemin, pas un fichier ouvert")
    if not isinstance(value, (bytes, bytearray, memoryview, str, os.PathLike)):
        raise TypeError("entrée : des octets, un chemin, ou {\"sha256\": …}")


def as_input(value):
    """Une entrée telle qu'une appli la donne → l'empreinte donnée ({"sha256": …}), ou la
    Source à envoyer (octets, chemin)."""
    check_input(value)
    if isinstance(value, dict):
        return value["sha256"]
    return Source(value)
