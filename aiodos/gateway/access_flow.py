"""La demande d'accès d'une application (docs/api-applications.md § 4.3 et 4.4), au
vocabulaire de la RFC 8628 (« device flow ») :

1. l'appli demande (POST /aiodos/v1/access/requests, ou par le programme d'aide quand la
   passerelle est fermée, app_connect.py) : elle reçoit un device_code, à garder, et un
   user_code de 6 chiffres, à afficher en grand ;
2. AIODOS montre la carte d'accord (événement « gateway_access » du bus, fenêtre ramenée au
   plus une fois toutes les 30 s) ; l'utilisateur compare le code, décoche s'il veut des
   droits ou des cercles, et autorise ou refuse (routes de l'interface, derrière son jeton :
   aucune route de la passerelle n'accorde quoi que ce soit) ;
3. l'appli relève sa clé (POST /aiodos/v1/access/token), toutes les 2 s : une seule fois.

Les demandes ne vivent qu'en mémoire (un redémarrage les efface : l'appli redemande), avec
le haché du device_code. La clé en clair n'existe qu'entre l'accord et la relève ; un accès
accordé mais jamais relevé en 10 minutes est effacé. Contre l'insistance : 3 demandes en
attente au plus en tout, une par couple (nom déclaré, programme), 10 par heure et par
programme, et 3 refus du même nom ou du même programme en une heure le mettent en sourdine
une heure. Le premier accord ouvre la passerelle aux applications (gateway_apps).
"""
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field

from aiodos import db, settings
from aiodos.gateway import apps, scopes
from aiodos.gateway.caller import Caller
from aiodos.gateway.errors import ApiError
from aiodos.i18n import t

log = logging.getLogger("aiodos.gateway")

EXPIRES_IN = 600
INTERVAL = 2
SLOW_DOWN = 5
MAX_PENDING = 3
PER_HOUR = 10
MUTE_AFTER = 3
MUTE_S = 3600
RAISE_EVERY = 30
# i18n: tel quel (noms qu'une appli ne peut pas prendre, en français comme en anglais)
RESERVED = {"aiodos", "autres outils", "other tools", "windows", "microsoft"}
CLIENT_ID = re.compile(r"[a-z0-9.-]{2,40}")
_INVISIBLE = {0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}
DURATIONS = {"forever": None, "30d": 30 * 86400, "session": "session"}


@dataclass
class Pending:
    id: str
    device_hash: str
    user_code: str
    client_name: str
    client_id: str
    client_version: str
    purpose: str
    scopes: list
    where: list
    caller: Caller
    created: float
    expires: float
    via: str = "gateway"
    upgrade_of: str | None = None
    existing: dict | None = None
    status: str = "pending"             # pending | approved | denied | collected
    key: str | None = None
    app_id: str | None = None
    granted: dict = field(default_factory=dict)
    replace: str | None = None
    interval: int = INTERVAL
    last_poll: float = 0.0

    def view(self) -> dict:
        """Ce que la carte d'accord montre (jamais le device_code ni la clé)."""
        program = self.caller.program
        upgrade = apps.get(self.upgrade_of) if self.upgrade_of else None
        return {"id": self.id, "user_code": self.user_code, "client_name": self.client_name,
                "client_id": self.client_id, "client_version": self.client_version,
                "purpose": self.purpose, "scopes": self.scopes, "where": self.where,
                "program": program, "program_generic": self.caller.generic,
                "same_account": self.caller.same_account, "created": self.created,
                "expires": self.expires, "status": self.status, "via": self.via,
                "upgrade": ({"app_id": upgrade["id"], "scopes": upgrade["scopes"],
                             "where": upgrade["where"]} if upgrade else None),
                "existing": ({"app_id": self.existing["id"], "created": self.existing["created"]}
                             if self.existing else None),
                "served": {cap: sorted(scopes.SERVED[cap]) for cap in self.scopes}}


_lock = threading.Lock()
_requests = {}              # id -> Pending
_history = []               # (instant, nom, programme, issue) : limites et sourdine
_hooks = {"approval_ui": False, "on_new": None, "on_open": None, "publish": None}
_raised = {"at": 0.0}


def configure(*, approval_ui: bool, on_new=None, on_open=None, publish=None):
    """approval_ui : une fenêtre peut montrer la carte ; on_new() la ramène ; on_open()
    ouvre la passerelle aux applications ; publish(événement) : le bus de l'interface."""
    _hooks.update(approval_ui=bool(approval_ui), on_new=on_new, on_open=on_open,
                  publish=publish)


