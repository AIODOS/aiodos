# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Les erreurs d'aiodos-client : une sous-classe par code stable (docs/api-applications.md § 3).

Toutes dérivent d'AiodosError(code, status, message, hint, retryable, detail, retry_after) :
- `code` : le code stable de la passerelle (« tools_unavailable »…) ou du client
  (« unreachable », « cancelled »…) ; c'est lui qu'une appli traduit ou teste ;
- `status` : le statut HTTP (None pour une erreur née dans le client) ;
- `message` et `hint` : le texte d'AIODOS (ou du client) et son conseil ;
- `retryable`, `detail` (dict), `retry_after` (secondes, d'après Retry-After).

`from_response()` fabrique la bonne sous-classe à partir d'une réponse d'erreur, quelle qu'en
soit la forme : enveloppe d'OpenAI avec son objet `aiodos`, forme d'Ollama
(`{"error": "…", "code": "…", "hint": "…"}`), forme d'OAuth (`{"error", "error_description"}`),
erreur d'un flux (`data: {"error": {…}}`) ou sortie du programme d'aide.

Une clé d'accès n'apparaît jamais dans un message : le client n'en écrit aucune, et
`redact()` masque par prudence tout ce qui en aurait la forme dans un texte reçu.
"""

import email.utils
import json
import re
import time

from . import texts

# Ce qui a la forme d'une clé d'appli (aiodos_app_<id>_<secret>) ou de la clé générale.
_SECRET = re.compile(r"aiodos_app_([a-z2-7]{8})_[A-Za-z0-9_-]{16,}|aiodos-[A-Za-z0-9_-]{16,}")

# Statut HTTP de chaque code (docs/api-applications.md § 3), pour une erreur qui arrive sans
# statut (en cours de flux, ou du programme d'aide).
STATUS_OF = {
    "host_refused": 400, "invalid_request": 400, "context_length_exceeded": 400,
    "tools_unavailable": 400, "vision_unsupported": 400, "local_only": 400,
    "unsupported_parameter": 400, "invalid_api_key": 401, "key_revoked": 401,
    "key_expired": 401, "scope_denied": 403, "where_denied": 403, "apps_closed": 403,
    "requests_closed": 403, "blocked_program": 403, "other_account": 403,
    "program_mismatch": 403, "origin_refused": 403, "model_not_found": 404,
    "not_supported": 404, "no_machine": 409, "file_too_large": 413,
    "unsupported_media_type": 415, "rate_limited": 429, "busy": 429, "peer_failed": 502,
    "engine_failed": 502, "engine_unavailable": 503, "approval_unavailable": 503,
    # les travaux ComfyUI (§ 5.7)
    "graph_not_api": 400, "graph_invalid": 400, "patch_invalid": 400, "forbidden_value": 400,
    "forbidden_node": 400, "input_missing": 400, "input_hash_mismatch": 400,
    "engine_not_found": 404, "job_not_found": 404, "file_not_found": 404,
    "input_not_found": 404, "model_unavailable": 409, "model_unresolved": 409,
    "model_ambiguous": 409, "job_not_cancellable": 409, "job_not_pausable": 409,
    "graph_too_large": 413, "input_too_large": 413, "quota_exceeded": 429,
}

# Codes à réessayer quand la réponse ne le dit pas elle-même.
RETRYABLE = frozenset({"unreachable", "timeout", "rate_limited", "busy", "engine_unavailable",
                       "peer_failed", "engine_failed"})

# Codes de la forme d'OAuth (relève de la clé, docs/api-applications.md § 4.3).
OAUTH_CODES = frozenset({"authorization_pending", "slow_down", "access_denied",
                         "expired_token", "invalid_grant", "invalid_request",
                         "invalid_client", "unsupported_grant_type"})


def redact(text):
    """Le texte, où tout ce qui a la forme d'une clé d'accès est masqué."""
    if not isinstance(text, str):
        return text
    return _SECRET.sub(lambda m: f"aiodos_app_{m.group(1)}_…" if m.group(1) else "aiodos-…",
                       text)


def _clean(value, depth=0):
    """Le détail d'une erreur, sans rien qui ressemble à une clé (récursif, borné)."""
    if depth > 8:
        return None
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, depth + 1) for v in value]
    return value


