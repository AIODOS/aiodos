"""La réputation des machines du réseau public, vue d'ici (docs/conception/reseau-public-calcul.md
§ 8 ; décisions de l'auteur du 27/10/2026) : une ligne par clé de prêteur, la clé publique de
son nœud (celle de l'annuaire, « public:<clé>:<modèle> »), jamais une clé jetable, qui ne
nomme qu'une conversation de cette machine.

Ce qu'on y garde (table public_reputation, migration 28) : les réponses servies ici, la
vitesse mesurée ici (jetons par seconde, moyenne glissante, depuis l'envoi de la demande
jusqu'au dernier mot, MAX_TPS au plus), les contrôles réussis ou ratés
(network/spot_check.py) et la mise de côté : un contrôle raté écarte la machine ASIDE_DAYS
jours, un deuxième pour de bon. Une machine mise de côté n'est plus choisie pour mes
conversations, ni comme machine de contrôle.

Un échec qui n'est pas une preuve de triche (relecture du 28/10/2026 : des signes de vie
pendant 180 s puis rien, une réponse lente, coupée, illisible, trop longue, ou dont le moteur
s'arrête : public_chat.FAULTS) écarte la machine BENCH_S secondes (bench), en mémoire
seulement : pendant ce temps, elle n'est pas plus choisie qu'une machine mise de côté
(is_set_aside). Sans cela, un prêteur qui lisait la conversation puis ne répondait jamais
restait candidat à chaque message. Des refus « avant calcul » répétés, qui contredisent le
profil qu'il annonçait au moment du choix (network/public_refusals.py), l'écartent de même.

Rien n'est partagé avec d'autres machines : pas de liste noire commune, qu'un menteur
pourrait remplir pour écarter des machines honnêtes. Rien du contenu des conversations n'y
entre. Les machines jamais prises en défaut et plus vues depuis KEEP_DAYS jours sont
oubliées, et la table garde au plus MAX_ROWS lignes (les plus anciennes, jamais prises en
défaut, partent d'abord) ; une machine prise en défaut reste notée.

Contre la collusion (29/10/2026, migration 29) : une clé de nœud ne coûte rien, et deux nœuds
d'un même menteur pouvaient se contrôler l'un l'autre. Un contrôle réussi ne se compte donc
plus que s'il a été mené par une de mes machines, ou par deux machines de contrôle publiques
indépendantes (spot_check) : checks_sure les compte (checks_ok compte aussi ceux d'avant cette
règle, quand une seule machine publique suffisait). Un contrôle mené jusqu'au bout sans
conclusion (une seule machine publique d'accord, des machines en désaccord) est compté à part
(checks_open : ni réussi, ni raté). Une machine « établie » (established) a un passé ici : vue
depuis ESTABLISHED_S au moins (sa première vue, network/sightings.py), au moins un contrôle
réussi compté ainsi, aucun raté. Elle est préférée comme machine de contrôle, et seule elle
peut, sans seconde machine, faire compter une faute flagrante ou peser contre des machines
sans passé (spot_check). Depuis le 29/10/2026 au soir, elle est aussi préférée pour répondre,
seule elle passe devant cette machine, et un modèle qu'elle a devient contrôlable
(network/placement.py ; established_among, pour tout l'annuaire d'un coup).
"""
import logging
import re
import threading
import time

from aiodos import db
from aiodos.network import sightings

log = logging.getLogger("aiodos.net")

ASIDE_DAYS = 7                 # un contrôle raté : de côté une semaine
FOREVER_AFTER = 2              # contrôles ratés avant la mise de côté pour de bon
DAY_S = 86400
KEEP_DAYS = 180                # une machine sans faute, plus vue depuis : oubliée
MAX_ROWS = 10_000              # lignes gardées, au plus
MIN_TOKENS = 16                # en dessous, la vitesse ne dit rien (comme node/speed.py)
SMOOTHING = 0.3                # poids d'une nouvelle mesure dans la vitesse gardée
# Vitesse mesurée ici, au plus (relecture du 28/10/2026) : une seule conversation ne va pas
# plus vite sur une machine d'aujourd'hui, même un tout petit modèle sur la plus grosse carte
# (quelques centaines de jetons par seconde), d'autant que la mesure compte aussi le relais, le
# chargement et la lecture de la demande. Au-delà, ramenée à cette valeur : une mesure
# aberrante ne pèse pas des dizaines de réponses dans la moyenne glissante
MAX_TPS = 1000
SHORT = 8                      # caractères de la clé dits au journal, au plus
BENCH_S = 600                  # un échec (public_chat.FAULTS) : écartée 10 minutes
MAX_BENCHED = 10_000           # machines écartées gardées en mémoire, au plus
# Établie : vue depuis un jour au moins. Un menteur doit attendre, et passer un contrôle qu'il
# ne peut pas reconnaître (5 % des réponses, tirées au hasard), mené par une de mes machines
# ou par deux machines indépendantes : il lui faut répondre honnêtement presque toujours
ESTABLISHED_S = 86400
BATCH = 500                    # clés lues par requête (established_among), au plus
_KEY = re.compile(r"[0-9A-Za-z]{16,128}")      # comme network/public_chat.py