def approval_ui() -> bool:
    return bool(_hooks["approval_ui"])


def publish(event: dict):
    """Un événement pour l'interface (bus), s'il y en a une."""
    _publish(event)


def _publish(event: dict):
    if _hooks["publish"]:
        try:
            _hooks["publish"](event)
        except Exception:
            log.exception("Passerelle : événement non publié")


# ------------------------------------------------------------------ nettoyage
def clean_text(value, limit: int, what: str, required: bool = False) -> str:
    """Texte déclaré par l'appli : NFC, sans caractères de contrôle ni de sens d'écriture,
    espaces resserrés, limité ; vide et requis : 400."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError("invalid_request", t("gateway_access.field_not_text", field=what),
                       param=what)
    text = unicodedata.normalize("NFC", value)
    text = "".join(c for c in text if ord(c) not in _INVISIBLE
                   and not unicodedata.category(c).startswith("C"))
    text = " ".join(text.split())
    if len(text) > limit:
        raise ApiError("invalid_request", t("gateway_access.field_too_long", field=what,
                                            limit=limit), param=what)
    if required and not text:
        raise ApiError("invalid_request", t("gateway_access.field_missing", field=what),
                       param=what)
    return text


def _validate(body) -> dict:
    if not isinstance(body, dict):
        raise ApiError("invalid_request", t("gateway_api.json_not_object"))
    name = clean_text(body.get("client_name"), 40, "client_name", required=True)
    if name.lower() in RESERVED:
        raise ApiError("invalid_request", t("gateway_access.name_reserved", name=name),
                       param="client_name")
    client_id = str(body.get("client_id") or "")
    if not CLIENT_ID.fullmatch(client_id):
        raise ApiError("invalid_request", t("gateway_access.client_id_invalid"),
                       param="client_id")
    wanted = scopes.parse_scopes(body.get("scope") or "")
    if not wanted:
        raise ApiError("invalid_request", t("gateway_access.scopes_missing"), param="scope")
    return {"client_name": name, "client_id": client_id,
            "client_version": clean_text(body.get("client_version"), 40, "client_version"),
            "purpose": clean_text(body.get("purpose"), 200, "purpose"),
            "scopes": wanted, "where": scopes.parse_where(body.get("where"))}


def _blocked(program: str | None) -> bool:
    if not program:
        return False
    return db.query_one("SELECT 1 FROM gateway_blocked WHERE exe_path=? COLLATE NOCASE",
                        (program,)) is not None


def _expire_locked(now: float):
    for ident, req in list(_requests.items()):
        if req.status == "pending" and now >= req.expires:
            del _requests[ident]
            _publish({"type": "gateway_access", "id": ident, "status": "expired"})
        elif req.status == "approved" and now >= req.expires:
            # Accordé mais jamais relevé : l'accès ne servira à personne
            if req.app_id and not req.upgrade_of:
                apps.forget_app(req.app_id)
            del _requests[ident]
        elif req.status in ("denied", "collected") and now >= req.expires:
            del _requests[ident]
    _history[:] = [h for h in _history if now - h[0] < MUTE_S]


def _limits_locked(info: dict, caller: Caller, now: float):
    program = (caller.program or "").lower()
    name = info["client_name"].lower()
    denied = [h for h in _history if h[3] == "denied" and (h[1] == name or (program and h[2] == program))]
    if len(denied) >= MUTE_AFTER:
        raise ApiError("rate_limited", t("gateway_access.muted", name=info["client_name"]),
                       retry_after=MUTE_S)
    asked = [h for h in _history if h[3] == "asked" and h[2] == program]
    if program and len(asked) >= PER_HOUR:
        raise ApiError("rate_limited", t("gateway_access.too_many_per_hour"), retry_after=600)
    waiting = [r for r in _requests.values() if r.status == "pending"]
    if len(waiting) >= MAX_PENDING:
        raise ApiError("rate_limited", t("gateway_access.too_many_pending"), retry_after=30)
    if any(r.client_name.lower() == name and (r.caller.program or "").lower() == program
           for r in waiting):
        raise ApiError("rate_limited", t("gateway_access.already_waiting"), retry_after=10)


# --------------------------------------------------------------------- demande
def start(body, caller: Caller, *, upgrade_of: str | None = None, via: str = "gateway") -> dict:
    """Nouvelle demande → la réponse 200 pour l'appli ; ApiError si elle est refusée."""
    info = _validate(body)
    if not settings.get("gateway_requests"):
        raise ApiError("requests_closed")
    if caller.same_account is False and settings.get("gateway_same_account"):
        raise ApiError("other_account")
    if _blocked(caller.program):
        raise ApiError("blocked_program")
    if not approval_ui():
        raise ApiError("approval_unavailable", retry_after=30)
    now = time.time()
    device_code = secrets.token_urlsafe(32)
    number = secrets.randbelow(10 ** 6)
    user_code = f"{number // 1000:03d} {number % 1000:03d}"
    with _lock:
        _expire_locked(now)
        _limits_locked(info, caller, now)
        ident = "req_" + secrets.token_urlsafe(6)
        existing = None if upgrade_of else apps.find_existing(info["client_id"], caller.program)
        req = Pending(id=ident, device_hash=_hash(device_code), user_code=user_code,
                      caller=caller, created=now, expires=now + EXPIRES_IN, via=via,
                      upgrade_of=upgrade_of, existing=existing, **info)
        _requests[ident] = req
        _history.append((now, info["client_name"].lower(), (caller.program or "").lower(),
                         "asked"))
    log.info("Demande d'accès de « %s » (%s), programme %s", info["client_name"],
             info["client_id"], caller.program or "inconnu")
    _publish({"type": "gateway_access", "id": ident, "status": "pending"})
    _raise_window()
    return {"device_code": device_code, "user_code": user_code, "expires_in": EXPIRES_IN,
            "interval": INTERVAL, "request_id": ident,
            "message": t("gateway_access.request_waiting", code=user_code)}


