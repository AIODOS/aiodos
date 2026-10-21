"""Journal d'usage des applications, sans contenu (invariant I8 de la conception ;
docs/api-applications.md).

- Jamais de contenu : ni consigne, ni réponse, ni image, ni passage, ni en-tête, ni clé.
- gateway_log : une ligne par requête terminée (pour un flux, à sa fin) : l'heure, l'appli,
  la route, le modèle concret, la machine, le lieu, le statut, le code d'erreur, la durée,
  les octets, et le programme seulement s'il diffère de celui vu à l'accord. Gardé
  gateway_log_days jours (30), 20 000 lignes au plus.
- gateway_usage : un compteur par jour, appli, famille (chat, embed, models, access, mcp…)
  et lieu ; gardé 90 jours.
Les lignes sont gardées en mémoire et écrites toutes les FLUSH_EVERY secondes (un fil), pour
ne pas écrire dans la base depuis la boucle de la passerelle.
"""
import logging
import threading
import time
from datetime import datetime

from aiodos import db, settings

log = logging.getLogger("aiodos.gateway")

FLUSH_EVERY = 5
LOG_ROWS = 20000
USAGE_DAYS = 90
PURGE_EVERY = 3600

_lock = threading.Lock()
_pending = []
_state = {"thread": None, "purged": 0.0}


def family(path: str) -> str:
    if path.startswith(("/v1/chat", "/v1/completions", "/api/chat", "/api/generate")):
        return "chat"
    if path.startswith(("/v1/embeddings", "/api/embed")):
        return "embed"
    if path.startswith(("/v1/models", "/api/tags", "/api/show", "/api/ps", "/aiodos/v1/models")):
        return "models"
    if path.startswith("/aiodos/v1/access"):
        return "access"
    if path.startswith(("/aiodos/v1/jobs", "/aiodos/v1/inputs/", "/aiodos/v1/files/",
                        "/aiodos/v1/engines")):
        return "engines"
    if path.startswith("/mcp"):
        return "mcp"
    return "other"


def record(*, app_id, route, status, ms, model=None, machine=None, place=None, code=None,
           bytes_in=0, bytes_out=0, program=None):
    """Une requête terminée (appelé par Guard)."""
    with _lock:
        _pending.append((time.time(), app_id, route, model, machine, place, int(status), code,
                         int(ms), int(bytes_in), int(bytes_out), program))
        if _state["thread"] is None or not _state["thread"].is_alive():
            _state["thread"] = threading.Thread(target=_loop, daemon=True,
                                                name="aiodos-journal-applis")
            _state["thread"].start()


def _loop():
    while True:
        time.sleep(FLUSH_EVERY)
        try:
            if not flush():
                with _lock:
                    if not _pending:
                        _state["thread"] = None
                        return
        except Exception:
            log.exception("Journal des applications : écriture impossible")


def flush() -> int:
    """Écrit les lignes en attente ; → leur nombre."""
    with _lock:
        rows, _pending[:] = list(_pending), []
    if not rows:
        return 0
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO gateway_log(at, app_id, route, model, machine, place, status, code, ms,"
            " bytes_in, bytes_out, program) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        for at, app_id, route, _, _, place, status, *_ in rows:
            day = datetime.fromtimestamp(at).strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO gateway_usage(app_id, day, family, place, requests, errors) "
                "VALUES(?,?,?,?,1,?) ON CONFLICT(app_id, day, family, place) DO UPDATE SET "
                "requests=requests+1, errors=errors+excluded.errors",
                (app_id or "", day, family(route), place or "", int(status >= 400)))
    if time.time() - _state["purged"] > PURGE_EVERY:
        purge()
    return len(rows)


def purge():
    _state["purged"] = time.time()
    try:
        days = max(1, int(settings.get("gateway_log_days") or 30))
    except (TypeError, ValueError):
        days = 30
    now = time.time()
    db.execute("DELETE FROM gateway_log WHERE at < ?", (now - days * 86400,))
    db.execute("DELETE FROM gateway_log WHERE id <= (SELECT MAX(id) FROM gateway_log) - ?",
               (LOG_ROWS,))
    oldest = datetime.fromtimestamp(now - USAGE_DAYS * 86400).strftime("%Y-%m-%d")
    db.execute("DELETE FROM gateway_usage WHERE day < ?", (oldest,))


def recent(app_id: str, limit: int = 50) -> list:
    flush()
    rows = db.query("SELECT at, route, model, machine, place, status, code, ms, program "
                    "FROM gateway_log WHERE app_id=? ORDER BY id DESC LIMIT ?",
                    (app_id, max(1, min(500, int(limit)))))
    return [dict(r) for r in rows]


def summary(app_id: str) -> dict:
    """Compteurs du jour et des 7 derniers jours, et ce qui doit sauter aux yeux : calculs
    chez un ami, refus, programme inattendu."""
    flush()
    today = datetime.now().strftime("%Y-%m-%d")
    week = datetime.fromtimestamp(time.time() - 6 * 86400).strftime("%Y-%m-%d")
    rows = db.query("SELECT day, place, SUM(requests) AS n, SUM(errors) AS e FROM gateway_usage "
                    "WHERE app_id=? AND day >= ? GROUP BY day, place", (app_id, week))
    out = {"today": 0, "week": 0, "errors_week": 0, "friend_week": 0}
    for r in rows:
        out["week"] += r["n"]
        out["errors_week"] += r["e"]
        if r["day"] == today:
            out["today"] += r["n"]
        if r["place"] == "friend":
            out["friend_week"] += r["n"]
    unexpected = db.query_one("SELECT COUNT(*) AS n, MAX(at) AS last, program FROM gateway_log "
                              "WHERE app_id=? AND program IS NOT NULL", (app_id,))
    out["unexpected_program"] = ({"count": unexpected["n"], "last": unexpected["last"],
                                  "program": unexpected["program"]}
                                 if unexpected and unexpected["n"] else None)
    return out
