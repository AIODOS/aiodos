"""Recherche sémantique (cosinus) dans les passages, et sommaire des sources.

Les vecteurs de chaque bibliothèque sont gardés en mémoire sous forme de
matrice normalisée ; toute écriture dans le stockage invalide ce cache.
"""
import threading

import numpy as np

from aiodos import db
from aiodos.i18n import t

_lock = threading.Lock()
_version = 0
_cache = {}      # (collection_id, dimension) -> (version, ids, matrice normalisée)


def invalidate():
    """Les matrices en cache sont à refaire, une fois finie la transaction en cours (s'il y
    en a une) : refaites avant le COMMIT, elles garderaient l'état d'avant."""
    db.after_transaction(_bump)


def _bump():
    global _version
    with _lock:
        _version += 1


def _matrix(collection_id, dim):
    """Vecteurs de la bibliothèque qui ont la dimension de la question. Les autres
    (passages sans vecteur, ou d'un ancien modèle d'embedding) sont ignorés :
    pendant une réindexation, les passages déjà recalculés restent trouvables."""
    with _lock:
        hit = _cache.get((collection_id, dim))
        if hit and hit[0] == _version:
            return hit[1], hit[2]
        version = _version
    rows = db.query("SELECT k.id, k.embedding FROM chunks k "
                    "JOIN entries e ON e.id = k.entry_id WHERE e.collection_id = ? "
                    "AND length(k.embedding) = ?", (collection_id, dim * 4))
    ids = np.array([r["id"] for r in rows], dtype=np.int64)
    matrix = np.empty((0, dim), dtype=np.float32)
    if rows:
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.where(norms == 0, 1, norms)
    with _lock:
        _cache[(collection_id, dim)] = (version, ids, matrix)
    return ids, matrix


def allowed_collections(libraries: bool, conversations: bool, only=None) -> list:
    """only : ensemble d'identifiants de bibliothèques auxquelles se limiter (None : toutes)."""
    out = []
    for r in db.query("SELECT id, name, enabled, system FROM collections"):
        if only is not None and r["id"] not in only:
            continue
        if (r["system"] and conversations) or (not r["system"] and libraries and r["enabled"]):
            out.append((r["id"], r["name"]))
    return out


def _unit(vector):
    query = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(query))
    return query / norm if norm else None


def _ranked(query, collection_ids, per_collection, exclude=None) -> list:
    """(score, id de passage) des meilleurs passages de ces bibliothèques, du plus proche
    au plus lointain. exclude(collection_id) : ids des passages à écarter avant le
    classement (ils ne prennent alors la place d'aucun autre)."""
    candidates = []
    for collection_id in collection_ids:
        ids, matrix = _matrix(collection_id, query.shape[0])
        if not len(ids):
            continue
        scores = matrix @ query
        banned = exclude(collection_id) if exclude else ()
        if len(banned):
            scores = np.where(np.isin(ids, np.asarray(banned, dtype=np.int64)), -np.inf, scores)
        for i in np.argsort(-scores)[:per_collection]:
            if np.isfinite(scores[i]):
                candidates.append((float(scores[i]), int(ids[i])))
    candidates.sort(reverse=True)
    return candidates


def _rows(candidates) -> dict:
    if not candidates:
        return {}
    placeholders = ",".join("?" * len(candidates))
    return {r["id"]: r for r in db.query(
        f"""SELECT k.id, k.text, k.entry_id, e.source, e.kind, e.origin, e.collection_id,
                   c.system, c.name AS collection
            FROM chunks k JOIN entries e ON e.id = k.entry_id
            JOIN collections c ON c.id = e.collection_id
            WHERE k.id IN ({placeholders})""", tuple(cid for _, cid in candidates))}


def search(vector, k=4, libraries=True, conversations=False, exclude_conversation=None,
           only=None) -> list:
    """Les k passages les plus proches du vecteur, dans les sources autorisées.

    exclude_conversation : id d'une conversation dont la mémoire est ignorée
    (celle en cours : ses messages sont déjà dans l'historique envoyé au modèle).
    only : bibliothèques auxquelles se limiter (cf. allowed_collections).
    """
    query = _unit(vector)
    if query is None:
        return []
    allowed = [cid for cid, _name in allowed_collections(libraries, conversations, only)]
    candidates = _ranked(query, allowed, k * 3)          # marge pour les exclusions
    rows = _rows(candidates)
    excluded = str(exclude_conversation) if exclude_conversation is not None else None
    hits = []
    for score, cid in candidates:
        row = rows.get(cid)
        if row is None or (row["system"] and excluded and row["origin"] == excluded):
            continue
        hits.append({"score": score, "text": row["text"], "source": row["source"],
                     "kind": row["kind"], "collection": row["collection"],
                     "collection_id": row["collection_id"]})
        if len(hits) == k:
            break
    return hits


def search_in(vector, k, collection_ids, exclude=None) -> list:
    """Les k passages les plus proches, dans ces bibliothèques seulement, sans ceux
    qu'écarte exclude(collection_id) (cf. _ranked). Chaque passage dit d'où il vient
    (entrée, bibliothèque), pour un dernier contrôle avant qu'il serve."""
    query = _unit(vector)
    if query is None or not collection_ids:
        return []
    candidates = _ranked(query, collection_ids, k, exclude)
    rows = _rows(candidates)
    hits = []
    for score, cid in candidates:
        row = rows.get(cid)
        if row is None:
            continue
        hits.append({"score": score, "text": row["text"], "source": row["source"],
                     "kind": row["kind"], "collection": row["collection"],
                     "collection_id": row["collection_id"], "entry_id": row["entry_id"]})
        if len(hits) == k:
            break
    return hits


def manifest(libraries=True, conversations=False, per_collection=25, only=None) -> str:
    """Sommaire de ce que l'IA possède : l'IA ne voit sinon que des extraits."""
    return manifest_text(manifest_parts(libraries, conversations, per_collection, only))


def manifest_parts(libraries=True, conversations=False, per_collection=25, only=None) -> list:
    """Le sommaire, bibliothèque par bibliothèque : (id, nom, nombre d'éléments, noms
    montrés). Une réponse qui cite ces noms s'en est servie (pipeline.summary_mentioned)."""
    parts = []
    for collection_id, name in allowed_collections(libraries, conversations, only):
        sources = [r["source"] for r in db.query(
            "SELECT source FROM entries WHERE collection_id=? ORDER BY source",
            (collection_id,))]
        if sources:
            parts.append((collection_id, name, len(sources), sources[:per_collection]))
    return parts


def manifest_text(parts) -> str:
    """Le sommaire tel que le modèle le lit, dans la langue en cours (library_prompts.json)."""
    lines = []
    for _collection_id, name, total, shown in parts:
        more = f" … (+{total - len(shown)})" if total > len(shown) else ""
        lines.append(t("library_prompts.manifest_line", name=name, n=total,
                       items=", ".join(shown), more=more))
    return "\n".join(lines)