def _rebuild(cls, *args):
    return cls(*args)


class AiodosError(Exception):
    """Erreur d'AIODOS ou du client, à code stable."""

    default_code = "error"

    def __init__(self, code=None, status=None, message=None, hint=None, retryable=None,
                 detail=None, retry_after=None, param=None):
        self.code = str(code or self.default_code)
        self.status = status
        self.message = redact(str(message)) if message else self.code
        self.hint = redact(str(hint)) if hint else None
        if retryable is None:
            retryable = self.code in RETRYABLE or status in (429, 502, 503)
        self.retryable = bool(retryable)
        self.detail = _clean(detail) if isinstance(detail, dict) else {}
        self.retry_after = retry_after
        self.param = param
        super().__init__(self.message)

    def __str__(self):
        return self.message

    def __repr__(self):
        return (f"{type(self).__name__}(code={self.code!r}, status={self.status!r}, "
                f"message={self.message!r})")

    def __reduce__(self):
        return (_rebuild, (type(self), self.code, self.status, self.message, self.hint,
                           self.retryable, self.detail, self.retry_after, self.param))


# --- AIODOS introuvable, ou pas le bon programme ---------------------------------------
class NotInstalled(AiodosError):
    default_code = "not_installed"


class NotRunning(AiodosError):
    default_code = "not_running"


class Unreachable(AiodosError):
    """Connexion refusée ou coupée (le message contient « Connexion refusée »)."""
    default_code = "unreachable"


class GatewayClosed(AiodosError):
    default_code = "gateway_closed"


class AppsClosed(GatewayClosed):
    """Passerelle ouverte à la clé générale seulement."""
    default_code = "apps_closed"


class NotAiodos(AiodosError):
    default_code = "not_aiodos"


class TooOld(AiodosError):
    default_code = "too_old"


class Untrusted(AiodosError):
    """La preuve d'identité est fausse : la clé n'a pas été envoyée."""
    default_code = "untrusted"


class NonLocalUrl(AiodosError):
    default_code = "non_local_url"


class ProtocolError(AiodosError):
    default_code = "protocol"


class Timeout(AiodosError):
    default_code = "timeout"


class Cancelled(AiodosError):
    """Arrêté à la demande de l'appli (message « Arrêté »)."""
    default_code = "cancelled"


# --- L'accès ---------------------------------------------------------------------------
class NeedsAccess(AiodosError):
    """Pas de clé, ou clé inconnue d'AIODOS (invalid_api_key)."""
    default_code = "needs_access"


class AccessDenied(AiodosError):
    default_code = "access_denied"


class BlockedProgram(AccessDenied):
    default_code = "blocked_program"


class AccessExpired(AiodosError):
    """Demande d'accès expirée, ou inconnue d'AIODOS (expired_token, invalid_grant)."""
    default_code = "expired_token"


class Revoked(AiodosError):
    """Clé retirée ou expirée dans AIODOS (key_revoked, key_expired)."""
    default_code = "key_revoked"


class RequestsClosed(AiodosError):
    default_code = "requests_closed"


class ApprovalUnavailable(AiodosError):
    """AIODOS tourne sans fenêtre : personne ne peut autoriser l'appli."""
    default_code = "approval_unavailable"


class ScopeDenied(AiodosError):
    default_code = "scope_denied"

    @property
    def scope(self):
        return self.detail.get("scope")


class WhereDenied(AiodosError):
    default_code = "where_denied"

    @property
    def allowed(self):
        return self.detail.get("allowed")


class ProgramMismatch(AiodosError):
    default_code = "program_mismatch"


class OtherAccount(AiodosError):
    default_code = "other_account"


