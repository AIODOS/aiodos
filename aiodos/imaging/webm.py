"""Une vidéo WebM venue d'ailleurs, vérifiée puis réécrite : celle qu'une machine du réseau
public a créée pour moi (network/public_video_ask.py), et celle que cette machine rend à un
inconnu (network/public_video.py ; docs/conception/reseau-public-calcul.md § 11).

Ce que rend sd-server (imaging/generate.py ; vu le 29/10/2026 sur deux vidéos de Wan 2.1
écrites par sd-cli, le même code) : un en-tête EBML (DocType « webm »), un segment de taille
connue avec un index (SeekHead, Cues), les informations, une seule piste vidéo VP8, une
grappe (Cluster) par image, chaque image en image clé (encodée par libwebp), à 1 000 / cadence
ms d'écart, autant d'images que demandé ; et des étiquettes (Tags) dont COMMENT dit la
description entière et les réglages, sauf si la demande dit embed_image_metadata faux.

check(data, largeur, hauteur, images, cadence) lit le fichier en entier, borné (MAX_BYTES,
MAX_ELEMENTS), sans dépendance : l'en-tête et son DocType, un seul segment jusqu'à la fin du
fichier, une seule piste vidéo (VP8, VP9 ou AV1, les codecs du WebM ; ni chiffrée ni
compressée), de la taille demandée, exactement le nombre d'images demandé, sans
regroupement (lacing), leurs temps jamais décroissants, la première en image clé ; pour VP8,
l'en-tête de chaque image clé (code de départ, taille). Puis il la réécrit (_written) : un
en-tête, les informations (durée : images ÷ cadence), la piste vidéo, une grappe par image,
retimées à la cadence demandée ; rien d'autre ne reste (ni étiquettes, ni pièces jointes, ni
chapitres, ni autre piste, ni ce qui suit). Avec PyAV (deep ; installé avec faster-whisper,
absent d'un téléphone), la vidéo réécrite est décodée en entier : autant d'images, de la
bonne taille, pas toutes d'une seule couleur. ValueError sinon, sans détail pour un inconnu.
"""
import io
import struct

MAX_BYTES = 64 * 1024 ** 2        # au-delà, pas lue (les bornes de l'appelant sont plus basses)
MAX_ELEMENTS = 100_000            # éléments lus, au plus : 81 images en font moins de 600
CODECS = {"V_VP8", "V_VP9", "V_AV1"}
DOC_TYPE_TEXT = "webm"
# Lus : le WebM, et le Matroska que sd-server écrit quand la vidéo a du son (LTX-2.5, MiniMax
# H3 : son en PCM, que le WebM n'admet pas) ; la piste vidéo seule est gardée, en WebM
DOC_TYPES_READ = {DOC_TYPE_TEXT, "matroska"}
APP = "AIODOS"                    # MuxingApp, WritingApp de la vidéo réécrite

# Les identifiants EBML et Matroska lus ou écrits ici (avec leurs bits de longueur)
EBML, DOC_TYPE = 0x1A45DFA3, 0x4282
EBML_VERSION, EBML_READ_VERSION, EBML_MAX_ID, EBML_MAX_SIZE = 0x4286, 0x42F7, 0x42F2, 0x42F3
DOC_TYPE_VERSION, DOC_TYPE_READ_VERSION = 0x4287, 0x4285
SEGMENT, SEEK_HEAD, INFO, TRACKS, CLUSTER, CUES = (0x18538067, 0x114D9B74, 0x1549A966,
                                                    0x1654AE6B, 0x1F43B675, 0x1C53BB6B)
