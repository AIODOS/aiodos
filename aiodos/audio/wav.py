"""Lire un WAV (ce qu'écrit audio.cpp) sans rien d'autre que la bibliothèque standard : son
format, sa durée, et s'il n'est pas du silence. Le module wave de Python ne lit pas les
échantillons flottants (format 3) qu'écrivent certains modèles.

Un WAV qui arrive d'une autre machine passe ici avant d'être gardé (jobs/kinds/audio.py) : un
format inconnu, une durée nulle ou un silence sont refusés.
"""
import array
import math
import struct
import sys

PCM, FLOAT, EXTENSIBLE = 1, 3, 0xFFFE
MAX_SAMPLES = 2_000_000           # le silence se juge sur un échantillon de cette taille
SILENT_PEAK = 1e-3                # crête sous laquelle un son est du silence (pleine échelle 1)


class WavError(ValueError):
    pass


def _chunks(data: bytes):
    """(identifiant, début, taille) des morceaux d'un fichier RIFF/WAVE."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavError("not_wav")
    at = 12
    while at + 8 <= len(data):
        name, size = data[at:at + 4], struct.unpack_from("<I", data, at + 4)[0]
        yield name, at + 8, size
        at += 8 + size + (size & 1)


def read(data: bytes) -> dict:
    """{"format": PCM | FLOAT, "channels", "rate", "bits", "frames", "seconds", "offset",
    "size"} d'un WAV ; WavError sinon."""
    fmt = body = None
    for name, start, size in _chunks(data):
        if name == b"fmt " and size >= 16:
            fmt = data[start:start + size]
        elif name == b"data":
            body = (start, min(size, len(data) - start))
            break
    if fmt is None or body is None:
        raise WavError("no_audio")
    tag, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag == EXTENSIBLE and len(fmt) >= 26:
        tag = struct.unpack_from("<H", fmt, 24)[0]        # le sous-format, ses deux premiers octets
    widths = {FLOAT: (32, 64), PCM: (8, 16, 24, 32)}
    if tag not in widths or bits not in widths[tag] or not channels or not rate or not align:
        raise WavError("format")
    frames = body[1] // align
    return {"format": tag, "channels": channels, "rate": rate, "bits": bits, "frames": frames,
            "seconds": frames / rate, "offset": body[0], "size": frames * align}


def _samples(data: bytes, info: dict) -> list:
    """Un échantillon des valeurs (pleine échelle 1), MAX_SAMPLES au plus, pris régulièrement."""
    width = info["bits"] // 8
    count = info["size"] // width
    step = max(1, math.ceil(count / MAX_SAMPLES))
    raw = memoryview(data)[info["offset"]:info["offset"] + count * width]
    if info["format"] == FLOAT or width in (2, 4) and info["format"] == PCM:
        code = {(FLOAT, 4): "f", (FLOAT, 8): "d", (PCM, 2): "h", (PCM, 4): "i"}[
            (info["format"], width)]
        values = array.array(code)
        values.frombytes(bytes(raw))
        if sys.byteorder != "little":
            values.byteswap()
        scale = 1.0 if info["format"] == FLOAT else float(2 ** (info["bits"] - 1))
        return [v / scale for v in values[::step]]
    if width == 1:                    # 8 bits, non signé
        return [(b - 128) / 128.0 for b in bytes(raw[::step])]
    found = []                        # 24 bits
    for i in range(0, count, step):
        b = raw[i * 3:i * 3 + 3]
        found.append(int.from_bytes(bytes(b), "little", signed=True) / 8388608.0)
    return found


def levels(data: bytes, info: dict | None = None) -> dict:
    """{"peak", "rms"} (pleine échelle 1) d'un WAV."""
    info = info or read(data)
    values = [v for v in _samples(data, info) if math.isfinite(v)]
    if not values:
        return {"peak": 0.0, "rms": 0.0}
    return {"peak": max(abs(v) for v in values),
            "rms": math.sqrt(sum(v * v for v in values) / len(values))}


def check(data: bytes, min_seconds: float = 0.2) -> dict:
    """Le WAV lu (read) et ses niveaux, s'il dure et n'est pas du silence ; WavError sinon
    (code : not_wav, no_audio, format, too_short, silent)."""
    info = read(data)
    if info["seconds"] < min_seconds:
        raise WavError("too_short")
    found = levels(data, info)
    if found["peak"] < SILENT_PEAK:
        raise WavError("silent")
    return {**info, **found}