def _raise_window():
    now = time.monotonic()
    if now - _raised["at"] < RAISE_EVERY or not _hooks["on_new"]:
        return
    _raised["at"] = now
    try:
        _hooks["on_new"]()
    except Exception:
        log.exception("Passerelle : fenêtre non ramenée")


def _hash(device_code: str) -> str:
    return hashlib.sha256(str(device_code).encode("utf-8")).hexdigest()


def _find_locked(device_code) -> Pending | None:
    wanted = _hash(device_code)
    for req in _requests.values():
        if hmac.compare_digest(req.device_hash, wanted):
            return req
    return None


def poll(device_code) -> tuple:
    """(statut HTTP, corps) de POST /aiodos/v1/access/token, au format d'OAuth."""
    now = time.time()
    with _lock:
        _expire_locked(now)
        req = _find_locked(device_code) if isinstance(device_code, str) and device_code else None
        if req is None:
            return 400, _oauth("invalid_grant", t("gateway_access.grant_unknown"))
        if req.status == "pending":
            too_soon = now - req.last_poll < req.interval * 0.8
            req.last_poll = now
            if too_soon:
                req.interval += SLOW_DOWN
                return 400, _oauth("slow_down", t("gateway_access.slow_down", seconds=SLOW_DOWN))
            return 400, _oauth("authorization_pending", t("gateway_access.pending_user"))
        if req.status == "denied":
            del _requests[req.id]
            return 400, _oauth("access_denied", t("gateway_access.access_denied"))
        if req.status != "approved":
            return 400, _oauth("invalid_grant", t("gateway_access.grant_unknown"))
        granted = dict(req.granted)
        key, req.key, req.status = req.key, None, "collected"
        replace = req.replace
        del _requests[req.id]
    if replace:
        apps.revoke(replace)          # l'ancienne clé tombe quand la nouvelle est relevée
    body = {"token_type": "Bearer", "scope": " ".join(granted["scopes"]),
            "aiodos": {"app_id": granted["app_id"], "where": granted["where"],
                       "priority": granted["priority"], "expires": granted["expires"]}}
    if key:
        body = {"access_token": key, **body}
    else:
        body["aiodos"]["upgraded"] = True
    log.info("Clé relevée par « %s » (%s)", granted["name"], granted["app_id"])
    return 200, body


def _oauth(error: str, description: str) -> dict:
    return {"error": error, "error_description": description}


# --------------------------------------------------------------- l'interface
def pending() -> list:
    """Demandes en attente, pour la carte d'accord (la plus ancienne d'abord)."""
    with _lock:
        _expire_locked(time.time())
        return [r.view() for r in sorted(_requests.values(), key=lambda r: r.created)
                if r.status == "pending"]