TAGS, CHAPTERS, ATTACHMENTS = 0x1254C367, 0x1043A770, 0x1941A469
TIMECODE_SCALE, DURATION, MUXING_APP, WRITING_APP = 0x2AD7B1, 0x4489, 0x4D80, 0x5741
TRACK_ENTRY, TRACK_NUMBER, TRACK_UID, TRACK_TYPE = 0xAE, 0xD7, 0x73C5, 0x83
FLAG_LACING, CODEC_ID, CODEC_PRIVATE, DEFAULT_DURATION = 0x9C, 0x86, 0x63A2, 0x23E383
CONTENT_ENCODINGS, VIDEO, PIXEL_WIDTH, PIXEL_HEIGHT = 0x6D80, 0xE0, 0xB0, 0xBA
TIMECODE, SIMPLE_BLOCK, BLOCK_GROUP, BLOCK, REFERENCE_BLOCK = 0xE7, 0xA3, 0xA0, 0xA1, 0xFB
# Ce qui peut suivre une grappe de taille inconnue : elle s'arrête là
LEVEL_ONE = {SEEK_HEAD, INFO, TRACKS, CLUSTER, CUES, TAGS, CHAPTERS, ATTACHMENTS}
VIDEO_TRACK = 1                   # TrackType d'une piste vidéo
VP8_START = b"\x9d\x01\x2a"       # code de départ d'une image clé VP8
TIMECODE_NS = 1_000_000           # TimecodeScale par défaut : la milliseconde


def _bad(why: str):
    """Un code court, jamais montré : l'appelant dit « illisible »."""
    return ValueError(why)


# ------------------------------------------------------------------ lecture
class _Reader:
    """Les éléments d'un fichier EBML, bornés : MAX_ELEMENTS en tout, chacun dans son parent."""

    def __init__(self, data: bytes):
        self.data, self.seen = data, 0

    def vint(self, pos: int, end: int, ident: bool) -> tuple:
        """(valeur, longueur, inconnue) d'un nombre de longueur variable : un identifiant
        (ident : ses bits de longueur gardés, 4 octets au plus) ou une taille (sans eux ;
        tous les bits à 1 : taille inconnue)."""
        if pos >= end:
            raise _bad("cut")
        first = self.data[pos]
        if not first:
            raise _bad("vint")
        length = 9 - first.bit_length()
        if (ident and length > 4) or pos + length > end:
            raise _bad("cut")
        value = int.from_bytes(self.data[pos:pos + length], "big")
        if ident:
            return value, length, False
        value &= (1 << (7 * length)) - 1
        return value, length, value == (1 << (7 * length)) - 1

    def children(self, start: int, end: int):
        """(identifiant, début, fin) de chaque élément de [start, end). Une taille inconnue
        n'est permise qu'au segment (jusqu'à la fin) et à une grappe (jusqu'au prochain élément
        de premier niveau)."""
        pos = start
        while pos < end:
            self.seen += 1
            if self.seen > MAX_ELEMENTS:
                raise _bad("elements")
            ident, n, _ = self.vint(pos, end, True)
            size, m, unknown = self.vint(pos + n, end, False)
            body = pos + n + m
            if unknown and ident == SEGMENT:
                stop = end
            elif unknown and ident == CLUSTER:
                stop = self._cluster_end(body, end)
            elif unknown:
                raise _bad("unknown_size")
            else:
                stop = body + size
                if stop > end:
                    raise _bad("cut")
            yield ident, body, stop
            pos = stop

    def _cluster_end(self, pos: int, end: int) -> int:
        while pos < end:
            ident, n, _ = self.vint(pos, end, True)
            if ident in LEVEL_ONE:
                return pos
            size, m, unknown = self.vint(pos + n, end, False)
            if unknown or pos + n + m + size > end:
                raise _bad("cluster")
            pos += n + m + size
            self.seen += 1
            if self.seen > MAX_ELEMENTS:
                raise _bad("elements")
        return end

    def uint(self, start: int, stop: int) -> int:
        if stop - start > 8:
            raise _bad("uint")
        return int.from_bytes(self.data[start:stop], "big")

    def text(self, start: int, stop: int) -> str:
        return self.data[start:stop].rstrip(b"\x00").decode("ascii", "replace")


def _header(reader: _Reader, start: int, stop: int):
    doc = None
    for ident, body, end in reader.children(start, stop):
        if ident == DOC_TYPE:
            doc = reader.text(body, end)
    if doc not in DOC_TYPES_READ:
        raise _bad("header")


