"""Sessions de chat — historique, persistance JSON, dossiers de classement."""
import json
import time
from pathlib import Path

from core import config

SESS_DIR = config.DATA_DIR / "sessions"
FOLDERS_PATH = SESS_DIR / "_folders.json"


def _ensure():
    SESS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------- dossiers
def _load_folders():
    if FOLDERS_PATH.exists():
        try:
            return list(json.loads(FOLDERS_PATH.read_text(encoding="utf-8")))
        except Exception:
            return []
    return []


def _save_folders(lst):
    _ensure()
    FOLDERS_PATH.write_text(
        json.dumps(sorted(set(lst)), ensure_ascii=False, indent=2),
        encoding="utf-8")


def list_folders():
    return sorted(set(_load_folders()))


def create_folder(name):
    name = name.strip()
    if name:
        f = _load_folders()
        if name not in f:
            f.append(name)
            _save_folders(f)


def delete_folder(name):
    """Supprime le dossier ; ses conversations repassent à la racine."""
    for s in list_sessions():
        if s.get("folder") == name:
            set_folder(s["id"], "")
    _save_folders([f for f in _load_folders() if f != name])


# ---------------------------------------------------------------------- sessions
def new_session():
    return {
        "id": str(int(time.time() * 1000)),
        "title": "Nouvelle conversation",
        "created": time.time(),
        "model": "",
        "folder": "",
        "messages": [],   # [{"role": "user"|"assistant", "content": str}]
    }


def save(session):
    _ensure()
    (SESS_DIR / f"{session['id']}.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def load(sid):
    p = SESS_DIR / f"{sid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def delete(sid):
    p = SESS_DIR / f"{sid}.json"
    if p.exists():
        p.unlink()


def rename(sid, title):
    s = load(sid)
    if s:
        s["title"] = title.strip() or s["title"]
        save(s)


def set_folder(sid, folder):
    s = load(sid)
    if s:
        s["folder"] = folder
        save(s)


def list_sessions():
    _ensure()
    out = []
    for p in SESS_DIR.glob("*.json"):
        if p.name.startswith("_"):        # ignore _folders.json & co.
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "title": d.get("title", "Sans titre"),
                        "created": d.get("created", 0),
                        "folder": d.get("folder", "")})
        except Exception:
            continue
    out.sort(key=lambda x: x["created"], reverse=True)
    return out
