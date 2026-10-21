# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""L'accès d'une appli à AIODOS (docs/api-applications.md § 4) : une appli, une clé.

- `connect()` : l'entrée principale. Passerelle ouverte : `POST /aiodos/v1/access/requests` ;
  passerelle fermée (ou ouverte à la clé générale seulement), ou AIODOS arrêté : le programme
  d'aide d'AIODOS (`python -m aiodos --app-request`, § 4.4). Puis le code à afficher, la
  relève de la clé et son rangement dans le coffre.
- `request_access()` : la demande seule ; `AccessRequest.wait()` : la relève, au vocabulaire
  de la RFC 8628 (toutes les `interval` secondes ; `slow_down` : 5 s de plus ; connexion
  refusée tolérée tant que la passerelle n'écoute pas, jusqu'à `expires_in`).
- Avec une clé valide, la même demande ajoute des droits à la même clé (`upgraded`).
- `disconnect()` : `POST /aiodos/v1/access/revoke`, puis la clé est effacée du coffre.
"""

import json
import logging
import time
from dataclasses import dataclass

from . import _discovery, _proof, _transport, errors

log = logging.getLogger("aiodos_client")

CIRCLES = ("here", "mine", "friends", "public")
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
SLOW_DOWN = 5.0                 # RFC 8628 : 5 s de plus à chaque slow_down
MIN_INTERVAL = 0.05
MAX_INTERVAL = 60.0
REQUEST_TIMEOUT = 30.0
POLL_TIMEOUT = 10.0
POLL_CONNECT_TIMEOUT = 2.0


def normalize_where(where):
    """Les cercles de calcul (liste, ou texte séparé par des virgules) vérifiés, ou None."""
    if where is None:
        return None
    items = where.split(",") if isinstance(where, str) else list(where)
    result = []
    for item in items:
        circle = str(item).strip()
        if not circle:
            continue
        if circle not in CIRCLES:
            raise ValueError(f"where : cercle inconnu {circle!r} (parmi {', '.join(CIRCLES)})")
        if circle not in result:
            result.append(circle)
    if not result:
        raise ValueError(f"where : au moins un cercle parmi {', '.join(CIRCLES)}")
    return result


def normalize_scopes(scopes):
    """Les droits demandés (liste, ou texte séparé par des espaces), sans doublon."""
    items = scopes.split() if isinstance(scopes, str) else list(scopes or ())
    result = []
    for item in items:
        scope = str(item).strip()
        if not scope or any(ch.isspace() for ch in scope):
            raise ValueError(f"scopes : droit invalide {item!r}")
        if scope not in result:
            result.append(scope)
    if not result:
        raise ValueError("scopes : au moins un droit (chat, embed, libraries)")
    return result


def _circles(value):
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(part) for part in value)
    return ()


@dataclass(frozen=True)
class Access:
    """L'accès accordé : les droits et les cercles **accordés** (l'utilisateur a pu en
    décocher), la priorité, l'échéance, l'identifiant public de la clé."""
    scopes: tuple = ()
    where: tuple = ()
    priority: str | None = None
    expires: float | None = None
    app_id: str | None = None
    upgraded: bool = False          # droits ajoutés à la clé qu'avait déjà l'appli


def _access_from(data, upgraded):
    ext = data.get("aiodos") if isinstance(data.get("aiodos"), dict) else {}
    scope = data.get("scope")
    return Access(scopes=tuple(scope.split()) if isinstance(scope, str) else (),
                  where=_circles(ext.get("where")), priority=ext.get("priority"),
                  expires=ext.get("expires"), app_id=ext.get("app_id"), upgraded=upgraded)


def _valid_request(data):
    return (isinstance(data, dict) and isinstance(data.get("device_code"), str)
            and data["device_code"] and isinstance(data.get("user_code"), str)
            and data["user_code"])


def _number(value, default):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value > 0 else float(default)


class AccessRequest:
    """Une demande d'accès en cours : `user_code` est à afficher en grand dans l'appli,
    `wait()` relève la clé. Le `device_code` reste caché (jamais dans repr ni le journal)."""

    def __init__(self, client, endpoint, data, upgrade=False):
        self._client = client
        self._endpoint = endpoint
        self._device_code = data["device_code"]
        self.user_code = data["user_code"]
        self.request_id = data.get("request_id") if isinstance(data.get("request_id"), str) \
            else None
        self.interval = _number(data.get("interval"), 2)
        self.expires_in = _number(data.get("expires_in"), 600)
        self.message = data.get("message") if isinstance(data.get("message"), str) else None
        self.url = endpoint.url
        self.upgrade = bool(upgrade)
        self._expires_at = time.monotonic() + self.expires_in
        self._finished = False

    def __repr__(self):
        return (f"AccessRequest(user_code={self.user_code!r}, request_id={self.request_id!r}, "
                f"url={self.url!r}, upgrade={self.upgrade})")

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    @property
    def active(self) -> bool:
        """Encore en attente d'une réponse (ni accordée, ni refusée, ni expirée) ?"""
        return not self._finished and not self.expired

    def wait(self, cancel=None, timeout=None) -> Access:
        """Relève la clé : interroge AIODOS toutes les `interval` secondes jusqu'à la réponse
        de l'utilisateur, range la clé dans le coffre et rend l'Access. Lève AccessDenied,
        AccessExpired, Cancelled (si `cancel()` devient vrai) ou Timeout (`timeout` s)."""
        client = self._client
        stop = _transport.as_cancel(cancel)
        started = time.monotonic()
        interval = min(MAX_INTERVAL, max(MIN_INTERVAL, self.interval))
        client._set_pending(self)
        try:
            while True:
                if stop is not None and stop():
                    raise errors.make("cancelled", client.lang)
                now = time.monotonic()
                if now >= self._expires_at:
                    self._finished = True
                    raise errors.make("expired_token", client.lang)
                if timeout is not None and now - started >= timeout:
                    raise errors.make("timeout", client.lang, detail={"seconds": timeout})
                outcome = self._poll()
                if isinstance(outcome, Access):
                    return outcome
                delay = interval
                if outcome == "slow_down":
                    interval = min(MAX_INTERVAL, interval + SLOW_DOWN)
                    delay = interval
                elif isinstance(outcome, float):
                    delay = max(interval, outcome)          # Retry-After
                end = self._expires_at if timeout is None else min(self._expires_at,
                                                                   started + timeout)
                if _transport.pause(min(delay, max(0.0, end - time.monotonic())), stop):
                    raise errors.make("cancelled", client.lang)
        finally:
            client._clear_pending(self)

    def _poll(self):
        """Une relève : None (pas encore), « slow_down », un délai (s), ou l'Access."""
        client = self._client
        body = json.dumps({"grant_type": GRANT_TYPE, "device_code": self._device_code})
        try:
            resp = _transport.request(self._endpoint, "POST", "/aiodos/v1/access/token",
                                      headers=client._headers(json_body=True),
                                      body=body.encode("utf-8"), timeout=POLL_TIMEOUT,
                                      connect_timeout=POLL_CONNECT_TIMEOUT, lang=client.lang)
        except (errors.Unreachable, errors.Timeout):
            return None             # la passerelle n'écoute pas encore : on continue
        if resp.status == 200:
            data = resp.json(client.lang)
            if not isinstance(data, dict):
                raise errors.make("protocol", client.lang)
            token = data.get("access_token")
            ext = data.get("aiodos") if isinstance(data.get("aiodos"), dict) else {}
            if isinstance(token, str) and token:
                if not _proof.key_id(token):
                    raise errors.make("protocol", client.lang)
                client._store_key(token)
                self._finished = True
                log.info("Accès accordé par AIODOS : clé %s rangée.", _proof.key_id(token))
                return _access_from(data, upgraded=False)
            if ext.get("upgraded") is True:
                client._forget_trust()
                self._finished = True
                log.info("Droits ajoutés par AIODOS à la clé de l'appli.")
                return _access_from(data, upgraded=True)
            raise errors.make("protocol", client.lang)
        exc = errors.from_response(resp.status, resp.headers, resp.body, client.lang)
        if exc.code == "authorization_pending":
            return None
        if exc.code == "slow_down":
            return "slow_down"
        if isinstance(exc, (errors.RateLimited, errors.EngineUnavailable)) or resp.status >= 500:
            return float(exc.retry_after) if exc.retry_after else None
        self._finished = True
        if isinstance(exc, errors.AccessDenied):
            client._set_denied(True)
        raise exc


