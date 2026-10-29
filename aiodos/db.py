"""Base SQLite unique : conversations, connaissances, file de tâches, réglages.

Une connexion par thread (sqlite3 ne se partage pas bien), mode WAL pour que
l'interface lise pendant que la file de tâches écrit. Les écritures groupées
passent par transaction(), qui pose un verrou d'écriture dès le début : c'est
ce qui remplace le verrou maison de la v1 (cf. l'écrasement de chunks.json).
"""
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from aiodos import paths

SCHEMA_VERSION = 32

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
    # Fiabilité de chaque machine (docs/phase3-reseau-public.md, § 5) : résultats d'un ami
    # contrôlés par échantillon et conformes, résultats refusés, unités qu'elle n'a pas pu
    # calculer pour moi
    12: """
    ALTER TABLE ledger ADD COLUMN checked_units INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE ledger ADD COLUMN refused_units INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE ledger ADD COLUMN failed_units INTEGER NOT NULL DEFAULT 0;
    """,
    # Le dossier où chaque image a été écrite : changer de dossier ne perd pas les
    # précédentes (imaging/gallery.py, path_of)
    13: """
    ALTER TABLE images ADD COLUMN folder TEXT;
    """,
    # Les réponses qui n'étaient qu'une erreur portent une marque (chat/conversations.py,
    # ERROR_SOURCE) : celles d'avant la reçoivent, une fois pour toutes, d'après leur début
    14: """
    UPDATE messages SET sources='[{"type": "error"}]'
     WHERE role='assistant' AND substr(content, 1, 2)='⚠ '
       AND (sources IS NULL OR sources='' OR sources='[]');
    """,
    # Les erreurs d'avant la marque qui avaient déjà des sources (le modèle a échoué après
    # la recherche dans les bibliothèques), et celles entrées dans la mémoire des
    # conversations avant qu'elle les écarte
    15: """
    UPDATE messages SET sources=json_insert(sources, '$[#]', json('{"type": "error"}'))
     WHERE role='assistant' AND substr(content, 1, 2)='⚠ '
       AND json_valid(sources) AND json_type(sources)='array'
       AND NOT EXISTS (SELECT 1 FROM json_each(messages.sources)
                        WHERE json_extract(messages.sources, json_each.fullkey || '.type')
                              = 'error');
    DELETE FROM chunks WHERE substr(text, 1, 14)='Assistant : ⚠ '
       AND entry_id IN (SELECT e.id FROM entries e JOIN collections c ON c.id=e.collection_id
                         WHERE c.system=1);
    """,
    # Appairage par groupe (network/group.py) : la machine qui a présenté celle-ci (vide
    # pour un appairage direct), et « toujours accepter les machines de ce groupe »
    16: """
    ALTER TABLE peers ADD COLUMN introduced_by TEXT;
    ALTER TABLE peers ADD COLUMN group_ok INTEGER NOT NULL DEFAULT 0;
    """,
    # Appairage par groupe : machines refusées ou retirées ici, qu'aucune présentation ne
    # ramène (seul un nouvel appairage par code les efface) ; messages à redire à une de
    # mes machines hors ligne (retrait, mise de côté).
    17: """
    CREATE TABLE IF NOT EXISTS group_declined (
        node_id TEXT PRIMARY KEY,
        at      REAL NOT NULL,
        reason  TEXT
    );
    CREATE TABLE IF NOT EXISTS group_outbox (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        target  TEXT NOT NULL,
        action  TEXT NOT NULL,
        payload TEXT NOT NULL,
        at      REAL NOT NULL
    );
    """,
    # Retour à une version d'origine 2.0.x (updates/launcher.py) : elle ignore les niveaux de
    # confiance et se servirait de la clé d'un ami comme de celle d'une de mes machines. La
    # clé en clair (32 octets) d'un ami ou d'une machine mise de côté prend la marque « k2: »
    # (node/vault.py, HELD), comme celles enregistrées désormais (network/peers.py, _kept).
    # Une clé scellée par DPAPI, déjà marquée ou d'une autre taille n'a pas 32 octets : elle
    # reste telle quelle, et la migration rejouée ne marque rien deux fois.
    18: """
    UPDATE peers SET key = CAST(X'6B323A' || CAST(key AS BLOB) AS BLOB)
     WHERE (trust IS NOT 'mine' OR set_aside_at IS NOT NULL)
       AND length(CAST(key AS BLOB)) = 32;
    """,
    # Durée de calcul d'une image dite par la machine qui l'a faite, gardée sans contrôle
    # jusque-là : un infini (« inf » envoyé par un ami) faisait tomber la galerie et la
    # conversation qui l'avait demandée (imaging/gallery.py, MAX_SECONDS). Hors bornes :
    # oubliée.
    19: """
    UPDATE images SET seconds = NULL
     WHERE seconds IS NOT NULL AND NOT (seconds >= 0 AND seconds <= 604800);
    """,
    # Applications (gateway/apps.py, docs/api-applications.md) : une clé par appli, dont
    # seul le haché est gardé ; programmes bloqués ; journal sans contenu et compteurs.
    # reach : cercles accordés (here, mine, friends, public) ; consents : par capacité, les
    # cercles montrés à l'accord alors qu'ils étaient servis (gateway/consents.py).
    20: """
    CREATE TABLE IF NOT EXISTS gateway_apps (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL DEFAULT 'app',
        client_id     TEXT,
        name          TEXT NOT NULL,
        version       TEXT,
        purpose       TEXT,
        key_hash      TEXT NOT NULL UNIQUE,
        scopes        TEXT NOT NULL,
        libraries     TEXT NOT NULL DEFAULT '[]',
        reach         TEXT NOT NULL DEFAULT 'here,mine,friends,public',
        consents      TEXT NOT NULL DEFAULT '{}',
        priority      TEXT NOT NULL DEFAULT 'interactive',
        exe_path      TEXT,
        exe_pinned    INTEGER NOT NULL DEFAULT 0,
        account_check INTEGER NOT NULL DEFAULT 1,
        created       REAL NOT NULL,
        last_used     REAL,
        last_program  TEXT,
        expires       REAL,
        session_only  INTEGER NOT NULL DEFAULT 0,
        revoked       REAL
    );
    CREATE TABLE IF NOT EXISTS gateway_blocked (
        exe_path TEXT PRIMARY KEY,
        name     TEXT,
        at       REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gateway_usage (
        app_id    TEXT NOT NULL,
        day       TEXT NOT NULL,
        family    TEXT NOT NULL,
        place     TEXT NOT NULL,
        requests  INTEGER NOT NULL DEFAULT 0,
        errors    INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (app_id, day, family, place)
    );
    CREATE TABLE IF NOT EXISTS gateway_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        at         REAL NOT NULL,
        app_id     TEXT,
        route      TEXT NOT NULL,
        model      TEXT,
        machine    TEXT,
        place      TEXT,
        status     INTEGER NOT NULL,
        code       TEXT,
        ms         INTEGER,
        bytes_in   INTEGER,
        bytes_out  INTEGER,
        program    TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_gateway_log_app ON gateway_log(app_id, at);
    """,
    # La file comme un torrent (docs/conception/partage-puissance.md § 2.5, livraison L2a) :
    # pause « après l'unité en cours » ou « tout de suite », annulation qui atteint les autres
    # machines, reprise sans perte après un redémarrage. Les travaux des applis (L2b) y ont
    # déjà leurs colonnes : numéro public, appli, libellé, référence de l'appli, cercles.
    # Unités : cercles permis, entrée en file, disparitions (lost), arrêts d'AIODOS pendant
    # le calcul (interrupts), remises (handouts), lieu et avancement du calcul.
    21: """
    ALTER TABLE jobs ADD COLUMN public_id TEXT;
    ALTER TABLE jobs ADD COLUMN owner_app TEXT;
    ALTER TABLE jobs ADD COLUMN label TEXT;
    ALTER TABLE jobs ADD COLUMN client_ref TEXT;
    ALTER TABLE jobs ADD COLUMN reach TEXT;
    ALTER TABLE jobs ADD COLUMN pause_mode TEXT;
    ALTER TABLE jobs ADD COLUMN cancelled_by TEXT;
    ALTER TABLE jobs ADD COLUMN wait_until REAL;
    ALTER TABLE units ADD COLUMN reach TEXT;
    ALTER TABLE units ADD COLUMN queued_at REAL;
    ALTER TABLE units ADD COLUMN lost INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE units ADD COLUMN interrupts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE units ADD COLUMN handouts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE units ADD COLUMN place TEXT;
    ALTER TABLE units ADD COLUMN progress TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_public ON jobs(public_id) WHERE public_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_ref ON jobs(owner_app, client_ref)
        WHERE client_ref IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_jobs_owner_app ON jobs(owner_app, created);
    CREATE TABLE IF NOT EXISTS engine_durations (
        signature TEXT NOT NULL,
        machine   TEXT NOT NULL,
        seconds   TEXT NOT NULL,
        PRIMARY KEY (signature, machine)
    );
    """,
    # Les travaux de moteur des applis (livraison L2b, docs/conception/partage-puissance.md
    # § 5.1 à 5.3) : les entrées qu'une appli envoie avant son travail (par appli et par
    # empreinte, jamais vues d'une autre), leur lien aux travaux qui les citent, les fichiers
    # produits (gardés jusqu'à leur récupération par l'appli, puis 7 jours), et l'identifiant
    # du calcul d'une unité dans le moteur (run_uuid, le prompt_id de ComfyUI)
    22: """
    CREATE TABLE IF NOT EXISTS gateway_inputs (
        app_id   TEXT NOT NULL,
        sha256   TEXT NOT NULL,
        size     INTEGER NOT NULL,
        type     TEXT NOT NULL,
        created  REAL NOT NULL,
        used     REAL NOT NULL,
        PRIMARY KEY (app_id, sha256)
    );
    CREATE TABLE IF NOT EXISTS job_inputs (
        job_id   INTEGER NOT NULL,
        app_id   TEXT NOT NULL,
        sha256   TEXT NOT NULL,
        PRIMARY KEY (job_id, sha256)
    );
    CREATE INDEX IF NOT EXISTS ix_job_inputs_input ON job_inputs(app_id, sha256);
    CREATE TABLE IF NOT EXISTS gateway_files (
        id          TEXT PRIMARY KEY,
        app_id      TEXT NOT NULL,
        job_id      INTEGER,
        unit_index  INTEGER,
        node        TEXT,
        type        TEXT NOT NULL,
        bytes       INTEGER NOT NULL,
        sha256      TEXT NOT NULL,
        width       INTEGER,
        height      INTEGER,
        machine     TEXT,
        place       TEXT,
        engine      TEXT,
        path        TEXT NOT NULL,
        created     REAL NOT NULL,
        fetched_at  REAL,
        served      INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ix_gateway_files_job ON gateway_files(job_id, created);
    CREATE INDEX IF NOT EXISTS ix_gateway_files_app ON gateway_files(app_id, fetched_at);
    ALTER TABLE units ADD COLUMN run_uuid TEXT;
    """,
    # Le cloud est retiré (21/10/2026, décision de l'auteur) : ses réglages, clés d'API
    # comprises, sont effacés, et un modèle par défaut du cloud revient à celui d'AIODOS
    23: """
    DELETE FROM settings WHERE key IN ('cloud_confirm', 'mistral_key', 'qwen_key',
                                       'mistral_cloud_model', 'qwen_cloud_model');
    DELETE FROM settings WHERE key = 'default_model' AND value LIKE '"cloud:%';
    """,
    # Les pièces jointes d'un message (chat/attachments.py) : le fichier reste sur ce PC
    # (data/attachments/<id>/), ce qu'on en lit est gardé ici (text, text_state)
    24: """
    CREATE TABLE IF NOT EXISTS attachments (
        id          TEXT PRIMARY KEY,
        message_id  INTEGER,
        name        TEXT NOT NULL,
        kind        TEXT NOT NULL,
        type        TEXT NOT NULL,
        size        INTEGER NOT NULL,
        sha256      TEXT NOT NULL,
        created     REAL NOT NULL,
        text        TEXT,
        text_state  TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_attachments_message ON attachments(message_id);
    """,
    # « Puissance prêtée » (node/places.py, 60 % par défaut) remplace « GPU prêté au
    # maximum » (100 % par défaut) : une part choisie plus basse est reprise telle quelle
    25: """
    INSERT OR IGNORE INTO settings(key, value)
        SELECT 'lend_power', value FROM settings
        WHERE key = 'share_max_gpu' AND CAST(value AS INTEGER) BETWEEN 10 AND 99;
    DELETE FROM settings WHERE key = 'share_max_gpu';
    """,
    # Fin de partie (jobs/endgame.py, docs/conception/partage-puissance.md § 2.7) : le doublon
    # d'une unité en calcul, avec sa machine, son lieu, son départ, son bail (NULL : calculé
    # ici) et son propre run_uuid ; le calcul principal reste dans units. taken : l'instant où
    # le premier résultat d'une unité a commencé d'entrer, après quoi aucun autre ne la prend.
    26: """
    CREATE TABLE IF NOT EXISTS unit_copies (
        unit_id     INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
        node        TEXT NOT NULL,
        place       TEXT NOT NULL,
        started     REAL NOT NULL,
        lease_until REAL,
        run_uuid    TEXT,
        PRIMARY KEY (unit_id, node)
    );
    CREATE INDEX IF NOT EXISTS ix_unit_copies_node ON unit_copies(node);
    ALTER TABLE units ADD COLUMN taken REAL;
    """,
    # Réseau public qui calcule (network/public_chat.py, chat/public_consent.py) : « Sur mes
    # machines seulement », par conversation, désactivé par défaut ; activé, rien de cette
    # conversation ne part chez un inconnu
    27: """
    ALTER TABLE conversations ADD COLUMN mine_only INTEGER NOT NULL DEFAULT 0;
    """,
    # Contrôles au hasard du réseau public (network/spot_check.py, network/reputation.py) :
    # par clé de prêteur (la clé publique de son nœud, jamais une clé jetable), les réponses
    # servies ici, la vitesse mesurée ici, les contrôles réussis ou ratés, la mise de côté
    # (aside_until : jusqu'à quand ; forever : pour de bon). Rien n'est partagé.
    28: """
    CREATE TABLE IF NOT EXISTS public_reputation (
        key           TEXT PRIMARY KEY,
        served        INTEGER NOT NULL DEFAULT 0,
        tps           REAL,
        checks_ok     INTEGER NOT NULL DEFAULT 0,
        checks_failed INTEGER NOT NULL DEFAULT 0,
        aside_until   REAL,
        forever       INTEGER NOT NULL DEFAULT 0,
        first_seen    REAL NOT NULL,
        last_seen     REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_public_reputation_seen ON public_reputation(last_seen);
    """,
    # Contre la collusion (network/sightings.py, network/reputation.py, network/spot_check.py,
    # 29/10/2026) : la première vue de chaque clé de l'annuaire public et l'empreinte de sa
    # liste de modèles ; par prêteur, les contrôles réussis comptés par les règles d'aujourd'hui
    # (une de mes machines, ou deux machines de contrôle indépendantes) et ceux sans conclusion.
    # Les clés déjà notées gardent leur première vue.
    29: """
    ALTER TABLE public_reputation ADD COLUMN checks_sure INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE public_reputation ADD COLUMN checks_open INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE IF NOT EXISTS public_sightings (
        key        TEXT PRIMARY KEY,
        first_seen REAL NOT NULL,
        last_seen  REAL NOT NULL,
        models     TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_public_sightings_seen ON public_sightings(last_seen);
    INSERT OR IGNORE INTO public_sightings(key, first_seen, last_seen)
        SELECT key, first_seen, last_seen FROM public_reputation;
    """,
    # Dossiers suivis des bibliothèques (knowledge/folders.py, 29/10/2026) : chaque dossier
    # ajouté (chemin, préfixe de ses entrées, dernière lecture, en ligne ou non, fichiers non
    # pris en charge), et chaque fichier lu (taille et date à sa lecture, état : en attente,
    # indexé, sauté, en échec, annulé ; raison ; travail qui l'a en charge)
    30: """
    CREATE TABLE IF NOT EXISTS library_folders (
        id            INTEGER PRIMARY KEY,
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        path          TEXT NOT NULL,
        root          TEXT NOT NULL,
        added         REAL NOT NULL,
        scanned       REAL,
        online        INTEGER NOT NULL DEFAULT 1,
        ignored       INTEGER NOT NULL DEFAULT 0,
        ignored_types TEXT,
        UNIQUE (collection_id, path)
    );
    CREATE TABLE IF NOT EXISTS library_files (
        folder_id INTEGER NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE,
        rel       TEXT NOT NULL,
        size      INTEGER,
        mtime_ns  INTEGER,
        state     TEXT NOT NULL,
        reason    TEXT,
        job_id    INTEGER,
        PRIMARY KEY (folder_id, rel)
    );
    """,
    # Ce que cette machine calcule pour d'autres (network/lent_history.py, 29/10/2026) : une
    # ligne par calcul fini, refusé ou arrêté, pour le réseau public, un ami ou une de mes
    # machines (son nom, jamais pour le réseau public) ; le genre, le modèle, la durée, la
    # taille, l'issue et sa raison courte ; des refus ou arrêts identiques réunis (times).
    # Jamais le contenu, ni rien qui identifie un demandeur du réseau public. 90 jours et
    # 5 000 lignes au plus
    31: """
    CREATE TABLE IF NOT EXISTS lent_history (
        id       INTEGER PRIMARY KEY,
        at       REAL NOT NULL,
        kind     TEXT NOT NULL,
        circle   TEXT NOT NULL,
        peer     TEXT,
        model    TEXT,
        seconds  REAL,
        tokens   INTEGER,
        width    INTEGER,
        height   INTEGER,
        frames   INTEGER,
        passages INTEGER,
        outcome  TEXT NOT NULL,
        reason   TEXT,
        times    INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS ix_lent_history_at ON lent_history(at);
    """,
    # « Automatique » (auto:best) devient le modèle de texte par défaut (décision de l'auteur
    # du 29/10/2026, chat/model_menu.py) : le choix par défaut d'avant s'enregistrait à chaque
    # changement de modèle dans une conversation ; il est oublié une fois, et se règle
    # désormais dans Réglages → Modèles
    32: """
    DELETE FROM settings WHERE key = 'default_model';
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


def is_open() -> bool:
    """Une base a-t-elle déjà été ouverte dans ce processus ? Sans en créer ni en migrer
    une (la langue des textes, lue avant le démarrage du nœud, cf. i18n/language.py)."""
    return bool(_initialized)


def connect() -> sqlite3.Connection:
    """Connexion du thread courant (recréée si la base a changé, ex. en test)."""
    path = str(paths.db_path())
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Minuscules de tout l'Unicode (« École » → « école ») : lower() de SQLite ne connaît
    # que l'ASCII (recherche dans les conversations)
    conn.create_function("aiodos_fold", 1, lambda text: text.casefold()
                         if isinstance(text, str) else text, deterministic=True)
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
                conn.executescript(_replayable(conn, MIGRATIONS[target]))
                version = target
                conn.execute(f"PRAGMA user_version={version}")    # migration par migration
        conn.execute(f"PRAGMA user_version={version}")
        _initialized.add(path)


_ADD_COLUMN = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)[^;]*;", re.IGNORECASE)


def _replayable(conn, script: str) -> str:
    """Une migration rejouée (arrêt au milieu d'une mise à jour, base remise à une version
    antérieure) ne doit pas échouer sur une colonne déjà ajoutée : ces ajouts sont sautés."""
    def keep(match):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({match.group(1)})")}
        return "" if match.group(2) in columns else match.group(0)
    return _ADD_COLUMN.sub(keep, script)


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
    _local.after = []
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    finally:
        _local.depth = 0
        hooks, _local.after = _local.after, []
        for hook in hooks:
            hook()


def after_transaction(hook):
    """hook() une fois finie la transaction en cours de ce fil, ou tout de suite s'il n'y
    en a pas : un cache vidé avant le COMMIT se remplirait, entre-temps, de l'état d'avant
    (knowledge/search.invalidate)."""
    if getattr(_local, "depth", 0):
        _local.after.append(hook)
    else:
        hook()


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
