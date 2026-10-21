# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Les fichiers produits par un travail (docs/api-applications.md § 5.7, « Les fichiers ») :
`File`, la fiche et le contenu d'un fichier, et de quoi les ranger sur le disque.

- La fiche : `GET /aiodos/v1/files/{id}` (`File.refresh()`) ; elle vient aussi avec le travail
  (`job.files`) et avec ses événements.
- Le contenu : `GET /aiodos/v1/files/{id}/content`, avec `Range` pour reprendre. `save(path)`
  écrit en flux dans « <nom>.part », reprend ce qui y est déjà (`Range: bytes=N-`), vérifie
  l'empreinte (SHA-256) du tout, puis renomme. Une coupure en route se reprend seule (deux
  fois au plus dans un même appel) ; le « .part » sert encore à l'appel suivant.
  `content()` rend les octets, vérifiés ; `content(start, end)`, une plage.
- Un fichier servi jusqu'à son dernier octet est **récupéré** : AIODOS le garde encore 7 jours
  (`fetched`, `expires_at`) ; jamais récupéré, il n'est jamais effacé seul.
- `delete()` l'efface dans AIODOS ; `link()` (un lien signé, sans clé) n'est pas encore servi.

Tant que `features` n'annonce pas « files », ces fonctions lèvent NotSupported sans rien
essayer.
"""

import hashlib
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

from . import errors, texts

log = logging.getLogger("aiodos_client")

FEATURE = "files"
LINKS = "files.link"
RETRIES = 2                     # reprises d'une coupure dans un même save()
READ_BLOCK = 1024 * 1024
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
              "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm",
              "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac",
              "audio/ogg": ".ogg", "model/gltf-binary": ".glb"}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                       *(f"lpt{i}" for i in range(1, 10))})
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


def extension(kind) -> str:
    """L'extension d'un genre de fichier (« image/png » → « .png »), « .bin » sinon."""
    return EXTENSIONS.get(str(kind or "").split(";", 1)[0].strip().lower(), ".bin")


def safe_name(text, fallback="aiodos") -> str:
    """Un nom de fichier sûr sous Windows comme ailleurs : sans séparateur ni caractère
    réservé, sans point ni espace final, jamais un nom de périphérique (CON, NUL…)."""
    name = _UNSAFE.sub("_", str(text or "")).strip().rstrip(". ")[:120].rstrip(". ")
    if not name or set(name) <= {"."}:
        name = fallback
    if name.split(".", 1)[0].lower() in _RESERVED:
        name = "_" + name
    return name


def names_for(files, stem=None) -> list:
    """Les noms d'un téléchargement : « <stem>_1.png », « <stem>_2.png »… (sans numéro pour
    un seul fichier), ou, sans `stem`, l'identifiant de chaque fichier (« file_….png »)."""
    if not stem:
        return [f.default_name for f in files]
    base = safe_name(stem)
    if len(files) == 1:
        return [base + files[0].extension]
    width = len(str(len(files)))
    return [f"{base}_{n:0{width}d}{f.extension}" for n, f in enumerate(files, 1)]


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    return value if isinstance(value, str) else None


def _hash_file(path, digest=None):
    digest = digest or hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(READ_BLOCK), b""):
            digest.update(block)
    return digest


class _Cut(Exception):
    """Une coupure après des octets reçus : à reprendre (Range)."""

    def __init__(self, error):
        super().__init__(error.code)
        self.error = error