class OriginRefused(AiodosError):
    default_code = "origin_refused"


class HostRefused(AiodosError):
    default_code = "host_refused"


# --- La demande ------------------------------------------------------------------------
class InvalidRequest(AiodosError):
    default_code = "invalid_request"


class Unsupported(InvalidRequest):
    """Paramètre standard qu'AIODOS ne traite pas (unsupported_parameter, `param`)."""
    default_code = "unsupported_parameter"


class UnsupportedMediaType(InvalidRequest):
    default_code = "unsupported_media_type"


class ContextTooLong(InvalidRequest):
    """La demande dépasse le contexte (`needed` et `limit` quand AIODOS les connaît)."""
    default_code = "context_length_exceeded"

    @property
    def needed(self):
        return self.detail.get("needed")

    @property
    def limit(self):
        return self.detail.get("limit")


class ToolsUnavailable(InvalidRequest):
    default_code = "tools_unavailable"


class VisionUnsupported(InvalidRequest):
    default_code = "vision_unsupported"


class LocalOnly(InvalidRequest):
    default_code = "local_only"


class FileTooLarge(AiodosError):
    default_code = "file_too_large"


class ModelNotFound(AiodosError):
    default_code = "model_not_found"


class NoMachine(AiodosError):
    default_code = "no_machine"


class NotSupported(AiodosError):
    """Fonction qu'AIODOS (ou ce client) ne sert pas encore : rien n'a été essayé."""
    default_code = "not_supported"


# --- Charge et pannes ------------------------------------------------------------------
class RateLimited(AiodosError):
    """Trop de demandes : attendre `retry_after` secondes (Retry-After)."""
    default_code = "rate_limited"


class Busy(RateLimited):
    default_code = "busy"


class EngineUnavailable(AiodosError):
    default_code = "engine_unavailable"


class PeerFailed(AiodosError):
    default_code = "peer_failed"


class EngineFailed(PeerFailed):
    default_code = "engine_failed"


# --- Les travaux ComfyUI (docs/api-applications.md § 3, « Les travaux ComfyUI ajoutent ») -
class GraphInvalid(InvalidRequest):
    """Graphe refusé avant toute mise en file : mal formé (graph_invalid), au format de
    l'éditeur (graph_not_api), ou retouche impossible (patch_invalid) ; `code` les distingue."""
    default_code = "graph_invalid"

    @property
    def node(self):
        return self.detail.get("node")

    @property
    def graph(self):
        """L'indice du graphe en cause (dans `graphs`)."""
        return self.detail.get("graph")


class ForbiddenValue(InvalidRequest):
    """Un chemin, une adresse ou une annotation de dossier dans une valeur du graphe."""
    default_code = "forbidden_value"

    @property
    def node(self):
        return self.detail.get("node")

    @property
    def input(self):
        return self.detail.get("input")

    @property
    def reason(self):
        return self.detail.get("reason")


class ForbiddenNode(InvalidRequest):
    """Un nœud d'un service en ligne (`detail["class"]`)."""
    default_code = "forbidden_node"

    @property
    def node_class(self):
        return self.detail.get("class")


class InputMissing(InvalidRequest):
    """Jeton @in: sans entrée déclarée, ou entrée jamais envoyée par cette appli."""
    default_code = "input_missing"

    @property
    def input(self):
        return self.detail.get("input")


class InputHashMismatch(InvalidRequest):
    """Les octets reçus par AIODOS n'ont pas l'empreinte de l'adresse (`sha256` : la leur)."""
    default_code = "input_hash_mismatch"

    @property
    def sha256(self):
        return self.detail.get("sha256")


class InputTooLarge(FileTooLarge):
    """Une entrée de plus de 100 Mo : refusée par le client avant l'envoi, ou par AIODOS."""
    default_code = "input_too_large"


class GraphTooLarge(FileTooLarge):
    """2 Mo par graphe, 8 Mo et 64 graphes par travail."""
    default_code = "graph_too_large"