def _post_request(client, endpoint, payload, key, stop):
    return _transport.request(endpoint, "POST", "/aiodos/v1/access/requests",
                              headers=client._headers(key, json_body=True), body=payload,
                              timeout=REQUEST_TIMEOUT, cancel=stop, lang=client.lang)


def _over_gateway(client, endpoint, body, stop):
    """La demande par la passerelle ouverte. Lève AppsClosed si elle n'est ouverte qu'à la
    clé générale (le programme d'aide prend alors le relais)."""
    info = client._info(endpoint)
    access = info.get("access") if isinstance(info.get("access"), dict) else {}
    if access.get("apps") is False:
        raise errors.make("apps_closed", client.lang)
    if access.get("requests") is False:
        raise errors.make("requests_closed", client.lang)
    if access.get("approval_ui") is False:
        raise errors.make("approval_unavailable", client.lang)
    key = None
    if _proof.key_id(client._stored_key()):
        try:
            key, _ = client._trust(endpoint)            # droits en plus pour la même clé
        except (errors.Untrusted, errors.NeedsAccess):
            key = None                                  # la clé n'est pas envoyée
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    resp = _post_request(client, endpoint, payload, key, stop)
    if key is not None and resp.status == 401:          # clé retirée ou inconnue : à neuf
        key = None
        resp = _post_request(client, endpoint, payload, None, stop)
    client._check(resp.status, resp.headers, resp.body, endpoint)
    data = resp.json(client.lang)
    if not _valid_request(data):
        raise errors.make("protocol", client.lang)
    return AccessRequest(client, endpoint, data, upgrade=key is not None)


