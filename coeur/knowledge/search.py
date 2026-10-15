"""Recherche sémantique (cosinus) dans les passages, et sommaire des sources.

Les vecteurs de chaque bibliothèque sont gardés en mémoire sous forme de
matrice normalisée ; toute écriture dans le stockage invalide ce cache.
"""
import threading
from collections import Counter

import numpy as np

from coeur import db

_lock = threading.Lock()
_version = 0
_cache = {}      # collection_id -> (version, ids, matrice normalisée)


def invalidate():
    global _version
    with _lock:
        _version += 1


def _matrix(collection_id):
    with _lock:
        hit = _cache.get(collection_id)
        if hit and hit[0] == _version:
            return hit[1], hit[2]
        version = _version
    rows = db.query("SELECT k.id, k.embedding FROM chunks k "
                    "JOIN entries e ON e.id = k.entry_id WHERE e.collection_id = ?",
                    (collection_id,))
    ids = np.empty(0, dtype=np.int64)
    matrix = np.empty((0, 0), dtype=np.float32)
    if rows:
        vectors = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        # garde la dimension majoritaire (si le modèle d'embedding a changé)
        dim = Counter(len(v) for v in vectors).most_common(1)[0][0]
        keep = [i for i, v in enumerate(vectors) if len(v) == dim]
        ids = np.array([rows[i]["id"] for i in keep], dtype=np.int64)
        matrix = np.vstack([vectors[i] for i in keep])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.where(norms == 0, 1, norms)
    with _lock:
        _cache[collection_id] = (version, ids, matrix)
    return ids, matrix


def allowed_collections(libraries: bool, conversations: bool) -> list:
    out = []
    for r in db.query("SELECT id, name, enabled, system FROM collections"):
        if (r["system"] and conversations) or (not r["system"] and libraries and r["enabled"]):
            out.append((r["id"], r["name"]))
    return out


def search(vector, k=4, libraries=True, conversations=False, exclude_conversation=None) -> list:
    """Les k passages les plus proches du vecteur, dans les sources autorisées.

    exclude_conversation : id d'une conversation dont la mémoire est ignorée
    (celle en cours : ses messages sont déjà dans l'historique envoyé au modèle).
    """
    query = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(query))
    if not norm:
        return []
    query = query / norm
    candidates = []
    for collection_id, _name in allowed_collections(libraries, conversations):
        ids, matrix = _matrix(collection_id)
        if not len(ids) or matrix.shape[1] != query.shape[0]:
            continue
        scores = matrix @ query
        for i in np.argsort(-scores)[:k * 3]:          # marge pour les exclusions
            candidates.append((float(scores[i]), int(ids[i])))
    candidates.sort(reverse=True)
    if not candidates:
        return []
    placeholders = ",".join("?" * len(candidates))
    rows = {r["id"]: r for r in db.query(
        f"""SELECT k.id, k.text, e.source, e.kind, e.origin, c.system, c.name AS collection
            FROM chunks k JOIN entries e ON e.id = k.entry_id
            JOIN collections c ON c.id = e.collection_id
            WHERE k.id IN ({placeholders})""", tuple(cid for _, cid in candidates))}
    excluded = str(exclude_conversation) if exclude_conversation is not None else None
    hits = []
    for score, cid in candidates:
        row = rows.get(cid)
        if row is None or (row["system"] and excluded and row["origin"] == excluded):
            continue
        hits.append({"score": score, "text": row["text"], "source": row["source"],
                     "kind": row["kind"], "collection": row["collection"]})
        if len(hits) == k:
            break
    return hits


def manifest(libraries=True, conversations=False, per_collection=25) -> str:
    """Sommaire de ce que l'IA possède : l'IA ne voit sinon que des extraits."""
    allowed = allowed_collections(libraries, conversations)
    lines = []
    for collection_id, name in allowed:
        sources = [r["source"] for r in db.query(
            "SELECT source FROM entries WHERE collection_id=? ORDER BY source",
            (collection_id,))]
        if not sources:
            continue
        shown = ", ".join(sources[:per_collection])
        more = f" … (+{len(sources) - per_collection})" if len(sources) > per_collection else ""
        lines.append(f"• {name} ({len(sources)} éléments) : {shown}{more}")
    return "\n".join(lines)
