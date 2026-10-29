"""Les premières vues des machines du réseau public, vues d'ici (docs/conception/
reseau-public-calcul.md § 8 ; contre la collusion, 29/10/2026).

Pourquoi : une clé de nœud ne coûte rien. Un menteur qui tient deux nœuds annonçant le même
modèle pouvait se contrôler lui-même : l'un répondait, l'autre servait de machine de contrôle
et lui donnait raison (network/spot_check.py). Une clé ne dit pas qui la tient ; ce qu'on voit
d'ici, c'est quand elle est apparue dans l'annuaire : deux nœuds créés ensemble apparaissent
ensemble, souvent avec la même liste de modèles. C'est un indice faible (un menteur patient
espace la création de ses nœuds) ; il s'ajoute au passé établi ici (reputation.established),
qui seul compte vraiment.

Ce qu'on garde (table public_sightings, migration 29) : par clé de nœud de l'annuaire, la
première fois qu'on l'a vue (first_seen), la dernière (last_seen, redite au plus une fois par
REFRESH_S), et l'empreinte de sa liste de modèles à la première vue (models, fingerprint :
une empreinte, pas les noms). Les nœuds sont notés à chaque lecture de l'annuaire au
compagnon (network/public.py : la synchronisation toutes les 15 s, et la relecture du choix
des machines), aussi quand network/public_controls.py relit l'annuaire gardé (sans coût :
une clé déjà notée ne l'est pas deux fois avant REFRESH_S), et toute clé que la réputation
note pour la première fois (reputation._ensure). Rien du contenu des conversations ; rien
n'est partagé avec d'autres machines.

Ce qu'on en tire :
- together(a, b) : « apparues ensemble », premières vues à moins de TOGETHER_S l'une de
  l'autre. Une clé jamais vue passe pour vue à l'instant (prudent : une inconnue n'est
  indépendante de rien de récent).
- alike(a, b) : « se ressemblent », apparues ensemble et la même empreinte de modèles (connue
  des deux).
- groups(clés) : ces machines en groupes d'apparues ensemble, de proche en proche ; deux
  machines de groupes différents ne sont jamais apparues ensemble (network/placement.py : un
  modèle n'est contrôlable par le seul réseau public que chez trois groupes au moins).

Bornes : une clé plus vue depuis KEEP_DAYS jours est oubliée ; MAX_ROWS lignes au plus, et la
table pleine ne prend plus de clé nouvelle (une inondation de clés neuves n'efface pas la
première vue des anciennes ; une clé non notée passe pour vue à l'instant, ce qui ne lui donne
rien).

Limites (dites dans la conception, § 8) : la première observation d'ici (la première réponse
publique après l'installation) voit toutes les machines déjà là au même instant : elles
paraissent apparues ensemble et ne peuvent pas se blanchir entre elles ; seules mes machines,
ou des machines arrivées ensuite, le peuvent. La précision suit la fréquence des relectures de
l'annuaire.
"""
import hashlib
import re
import threading
import time

from aiodos import db

TOGETHER_S = 6 * 3600          # premières vues à moins de 6 h l'une de l'autre : ensemble
REFRESH_S = 86400              # dernière vue redite en base au plus une fois par jour
DAY_S = 86400
KEEP_DAYS = 180                # plus vue depuis : oubliée (comme reputation.KEEP_DAYS)
MAX_ROWS = 20_000              # lignes gardées, au plus (l'annuaire en garde 2 000 à la fois)
MAX_NOTED = 20_000             # clés retenues en mémoire (REFRESH_S), au plus
BATCH = 500                    # clés lues par requête (firsts), au plus
_KEY = re.compile(r"[0-9A-Za-z]{16,128}")      # comme reputation.valid_key

_lock = threading.Lock()
_noted = {}                    # clé -> dernière écriture en base (time.monotonic)


def _valid(key) -> bool:
    return isinstance(key, str) and bool(_KEY.fullmatch(key))


def fingerprint(models) -> str:
    """L'empreinte d'une liste de modèles (noms sans doublons, minuscules, triés) : 16
    caractères, qui ne disent pas les noms."""
    names = sorted({m.strip().casefold() for m in models or [] if isinstance(m, str)})
    return hashlib.sha256("\n".join(names).encode()).hexdigest()[:16]


def _prune(conn, now: float):
    conn.execute("DELETE FROM public_sightings WHERE last_seen < ?", (now - KEEP_DAYS * DAY_S,))


def _room(conn) -> int:
    return MAX_ROWS - conn.execute("SELECT COUNT(*) FROM public_sightings").fetchone()[0]


