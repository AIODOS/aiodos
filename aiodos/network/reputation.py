"""La réputation des machines du réseau public, vue d'ici (docs/conception/reseau-public-calcul.md
§ 8 ; décisions de l'auteur du 27/10/2026) : une ligne par clé de prêteur, la clé publique de
son nœud (celle de l'annuaire, « public:<clé>:<modèle> »), jamais une clé jetable, qui ne
nomme qu'une conversation de cette machine.

Ce qu'on y garde (table public_reputation, migration 28) : les réponses servies ici, la
vitesse mesurée ici (jetons par seconde, moyenne glissante), les contrôles réussis ou ratés
(network/spot_check.py) et la mise de côté : un contrôle raté écarte la machine ASIDE_DAYS
jours, un deuxième pour de bon. Une machine mise de côté n'est plus choisie pour mes
conversations, ni comme machine de contrôle.

Rien n'est partagé avec d'autres machines : pas de liste noire commune, qu'un menteur
pourrait remplir pour écarter des machines honnêtes. Rien du contenu des conversations n'y
entre. Les machines jamais prises en défaut et plus vues depuis KEEP_DAYS jours sont
oubliées, et la table garde au plus MAX_ROWS lignes (les plus anciennes, jamais prises en
défaut, partent d'abord) ; une machine prise en défaut reste notée.
"""
import logging
import re

from aiodos import db

log = logging.getLogger("aiodos.net")

ASIDE_DAYS = 7                 # un contrôle raté : de côté une semaine
FOREVER_AFTER = 2              # contrôles ratés avant la mise de côté pour de bon
DAY_S = 86400
KEEP_DAYS = 180                # une machine sans faute, plus vue depuis : oubliée
MAX_ROWS = 10_000              # lignes gardées, au plus
MIN_TOKENS = 16                # en dessous, la vitesse ne dit rien (comme node/speed.py)
SMOOTHING = 0.3                # poids d'une nouvelle mesure dans la vitesse gardée
MAX_TPS = 100_000              # au-delà, la mesure est fausse (comme network/public.py)
_KEY = re.compile(r"[0-9A-Za-z]{16,128}")      # comme network/public_chat.py

_COLUMNS = ("key", "served", "tps", "checks_ok", "checks_failed", "aside_until", "forever",
            "first_seen", "last_seen")


def valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY.fullmatch(key))


def short(key) -> str:
    """La clé abrégée, pour le journal (comme network/public_chat.py)."""
    return f"{str(key)[:10]}…"


def _check(key):
    if not valid_key(key):
        raise ValueError("clé de prêteur invalide")     # i18n: tel quel (erreur de programmation)


def _row(conn, key) -> dict | None:
    row = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM public_reputation WHERE key=?",
                       (key,)).fetchone()
    return dict(row) if row is not None else None


def _ensure(conn, key, now: float) -> dict:
    """La ligne de cette clé, créée au besoin (et la table bornée, cf. plus haut)."""
    found = _row(conn, key)
    if found is not None:
        return found
    conn.execute("INSERT INTO public_reputation(key, first_seen, last_seen) VALUES(?, ?, ?)",
                 (key, now, now))
    _prune(conn, now, key)
    return _row(conn, key)


def _prune(conn, now: float, new_key: str):
    """Oublier les machines sans faute trop anciennes, puis les plus anciennes au-delà de
    MAX_ROWS lignes ; jamais celle qu'on vient d'ajouter (la table peut dépasser MAX_ROWS
    s'il n'y reste que des machines prises en défaut)."""
    blameless = "checks_failed = 0 AND forever = 0 AND key != ?"
    conn.execute(f"DELETE FROM public_reputation WHERE {blameless} AND last_seen < ?",
                 (new_key, now - KEEP_DAYS * DAY_S))
    extra = conn.execute("SELECT COUNT(*) FROM public_reputation").fetchone()[0] - MAX_ROWS
    if extra > 0:
        conn.execute(f"DELETE FROM public_reputation WHERE key IN (SELECT key FROM "
                     f"public_reputation WHERE {blameless} ORDER BY last_seen LIMIT ?)",
                     (new_key, extra))


