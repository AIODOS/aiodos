"""Une application, une clé (docs/api-applications.md § 4.2).

- Forme : aiodos_app_<id>_<secret>. id : 8 caractères en base32 minuscule, public (journal,
  interface, preuve d'identité) ; secret : 256 bits (token_urlsafe(32), 43 caractères). Le
  préfixe permet aux détecteurs de secrets de repérer une clé qui fuit.
- AIODOS ne garde que sha256(clé) (gateway_apps.key_hash) : la clé n'est remise qu'une fois,
  à l'appli (access_flow.py) ou à l'écran pour un outil (« clé à la main », kind manual).
- La clé générale des autres outils (access.py) garde sa forme et ses droits d'aujourd'hui :
  GENERAL, construite à la demande, n'a pas de ligne ici.
- Contrôle (lookup) : par l'id, puis comparaison des hachés en temps constant, avec un cache
  en mémoire vidé à chaque changement : un retrait ou une modification valent aussitôt.
- Un signal d'arrêt par appli (stop_event) coupe ses réponses en cours quand on la retire.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass

from aiodos import db
from aiodos.gateway import scopes
from aiodos.i18n import t

PREFIX = "aiodos_app_"
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
ID_LENGTH = 8
SECRET_LENGTH = 43
KINDS = ("app", "manual")
GENERAL_ID = "general"
TOUCH_EVERY = 60            # s : « dernière utilisation » écrite au plus une fois par minute

_lock = threading.Lock()
_cache = {}                 # id -> (key_hash, statut, AppContext | None)
_stops = {}                 # id -> threading.Event
_touched = {}               # id -> instant monotonic de la dernière écriture


@dataclass(frozen=True)
class AppContext:
    """Ce que la passerelle sait de l'appli qui appelle."""
    id: str
    kind: str                       # app | manual | general
    name: str
    scopes: frozenset
    reach: frozenset                # cercles accordés
    priority: str = "interactive"
    consents: tuple = ()            # ((capacité, (cercles…)), …) ; vide : pas de restriction
    client_id: str | None = None
    version: str | None = None
    exe_path: str | None = None
    exe_pinned: bool = False
    account_check: bool = True
    libraries: tuple = ()           # ((id, date de création), …) choisies à l'accord
    expires: float | None = None

    def consented(self, capability: str):
        """Cercles montrés à l'accord pour cette capacité (None : pas de restriction)."""
        if self.kind != "app":
            return None
        return dict(self.consents).get(capability, ())

    def public(self) -> dict:
        """Ce que l'appli peut savoir d'elle-même (GET /aiodos/v1/info) ; le nom de la clé
        générale, dans la langue de la requête."""
        name = t("gateway_access.general_name") if self.kind == "general" else self.name
        return {"id": self.id, "name": name, "kind": self.kind,
                "scopes": [s for s in scopes.SCOPES if s in self.scopes],
                "where": [c for c in scopes.CIRCLES if c in self.reach],
                "priority": self.priority, "expires": self.expires}


# i18n: tel quel (son nom montré, traduit, est celui de public())
GENERAL = AppContext(id=GENERAL_ID, kind="general", name="Autres outils",
                     scopes=frozenset({"chat", "embed", "libraries"}),
                     reach=frozenset({"here", "mine", "friends"}), account_check=False)


# ------------------------------------------------------------------ les clés
def key_id(key) -> str | None:
    """L'id d'une clé d'appli, ou None si ce n'en est pas une."""
    text = str(key or "")
    if not text.startswith(PREFIX):
        return None
    rest = text[len(PREFIX):]
    if len(rest) != ID_LENGTH + 1 + SECRET_LENGTH or rest[ID_LENGTH] != "_":
        return None
    ident = rest[:ID_LENGTH]
    return ident if all(c in ID_ALPHABET for c in ident) else None


def is_app_key(key) -> bool:
    return key_id(key) is not None


def valid_id(ident) -> bool:
    text = str(ident or "")
    return len(text) == ID_LENGTH and all(c in ID_ALPHABET for c in text)


def hash_key(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _new_id() -> str:
    while True:
        ident = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))
        if not db.query_one("SELECT 1 FROM gateway_apps WHERE id=?", (ident,)):
            return ident