def sighted(nodes) -> int:
    """Les nœuds de l'annuaire vus à l'instant (network/public.seen : « key », « models ») :
    la première vue de chaque clé nouvelle, avec l'empreinte de ses modèles ; la dernière vue
    des autres, redite au plus une fois par REFRESH_S. → lignes écrites."""
    moment = time.monotonic()
    fresh = {}
    with _lock:
        for node in nodes or []:
            key = node.get("key") if isinstance(node, dict) else None
            if not _valid(key) or key in fresh:
                continue
            at = _noted.get(key)
            if at is None or moment - at >= REFRESH_S:
                fresh[key] = fingerprint(node.get("models"))
    if not fresh:
        return 0
    now = db.now()
    with db.transaction() as conn:
        _prune(conn, now)
        room = _room(conn)
        for key, models in fresh.items():
            if conn.execute("UPDATE public_sightings SET last_seen = ?, models = "
                            "COALESCE(models, ?) WHERE key = ?", (now, models, key)).rowcount:
                continue
            if room > 0:
                conn.execute("INSERT INTO public_sightings(key, first_seen, last_seen, models) "
                             "VALUES(?, ?, ?, ?)", (key, now, now, models))
                room -= 1
    with _lock:
        for key in fresh:
            _noted.pop(key, None)                 # en dernier : la plus récente
            _noted[key] = moment
        while len(_noted) > MAX_NOTED:
            del _noted[next(iter(_noted))]
    return len(fresh)


def note(conn, key, now: float):
    """Une clé que la réputation note pour la première fois (reputation._ensure, dans sa
    transaction) : vue au plus tard maintenant, si on ne l'avait pas déjà vue."""
    if _valid(key) and _room(conn) > 0:
        conn.execute("INSERT OR IGNORE INTO public_sightings(key, first_seen, last_seen) "
                     "VALUES(?, ?, ?)", (key, now, now))


def first_seen(key) -> float | None:
    """La première vue de cette clé (db.now), ou None : jamais vue."""
    if not _valid(key):
        return None
    row = db.query_one("SELECT first_seen FROM public_sightings WHERE key = ?", (key,))
    return row["first_seen"] if row is not None else None


def _models(key) -> str | None:
    row = db.query_one("SELECT models FROM public_sightings WHERE key = ?", (key,))
    return row["models"] if row is not None else None


def together(a, b, now=None) -> bool:
    """Ces deux machines publiques sont-elles apparues ensemble (premières vues à moins de
    TOGETHER_S) ? Une jamais vue l'est à l'instant ; None (une de mes machines) : jamais."""
    if a is None or b is None:
        return False
    now = db.now() if now is None else now
    first_a, first_b = first_seen(a), first_seen(b)
    return abs((now if first_a is None else first_a)
               - (now if first_b is None else first_b)) < TOGETHER_S


def alike(a, b, now=None) -> bool:
    """Ces deux machines publiques se ressemblent-elles : apparues ensemble, et la même
    empreinte de modèles à leur première vue (connue des deux) ?"""
    if a is None or b is None or not together(a, b, now):
        return False
    models = _models(a)
    return models is not None and models == _models(b)


def firsts(keys) -> dict:
    """{clé : première vue} de ces clés, lues par lots de BATCH ; une clé jamais vue (ou mal
    formée) n'y est pas."""
    keys = [key for key in dict.fromkeys(keys or []) if _valid(key)]
    found = {}
    for at in range(0, len(keys), BATCH):
        chunk = keys[at:at + BATCH]
        found.update((row["key"], row["first_seen"]) for row in db.query(
            f"SELECT key, first_seen FROM public_sightings WHERE key IN "
            f"({', '.join('?' * len(chunk))})", chunk))
    return found


def groups(keys, now=None, known=None) -> list:
    """Ces machines publiques en groupes d'« apparues ensemble », de proche en proche : rangées
    par première vue (une jamais vue : à l'instant, comme pour together), un nouveau groupe
    commence quand la suivante est apparue TOGETHER_S ou plus après la précédente. Deux
    machines de groupes différents ne sont donc jamais apparues ensemble (together), ni ne se
    ressemblent (alike le suppose) : trois groupes, ce sont, quelle que soit celle qui répond,
    deux machines de contrôle indépendantes d'elle et l'une de l'autre (spot_check, « Qui
    conclut »). Prudent : A, B, C apparues à 4 h d'écart chacune forment un seul groupe, même
    si A et C ne sont pas apparues ensemble. known : {clé : première vue} déjà lu (firsts).
    → [[clé…]…], le groupe le plus ancien d'abord."""
    now = db.now() if now is None else now
    keys = list(dict.fromkeys(keys or []))
    known = firsts(keys) if known is None else known
    found, last = [], None
    for at, key in sorted((known.get(key, now), key) for key in keys):
        if last is None or at - last >= TOGETHER_S:
            found.append([])
        found[-1].append(key)
        last = at
    return found


def forget():
    """Plus aucune écriture retenue en mémoire (essais : chaque essai a sa propre base)."""
    with _lock:
        _noted.clear()
