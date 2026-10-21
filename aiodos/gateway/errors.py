"""Erreurs de la passerelle, à codes stables (docs/api-applications.md § 3).

Une application ne lit pas les messages, faits pour l'utilisateur : elle réagit au code, qui
ne change pas d'une version à l'autre. Chaque code a son statut HTTP, un message et un
conseil en français et en anglais (Accept-Language), et dit si un nouvel essai a un sens.
Trois enveloppes, selon la famille de la route :
- /v1/* et /aiodos/v1/* : celle d'OpenAI, plus un objet « aiodos » (hint, retryable, detail) ;
- /api/* (forme d'Ollama) : {"error": "…", "code": "…", "hint": "…"} ; les clients d'Ollama
  ne lisent que « error » ;
- /mcp : une erreur JSON-RPC, le code dans error.data.code.
"""
from starlette.responses import JSONResponse

from aiodos.i18n import current, exists, language, t, using

# code : (statut, réessayer ?) ; son message et son conseil sont dans aiodos/i18n/*/
# gateway_errors.json (clés <code> et <code>_hint ; un code sans conseil n'a pas de _hint)
CODES = {
    "host_refused": (400, False),
    "invalid_request": (400, False),
    "context_length_exceeded": (400, False),
    "tools_unavailable": (400, False),
    "vision_unsupported": (400, False),
    "local_only": (400, False),
    "unsupported_parameter": (400, False),
    # Travaux de moteur (docs/conception/partage-puissance.md § 6.9)
    "graph_not_api": (400, False),
    "graph_invalid": (400, False),
    "patch_invalid": (400, False),
    "forbidden_value": (400, False),
    "forbidden_node": (400, False),
    "input_missing": (400, False),
    "input_hash_mismatch": (400, False),
    "invalid_api_key": (401, False),
    "key_revoked": (401, False),
    "key_expired": (401, False),
    "scope_denied": (403, False),
    "where_denied": (403, False),
    "apps_closed": (403, False),
    "requests_closed": (403, False),
    "blocked_program": (403, False),
    "other_account": (403, False),
    "program_mismatch": (403, False),
    "origin_refused": (403, False),
    "model_not_found": (404, False),
    "not_supported": (404, False),
    "engine_not_found": (404, False),
    "input_not_found": (404, False),
    "job_not_found": (404, False),
    "file_not_found": (404, False),
    "no_machine": (409, True),
    "model_unavailable": (409, True),
    "model_unresolved": (409, False),
    "model_ambiguous": (409, False),
    "job_not_cancellable": (409, False),
    "job_not_pausable": (409, False),
    "file_too_large": (413, False),
    "graph_too_large": (413, False),
    "input_too_large": (413, False),
    "unsupported_media_type": (415, False),
    "rate_limited": (429, True),
    "quota_exceeded": (429, True),
    "busy": (429, True),
    "cancelled": (499, False),
    "peer_failed": (502, True),
    "engine_failed": (502, True),
    "engine_unavailable": (503, True),
    "approval_unavailable": (503, True),
    "timeout": (504, True),
    "internal_error": (500, True),
}

# Code par défaut d'un statut, pour une erreur qui n'en donne pas (answer.GatewayError)
BY_STATUS = {400: "invalid_request", 401: "invalid_api_key", 403: "scope_denied",
             404: "model_not_found", 409: "no_machine", 413: "file_too_large",
             415: "unsupported_media_type", 429: "rate_limited", 499: "cancelled",
             502: "engine_failed", 503: "engine_unavailable", 504: "timeout"}


class ApiError(Exception):
    """Refus d'une requête, avec son code stable (CODES). message : à la place de celui du
    code (plus précis) ; detail : données pour l'appli ; retry_after : en secondes."""

    def __init__(self, code: str, message: str | None = None, *, param=None, detail=None,
                 retry_after: int | None = None, status: int | None = None):
        known = CODES.get(code) or CODES["internal_error"]
        self.code = code if code in CODES else "internal_error"
        super().__init__(message or t(f"gateway_errors.{self.code}"))
        self.status = status or known[0]
        self.custom = message
        self.param = param
        self.detail = detail or {}
        self.retry_after = retry_after


def lang_of(headers) -> str:
    """La langue des messages : celle qu'Accept-Language préfère, du français et de
    l'anglais ; sinon celle de la requête en cours, sinon celle du nœud."""
    raw = ""
    if isinstance(headers, dict):
        raw = headers.get(b"accept-language", b"")
        raw = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
    elif headers is not None:
        raw = headers.get("accept-language") or ""
    return language.from_header(raw) or current()


def _texts(code: str):
    """(message, conseil, réessayer ?) de ce code, dans la langue en cours."""
    code = code if code in CODES else "internal_error"
    hint = f"gateway_errors.{code}_hint"
    return t(f"gateway_errors.{code}"), (t(hint) if exists(hint) else ""), CODES[code][1]


def body(path: str, error: ApiError, lang: str | None = None) -> dict:
    """Le corps de l'erreur, dans l'enveloppe de la famille de la route ; ses textes dans la
    langue lang (par défaut, celle de la requête en cours)."""
    with using(lang or current()):
        default, hint, retryable = _texts(error.code)
    # Le message précis (nom du modèle, de la machine…), dans la langue de la requête qui l'a
    # fait naître ; sinon celui du code
    message = error.custom or default
    if path.startswith("/api/"):
        return {"error": message, "code": error.code, "hint": hint}
    if path.startswith("/mcp"):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": message,
                          "data": {"code": error.code, "hint": hint}}}
    kind = "invalid_request_error" if error.status < 500 else "api_error"
    return {"error": {"message": message, "type": kind, "param": error.param,
                      "code": error.code,
                      "aiodos": {"hint": hint, "retryable": retryable, "detail": error.detail}}}


def response(path: str, error: ApiError, lang: str | None = None) -> JSONResponse:
    headers = {}
    if error.retry_after is not None:
        headers["Retry-After"] = str(int(error.retry_after))
    return JSONResponse(body(path, error, lang), status_code=error.status, headers=headers)


def from_status(status: int, message: str | None = None, code: str | None = None,
                **kw) -> ApiError:
    """L'ApiError d'une erreur qui n'avait qu'un statut (answer.GatewayError…)."""
    code = code or BY_STATUS.get(status) or ("internal_error" if status >= 500 else "invalid_request")
    return ApiError(code, message, status=status, **kw)