def approve(request_id: str, *, scopes_granted=None, where=None, priority="interactive",
            duration="forever", pin=False, replace=True, libraries=()) -> dict:
    """Accord donné dans l'interface. Droits et cercles : ceux demandés, que l'utilisateur
    peut décocher, jamais élargir. → la vue de la demande accordée."""
    with _lock:
        req = _requests.get(request_id)
        if req is None or req.status != "pending":
            raise ApiError("invalid_request", t("gateway_access.request_unknown"))
        granted_scopes = scopes.parse_scopes(scopes_granted if scopes_granted is not None
                                             else req.scopes)
        if not set(granted_scopes) <= set(req.scopes) or not granted_scopes:
            raise ApiError("invalid_request", t("gateway_access.scopes_beyond"))
        granted_where = scopes.parse_where(where if where is not None else req.where)
        if not set(granted_where) <= set(req.where):
            raise ApiError("invalid_request", t("gateway_access.where_beyond"))
        if duration not in DURATIONS:
            raise ApiError("invalid_request", t("gateway_access.duration_invalid"))
        now = time.time()
        consents = {cap: {c: now for c in granted_where if c in scopes.SERVED[cap]}
                    for cap in granted_scopes}
        span = DURATIONS[duration]
        expires = now + span if isinstance(span, int) else None
        if req.upgrade_of:
            current = apps.get(req.upgrade_of)
            if current is None or current["revoked"]:
                raise ApiError("invalid_request", t("gateway_access.upgrade_revoked"))
            merged = sorted(set(current["scopes"]) | set(granted_scopes),
                            key=scopes.SCOPES.index)
            old = current["consents"]
            for cap, circles in consents.items():
                old[cap] = {**{c: now for c in old.get(cap, [])}, **circles}
            apps.update(req.upgrade_of, scopes=merged, consents=old)
            ident, key = req.upgrade_of, None
            final_scopes, final_where = merged, current["where"]
            final_priority, final_expires = current["priority"], current["expires"]
        else:
            ident, key = apps.create(
                kind="app", name=req.client_name, client_id=req.client_id,
                version=req.client_version or None, purpose=req.purpose or None,
                scopes_granted=granted_scopes, reach=granted_where,
                priority=priority if priority in scopes.PRIORITIES else "interactive",
                exe_path=req.caller.program, exe_pinned=bool(pin) and not req.caller.generic,
                libraries=libraries, expires=expires, session_only=span == "session",
                consents=consents)
            final_scopes, final_where = granted_scopes, granted_where
            final_priority, final_expires = priority, expires
        req.status, req.key, req.app_id = "approved", key, ident
        req.replace = (req.existing["id"] if (req.existing and replace and not req.upgrade_of)
                       else None)
        req.expires = now + EXPIRES_IN            # la clé attend sa relève 10 minutes
        req.granted = {"app_id": ident, "scopes": final_scopes, "where": final_where,
                       "priority": final_priority, "expires": final_expires,
                       "name": req.client_name}
        view = req.view()
    if not settings.get("gateway_apps"):
        settings.set_value("gateway_apps", True)
        log.info("Premier accord donné à une application : la passerelle s'ouvre aux applis")
        if _hooks["on_open"]:
            _hooks["on_open"]()
    _publish({"type": "gateway_access", "id": request_id, "status": "approved"})
    _publish({"type": "gateway"})
    return view


def deny(request_id: str, block: bool = False) -> bool:
    with _lock:
        req = _requests.get(request_id)
        if req is None or req.status != "pending":
            return False
        req.status = "denied"
        req.expires = time.time() + EXPIRES_IN
        _history.append((time.time(), req.client_name.lower(),
                         (req.caller.program or "").lower(), "denied"))
        program, name = req.caller.program, req.client_name
    if block and program:
        db.execute("INSERT INTO gateway_blocked(exe_path, name, at) VALUES(?,?,?) "
                   "ON CONFLICT(exe_path) DO UPDATE SET name=excluded.name, at=excluded.at",
                   (program, name, time.time()))
    _publish({"type": "gateway_access", "id": request_id, "status": "denied"})
    return True


def blocked() -> list:
    return [dict(r) for r in db.query("SELECT exe_path, name, at FROM gateway_blocked "
                                      "ORDER BY at DESC")]


def unblock(exe_path: str) -> bool:
    return bool(db.execute("DELETE FROM gateway_blocked WHERE exe_path=?",
                           (exe_path,)).rowcount)


def forget():
    """Tout oublier (essais)."""
    with _lock:
        _requests.clear()
        _history.clear()
    _raised["at"] = 0.0