def _track(reader: _Reader, start: int, stop: int) -> dict:
    found = {"number": None, "type": None, "codec": None, "private": None, "width": None,
             "height": None, "encoded": False}
    for ident, body, end in reader.children(start, stop):
        if ident == TRACK_NUMBER:
            found["number"] = reader.uint(body, end)
        elif ident == TRACK_TYPE:
            found["type"] = reader.uint(body, end)
        elif ident == CODEC_ID:
            found["codec"] = reader.text(body, end)
        elif ident == CODEC_PRIVATE:
            found["private"] = bytes(reader.data[body:end])
        elif ident == CONTENT_ENCODINGS:
            found["encoded"] = True            # chiffrée ou compressée : pas lisible telle quelle
        elif ident == VIDEO:
            for inner, at, until in reader.children(body, end):
                if inner == PIXEL_WIDTH:
                    found["width"] = reader.uint(at, until)
                elif inner == PIXEL_HEIGHT:
                    found["height"] = reader.uint(at, until)
    return found


def _block(reader: _Reader, start: int, stop: int, simple: bool) -> tuple:
    """(piste, temps relatif, image clé, regroupée, données) d'un bloc."""
    track, n, _ = reader.vint(start, stop, False)
    if start + n + 3 > stop:
        raise _bad("block")
    at = start + n
    relative = int.from_bytes(reader.data[at:at + 2], "big", signed=True)
    flags = reader.data[at + 2]
    return track, relative, bool(flags & 0x80) if simple else None, bool(flags & 0x06), \
        bytes(reader.data[at + 3:stop])


def _cluster(reader: _Reader, start: int, stop: int, blocks: list):
    base = None
    for ident, body, end in reader.children(start, stop):
        if ident == TIMECODE:
            base = reader.uint(body, end)
        elif ident == SIMPLE_BLOCK:
            blocks.append((base, *_block(reader, body, end, True)))
        elif ident == BLOCK_GROUP:
            found, referenced = None, False
            for inner, at, until in reader.children(body, end):
                if inner == BLOCK:
                    found = _block(reader, at, until, False)
                elif inner == REFERENCE_BLOCK:
                    referenced = True
            if found is not None:
                track, relative, _key, laced, payload = found
                blocks.append((base, track, relative, not referenced, laced, payload))


def _read(data: bytes) -> tuple:
    """(échelle des temps en ns, pistes, blocs) : cf. le début du module."""
    reader = _Reader(data)
    top = list(reader.children(0, len(data)))
    if len(top) != 2 or top[0][0] != EBML or top[1][0] != SEGMENT or top[1][2] != len(data):
        raise _bad("header")             # l'en-tête, un segment, et rien après
    _header(reader, top[0][1], top[0][2])
    scale, tracks, blocks = TIMECODE_NS, [], []
    for ident, body, end in reader.children(top[1][1], top[1][2]):
        if ident == INFO:
            for inner, at, until in reader.children(body, end):
                if inner == TIMECODE_SCALE:
                    scale = reader.uint(at, until)
        elif ident == TRACKS:
            tracks += [_track(reader, at, until)
                       for inner, at, until in reader.children(body, end) if inner == TRACK_ENTRY]
        elif ident == CLUSTER:
            _cluster(reader, body, end, blocks)
    return scale, tracks, blocks


def _vp8_key(payload: bytes, width: int, height: int):
    """L'en-tête d'une image clé VP8 : code de départ, puis la taille (14 bits chacune)."""
    if len(payload) < 10 or payload[3:6] != VP8_START:
        raise _bad("vp8")
    w, h = struct.unpack("<HH", payload[6:10])
    if (w & 0x3FFF, h & 0x3FFF) != (width, height):
        raise _bad("vp8_size")


def frames_of(data: bytes, width: int, height: int, frames: int) -> tuple:
    """(piste vidéo, [(image clé, données)…]) : la vidéo lue et vérifiée (cf. le début du
    module), avant réécriture ; ValueError sinon."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_BYTES:
        raise _bad("big")
    scale, tracks, blocks = _read(bytes(data))
    videos = [t for t in tracks if t["type"] == VIDEO_TRACK]
    if len(videos) != 1:
        raise _bad("tracks")
    track = videos[0]
    if track["codec"] not in CODECS or track["encoded"] or track["number"] is None:
        raise _bad("codec")
    if (track["width"], track["height"]) != (width, height):
        raise _bad("size")
    if not 0 < scale <= 10 ** 9:
        raise _bad("scale")
    found, last = [], None
    for base, number, relative, key, laced, payload in blocks:
        if number != track["number"]:
            continue                            # une autre piste : laissée de côté
        if base is None or laced or not payload:
            raise _bad("block")
        at = (base + relative) * scale
        if last is not None and at < last:
            raise _bad("order")
        last = at
        if track["codec"] == "V_VP8":
            key = not payload[0] & 0x01         # le bit de l'image VP8 fait foi
            if key:
                _vp8_key(payload, width, height)
        found.append((key, payload))
        if len(found) > frames:
            raise _bad("frames")
    if len(found) != frames or not found[0][0]:
        raise _bad("frames")
    return track, found


# ----------------------------------------------------------------- écriture
def _size(n: int) -> bytes:
    """Une taille connue, sur le moins d'octets possible (tous les bits à 1 : réservé)."""
    length = 1
    while n >= (1 << (7 * length)) - 1:
        length += 1
    return ((1 << (7 * length)) | n).to_bytes(length, "big")


