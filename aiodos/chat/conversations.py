"""Conversations, messages et dossiers de classement."""
import json

from aiodos import db
from aiodos.knowledge import store

DEFAULT_TITLE = "Nouvelle conversation"
ERROR_MARK = "⚠ "                 # une réponse qui n'est qu'une erreur : montrée, jamais réutilisée
# À la place d'une réponse appuyée sur des passages d'autres machines, pour un modèle qui ne
# doit pas les recevoir (cloud, modèle prêté par un ami : cf. pipeline.peers_allowed)
WITHHELD = ("(Réponse appuyée sur des passages trouvés chez d'autres machines : "
            "non transmise à ce modèle.)")


def list_tree() -> dict:
    return {
        "folders": [dict(r) for r in db.query(
            "SELECT id, name FROM folders ORDER BY name COLLATE NOCASE")],
        "conversations": [dict(r) for r in db.query(
            "SELECT id, title, folder_id, model, updated FROM conversations "
            "ORDER BY updated DESC")],
    }


def create(title=DEFAULT_TITLE, model=None, folder_id=None) -> int:
    now = db.now()
    return db.execute(
        "INSERT INTO conversations(title, folder_id, model, created, updated) "
        "VALUES(?, ?, ?, ?, ?)", (title, folder_id, model, now, now)).lastrowid


def get(conv_id) -> dict | None:
    row = db.query_one("SELECT * FROM conversations WHERE id=?", (conv_id,))
    if row is None:
        return None
    conv = dict(row)
    conv["messages"] = [
        dict(m, sources=json.loads(m["sources"] or "[]"),
             media=json.loads(m["media"]) if m["media"] else None)
        for m in db.query("SELECT id, role, content, thinking, model, sources, media, created "
                          "FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,))]
    return conv


def search(query: str, limit: int = 50) -> list:
    """Conversations dont un message contient la recherche, la plus récente d'abord :
    [{"id", "snippet"}], l'extrait autour du premier passage trouvé (titre : cherché par
    l'interface elle-même)."""
    query = " ".join(str(query or "").split())
    if len(query) < 2:
        return []
    # instr, pas LIKE : ni jokers à protéger, et les capitales accentuées comptent
    # (« école » trouve « École », db.aiodos_fold)
    found, seen = [], set()
    for row in db.query("SELECT conversation_id, content FROM messages WHERE "
                        "instr(aiodos_fold(content), ?) > 0 ORDER BY id DESC LIMIT 2000",
                        (query.casefold(),)):
        if row["conversation_id"] in seen:
            continue
        seen.add(row["conversation_id"])
        found.append({"id": row["conversation_id"], "snippet": _snippet(row["content"], query)})
        if len(found) >= limit:
            break
    return found


def _snippet(text: str, query: str, before: int = 18, after: int = 70) -> str:
    """Quelques mots autour du passage trouvé, qui reste visible dans une liste étroite."""
    text = " ".join(str(text or "").split())
    at = text.lower().find(query.lower())
    if at < 0:
        return text[:before + after]
    start, end = max(0, at - before), min(len(text), at + len(query) + after)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def rename(conv_id, title):
    title = (title or "").strip()
    if not title:
        raise ValueError("Le titre ne peut pas être vide.")
    db.execute("UPDATE conversations SET title=? WHERE id=?", (title[:200], conv_id))


def move(conv_id, folder_id):
    db.execute("UPDATE conversations SET folder_id=? WHERE id=?", (folder_id, conv_id))


def delete(conv_id):
    """Supprime la conversation ET sa trace dans la mémoire inter-conversations."""
    db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    store.remove_by_origin(store.conversation_collection_id(), str(conv_id))


def add_message(conv_id, role, content, thinking=None, model=None, sources=None,
                conversation_model=None, media=None) -> int:
    """model : le modèle qui a répondu ; conversation_model : celui que la conversation
    garde pour la suite, s'il diffère (mode automatique : « auto:… », alors que la
    réponse retient la machine qui l'a calculée). media : images ou vidéos créées dans
    la conversation (chat/creations.py)."""
    now = db.now()
    with db.transaction() as c:
        message_id = c.execute(
            "INSERT INTO messages(conversation_id, role, content, thinking, model, "
            "sources, media, created) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (conv_id, role, content, thinking, model,
             json.dumps(sources or [], ensure_ascii=False),
             json.dumps(media, ensure_ascii=False) if media else None, now)).lastrowid
        c.execute("UPDATE conversations SET updated=?, model=COALESCE(?, model) WHERE id=?",
                  (now, conversation_model or model, conv_id))
    return message_id


def last_media(conv_id) -> dict | None:
    """La demande d'images ou de vidéos de la dernière réponse, si cette réponse en est une
    (chat/retouches.py : « Fais-la de nuit » se rapporte à elle)."""
    row = db.query_one("SELECT media FROM messages WHERE conversation_id=? AND role='assistant' "
                       "ORDER BY id DESC LIMIT 1", (conv_id,))
    if row is None or not row["media"]:
        return None
    try:
        media = json.loads(row["media"])
    except ValueError:
        return None
    return media if isinstance(media, dict) and media.get("prompt") else None


def creation_message(message_id) -> dict | None:
    """Une réponse de création (colonne media), le message qui l'a demandée, et si elle est
    la dernière de sa conversation (« Répondre en texte », chat/creations.py)."""
    row = db.query_one("SELECT id, conversation_id, media FROM messages WHERE id=? "
                       "AND role='assistant'", (int(message_id),))
    if row is None or not row["media"]:
        return None
    try:
        media = json.loads(row["media"])
    except ValueError:
        return None
    last = db.query_one("SELECT MAX(id) AS id FROM messages WHERE conversation_id=?",
                        (row["conversation_id"],))["id"]
    asked = db.query_one("SELECT id, content FROM messages WHERE conversation_id=? AND id<? "
                         "AND role='user' ORDER BY id DESC LIMIT 1",
                         (row["conversation_id"], row["id"]))
    return {"conversation_id": row["conversation_id"], "media": media, "last": last == row["id"],
            "asked_id": asked["id"] if asked else None,
            "asked": asked["content"] if asked else None}


def remove_messages(conv_id, ids):
    """Retire ces messages de la conversation."""
    ids = [int(i) for i in ids]
    if ids:
        marks = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM messages WHERE conversation_id=? AND id IN ({marks})",
                   (conv_id, *ids))


