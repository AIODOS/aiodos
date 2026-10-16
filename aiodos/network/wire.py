"""Sorties de calcul ↔ JSON pour le réseau : les tableaux numpy (vecteurs
d'embedding) voyagent en base64, en float32, plutôt qu'en listes de nombres
(4 fois plus léger). Seul ce type de tableau est accepté en entrée."""
import base64

import numpy as np

_ARRAY = "__array__"


def pack(value):
    if isinstance(value, np.ndarray):
        array = np.ascontiguousarray(value, dtype=np.float32)
        return {_ARRAY: base64.b64encode(array.tobytes()).decode("ascii"),
                "shape": list(array.shape)}
    if isinstance(value, dict):
        return {k: pack(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [pack(v) for v in value]
    return value


def unpack(value):
    if isinstance(value, dict):
        if _ARRAY in value:
            shape = tuple(int(n) for n in value.get("shape") or ())
            data = base64.b64decode(str(value[_ARRAY]).encode("ascii"), validate=True)
            array = np.frombuffer(data, dtype=np.float32)
            if not shape or any(n < 0 for n in shape) or array.size != int(np.prod(shape)):
                raise ValueError("Tableau reçu incohérent.")
            return array.reshape(shape)
        return {k: unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unpack(v) for v in value]
    return value
