"""Conversations, messages et dossiers de classement."""
import json

from aiodos import db
from aiodos.knowledge import store

DEFAULT_TITLE = "Nouvelle conversation"
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
    rows = db.query("SELECT role, content, sources FROM messages WHERE conversation_id=? "
                    "ORDER BY id DESC LIMIT ?", (conv_id, limit))
    return [{"role": r["role"],
             "content": WITHHELD if not with_others and r["role"] == "assistant"
             and cites_others(r["sources"]) else r["content"]}
            for r in reversed(rows)]


def auto_title(conv_id, text) -> str | None:
    """Donne un titre à une conversation neuve à partir du premier message."""
    row = db.query_one("SELECT title FROM conversations WHERE id=?", (conv_id,))
    if row is None or row["title"] != DEFAULT_TITLE:
        return None
    line = " ".join((text or "").split())
    title = (line[:60] + "…") if len(line) > 60 else line
    if title:
        db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
    return title


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