def get(key) -> dict | None:
    """Ce qu'on sait de cette machine : served, tps (None : pas mesurée), checks_ok,
    checks_failed, aside_until (None : jamais écartée), forever, first_seen, last_seen ;
    None si elle n'a jamais servi ni été contrôlée d'ici."""
    if not valid_key(key):
        return None
    row = db.query_one(f"SELECT {', '.join(_COLUMNS)} FROM public_reputation WHERE key=?",
                       (key,))
    return dict(row) if row is not None else None


def note_served(key, tokens, seconds):
    """Une réponse servie ici par cette machine : `tokens` jetons en `seconds` secondes de
    génération (du premier mot au dernier). La vitesse n'est retenue qu'au-delà de
    MIN_TOKENS jetons."""
    _check(key)
    now = db.now()
    with db.transaction() as conn:
        row = _ensure(conn, key, now)
        tps = row["tps"]
        if isinstance(tokens, (int, float)) and isinstance(seconds, (int, float)) \
                and tokens >= MIN_TOKENS and seconds > 0:
            measured = tokens / seconds
            if 0 < measured <= MAX_TPS:
                tps = measured if tps is None else (1 - SMOOTHING) * tps + SMOOTHING * measured
        conn.execute("UPDATE public_reputation SET served = served + 1, tps = ?, "
                     "last_seen = ? WHERE key = ?",
                     (round(tps, 2) if tps is not None else None, now, key))


def _aside(conn, key, now: float, forever: bool):
    if forever:
        conn.execute("UPDATE public_reputation SET forever = 1 WHERE key = ?", (key,))
    else:
        conn.execute("UPDATE public_reputation SET aside_until = MAX(COALESCE(aside_until, 0), "
                     "?) WHERE key = ?", (now + ASIDE_DAYS * DAY_S, key))
    # i18n: tel quel (journal)
    log.warning("Réseau public : %s mis de côté %s", short(key),
                "pour de bon" if forever else f"{ASIDE_DAYS} jours")


def set_aside(key, forever=False):
    """Mettre cette machine de côté ASIDE_DAYS jours, ou pour de bon."""
    _check(key)
    now = db.now()
    with db.transaction() as conn:
        _ensure(conn, key, now)
        conn.execute("UPDATE public_reputation SET last_seen = ? WHERE key = ?", (now, key))
        _aside(conn, key, now, forever)


def is_set_aside(key) -> bool:
    """Cette machine est-elle de côté (contrôle raté, pas encore ASIDE_DAYS jours ; ou pour
    de bon) ? Une clé mal formée l'est toujours."""
    if not valid_key(key):
        return True
    row = get(key)
    if row is None:
        return False
    return bool(row["forever"]) or (row["aside_until"] or 0) > db.now()


def note_check(key, ok: bool) -> dict:
    """Le résultat d'un contrôle de cette machine (spot_check.run_check). Raté : de côté
    ASIDE_DAYS jours ; raté FOREVER_AFTER fois : pour de bon. → la ligne à jour."""
    _check(key)
    now = db.now()
    with db.transaction() as conn:
        _ensure(conn, key, now)
        column = "checks_ok" if ok else "checks_failed"
        conn.execute(f"UPDATE public_reputation SET {column} = {column} + 1, last_seen = ? "
                     f"WHERE key = ?", (now, key))
        if not ok:
            _aside(conn, key, now, _row(conn, key)["checks_failed"] >= FOREVER_AFTER)
    return get(key)


def checked(key) -> int:
    """Contrôles menés jusqu'au bout sur cette machine, réussis ou ratés."""
    row = get(key)
    return row["checks_ok"] + row["checks_failed"] if row else 0


def speed(key) -> float | None:
    """Jetons par seconde mesurés ici pour cette machine, ou None."""
    row = get(key)
    return row["tps"] if row else None