def _element(ident: int, payload: bytes) -> bytes:
    return ident.to_bytes((ident.bit_length() + 7) // 8, "big") + _size(len(payload)) + payload


def _uint(ident: int, value: int) -> bytes:
    return _element(ident, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def _written(track: dict, found: list, width: int, height: int, fps: float) -> bytes:
    """La vidéo réécrite (cf. le début du module) : temps en millisecondes, image i à
    i × 1000 / fps, une grappe par image."""
    header = _element(EBML, b"".join((
        _uint(EBML_VERSION, 1), _uint(EBML_READ_VERSION, 1), _uint(EBML_MAX_ID, 4),
        _uint(EBML_MAX_SIZE, 8), _element(DOC_TYPE, DOC_TYPE_TEXT.encode()),
        _uint(DOC_TYPE_VERSION, 4), _uint(DOC_TYPE_READ_VERSION, 2))))
    info = _element(INFO, b"".join((
        _uint(TIMECODE_SCALE, TIMECODE_NS),
        _element(DURATION, struct.pack(">d", len(found) * 1000.0 / fps)),
        _element(MUXING_APP, APP.encode()), _element(WRITING_APP, APP.encode()))))
    entry = [_uint(TRACK_NUMBER, 1), _uint(TRACK_UID, 1), _uint(TRACK_TYPE, VIDEO_TRACK),
             _uint(FLAG_LACING, 0), _element(CODEC_ID, track["codec"].encode())]
    if track["private"]:
        entry.append(_element(CODEC_PRIVATE, track["private"]))
    entry += [_uint(DEFAULT_DURATION, round(1e9 / fps)),
              _element(VIDEO, _uint(PIXEL_WIDTH, width) + _uint(PIXEL_HEIGHT, height))]
    tracks = _element(TRACKS, _element(TRACK_ENTRY, b"".join(entry)))
    clusters = b"".join(
        _element(CLUSTER, _uint(TIMECODE, round(i * 1000 / fps)) + _element(
            SIMPLE_BLOCK, b"\x81\x00\x00" + (b"\x80" if key else b"\x00") + payload))
        for i, (key, payload) in enumerate(found))
    return header + _element(SEGMENT, info + tracks + clusters)


# ---------------------------------------------------------------- décodage
def _decoded(data: bytes, width: int, height: int, frames: int):
    """Avec PyAV : la vidéo décodée en entier (cf. le début du module) ; sans, rien."""
    try:
        import av
    except ImportError:
        return
    count, varied = 0, False
    try:
        with av.open(io.BytesIO(data), format="matroska") as container:
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                count += 1
                if count > frames or (frame.width, frame.height) != (width, height):
                    raise _bad("decoded")
                if not varied:
                    plane = frame.to_ndarray(format="gray")
                    varied = int(plane.max()) != int(plane.min())
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — tout ce que PyAV refuse : la vidéo ne l'est pas
        raise _bad("decoded") from None
    if count != frames or not varied:
        raise _bad("decoded")


def check(data: bytes, width: int, height: int, frames: int, fps: float,
          deep: bool = True) -> bytes:
    """La vidéo vérifiée et réécrite sans rien d'autre que ses images (cf. le début du
    module) ; deep : décodée en entier si PyAV est là. ValueError si elle n'est pas la vidéo
    demandée."""
    if not (isinstance(fps, (int, float)) and 0 < fps <= 240):
        raise _bad("fps")
    track, found = frames_of(data, width, height, frames)
    clean = _written(track, found, width, height, fps)
    if deep:
        _decoded(clean, width, height, frames)
    return clean
