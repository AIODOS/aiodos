"""Base SQLite unique : conversations, connaissances, file de tâches, réglages.

Une connexion par thread (sqlite3 ne se partage pas bien), mode WAL pour que
l'interface lise pendant que la file de tâches écrit. Les écritures groupées
passent par transaction(), qui pose un verrou d'écriture dès le début : c'est
ce qui remplace le verrou maison de la v1 (cf. l'écrasement de chunks.json).
"""
import sqlite3
import threading
import time
from contextlib import contextmanager

from aiodos import paths

SCHEMA_VERSION = 3

# Évolutions du schéma, appliquées dans l'ordre aux bases existantes.
MIGRATIONS = {
    # Phase 1 : machines appairées, compte des échanges de calcul (réciprocité),
    # unités réservables par d'autres nœuds, bibliothèques « gardées sur cette machine ».
    2: """
    CREATE TABLE IF NOT EXISTS peers (
        node_id   TEXT PRIMARY KEY,
        name      TEXT NOT NULL,
        key       BLOB NOT NULL,
        address   TEXT,
        port      INTEGER,
        profile   TEXT,
        paired_at REAL NOT NULL,
        last_seen REAL
    );
    CREATE TABLE IF NOT EXISTS ledger (
        node_id        TEXT PRIMARY KEY,
        given_s        REAL NOT NULL DEFAULT 0,
        received_s     REAL NOT NULL DEFAULT 0,
        given_units    INTEGER NOT NULL DEFAULT 0,
        received_units INTEGER NOT NULL DEFAULT 0
    );
    ALTER TABLE units ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE units ADD COLUMN lease_until REAL;
    ALTER TABLE collections ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0;
    """,
    # Phase 2 : clé Internet de chaque machine appairée (compagnon aiodos-net), avec les
    # indices pour la joindre (relais, adresses directes) et la date de la dernière annonce.
    3: """
    ALTER TABLE peers ADD COLUMN net_id TEXT;
    ALTER TABLE peers ADD COLUMN net_addrs TEXT;
    ALTER TABLE peers ADD COLUMN net_seen REAL;
    """,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id        INTEGER PRIMARY KEY,
    title     TEXT NOT NULL,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    model     TEXT,
    created   REAL NOT NULL,
    updated   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    thinking        TEXT,
    model           TEXT,
    sources         TEXT,
    created         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS collections (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    system  INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    origin        TEXT,
    created       REAL NOT NULL,
    UNIQUE (collection_id, source)
);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY,
    entry_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_entry ON chunks(entry_id);

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    priority   INTEGER NOT NULL,
    status     TEXT NOT NULL,
    params     TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    total      INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    skipped    INTEGER NOT NULL DEFAULT 0,
    result     TEXT,
    error      TEXT,
    created    REAL NOT NULL,
    started    REAL,
    finished   REAL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, priority, created);

CREATE TABLE IF NOT EXISTS units (
    id        INTEGER PRIMARY KEY,
    job_id    INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    task      TEXT NOT NULL,
    resource  TEXT NOT NULL,
    payload   TEXT NOT NULL,
    status    TEXT NOT NULL,
    attempts  INTEGER NOT NULL DEFAULT 0,
    node      TEXT,
    result    TEXT,
    error     TEXT,
    created   REAL NOT NULL,
    started   REAL,
    finished  REAL,
    compute_s REAL
);
CREATE INDEX IF NOT EXISTS ix_units_pick ON units(status, resource, job_id);
CREATE INDEX IF NOT EXISTS ix_units_job ON units(job_id, status);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized = set()          # chemins de bases déjà initialisées dans ce processus


def now() -> float:
    return time.time()


def connect() -> sqlite3.Connection:
    """Connexion du thread courant (recréée si la base a changé, ex. en test)."""
    path = str(paths.db_path())
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    _local.conn, _local.path, _local.depth = conn, path, 0
    _ensure_schema(conn, path)
    return conn


def _ensure_schema(conn, path):
    with _init_lock:
        if path in _initialized:
            return
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            conn.executescript(SCHEMA)
            version = 1
        for target in sorted(MIGRATIONS):
            if version < target:
                conn.executescript(MIGRATIONS[target])
                version = target
        conn.execute(f"PRAGMA user_version={version}")
        _initialized.add(path)


@contextmanager
def transaction():
    """Transaction d'écriture (BEGIN IMMEDIATE). Réentrante dans un même thread."""
    conn = connect()
    if _local.depth:
        _local.depth += 1
        try:
            yield conn
        finally:
            _local.depth -= 1
        return
    conn.execute("BEGIN IMMEDIATE")
    _local.depth = 1
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    finally:
        _local.depth = 0


def close():
    """Ferme la connexion du thread courant (tests, arrêt)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


def query(sql, params=()):
    return connect().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return connect().execute(sql, params).fetchone()


def execute(sql, params=()):
    return connect().execute(sql, params)