class NotFound(AiodosError):
    """Introuvable, ou à une autre appli (une appli ne voit que ce qui est à elle)."""
    default_code = "not_found"


class JobNotFound(NotFound):
    default_code = "job_not_found"


class FileNotFound(NotFound):
    default_code = "file_not_found"


class InputNotFound(NotFound):
    default_code = "input_not_found"


class EngineNotFound(NotFound):
    default_code = "engine_not_found"


class ModelUnavailable(AiodosError):
    """Il manque des modèles aux machines permises (`models` : [{folder, name}] ; `machine` :
    celle à qui il en manque le moins)."""
    default_code = "model_unavailable"

    @property
    def models(self):
        found = self.detail.get("models")
        return found if isinstance(found, list) else []

    @property
    def machine(self):
        return self.detail.get("machine")


class ModelUnresolved(AiodosError):
    """Un jeton @model: sans modèle déclaré dans « models »."""
    default_code = "model_unresolved"


class ModelAmbiguous(AiodosError):
    """Le même nom de modèle dans plusieurs dossiers (`candidates`) : le déclarer."""
    default_code = "model_ambiguous"

    @property
    def candidates(self):
        found = self.detail.get("candidates")
        return found if isinstance(found, list) else []


class JobNotCancellable(AiodosError):
    """Le travail est déjà fini (ou, pour l'oublier, pas encore fini)."""
    default_code = "job_not_cancellable"


class JobNotPausable(AiodosError):
    """Le travail est déjà fini."""
    default_code = "job_not_pausable"


class QuotaExceeded(AiodosError):
    """Une limite de l'appli atteinte (`quota` : active_jobs, inputs, unfetched_files…) :
    récupérer ou supprimer des fichiers et des travaux finis. Pas un RateLimited : attendre
    ne suffit pas toujours."""
    default_code = "quota_exceeded"

    @property
    def quota(self):
        return self.detail.get("quota")


BY_CODE = {
    "not_installed": NotInstalled, "not_running": NotRunning, "unreachable": Unreachable,
    "gateway_closed": GatewayClosed, "apps_closed": AppsClosed, "not_aiodos": NotAiodos,
    "too_old": TooOld, "untrusted": Untrusted, "non_local_url": NonLocalUrl,
    "protocol": ProtocolError, "timeout": Timeout, "cancelled": Cancelled,
    "needs_access": NeedsAccess, "invalid_api_key": NeedsAccess,
    "access_denied": AccessDenied, "blocked_program": BlockedProgram,
    "expired_token": AccessExpired, "invalid_grant": AccessExpired,
    "key_revoked": Revoked, "key_expired": Revoked, "requests_closed": RequestsClosed,
    "approval_unavailable": ApprovalUnavailable, "scope_denied": ScopeDenied,
    "where_denied": WhereDenied, "program_mismatch": ProgramMismatch,
    "other_account": OtherAccount, "origin_refused": OriginRefused,
    "host_refused": HostRefused, "invalid_request": InvalidRequest,
    "unsupported_parameter": Unsupported, "unsupported_media_type": UnsupportedMediaType,
    "context_length_exceeded": ContextTooLong, "tools_unavailable": ToolsUnavailable,
    "vision_unsupported": VisionUnsupported, "local_only": LocalOnly,
    "file_too_large": FileTooLarge, "model_not_found": ModelNotFound,
    "no_machine": NoMachine, "not_supported": NotSupported, "rate_limited": RateLimited,
    "busy": Busy, "engine_unavailable": EngineUnavailable, "peer_failed": PeerFailed,
    "engine_failed": EngineFailed,
    "graph_invalid": GraphInvalid, "graph_not_api": GraphInvalid, "patch_invalid": GraphInvalid,
    "forbidden_value": ForbiddenValue, "forbidden_node": ForbiddenNode,
    "input_missing": InputMissing, "input_hash_mismatch": InputHashMismatch,
    "input_too_large": InputTooLarge, "graph_too_large": GraphTooLarge,
    "job_not_found": JobNotFound, "file_not_found": FileNotFound,
    "input_not_found": InputNotFound, "engine_not_found": EngineNotFound,
    "model_unavailable": ModelUnavailable, "model_unresolved": ModelUnresolved,
    "model_ambiguous": ModelAmbiguous, "job_not_cancellable": JobNotCancellable,
    "job_not_pausable": JobNotPausable, "quota_exceeded": QuotaExceeded,
}


