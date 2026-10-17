"""Bibliothèques consultables : ce que les machines appairées peuvent y chercher.

Chercher dans la bibliothèque d'une autre machine ne copie aucun fichier : la question
part chez elle, qui cherche dans ce qui est ouvert à la machine qui demande et ne rend que
quelques passages (network/consult.py). Ce module dit ce qui est ouvert, et à qui :

- chaque bibliothèque est « consultable par » personne (par défaut), mes machines, ou mes
  machines et mes amis. « Tout le monde » (EVERYONE) est réservé aux inconnus de la
  phase 3 : la base a sa place, mais set_open_to le refuse encore ;
- dans une bibliothèque ouverte, un dossier ou un document peut être fermé. Il ne sort
  jamais, même réindexé, et un fichier ajouté plus tard dans un dossier fermé l'est aussi ;
- une bibliothèque gardée sur ce PC (le cadenas) et la mémoire des conversations ne sont
  jamais consultables, quel que soit le réglage enregistré.

Ce réglage ne touche pas à la portée de calcul (store.set_scope) : une bibliothèque peut
être calculée par mes amis sans être consultable par eux, et l'inverse.

Deux contrôles indépendants protègent ce qui est fermé : les passages fermés sont écartés
avant le classement (search.search_in), puis chaque passage trouvé est revérifié par une
requête à part avant de sortir (still_open).
"""
from aiodos import db
from aiodos.knowledge import search as vector_search

NOBODY, MINE, FRIENDS, EVERYONE = "none", "mine", "friends", "everyone"
LEVELS = (NOBODY, MINE, FRIENDS)          # proposés aujourd'hui ; EVERYONE : phase 3

# Entrée fermée : son document est fermé, ou un dossier qui la contient (même début de
# chemin suivi de « / » ; substr plutôt que LIKE, où « % » et « _ » d'un nom compteraient)
_CLOSED = """EXISTS (SELECT 1 FROM closed_parts p WHERE p.collection_id = e.collection_id
             AND (p.path = e.source OR (p.folder = 1 AND
                  substr(e.source, 1, length(p.path) + 1) = p.path || '/')))"""


def _editable(collection_id):
    row = db.query_one("SELECT system, local_only FROM collections WHERE id=?", (collection_id,))
    if row is None:
        raise ValueError("Bibliothèque introuvable.")
    if row["system"]:
        raise ValueError("La mémoire des conversations n'est jamais consultable.")
    return row


# ------------------------------------------------------------------ réglages
def set_open_to(collection_id, level: str):
    """Qui peut chercher dans cette bibliothèque : NOBODY, MINE ou FRIENDS."""
    if level == EVERYONE:
        raise ValueError("« Tout le monde » viendra avec la phase 3 (machines inconnues) : "
                         "pas encore.")
    if level not in LEVELS:
        raise ValueError(f"Réglage de consultation inconnu : {level!r}")
    row = _editable(collection_id)
    if row["local_only"] and level != NOBODY:
        raise ValueError("Cette bibliothèque est gardée sur ce PC : elle n'est jamais "
                         "consultable. Retire d'abord le cadenas.")
    db.execute("UPDATE collections SET open_to=? WHERE id=?", (level, collection_id))


def _path(path, folder: bool) -> str:
    """Chemin tel que la règle le comparera : celui d'un document, exactement comme il est
    enregistré (« …/docs/ » et « …/docs » sont deux pages, une espace compte) ; celui d'un
    dossier, sans « / » final."""
    path = str(path or "")
    if folder:
        path = path.rstrip("/")
    if not path.strip():
        raise ValueError("Chemin vide.")
    return path


def close(collection_id, path, folder: bool):
    """Ferme à la consultation un document (folder=False : ce chemin exact) ou un dossier
    (tout ce qui est dessous, y compris ce qui y sera indexé plus tard). Un chemin qui ne
    désigne rien dans la bibliothèque est refusé : la règle ne fermerait rien."""
    _editable(collection_id)
    path = _path(path, folder)
    if folder:
        found = db.query_one("SELECT 1 FROM entries WHERE collection_id=? AND "
                             "substr(source, 1, length(?) + 1) = ? || '/' LIMIT 1",
                             (collection_id, path, path))
    else:
        found = db.query_one("SELECT 1 FROM entries WHERE collection_id=? AND source=? LIMIT 1",
                             (collection_id, path))
    if not found:
        raise ValueError(f"« {path} » n'est pas dans cette bibliothèque : rien à fermer.")
    db.execute("INSERT OR IGNORE INTO closed_parts(collection_id, path, folder, created) "
               "VALUES(?, ?, ?, ?)", (collection_id, path, 1 if folder else 0, db.now()))


def reopen(collection_id, path, folder: bool):
    db.execute("DELETE FROM closed_parts WHERE collection_id=? AND path=? AND folder=?",
               (collection_id, _path(path, folder), 1 if folder else 0))


def closed_parts() -> dict:
    """{bibliothèque: [{"path", "folder"}]} : les dossiers et documents fermés."""
    parts = {}
    for r in db.query("SELECT collection_id, path, folder FROM closed_parts "
                      "ORDER BY collection_id, path COLLATE NOCASE"):
        parts.setdefault(r["collection_id"], []).append(
            {"path": r["path"], "folder": bool(r["folder"])})
    return parts


# ----------------------------------------------------- ce qui est ouvert, à qui
def _marks(levels) -> str:
    return ",".join("?" * len(levels))


def open_collections(levels) -> list:
    """(id, nom) des bibliothèques ouvertes à l'un de ces niveaux (MINE, FRIENDS…) :
    jamais celles gardées sur ce PC, ni la mémoire des conversations."""
    levels = tuple(levels)
    if not levels:
        return []
    return [(r["id"], r["name"]) for r in db.query(
        f"SELECT id, name FROM collections WHERE system=0 AND local_only=0 "
        f"AND open_to IN ({_marks(levels)}) ORDER BY id", levels)]


def closed_chunk_ids(collection_id) -> list:
    """Passages de cette bibliothèque qui ne doivent jamais sortir (parties fermées)."""
    return [r["id"] for r in db.query(
        f"SELECT k.id FROM chunks k JOIN entries e ON e.id = k.entry_id "
        f"WHERE e.collection_id=? AND {_CLOSED}", (collection_id,))]


def still_open(entry_ids, levels) -> set:
    """Parmi ces entrées, celles qui sont bien ouvertes à ces niveaux : second contrôle,
    fait d'une seule requête, indépendant du choix des passages."""
    entry_ids, levels = tuple(set(entry_ids)), tuple(levels)
    if not entry_ids or not levels:
        return set()
    return {r["id"] for r in db.query(
        f"""SELECT e.id FROM entries e JOIN collections c ON c.id = e.collection_id
            WHERE e.id IN ({_marks(entry_ids)}) AND c.system = 0 AND c.local_only = 0
              AND c.open_to IN ({_marks(levels)}) AND NOT {_CLOSED}""",
        entry_ids + levels)}


def search(vector, k, levels) -> list:
    """Les k passages les plus proches dans ce qui est ouvert à ces niveaux, et rien
    d'autre (cf. les deux contrôles du docstring du module)."""
    collections = [cid for cid, _name in open_collections(levels)]
    hits = vector_search.search_in(vector, k, collections, exclude=closed_chunk_ids)
    allowed = still_open([h["entry_id"] for h in hits], levels)
    return [h for h in hits if h["entry_id"] in allowed]