class File:
    """Un fichier produit par un travail : sa fiche (`id`, `unit`, `node`, `type`, `bytes`,
    `sha256`, `width`, `height`, `machine`, `place`, `engine`, `verified`, `fetched`,
    `expires_at`, `created_at` ; `data` : la fiche telle quelle) et son contenu."""

    def __init__(self, client, file_id, data=None, job_id=None):
        self._client = client
        self.id = str(file_id)
        self.job_id = job_id
        self._apply(data or {})

    def _apply(self, data):
        self.data = dict(data)
        get = self.data.get
        self.unit = _int(get("unit"))
        self.node = _text(get("node"))
        self.type = _text(get("type"))
        self.bytes = _int(get("bytes"))
        sha = _text(get("sha256"))
        self.sha256 = sha.lower() if sha else None
        self.width = _int(get("width"))
        self.height = _int(get("height"))
        self.machine = _text(get("machine"))
        self.place = _text(get("place"))
        self.engine = _text(get("engine"))
        self.verified = _text(get("verified"))
        self.fetched = get("fetched") is True
        self.expires_at = _number(get("expires_at"))
        self.created_at = _number(get("created_at"))

    def __repr__(self):
        return f"File({self.id!r}, type={self.type!r}, bytes={self.bytes!r})"

    def __eq__(self, other):
        return isinstance(other, File) and other.id == self.id

    def __hash__(self):
        return hash(("aiodos.file", self.id))

    @property
    def size(self):
        """La taille en octets (`bytes`)."""
        return self.bytes

    @property
    def extension(self) -> str:
        return extension(self.type)

    @property
    def default_name(self) -> str:
        """Son nom sur le disque sans autre choix : « file_….png »."""
        return safe_name(self.id, "fichier") + self.extension

    @property
    def _path(self):
        return f"/aiodos/v1/files/{quote(self.id, safe='')}"

    # --- La fiche ---------------------------------------------------------------------
    def refresh(self):
        """Relit la fiche dans AIODOS (`GET /aiodos/v1/files/{id}`) ; rend le File."""
        data = self._client._call("GET", self._path, feature=FEATURE)
        if not isinstance(data, dict):
            raise errors.make("protocol", self._client.lang)
        self._apply(data)
        return self

    def _known(self):
        """La taille et l'empreinte, relues si la fiche ne les a pas."""
        if self.bytes is None or self.sha256 is None:
            self.refresh()
        if self.bytes is None or self.sha256 is None:
            raise errors.make("protocol", self._client.lang)
        return self.bytes, self.sha256

    # --- Le contenu -------------------------------------------------------------------
    def _open(self, offset=0, end=None, cancel=None):
        """L'échange ouvert sur le contenu, à partir de `offset` (une plage au-delà de 0)."""
        headers = {}
        if offset or end is not None:
            headers["Range"] = f"bytes={int(offset)}-{'' if end is None else int(end)}"
        return self._client._stream(self._path + "/content", method="GET", accept="*/*",
                                    headers=headers, cancel=cancel, feature=FEATURE)

    def _cut(self):
        """La réponse s'est arrêtée avant la fin : Unreachable, à reprendre."""
        lang = self._client.lang
        return errors.make("unreachable", lang, message=texts.error("cut", lang),
                           hint=texts.hint("connection_lost", lang))

    def _mismatch(self):
        lang = self._client.lang
        return errors.make("protocol", lang, message=texts.error("hash_mismatch", lang),
                           hint=texts.hint("hash_mismatch", lang),
                           detail={"file": self.id, "sha256": self.sha256})

    def content(self, start=None, end=None, cancel=None) -> bytes:
        """Les octets du fichier, vérifiés (SHA-256) ; ou une plage [start, end] (octets
        compris ; `end` absent : jusqu'au bout), sans vérification. Pour un gros fichier,
        save() écrit en flux sur le disque."""
        lang = self._client.lang
        if start is None and end is None:
            size, sha = self._known()
            with self._open(cancel=cancel) as exchange:
                data = self._collect(exchange, size)
            if len(data) < size:
                raise self._cut()
            if hashlib.sha256(data).hexdigest() != sha:
                raise self._mismatch()
            self.fetched = True
            return data
        first = int(start or 0)
        if first < 0 or (end is not None and int(end) < first):
            raise ValueError("content : 0 <= start <= end")
        with self._open(first, end, cancel) as exchange:
            status = exchange.response.status
            data = self._collect(exchange, None)
        if status == 200:                       # plage ignorée : tout est venu
            data = data[first:None if end is None else int(end) + 1]
        elif status != 206:
            raise errors.make("protocol", lang)
        return data

    def _collect(self, exchange, size):
        limit = size if size is not None else self.bytes
        pieces, count = [], 0
        for piece in exchange.chunks():
            count += len(piece)
            if limit is not None and count > limit:     # plus long qu'annoncé : refusé
                raise errors.make("protocol", self._client.lang)
            pieces.append(piece)
        return b"".join(pieces)

    def save(self, path, cancel=None) -> Path:
        """Écrit le fichier à `path` (un dossier : il y prend son nom, « file_….png ») et
        rend son chemin (Path). En flux, dans « <nom>.part » d'abord : une coupure se reprend
        (Range) là où elle s'est arrêtée, dans ce même appel (deux fois au plus) ou au
        suivant ; l'empreinte du tout est vérifiée avant de renommer. Un fichier déjà là et
        identique n'est pas téléchargé de nouveau."""
        self._client._gate(FEATURE)
        target = Path(path)
        if target.is_dir():
            target = target / self.default_name
        size, sha = self._known()
        if target.is_file() and target.stat().st_size == size \
                and _hash_file(target).hexdigest() == sha:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        cuts, fresh = 0, False
        while True:
            try:
                digest, resumed = self._receive(part, size, cancel)
            except _Cut as cut:
                if cuts >= RETRIES:
                    raise cut.error from None
                cuts += 1
                log.info("Téléchargement de %s coupé : reprise (%d).", self.id, cuts)
                continue
            if digest.hexdigest() == sha:
                break
            part.unlink(missing_ok=True)
            if resumed and not fresh:           # un « .part » d'autre chose : tout reprendre
                fresh = True
                continue
            raise self._mismatch()
        os.replace(part, target)
        self.fetched = True
        return target

    def _receive(self, part, size, cancel):
        """Complète « .part » → (empreinte du tout, repris d'un « .part » ?). Lève _Cut
        quand la réponse s'arrête après des octets reçus."""
        offset = part.stat().st_size if part.is_file() else 0
        if offset > size:
            part.unlink()
            offset = 0
        digest = _hash_file(part) if offset else hashlib.sha256()
        if offset == size:
            return digest, True
        resumed = offset > 0
        with self._open(offset, cancel=cancel) as exchange:
            response = exchange.response
            mode = "ab"
            if response.status == 206:
                found = _CONTENT_RANGE.match(response.headers.get("Content-Range") or "")
                if (not found or int(found.group(1)) != offset
                        or found.group(3) not in ("*", str(size))):
                    raise errors.make("protocol", self._client.lang)
            elif response.status == 200:            # plage ignorée : tout, depuis le début
                offset, mode, resumed, digest = 0, "wb", False, hashlib.sha256()
            else:
                raise errors.make("protocol", self._client.lang)
            expected = size - offset
            received = 0
            with open(part, mode) as out:
                try:
                    for piece in exchange.chunks():
                        if received + len(piece) > expected:
                            raise errors.make("protocol", self._client.lang)
                        out.write(piece)
                        digest.update(piece)
                        received += len(piece)
                except (errors.Unreachable, errors.Timeout) as exc:
                    if received:
                        raise _Cut(exc) from exc
                    raise
            if received < expected:
                if received:
                    raise _Cut(self._cut())
                raise self._cut()
        return digest, resumed

    def delete(self) -> bool:
        """Efface le fichier dans AIODOS (sa copie sur ce disque reste) ; rend vrai."""
        data = self._client._call("DELETE", self._path, feature=FEATURE)
        return isinstance(data, dict) and data.get("deleted") is True

    def link(self, ttl=900, uses=3):
        """Un lien signé, sans clé (60 min et 5 usages au plus) : pas encore servi par
        AIODOS, NotSupported (« files.link »)."""
        self._client._refuse_unserved(LINKS)


class FileList(list):
    """Les fichiers d'un travail (File), tels que les dit son dernier état connu. Appelée,
    `job.files(unfetched=False)` les relit dans AIODOS (`GET …/files`) : tous, ou seulement
    ceux pas encore récupérés."""

    def __init__(self, items=(), job=None):
        super().__init__(items)
        self._job = job

    def __call__(self, unfetched=False):
        if self._job is None:
            raise TypeError("ces fichiers ne sont liés à aucun travail")
        return self._job._list_files(unfetched)


class Downloads(list):
    """Les chemins des fichiers téléchargés (Path), dans l'ordre des fichiers du travail ;
    `files` : les File correspondants ; `job` : le travail."""

    def __init__(self, paths=(), files=(), job=None):
        super().__init__(paths)
        self.files = list(files)
        self.job = job