def _over_helper(client, endpoint, body, stop):
    """La demande par le programme d'aide d'AIODOS (passerelle fermée, AIODOS arrêté)."""
    inst = _discovery.installation()
    if inst is None:
        raise errors.make("not_installed", client.lang)
    disc = _discovery.read_discovery()
    if disc is not None and disc.running:
        _discovery.allow_foreground(disc.pid)
    data = _discovery.app_request(inst, body, client.lang, cancel=stop)
    url = data.get("url")
    target = _transport.Endpoint(url, client.lang) if url else endpoint
    return AccessRequest(client, target, data, upgrade=False)


def request_access(client, scopes, where=None, purpose=None, cancel=None) -> AccessRequest:
    """La demande d'accès (sans la relève) : rend l'AccessRequest, dont le code est à
    afficher. Passe par le programme d'aide quand la passerelle est fermée."""
    body = {"client_name": client.app, "client_id": client.app_id,
            "client_version": client.version, "scope": " ".join(normalize_scopes(scopes))}
    circles = normalize_where(where)
    if circles is not None:
        body["where"] = circles
    if purpose:
        purpose = str(purpose)
        if len(purpose) > 200:
            raise ValueError("purpose : 200 caractères au plus")
        body["purpose"] = purpose
    stop = _transport.as_cancel(cancel)
    endpoint, disc, applies = client._resolve()
    use_helper = False
    if applies:
        alive = disc.running and _discovery.pid_alive(disc.pid)
        if alive and disc.state == "error":
            raise errors.make("gateway_closed", client.lang, message=disc.error)
        use_helper = not alive or disc.state == "closed"
    request = None
    if not use_helper:
        try:
            request = _over_gateway(client, endpoint, body, stop)
        except (errors.Unreachable, errors.AppsClosed) as exc:
            log.info("Passerelle %s : demande d'accès par le programme d'aide d'AIODOS.",
                     "fermée aux applications" if isinstance(exc, errors.AppsClosed)
                     else "injoignable")
            use_helper = True
    if use_helper:
        request = _over_helper(client, endpoint, body, stop)
    client._set_pending(request)
    log.info("Demande d'accès envoyée (%s).", request.request_id or "sans identifiant")
    return request


def connect(client, scopes, where=None, purpose=None, on_code=None, cancel=None,
            timeout=600) -> Access:
    """La demande d'accès, le code donné à `on_code` (dans le fil de l'appelant), la relève et
    le rangement de la clé. Déjà connectée avec ces droits : l'accès actuel, sans demande."""
    wanted = normalize_scopes(scopes)
    current = client.status()
    if current.state == "ready" and all(scope in current.scopes for scope in wanted):
        return Access(scopes=current.scopes, where=current.where, priority=current.priority,
                      expires=current.expires, app_id=current.app_id)
    request = request_access(client, wanted, where, purpose, cancel)
    if on_code is not None:
        on_code(request.user_code)
    return request.wait(cancel=cancel, timeout=timeout)


def disconnect(client) -> bool:
    """Retire la clé dans AIODOS (« Se déconnecter d'AIODOS »), puis l'efface du coffre."""
    revoked = False
    try:
        if client._stored_key():
            data = client._call("POST", "/aiodos/v1/access/revoke", body={})
            revoked = isinstance(data, dict) and data.get("revoked") is True
    except errors.AiodosError as exc:
        log.info("Retrait auprès d'AIODOS impossible (%s) : la clé est effacée quand même.",
                 exc.code)
    finally:
        client._delete_key()
        client._set_denied(False)
    return revoked