def rewind(conv_id, message_id) -> str | None:
    """Reprend la conversation à ce message de l'utilisateur (« Régénérer », « Modifier ») :
    lui et tout ce qui le suit en sont retirés, et de la mémoire des conversations (ses
    passages portent le numéro de leur message : idx = id × 1000 + n). → son texte, ou None
    si ce n'est pas un message de l'utilisateur dans cette conversation."""
    row = db.query_one("SELECT content FROM messages WHERE id=? AND conversation_id=? "
                       "AND role='user'", (int(message_id), conv_id))
    if row is None:
        return None
    db.execute("DELETE FROM messages WHERE conversation_id=? AND id >= ?",
               (conv_id, int(message_id)))
    store.remove_chunks_from(store.conversation_collection_id(), str(conv_id),
                             int(message_id) * 1000)
    return row["content"]


def by_batch(batches) -> dict:
    """{lot : conversation} des images et vidéos demandées dans une conversation (la
    galerie mène à leur conversation)."""
    batches = sorted({str(b) for b in batches if b})
    if not batches:
        return {}
    marks = ",".join("?" * len(batches))
    return {r["batch"]: r["conversation_id"] for r in db.query(
        "SELECT json_extract(media, '$.batch') AS batch, conversation_id FROM messages "
        f"WHERE media IS NOT NULL AND json_extract(media, '$.batch') IN ({marks})",
        tuple(batches))}


def cites_others(sources) -> bool:
    """La réponse s'appuie-t-elle sur des passages trouvés chez d'autres machines ?
    `sources` : la liste enregistrée avec le message, ou son texte JSON."""
    if isinstance(sources, str):
        try:
            sources = json.loads(sources or "[]")
        except ValueError:
            return False
    return any(isinstance(s, dict) and s.get("type") == "remote" for s in sources or [])


def history(conv_id, limit=30, with_others=True) -> list:
    """Derniers échanges, dans l'ordre, au format attendu par les modèles. with_others=False :
    une réponse appuyée sur des passages d'autres machines est remplacée par WITHHELD."""
    return as_history(history_rows(conv_id, limit),
                      None if with_others else (lambda sources: not cites_others(sources)))


def history_rows(conv_id, limit=30) -> list:
    """Les derniers messages, dans l'ordre, avec leurs sources (lus avant d'ajouter la
    question : routes/chat.py en tire l'historique de chaque machine qui peut répondre)."""
    rows = db.query("SELECT role, content, sources FROM messages WHERE conversation_id=? "
                    "ORDER BY id DESC LIMIT ?", (conv_id, limit))
    return [dict(r) for r in reversed(rows)]


def as_history(rows, keep=None) -> list:
    """Messages au format des modèles. keep(sources) faux : la réponse est remplacée par
    WITHHELD (elle s'appuie sur ce que cette machine ne doit pas recevoir). Les réponses
    qui ne sont qu'une erreur n'y vont pas."""
    found = []
    for r in rows:
        if r["role"] == "assistant":
            if str(r["content"] or "").startswith(ERROR_MARK):
                continue
            if keep is not None and not keep(_sources(r["sources"])):
                found.append({"role": "assistant", "content": WITHHELD})
                continue
        found.append({"role": r["role"], "content": r["content"]})
    return found


def _sources(sources) -> list:
    if isinstance(sources, str):
        try:
            sources = json.loads(sources or "[]")
        except ValueError:
            return []
    return [s for s in sources or [] if isinstance(s, dict)]


def auto_title(conv_id, text) -> str | None:
    """Donne un titre à une conversation neuve à partir du premier message."""
    line = " ".join((text or "").split())
    title = (line[:60] + "…") if len(line) > 60 else line
    if not title:
        return None
    # D'un seul geste : deux messages à la fois ne se disputent pas le titre
    changed = db.execute("UPDATE conversations SET title=? WHERE id=? AND title=?",
                         (title, conv_id, DEFAULT_TITLE)).rowcount
    return title if changed else None


# --------------------------------------------------------------------- dossiers
def create_folder(name) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Le nom du dossier ne peut pas être vide.")
    if db.query_one("SELECT 1 FROM folders WHERE name=?", (name,)):
        raise ValueError(f"Le dossier « {name} » existe déjà.")
    return db.execute("INSERT INTO folders(name, created) VALUES(?, ?)",
                      (name, db.now())).lastrowid


def rename_folder(folder_id, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Le nom du dossier ne peut pas être vide.")
    clash = db.query_one("SELECT id FROM folders WHERE name=?", (name,))
    if clash and clash["id"] != folder_id:
        raise ValueError(f"Le dossier « {name} » existe déjà.")
    db.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))


def delete_folder(folder_id):
    """Les conversations du dossier reviennent à la racine (elles ne sont pas supprimées)."""
    db.execute("DELETE FROM folders WHERE id=?", (folder_id,))