def class_for(code: str):
    """La sous-classe d'un code (AiodosError pour un code inconnu)."""
    return BY_CODE.get(code, AiodosError)


def make(code: str, lang: str = "fr", *, message=None, hint=None, status=None,
         retryable=None, detail=None, retry_after=None, **fields):
    """Une erreur née dans le client (« unreachable », « cancelled »…), avec ses textes."""
    if message is None:
        message = texts.error(code, lang, **fields) or code
    if hint is None:
        hint = texts.hint(code, lang, **fields)
    return class_for(code)(code, status, message, hint, retryable, detail, retry_after)


def _header(headers, name):
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is None and isinstance(headers, dict):
        lower = name.lower()
        for key, val in headers.items():
            if str(key).lower() == lower:
                return val
    return value


def retry_after_of(headers):
    """Les secondes de Retry-After (nombre, ou date HTTP), ou None."""
    value = _header(headers, "Retry-After")
    if value is None:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _parse(body):
    if body is None or isinstance(body, (dict, list)):
        return body
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(body, str):
        try:
            return json.loads(body) if body.strip() else None
        except ValueError:
            return None
    return None


def _fallback_code(status):
    if status is None:
        return "protocol"
    if 300 <= status < 400:
        return "protocol"
    return {400: "invalid_request", 401: "invalid_api_key", 404: "not_supported",
            413: "file_too_large", 415: "unsupported_media_type", 429: "rate_limited",
            502: "peer_failed", 503: "engine_unavailable"}.get(status, f"http_{status}")


def from_response(status, headers=None, body=None, lang: str = "fr") -> AiodosError:
    """L'erreur que porte une réponse de la passerelle, sous sa bonne sous-classe.

    `body` : octets, texte ou objet déjà lu. Comprend l'enveloppe d'OpenAI (avec ou sans son
    objet `aiodos`), la forme d'Ollama, celle d'OAuth et celle de MCP (`error.data.code`).
    """
    data = _parse(body)
    code = message = hint = param = None
    retryable = None
    detail = {}
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):                       # OpenAI (+ aiodos), MCP, programme d'aide
            message = err.get("message")
            code = err.get("code")
            param = err.get("param")
            ext = err.get("aiodos")
            if isinstance(ext, dict):
                hint = ext.get("hint")
                retryable = ext.get("retryable") if isinstance(ext.get("retryable"), bool) \
                    else None
                detail = ext.get("detail") if isinstance(ext.get("detail"), dict) else {}
            if not isinstance(code, str):
                extra = err.get("data")
                code = extra.get("code") if isinstance(extra, dict) else None
        elif isinstance(err, str):
            if "error_description" in data or (err in OAUTH_CODES and "code" not in data):
                code = err                              # OAuth
                message = data.get("error_description")
            else:                                       # Ollama (et anciennes versions)
                message = err
                code = data.get("code")
                hint = data.get("hint")
    if not isinstance(code, str) or not code:
        code = _fallback_code(status)
        if status is not None and 300 <= status < 400:
            message = texts.error("protocol", lang)
            detail = {"status": status}
    if not isinstance(message, str) or not message:
        message = texts.error(code, lang) or (texts.error("http", lang, status=status)
                                              if status else code)
    if not isinstance(hint, str) or not hint:
        hint = texts.hint(code, lang)
    if status is None:
        status = STATUS_OF.get(code)
    return class_for(code)(code, status, message, hint, retryable, detail,
                           retry_after_of(headers), param if isinstance(param, str) else None)