def _new_key(ident: str) -> str:
    return f"{PREFIX}{ident}_{secrets.token_urlsafe(32)}"


def key_hash_bytes(ident: str) -> bytes | None:
    """sha256 de la clé de cette appli (preuve d'identité, info.py) ; retirée comprise ;
    None si l'id est inconnu."""
    row = db.query_one("SELECT key_hash FROM gateway_apps WHERE id=?", (str(ident),))
    return bytes.fromhex(row["key_hash"]) if row else None


# ----------------------------------------------------------------- lecture
def _context(row) -> AppContext:
    def loads(text, default):
        try:
            value = json.loads(text or "")
        except (TypeError, ValueError):
            return default
        return value if isinstance(value, type(default)) else default
    consents = loads(row["consents"], {})
    libraries = loads(row["libraries"], [])
    return AppContext(
        id=row["id"], kind=row["kind"], name=row["name"],
        scopes=frozenset(s for s in loads(row["scopes"], []) if s in scopes.SCOPES),
        reach=frozenset(c for c in str(row["reach"] or "").split(",") if c in scopes.CIRCLES),
        priority=row["priority"] if row["priority"] in scopes.PRIORITIES else "interactive",
        consents=tuple((cap, tuple(c for c in circles if c in scopes.CIRCLES))
                       for cap, circles in consents.items()
                       if isinstance(circles, (list, dict)) and cap in scopes.SCOPES),
        client_id=row["client_id"], version=row["version"], exe_path=row["exe_path"],
        exe_pinned=bool(row["exe_pinned"]), account_check=bool(row["account_check"]),
        libraries=tuple((int(pair[0]), float(pair[1])) for pair in libraries
                        if isinstance(pair, list) and len(pair) == 2
                        and isinstance(pair[0], int) and isinstance(pair[1], (int, float))),
        expires=row["expires"])


def lookup(key) -> tuple:
    """(statut, AppContext | None) : valid, revoked, expired ou invalid."""
    ident = key_id(key)
    if ident is None:
        return "invalid", None
    given = hash_key(key)
    with _lock:
        cached = _cache.get(ident)
    if cached is None:
        row = db.query_one("SELECT * FROM gateway_apps WHERE id=?", (ident,))
        if row is None:
            cached = ("", "invalid", None)
        else:
            status = "revoked" if row["revoked"] else "valid"
            cached = (row["key_hash"], status, _context(row))
        with _lock:
            _cache[ident] = cached
    stored, status, context = cached
    if not stored or not hmac.compare_digest(stored, given):
        return "invalid", None
    if status == "valid" and context.expires and time.time() >= context.expires:
        return "expired", context
    return status, context


def get(ident: str) -> dict | None:
    row = db.query_one("SELECT * FROM gateway_apps WHERE id=?", (str(ident),))
    return _row_view(row) if row else None


def _row_view(row) -> dict:
    context = _context(row)
    view = {**context.public(), "client_id": context.client_id, "version": context.version,
            "purpose": row["purpose"], "exe_path": context.exe_path,
            "exe_pinned": context.exe_pinned, "account_check": context.account_check,
            "consents": {cap: list(circles) for cap, circles in context.consents},
            "libraries": [list(pair) for pair in context.libraries],
            "created": row["created"], "last_used": row["last_used"],
            "last_program": row["last_program"], "revoked": row["revoked"],
            "session_only": bool(row["session_only"])}
    return view


def list_apps(include_revoked: bool = False) -> list:
    rows = db.query("SELECT * FROM gateway_apps ORDER BY created DESC")
    return [_row_view(r) for r in rows if include_revoked or not r["revoked"]]


def find_existing(client_id: str, exe_path: str | None) -> dict | None:
    """L'accès en cours d'une appli de même identifiant et de même programme (clé perdue,
    appli réinstallée : la carte propose de le remplacer)."""
    for row in db.query("SELECT * FROM gateway_apps WHERE kind='app' AND revoked IS NULL "
                        "AND client_id=? ORDER BY created DESC", (client_id,)):
        if (row["exe_path"] or None) == (exe_path or None):
            return _row_view(row)
    return None


