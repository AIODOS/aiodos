# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""L'état d'AIODOS vu par l'appli (docs/api-applications.md § 6) : `status()`, qui ne lève
jamais, et `wait_ready()`.

status() lit le fichier de découverte, appelle `GET /aiodos/v1/info` avec un défi (la preuve
d'identité, si l'appli a une clé), puis `info` avec la clé si la preuve est juste. Chaque état
porte son message conseillé (texts.py), en français ou en anglais.

| état              | quand                                                               |
|-------------------|---------------------------------------------------------------------|
| `not_installed`   | ni fichier de découverte, ni installation, et rien n'écoute          |
| `not_running`     | installé, rien n'écoute ; ou `running: false` ; ou pid mort          |
| `gateway_closed`  | AIODOS tourne, passerelle fermée                                     |
| `gateway_error`   | `gateway.state: error` (le message d'AIODOS)                         |
| `apps_closed`     | passerelle ouverte à la clé générale seulement                       |
| `not_aiodos`      | le port répond, mais pas comme AIODOS                                |
| `too_old`         | ancienne version (401 sans X-Aiodos-Api, ou `api` trop bas)          |
| `untrusted`       | preuve fausse : la clé n'a pas été envoyée                           |
| `headless`        | `approval_ui: false`, pas de clé                                     |
| `requests_closed` | `access.requests: false`, pas de clé                                 |
| `needs_access`    | pas de clé, ou `auth: invalid`                                       |
| `pending`         | demande en cours (`user_code`)                                       |
| `denied`          | la dernière demande a été refusée                                    |
| `revoked`         | `auth: revoked` ou `expired`                                         |
| `other_account`   | 403 `other_account`                                                  |
| `ready`           | `auth: valid`                                                        |
"""

import logging
import time
from dataclasses import dataclass
from dataclasses import field

from . import _discovery, _proof, errors, texts
from ._transport import as_cancel, pause

log = logging.getLogger("aiodos_client")

STATES = ("not_installed", "not_running", "gateway_closed", "gateway_error", "apps_closed",
          "not_aiodos", "too_old", "untrusted", "headless", "requests_closed", "needs_access",
          "pending", "denied", "revoked", "other_account", "ready")
CONNECT_TIMEOUT = 0.5                   # sur 127.0.0.1, une passerelle qui écoute répond aussitôt
READ_TIMEOUT = 5.0


@dataclass(frozen=True)
class Status:
    """L'état d'AIODOS pour cette appli, et son message conseillé."""
    state: str
    message: str
    url: str | None = None
    version: str | None = None
    instance: str | None = None
    features: tuple = ()
    scopes: tuple = ()
    where: tuple = ()
    user_code: str | None = None
    app_id: str | None = None           # identifiant public de la clé (« k3j9m2qa »)
    priority: str | None = None
    expires: float | None = None
    access: dict | None = field(default=None, compare=False, hash=False)
    limits: dict | None = field(default=None, compare=False, hash=False)

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def serves(self, feature: str) -> bool:
        """AIODOS annonce-t-il cette fonction (`features`) ?"""
        return feature in self.features


def _circles(value):
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(part) for part in value)
    return ()


def _make(client, state, *, message=None, **values):
    if not message:
        message = texts.state(state, client.lang, app=client.app,
                              code=values.get("user_code") or "")
    return Status(state=state, message=message, **values)


def status(client) -> Status:
    """L'état d'AIODOS (ne lève jamais)."""
    try:
        return _status(client)
    except Exception:                                   # status() ne lève jamais
        log.warning("status : erreur inattendue", exc_info=True)
        return _make(client, "not_running")


def _status(client) -> Status:
    pending = client._active_request()
    if pending is not None:
        return _make(client, "pending", url=pending.url, user_code=pending.user_code)
    endpoint, disc, applies = client._resolve()
    url = endpoint.url
    if applies:
        if not disc.running or not _discovery.pid_alive(disc.pid):
            return _make(client, "not_running", url=url)
        if disc.state == "error":
            return _make(client, "gateway_error", url=url, message=disc.error)
        if disc.state == "closed":
            return _make(client, "gateway_closed", url=url)
    key = client._stored_key()
    key_id = _proof.key_id(key)
    challenge = _proof.challenge() if key_id else None
    try:
        info = client._info(endpoint, key_id=key_id, challenge=challenge,
                            connect_timeout=CONNECT_TIMEOUT, timeout=READ_TIMEOUT)
    except errors.Unreachable:
        if disc is None and _discovery.installation() is None:
            return _make(client, "not_installed", url=url)
        return _make(client, "not_running", url=url)
    except errors.TooOld:
        return _make(client, "too_old", url=url)
    except errors.AiodosError:
        return _make(client, "not_aiodos", url=url)
    access = info.get("access") if isinstance(info.get("access"), dict) else {}
    common = {"url": url, "version": info.get("version"), "instance": info.get("instance"),
              "features": tuple(f for f in info.get("features") or () if isinstance(f, str)),
              "access": dict(access)}
    if key_id:
        if not _proof.verify(key, challenge, info.get("proof")):
            client._forget_trust()
            log.warning("Preuve d'identité fausse : la clé n'a pas été envoyée.")
            return _make(client, "untrusted", **common)
        client._remember_trust(endpoint, key, info)
        try:
            keyed = client._info(endpoint, key=key, connect_timeout=CONNECT_TIMEOUT,
                                 timeout=READ_TIMEOUT)
        except errors.OtherAccount:
            return _make(client, "other_account", **common)
        except errors.Unreachable:
            return _make(client, "not_running", url=url)
        except errors.TooOld:
            return _make(client, "too_old", url=url)
        except errors.AiodosError:
            return _make(client, "not_aiodos", url=url)
        if isinstance(keyed.get("access"), dict):
            access = keyed["access"]
            common["access"] = dict(access)
        auth = keyed.get("auth")
        if auth == "valid":
            if access.get("apps") is False:
                return _make(client, "apps_closed", **common)
            app = keyed.get("app") if isinstance(keyed.get("app"), dict) else {}
            limits = keyed.get("limits") if isinstance(keyed.get("limits"), dict) else None
            return _make(client, "ready", scopes=_circles(app.get("scopes")),
                         where=_circles(app.get("where")), app_id=app.get("id"),
                         priority=app.get("priority"), expires=app.get("expires"),
                         limits=limits, **common)
        if auth in ("revoked", "expired"):
            return _make(client, "revoked", **common)
        # auth « invalid » : comme sans clé
    if client._was_denied():
        return _make(client, "denied", **common)
    if access.get("apps") is False:
        return _make(client, "apps_closed", **common)
    if access.get("requests") is False:
        return _make(client, "requests_closed", **common)
    if access.get("approval_ui") is False:
        return _make(client, "headless", **common)
    return _make(client, "needs_access", **common)


def wait_ready(client, timeout=60.0, cancel=None, interval=0.5) -> Status:
    """Attend qu'AIODOS réponde (un état autre que not_installed et not_running), au plus
    `timeout` secondes, et rend le dernier état (ne lève jamais)."""
    stop = as_cancel(cancel)
    deadline = time.monotonic() + float(timeout)
    while True:
        current = status(client)
        if current.state not in ("not_installed", "not_running"):
            return current
        left = deadline - time.monotonic()
        if left <= 0 or (stop is not None and stop()):
            return current
        if pause(min(interval, left), stop):
            return current
