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

SCHEMA_VERSION = 11

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
    # Phase 2.5, partage avec les amis : niveau de confiance de chaque machine appairée
    # (« mine » : mes machines, « friend » : un ami ; les machines déjà appairées sont les
    # miennes), mise de côté après un contrôle échoué, bibliothèques partageables avec mes
    # amis, et journal de ce qui est parti chez eux.
    4: """
    ALTER TABLE peers ADD COLUMN trust TEXT NOT NULL DEFAULT 'mine';
    ALTER TABLE peers ADD COLUMN set_aside_at REAL;
    ALTER TABLE peers ADD COLUMN set_aside_reason TEXT;
    ALTER TABLE collections ADD COLUMN share_friends INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE IF NOT EXISTS shared_log (
        id      INTEGER PRIMARY KEY,
        node_id TEXT NOT NULL,
        at      REAL NOT NULL,
        unit_id INTEGER,
        task    TEXT NOT NULL,
        what    TEXT NOT NULL,
        sent_as TEXT,
        size    INTEGER
    );
    CREATE INDEX IF NOT EXISTS ix_shared_log_node ON shared_log(node_id, at);
    """,
    # Phase 2.4, invitation à distance : une invitation envoyée (« sent ») ou reçue
    # (« received »), son secret à usage unique, et l'autre machine une fois connue (clé
    # Internet et indices pour la joindre), le temps de s'appairer par les compagnons.
    5: """
    CREATE TABLE IF NOT EXISTS invitations (
        id        INTEGER PRIMARY KEY,
        secret    TEXT NOT NULL UNIQUE,
        role      TEXT NOT NULL,
        created   REAL NOT NULL,
        expires   REAL NOT NULL,
        node_id   TEXT,
        name      TEXT,
        net_id    TEXT,
        net_addrs TEXT,
        used      REAL
    );
    """,
    # Prêt pendant l'utilisation (17/10/2026) : share_when_idle passe à False par défaut.
    # Une machine où le partage a déjà été réglé garde l'ancienne règle (« seulement quand
    # je n'utilise pas ce PC »), celle que son utilisateur a acceptée, sauf s'il a choisi
    # lui-même : le nouveau défaut ne vaut que pour les nouvelles installations.
    6: """
    INSERT INTO settings(key, value)
    SELECT 'share_when_idle', 'true'
    WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'share_enabled')
      AND NOT EXISTS (SELECT 1 FROM settings WHERE key = 'share_when_idle');
    """,
    # Bibliothèques consultables (recherche par les machines appairées, sans copie des
    # fichiers) : qui peut chercher dans chaque bibliothèque (« none » : personne, par
    # défaut ; « mine » : mes machines ; « friends » : mes machines et mes amis ;
    # « everyone » réservé aux inconnus de la phase 3), les dossiers et documents fermés
    # d'une bibliothèque ouverte, et le journal des recherches reçues.
    7: """
    ALTER TABLE collections ADD COLUMN open_to TEXT NOT NULL DEFAULT 'none';
    CREATE TABLE IF NOT EXISTS closed_parts (
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        path          TEXT NOT NULL,
        folder        INTEGER NOT NULL DEFAULT 0,
        created       REAL NOT NULL,
        PRIMARY KEY (collection_id, path, folder)
    );
    CREATE TABLE IF NOT EXISTS consult_log (
        id        INTEGER PRIMARY KEY,
        node_id   TEXT NOT NULL,
        at        REAL NOT NULL,
        passages  INTEGER NOT NULL DEFAULT 0,
        documents TEXT,
        refused   TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_consult_log_node ON consult_log(node_id, at);
    """,
    # Images générées (imaging/gallery.py) : une ligne par image, le PNG dans data/images/out
    8: """
    CREATE TABLE IF NOT EXISTS images (
        id       INTEGER PRIMARY KEY,
        batch    TEXT NOT NULL,
        model    TEXT NOT NULL,
        prompt   TEXT NOT NULL,
        negative TEXT NOT NULL DEFAULT '',
        seed     INTEGER NOT NULL,
        width    INTEGER NOT NULL,
        height   INTEGER NOT NULL,
        steps    INTEGER NOT NULL,
        cfg      REAL NOT NULL,
        file     TEXT NOT NULL,
        machine  TEXT,
        seconds  REAL,
        created  REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_images_batch ON images(batch);
    """,
    # Vidéos dans la même galerie (Wan 2.1) : genre, nombre d'images, cadence
    9: """
    ALTER TABLE images ADD COLUMN media TEXT NOT NULL DEFAULT 'image';
    ALTER TABLE images ADD COLUMN frames INTEGER;
    ALTER TABLE images ADD COLUMN fps INTEGER;
    """,
    # Conversations servies par un modèle prêté, dans chaque sens (« Machines connectées »)
    10: """
    ALTER TABLE ledger ADD COLUMN given_chats INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE ledger ADD COLUMN received_chats INTEGER NOT NULL DEFAULT 0;
    """,
    # Images et vidéos créées dans la conversation (chat/creations.py) : la réponse de
    # l'assistant garde sa demande (lot, travail, modèle, description), en JSON
    11: """
    ALTER TABLE messages ADD COLUMN media TEXT;
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
