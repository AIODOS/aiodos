# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Les erreurs : chaque réponse d'erreur du contrat devient la bonne sous-classe, qui porte
code, statut, message, conseil, retryable, détail et Retry-After ; les codes propres aux
clients ont leurs textes."""

import json
import pickle

import pytest

from aiodos_client import errors
from aiodos_client.fake import new_key
from support import load_contract

EXPECTED = {
    "invalid_api_key": errors.NeedsAccess, "key_revoked": errors.Revoked,
    "where_denied": errors.WhereDenied, "model_not_found": errors.ModelNotFound,
    "busy": errors.Busy, "engine_unavailable": errors.EngineUnavailable,
    "context_length_exceeded": errors.ContextTooLong, "apps_closed": errors.AppsClosed,
    "requests_closed": errors.RequestsClosed, "origin_refused": errors.OriginRefused,
    "scope_denied": errors.ScopeDenied, "tools_unavailable": errors.ToolsUnavailable,
    "access_denied": errors.AccessDenied, "expired_token": errors.AccessExpired,
    "invalid_grant": errors.AccessExpired, "authorization_pending": errors.AiodosError,
    "slow_down": errors.AiodosError,
    "input_hash_mismatch": errors.InputHashMismatch,
    "job_not_cancellable": errors.JobNotCancellable, "job_not_found": errors.JobNotFound,
    "graph_not_api": errors.GraphInvalid, "model_unavailable": errors.ModelUnavailable,
}
# Les codes des travaux ComfyUI (docs/api-applications.md § 3) : classe, statut, parente
JOB_CODES = {
    "graph_not_api": (errors.GraphInvalid, 400, errors.InvalidRequest),
    "graph_invalid": (errors.GraphInvalid, 400, errors.InvalidRequest),
    "patch_invalid": (errors.GraphInvalid, 400, errors.InvalidRequest),
    "forbidden_value": (errors.ForbiddenValue, 400, errors.InvalidRequest),
    "forbidden_node": (errors.ForbiddenNode, 400, errors.InvalidRequest),
    "input_missing": (errors.InputMissing, 400, errors.InvalidRequest),
    "input_hash_mismatch": (errors.InputHashMismatch, 400, errors.InvalidRequest),
    "engine_not_found": (errors.EngineNotFound, 404, errors.NotFound),
    "job_not_found": (errors.JobNotFound, 404, errors.NotFound),
    "file_not_found": (errors.FileNotFound, 404, errors.NotFound),
    "input_not_found": (errors.InputNotFound, 404, errors.NotFound),
    "model_unavailable": (errors.ModelUnavailable, 409, errors.AiodosError),
    "model_unresolved": (errors.ModelUnresolved, 409, errors.AiodosError),
    "model_ambiguous": (errors.ModelAmbiguous, 409, errors.AiodosError),
    "job_not_cancellable": (errors.JobNotCancellable, 409, errors.AiodosError),
    "job_not_pausable": (errors.JobNotPausable, 409, errors.AiodosError),
    "graph_too_large": (errors.GraphTooLarge, 413, errors.FileTooLarge),
    "input_too_large": (errors.InputTooLarge, 413, errors.FileTooLarge),
    "quota_exceeded": (errors.QuotaExceeded, 429, errors.AiodosError),
    "engine_unavailable": (errors.EngineUnavailable, 503, errors.AiodosError),
}
CLIENT_CODES = ("unreachable", "gateway_closed", "not_aiodos", "too_old", "untrusted",
                "non_local_url", "timeout", "cancelled", "protocol", "not_installed",
                "not_running", "needs_access", "access_denied", "expired_token")


def _error_interactions():
    found = []
    for name in ("errors.json", "access.json", "embed.json", "chat.json", "jobs.json"):
        for item in load_contract(name)["interactions"]:
            response = item["response"]
            if response["status"] >= 400 and isinstance(response.get("json"), dict):
                found.append(pytest.param(item, id=f"{name}:{item['name']}"))
    return found


def _code_of(body):
    error = body["error"]
    if isinstance(error, dict):
        return error["code"]
    return body.get("code") or error


@pytest.mark.parametrize("item", _error_interactions())
def test_contract_error_becomes_the_right_class(item):
    response = item["response"]
    body = response["json"]
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    exc = errors.from_response(response["status"], response["headers"], raw)
    code = _code_of(body)
    assert type(exc) is EXPECTED[code]
    assert exc.code == code
    assert exc.status == response["status"]
    error = body["error"]
    if isinstance(error, dict):
        assert exc.message == error["message"]
        assert exc.hint == (error["aiodos"]["hint"] or None)        # sans conseil : None
        assert exc.retryable is error["aiodos"]["retryable"]
        assert exc.detail == error["aiodos"]["detail"]
        assert exc.param == error["param"]
    elif "error_description" in body:
        assert exc.message == body["error_description"]
    else:
        assert exc.message == error
        assert exc.hint == body["hint"]
    retry_after = response["headers"].get("Retry-After")
    assert exc.retry_after == (float(retry_after) if retry_after else None)
    assert str(exc) == exc.message


def test_specific_attributes():
    contract = {item["name"]: item for item in load_contract("errors.json")["interactions"]}
    too_long = contract["texte trop long pour le contexte"]["response"]
    exc = errors.from_response(too_long["status"], too_long["headers"], too_long["json"])
    assert (exc.needed, exc.limit) == (20512, 16384)
    assert isinstance(exc, errors.InvalidRequest)
    where = contract["lieu plus large que l'accord"]["response"]
    assert errors.from_response(403, where["headers"], where["json"]).allowed == ["here", "mine"]
    busy = contract["trop de demandes"]["response"]
    exc = errors.from_response(429, busy["headers"], busy["json"])
    assert isinstance(exc, errors.RateLimited) and exc.retry_after == 2.0 and exc.retryable
    scope = load_contract("embed.json")["interactions"][1]["response"]
    assert errors.from_response(403, scope["headers"], scope["json"]).scope == "embed"


def test_in_stream_error_and_helper_error():
    chat = {item["name"]: item for item in load_contract("chat.json")["interactions"]}
    line = chat["conversation en flux : panne en route"]["response"]["sse"][-1]
    exc = errors.from_response(None, None, line[len("data: "):])
    assert type(exc) is errors.EngineFailed and isinstance(exc, errors.PeerFailed)
    assert exc.status == 502 and exc.retryable and exc.hint == "Réessaie."
    helper = load_contract("discovery.json")["app_request"]["stdout_error"]
    exc = errors.from_response(None, None, helper)
    assert type(exc) is errors.ApprovalUnavailable
    assert exc.message == helper["error"]["message"] and exc.status == 503


def test_other_forms_and_fallbacks():
    old = errors.from_response(401, {}, b'{"error": "Cl\\u00e9 manquante."}')
    assert type(old) is errors.NeedsAccess and old.message == "Clé manquante."
    mcp = errors.from_response(403, {}, {"jsonrpc": "2.0", "error": {
        "code": -32600, "message": "Refusé", "data": {"code": "scope_denied"}}})
    assert type(mcp) is errors.ScopeDenied
    assert type(errors.from_response(503, {"Retry-After": "7"}, b"")) is errors.EngineUnavailable
    assert errors.from_response(503, {"retry-after": "7"}, b"").retry_after == 7.0
    redirect = errors.from_response(302, {"Location": "http://127.0.0.1:1/"}, b"")
    assert type(redirect) is errors.ProtocolError and redirect.detail == {"status": 302}
    unknown = errors.from_response(418, {}, {"error": {"message": "Théière", "code": "teapot"}})
    assert type(unknown) is errors.AiodosError and unknown.code == "teapot"
    assert errors.from_response(500, {}, b"<html>").code == "http_500"
    when = errors.from_response(429, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}, b"")
    assert when.retry_after > 0


@pytest.mark.parametrize("code", sorted(JOB_CODES))
def test_job_codes_have_classes_statuses_and_texts(code):
    cls, status, parent = JOB_CODES[code]
    exc = errors.from_response(None, None, {"error": {"code": code, "message": "x"}})
    assert type(exc) is cls and issubclass(cls, parent) and exc.status == status
    bare = errors.from_response(status, {}, {"error": {"code": code}})
    for lang in ("fr", "en"):
        made = errors.make(code, lang)
        assert type(made) is cls and made.message and made.message != code
    assert bare.message == errors.make(code, "fr").message


def test_job_error_details():
    graph = errors.from_response(400, {}, {"error": {"code": "patch_invalid", "aiodos": {
        "detail": {"node": "6", "graph": 0, "item": 1}}}})
    assert (graph.code, graph.node, graph.graph) == ("patch_invalid", "6", 0)
    value = errors.from_response(400, {}, {"error": {"code": "forbidden_value", "aiodos": {
        "detail": {"node": "6", "input": "text", "reason": "url"}}}})
    assert (value.node, value.input, value.reason) == ("6", "text", "url")
    node = errors.from_response(400, {}, {"error": {"code": "forbidden_node", "aiodos": {
        "detail": {"class": "OpenAIImage"}}}})
    assert node.node_class == "OpenAIImage"
    missing = errors.from_response(400, {}, {"error": {"code": "input_missing", "aiodos": {
        "detail": {"input": "ref0"}}}})
    assert missing.input == "ref0"
    ambiguous = errors.from_response(409, {}, {"error": {"code": "model_ambiguous", "aiodos": {
        "detail": {"candidates": ["checkpoints", "unet"]}}}})
    assert ambiguous.candidates == ["checkpoints", "unet"]
    unavailable = errors.from_response(409, {}, {"error": {"code": "model_unavailable"}})
    assert unavailable.models == []
    quota = errors.from_response(429, {"Retry-After": "30"}, {"error": {
        "code": "quota_exceeded", "aiodos": {"detail": {"quota": "active_jobs"}}}})
    assert quota.quota == "active_jobs" and quota.retryable and quota.retry_after == 30.0
    assert not isinstance(quota, errors.RateLimited)        # attendre ne suffit pas toujours
    assert errors.make("input_too_large", "fr").hint == "Allège le fichier : 100 Mo au plus " \
                                                        "par entrée."


@pytest.mark.parametrize("code", CLIENT_CODES)
def test_client_codes_have_classes_and_texts(code):
    for lang in ("fr", "en"):
        exc = errors.make(code, lang, url="http://127.0.0.1:11435", seconds="240",
                          host="exemple.fr", app="PANDORA")
        assert exc.code == code
        assert isinstance(exc, errors.AiodosError) and type(exc) is not errors.AiodosError
        assert exc.message and exc.message != code
        assert "{" not in exc.message


def test_reference_wordings():
    assert "Connexion refusée" in errors.make("unreachable", "fr", url="x").message
    assert "Connection refused" in errors.make("unreachable", "en", url="x").message
    assert errors.make("cancelled", "fr").message == "Arrêté"
    assert errors.make("cancelled", "en").message == "Stopped"
    assert errors.make("timeout", "fr", seconds="240").retryable
    assert not errors.make("cancelled", "fr").retryable


def test_keys_are_masked_and_errors_pickle():
    key = new_key()
    exc = errors.from_response(400, {}, {"error": {"message": f"Mauvaise clé {key}",
                                                   "code": "invalid_request",
                                                   "aiodos": {"hint": key, "detail": {
                                                       "seen": [key]}}}})
    secret = key[20:]
    assert secret not in exc.message and secret not in (exc.hint or "")
    assert secret not in json.dumps(exc.detail) and secret not in repr(exc)
    assert key[11:19] in exc.message                # l'identifiant public reste lisible
    copy = pickle.loads(pickle.dumps(exc))
    assert type(copy) is errors.InvalidRequest and copy.message == exc.message
    assert copy.code == exc.code and copy.status == 400