_COLUMNS = ("key", "served", "tps", "checks_ok", "checks_failed", "aside_until", "forever",
            "first_seen", "last_seen", "checks_sure", "checks_open")

_lock = threading.Lock()
_benched = {}                  # clé -> fin de l'écart (time.monotonic), en mémoire seulement


def valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY.fullmatch(key))


def short(key) -> str:
    """La clé abrégée, pour le journal : SHORT caractères au plus, jamais la clé entière."""
    return f"{str(key)[:SHORT]}…"


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
    sightings.note(conn, key, now)              # vue au plus tard maintenant
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
    checks_failed, aside_until (None : jamais écartée), forever, first_seen, last_seen,
    checks_sure, checks_open (cf. le début du module) ; None si elle n'a jamais servi ni été
    contrôlée d'ici."""
    if not valid_key(key):
        return None
    row = db.query_one(f"SELECT {', '.join(_COLUMNS)} FROM public_reputation WHERE key=?",
                       (key,))
    return dict(row) if row is not None else None


def note_served(key, tokens, seconds):
    """Une réponse servie ici par cette machine : `tokens` jetons en `seconds` secondes, depuis
    l'envoi de la demande jusqu'au dernier mot (le temps total vu d'ici : un prêteur qui garde
    sa réponse pour l'envoyer d'un coup ne paraît pas plus rapide). La vitesse n'est retenue
    qu'au-delà de MIN_TOKENS jetons, et ramenée à MAX_TPS au plus."""
    _check(key)
    now = db.now()
    with db.transaction() as conn:
        row = _ensure(conn, key, now)
        tps = row["tps"]
        if isinstance(tokens, (int, float)) and isinstance(seconds, (int, float)) \
                and tokens >= MIN_TOKENS and seconds > 0:
            measured = min(MAX_TPS, tokens / seconds)
            if measured > 0:
                tps = measured if tps is None else (1 - SMOOTHING) * tps + SMOOTHING * measured
        conn.execute("UPDATE public_reputation SET served = served + 1, tps = ?, "
                     "last_seen = ? WHERE key = ?",
                     (round(tps, 2) if tps is not None else None, now, key))


def _aside(conn, key, now: float, forever: bool, reason=None):
    if forever:
        conn.execute("UPDATE public_reputation SET forever = 1 WHERE key = ?", (key,))
    else:
        conn.execute("UPDATE public_reputation SET aside_until = MAX(COALESCE(aside_until, 0), "
                     "?) WHERE key = ?", (now + ASIDE_DAYS * DAY_S, key))
    # i18n: tel quel (journal)
    duration = "pour de bon" if forever else f"{ASIDE_DAYS} jours"
    if reason is not None:          # une ligne, sans contenu ni clé entière (spot_check._note)
        log.warning("Machine du réseau public mise de côté : contrôle raté (%s), %s, %s",
                    reason, short(key), duration)
    else:
        log.warning("Réseau public : %s mis de côté %s", short(key), duration)


def set_aside(key, forever=False):
    """Mettre cette machine de côté ASIDE_DAYS jours, ou pour de bon."""
    _check(key)
    now = db.now()
    with db.transaction() as conn:
        _ensure(conn, key, now)
        conn.execute("UPDATE public_reputation SET last_seen = ? WHERE key = ?", (now, key))
        _aside(conn, key, now, forever)