# ---------------------------------------------------------------- écriture
def _forget(ident: str):
    with _lock:
        _cache.pop(ident, None)


def create(*, kind: str, name: str, scopes_granted, reach, priority="interactive",
           client_id=None, version=None, purpose=None, exe_path=None, exe_pinned=False,
           account_check=True, libraries=(), expires=None, session_only=False,
           consents=None) -> tuple:
    """Nouvel accès → (id, clé en clair, à remettre une seule fois)."""
    if kind not in KINDS:                 # i18n: tel quel (erreur de programmation)
        raise ValueError(f"sorte d'accès inconnue : {kind}")
    ident = _new_id()
    key = _new_key(ident)
    db.execute(
        "INSERT INTO gateway_apps(id, kind, client_id, name, version, purpose, key_hash, scopes,"
        " libraries, reach, consents, priority, exe_path, exe_pinned, account_check, created,"
        " expires, session_only) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident, kind, client_id, name, version, purpose, hash_key(key),
         json.dumps([s for s in scopes.SCOPES if s in set(scopes_granted)]),
         json.dumps([list(pair) for pair in libraries]), scopes.where_text(reach),
         json.dumps(consents or {}), priority if priority in scopes.PRIORITIES else "interactive",
         exe_path, int(bool(exe_pinned)), int(bool(account_check)), time.time(), expires,
         int(bool(session_only))))
    return ident, key


def update(ident: str, **fields):
    """Droits, lieu, priorité, liaison au programme… : effet immédiat, même clé."""
    columns = {"scopes": lambda v: json.dumps([s for s in scopes.SCOPES if s in set(v)]),
               "reach": scopes.where_text,
               "priority": lambda v: v if v in scopes.PRIORITIES else "interactive",
               "consents": json.dumps, "libraries": lambda v: json.dumps([list(p) for p in v]),
               "exe_pinned": lambda v: int(bool(v)), "account_check": lambda v: int(bool(v)),
               "expires": lambda v: v, "name": str, "version": lambda v: v}
    sets, values = [], []
    for name, value in fields.items():
        if name in columns:
            sets.append(f"{name}=?")
            values.append(columns[name](value))
    if not sets:
        return
    db.execute(f"UPDATE gateway_apps SET {', '.join(sets)} WHERE id=?", (*values, ident))
    _forget(ident)


def revoke(ident: str) -> bool:
    """Retire l'accès : la ligne reste (info répond « revoked »), ses réponses en cours
    s'arrêtent."""
    done = db.execute("UPDATE gateway_apps SET revoked=? WHERE id=? AND revoked IS NULL",
                      (time.time(), ident)).rowcount
    _forget(ident)
    stop_event(ident).set()
    return bool(done)


def forget_app(ident: str):
    """Efface la ligne d'un accès (jamais relevé, ou expiré et retiré)."""
    db.execute("DELETE FROM gateway_apps WHERE id=?", (ident,))
    _forget(ident)


def purge_session_keys() -> int:
    """Au démarrage : les accès « jusqu'à la fermeture d'AIODOS » sont retirés."""
    rows = db.query("SELECT id FROM gateway_apps WHERE session_only=1 AND revoked IS NULL")
    for row in rows:
        revoke(row["id"])
    return len(rows)


def touch(ident: str, program: str | None = None):
    """Dernière utilisation (et programme qui s'est servi de la clé), au plus une fois par
    minute et par appli."""
    now = time.monotonic()
    with _lock:
        if now - _touched.get(ident, -TOUCH_EVERY) < TOUCH_EVERY:
            return
        _touched[ident] = now
    db.execute("UPDATE gateway_apps SET last_used=?, last_program=COALESCE(?, last_program) "
               "WHERE id=?", (time.time(), program, ident))


def stop_event(ident: str) -> threading.Event:
    """Levé quand l'appli est retirée : ses réponses en cours s'arrêtent."""
    with _lock:                          # un accès retiré ne revient pas : levé pour de bon
        return _stops.setdefault(ident, threading.Event())


def forget_all():
    """Tout oublier (essais)."""
    with _lock:
        _cache.clear()
        _stops.clear()
        _touched.clear()
