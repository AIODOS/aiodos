"""Bibliothèques : collections → entrées (fichier, URL, conversation) → passages.

Chaque passage garde son vecteur d'embedding (float32) pour la recherche.
La collection système « 💬 Conversations » sert de mémoire entre conversations ;
elle n'apparaît pas dans la liste des bibliothèques.
"""
import numpy as np

from aiodos import db
from aiodos.knowledge import search

DEFAULT_COLLECTION = "Général"
CONV_COLLECTION = "💬 Conversations"


def ensure_defaults():
    with db.transaction() as c:
        c.execute("INSERT OR IGNORE INTO collections(name, enabled, system, created) "
                  "VALUES(?, 1, 1, ?)", (CONV_COLLECTION, db.now()))
        has_library = c.execute(
            "SELECT 1 FROM collections WHERE system=0 LIMIT 1").fetchone()
        if not has_library:
            c.execute("INSERT INTO collections(name, enabled, system, created) "
                      "VALUES(?, 1, 0, ?)", (DEFAULT_COLLECTION, db.now()))


# ----------------------------------------------------------------- collections
def list_collections(include_system=False) -> list:
    rows = db.query("""
        SELECT c.id, c.name, c.enabled, c.system, c.local_only,
               (SELECT COUNT(*) FROM entries e WHERE e.collection_id = c.id) AS entries,
               (SELECT COUNT(*) FROM chunks k JOIN entries e ON e.id = k.entry_id
                 WHERE e.collection_id = c.id) AS chunks
        FROM collections c ORDER BY c.system, c.name COLLATE NOCASE""")
    return [dict(r, enabled=bool(r["enabled"]), system=bool(r["system"]),
                 local_only=bool(r["local_only"]))
            for r in rows if include_system or not r["system"]]


def collection_name(collection_id) -> str:
    row = db.query_one("SELECT name FROM collections WHERE id=?", (collection_id,))
    if row is None:
        raise ValueError("Bibliothèque introuvable.")
    return row["name"]


def conversation_collection_id() -> int:
    row = db.query_one("SELECT id FROM collections WHERE system=1 LIMIT 1")
    if row is None:
        ensure_defaults()
        row = db.query_one("SELECT id FROM collections WHERE system=1 LIMIT 1")
    return row["id"]


def _check_name(name) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Le nom ne peut pas être vide.")
    if db.query_one("SELECT 1 FROM collections WHERE name=?", (name,)):
        raise ValueError(f"Une bibliothèque « {name} » existe déjà.")
    return name


def create_collection(name) -> int:
    name = _check_name(name)
    return db.execute("INSERT INTO collections(name, enabled, system, created) "
                      "VALUES(?, 1, 0, ?)", (name, db.now())).lastrowid


def _ensure_editable(collection_id):
    row = db.query_one("SELECT system FROM collections WHERE id=?", (collection_id,))
    if row is None:
        raise ValueError("Bibliothèque introuvable.")
    if row["system"]:
        raise ValueError("La mémoire des conversations ne se modifie pas ici.")


def rename_collection(collection_id, name):
    _ensure_editable(collection_id)
    current = collection_name(collection_id)
    if (name or "").strip() == current:
        return
    db.execute("UPDATE collections SET name=? WHERE id=?",
               (_check_name(name), collection_id))


def delete_collection(collection_id):
    _ensure_editable(collection_id)
    db.execute("DELETE FROM collections WHERE id=?", (collection_id,))
    search.invalidate()


def set_enabled(collection_id, enabled: bool):
    db.execute("UPDATE collections SET enabled=? WHERE id=?",
               (1 if enabled else 0, collection_id))


def set_local_only(collection_id, local_only: bool):
    """Bibliothèque « sur cette machine seulement » : ses fichiers ne sont jamais
    confiés à une autre machine pour le calcul."""
    _ensure_editable(collection_id)
    db.execute("UPDATE collections SET local_only=? WHERE id=?",
               (1 if local_only else 0, collection_id))


def local_only_ids() -> set:
    return {r["id"] for r in db.query("SELECT id FROM collections WHERE local_only=1")}


def embedding_dim(collection_id) -> int | None:
    """Dimension des vecteurs déjà stockés dans cette bibliothèque (None si vide).
    Les passages sans vecteur (en attente de réindexation) ne comptent pas."""
    row = db.query_one("SELECT length(k.embedding) AS n FROM chunks k "
                       "JOIN entries e ON e.id = k.entry_id WHERE e.collection_id=? "
                       "AND length(k.embedding) > 0 LIMIT 1", (collection_id,))
    return row["n"] // 4 if row and row["n"] else None


# --------------------------------------------------------------------- entrées
def list_entries(collection_id=None, include_system=False) -> list:
    sql = """SELECT e.id, e.collection_id, c.name AS collection, e.source, e.kind,
                    e.origin, e.created,
                    (SELECT COUNT(*) FROM chunks k WHERE k.entry_id = e.id) AS chunks
             FROM entries e JOIN collections c ON c.id = e.collection_id
             WHERE (? IS NULL OR e.collection_id = ?) AND (? OR c.system = 0)
             ORDER BY c.name COLLATE NOCASE, e.source COLLATE NOCASE"""
    return [dict(r) for r in db.query(
        sql, (collection_id, collection_id, 1 if include_system else 0))]