def bench(key, reason=None):
    """Un échec de cette machine (public_chat.FAULTS : lente, coupée, illisible…) : écartée
    BENCH_S secondes, en mémoire seulement (cf. le début du module). reason (le code) va au
    journal."""
    if not valid_key(key):
        return
    now = time.monotonic()
    with _lock:
        for other in [k for k, until in _benched.items() if until <= now]:
            del _benched[other]
        _benched.pop(key, None)                   # en dernier : la plus récente
        _benched[key] = now + BENCH_S
        while len(_benched) > MAX_BENCHED:
            del _benched[next(iter(_benched))]
    # i18n: tel quel (journal) ; la clé abrégée, le code seulement
    log.info("Réseau public : %s écartée %d min (%s)", short(key), BENCH_S // 60, reason or "?")


def benched(key) -> bool:
    """Cette machine est-elle écartée après un échec (bench), pour quelques minutes encore ?"""
    with _lock:
        until = _benched.get(key)
    return until is not None and until > time.monotonic()


def forget():
    """Plus aucune machine écartée, plus rien de retenu des premières vues (essais) ; les
    tables, elles, restent."""
    with _lock:
        _benched.clear()
    sightings.forget()


def is_set_aside(key) -> bool:
    """Cette machine est-elle de côté (contrôle raté, pas encore ASIDE_DAYS jours ; ou pour
    de bon), ou écartée quelques minutes après un échec (benched) ? Une clé mal formée l'est
    toujours."""
    if not valid_key(key):
        return True
    if benched(key):
        return True
    row = get(key)
    if row is None:
        return False
    return bool(row["forever"]) or (row["aside_until"] or 0) > db.now()


def note_check(key, ok: bool, reason=None) -> dict:
    """Le résultat d'un contrôle de cette machine (spot_check.run_check). Réussi : compté
    aussi dans checks_sure (spot_check ne conclut plus qu'avec une de mes machines ou deux
    machines indépendantes). Raté : de côté ASIDE_DAYS jours ; raté FOREVER_AFTER fois : pour
    de bon ; reason (le verdict : link, unrelated…) va au journal. → la ligne à jour."""
    _check(key)
    now = db.now()
    with db.transaction() as conn:
        _ensure(conn, key, now)
        counted = "checks_ok = checks_ok + 1, checks_sure = checks_sure + 1" if ok \
            else "checks_failed = checks_failed + 1"
        conn.execute(f"UPDATE public_reputation SET {counted}, last_seen = ? WHERE key = ?",
                     (now, key))
        if not ok:
            _aside(conn, key, now, _row(conn, key)["checks_failed"] >= FOREVER_AFTER,
                   reason or "?")
    return get(key)


def note_open(key) -> dict:
    """Un contrôle de cette machine mené jusqu'au bout, sans conclusion (spot_check : une
    seule machine publique d'accord, ou des machines en désaccord) : ni réussi, ni raté.
    → la ligne à jour."""
    _check(key)
    now = db.now()
    with db.transaction() as conn:
        _ensure(conn, key, now)
        conn.execute("UPDATE public_reputation SET checks_open = checks_open + 1, "
                     "last_seen = ? WHERE key = ?", (now, key))
    return get(key)


def checked(key) -> int:
    """Contrôles menés jusqu'au bout sur cette machine et conclus, réussis ou ratés."""
    row = get(key)
    return row["checks_ok"] + row["checks_failed"] if row else 0


def inconclusive(key) -> int:
    """Contrôles menés jusqu'au bout sur cette machine, sans conclusion (note_open)."""
    row = get(key)
    return row["checks_open"] if row else 0


def first_seen(key) -> float | None:
    """La première vue de cette machine : dans l'annuaire (network/sightings.py) ou, plus tôt,
    sa première ligne ici ; None : jamais vue."""
    row = get(key)
    seen = [t for t in (row["first_seen"] if row else None, sightings.first_seen(key))
            if t is not None]
    return min(seen) if seen else None


def established(key) -> bool:
    """Cette machine publique est-elle établie ici : vue depuis ESTABLISHED_S au moins
    (first_seen), au moins un contrôle réussi par une de mes machines ou par deux machines de
    contrôle indépendantes (checks_sure), aucun contrôle raté, pas de côté ? (Un écart de
    quelques minutes après un échec, bench, n'y change rien : il dit l'instant, pas le passé.)"""
    row = get(key)
    if row is None or row["checks_sure"] < 1 or row["checks_failed"] or row["forever"]:
        return False
    now = db.now()
    if (row["aside_until"] or 0) > now:
        return False
    first = first_seen(key)
    return first is not None and first <= now - ESTABLISHED_S


def established_among(keys) -> set:
    """Celles de ces clés qui sont établies (established), pour tout l'annuaire à la fois
    (network/placement.py) : une lecture par lot de BATCH clés ne garde que les lignes qui
    peuvent l'être (un contrôle sûr, aucun raté, pas de côté), rares, relues ensuite une à une
    par established."""
    keys = [key for key in dict.fromkeys(keys or []) if valid_key(key)]
    now = db.now()
    maybe = []
    for at in range(0, len(keys), BATCH):
        chunk = keys[at:at + BATCH]
        maybe += [row["key"] for row in db.query(
            f"SELECT key FROM public_reputation WHERE key IN ({', '.join('?' * len(chunk))}) "
            "AND checks_sure >= 1 AND checks_failed = 0 AND forever = 0 "
            "AND COALESCE(aside_until, 0) <= ?", (*chunk, now))]
    return {key for key in maybe if established(key)}


def speed(key) -> float | None:
    """Jetons par seconde mesurés ici pour cette machine, ou None."""
    row = get(key)
    return row["tps"] if row else None