def _blobs(entry_id, chunks, first_idx=0):
    return [(entry_id, first_idx + i, text, np.asarray(vec, dtype=np.float32).tobytes())
            for i, (text, vec) in enumerate(chunks)]


def upsert_entry(collection_id, source, kind, origin, chunks) -> int:
    """Remplace l'entrée (même bibliothèque + même source) par ces passages.

    chunks : liste de couples (texte, vecteur).
    """
    with db.transaction() as c:
        c.execute("DELETE FROM entries WHERE collection_id=? AND source=?",
                  (collection_id, source))
        entry_id = c.execute(
            "INSERT INTO entries(collection_id, source, kind, origin, created) "
            "VALUES(?, ?, ?, ?, ?)",
            (collection_id, source, kind, origin, db.now())).lastrowid
        c.executemany("INSERT INTO chunks(entry_id, idx, text, embedding) "
                      "VALUES(?, ?, ?, ?)", _blobs(entry_id, chunks))
    search.invalidate()
    return entry_id


def entry_by_origin(collection_id, origin):
    row = db.query_one("SELECT id, source FROM entries WHERE collection_id=? AND origin=?",
                       (collection_id, origin))
    return dict(row) if row else None


def max_chunk_index(entry_id) -> int:
    row = db.query_one("SELECT MAX(idx) AS m FROM chunks WHERE entry_id=?", (entry_id,))
    return row["m"] if row and row["m"] is not None else -1


def append_chunks(collection_id, source, kind, origin, chunks_with_idx, replace=False):
    """Ajoute des passages à une entrée retrouvée par son origine (créée au besoin).
    Sert à la mémoire des conversations, indexée message par message.
    replace=True : les passages déjà là sont d'abord retirés (reconstruction)."""
    with db.transaction() as c:
        row = c.execute("SELECT id FROM entries WHERE collection_id=? AND origin=?",
                        (collection_id, origin)).fetchone()
        if row is None:
            entry_id = c.execute(
                "INSERT INTO entries(collection_id, source, kind, origin, created) "
                "VALUES(?, ?, ?, ?, ?)",
                (collection_id, source, kind, origin, db.now())).lastrowid
        else:
            entry_id = row["id"]
            c.execute("UPDATE entries SET source=? WHERE id=?", (source, entry_id))
            if replace:
                c.execute("DELETE FROM chunks WHERE entry_id=?", (entry_id,))
        c.executemany("INSERT INTO chunks(entry_id, idx, text, embedding) VALUES(?, ?, ?, ?)",
                      [(entry_id, idx, text, np.asarray(vec, dtype=np.float32).tobytes())
                       for idx, text, vec in chunks_with_idx])
    search.invalidate()


def chunk_texts(entry_id) -> list:
    """Texte des passages d'une entrée, dans l'ordre (vide si l'entrée n'existe plus)."""
    return [r["text"] for r in db.query(
        "SELECT text FROM chunks WHERE entry_id=? ORDER BY idx", (entry_id,))]


def replace_chunks(entry_id, chunks) -> bool:
    """Remplace les passages d'une entrée, qui garde son id, sa source et son
    origine (réindexation). Faux si l'entrée a été retirée entre-temps.

    chunks : liste de couples (texte, vecteur).
    """
    with db.transaction() as c:
        if c.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None:
            return False
        c.execute("DELETE FROM chunks WHERE entry_id=?", (entry_id,))
        c.executemany("INSERT INTO chunks(entry_id, idx, text, embedding) "
                      "VALUES(?, ?, ?, ?)", _blobs(entry_id, chunks))
    search.invalidate()
    return True


def retire_vectors():
    """Efface les vecteurs de tous les passages, en gardant leur texte : ils
    viennent d'un modèle d'embedding qui n'est plus celui des questions. La
    recherche ignore les passages sans vecteur jusqu'à leur réindexation."""
    db.execute("UPDATE chunks SET embedding=? WHERE length(embedding) > 0", (b"",))
    search.invalidate()


def remove_entry(entry_id):
    db.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    search.invalidate()


def remove_prefix(collection_id, prefix) -> int:
    """Retire un dossier importé : toutes les entrées dont la source commence par prefix/."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    n = db.execute("DELETE FROM entries WHERE collection_id=? AND "
                   "(source=? OR source LIKE ? ESCAPE '\\')",
                   (collection_id, prefix, escaped + "/%")).rowcount
    search.invalidate()
    return n


def remove_by_origin(collection_id, origin):
    db.execute("DELETE FROM entries WHERE collection_id=? AND origin=?",
               (collection_id, origin))
    search.invalidate()


def count_chunks() -> int:
    return db.query_one("SELECT COUNT(*) AS n FROM chunks")["n"]
