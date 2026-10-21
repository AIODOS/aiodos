"""GET /aiodos/v1/info et la preuve d'identité d'AIODOS (docs/api-applications.md § 5.1).

Sans clé, la route répond toujours 200 : ce que cette version sert (features : un client ne
compare jamais de numéros de version), et si AIODOS accepte les applis et les demandes.
Avec une clé, l'état de cette clé (valid, revoked, expired, invalid) et, valide, les droits
de l'appli.

La preuve : AIODOS arrêté, n'importe quel programme pourrait écouter sur le port et recueillir
les clés. Le client tire un défi et demande, sans sa clé, ?key_id=<id>&challenge=<défi> ;
AIODOS, qui garde sha256(clé), répond HMAC-SHA256(sha256(clé), "aiodos-proof/1\n" + défi).
Un imposteur ne sait pas la calculer. Pour un id inconnu, une preuve tirée au hasard : elle
ne dit pas si l'id existe ; pour une clé retirée, la vraie (l'appel suivant dira revoked).
"""
import base64
import hashlib
import hmac
import re
import secrets

from fastapi import APIRouter, Request

from aiodos import __version__, settings
from aiodos.gateway import access, access_flow, apps, limits

router = APIRouter(prefix="/aiodos/v1")

API = 1
FEATURES = ["access", "models", "chat", "embed", "open", "engines", "engines.comfyui", "jobs",
            "jobs.pause", "inputs", "files", "files.link"]
INSTANCE = secrets.token_urlsafe(6)          # tiré à chaque lancement
PROOF_CONTEXT = b"aiodos-proof/1\n"
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{16,128}")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def proof(key_hash: bytes, challenge: str) -> str:
    return b64url(hmac.new(key_hash, PROOF_CONTEXT + challenge.encode("ascii"),
                           hashlib.sha256).digest())


def access_flags() -> dict:
    return {"apps": bool(settings.get("gateway_apps")),
            "requests": bool(settings.get("gateway_requests")),
            "approval_ui": access_flow.approval_ui()}


def base() -> dict:
    return {"object": "aiodos.info", "product": "aiodos", "version": __version__, "api": API,
            "instance": INSTANCE, "features": list(FEATURES), "access": access_flags(),
            "auth": "none"}


def _given_key(request: Request) -> str:
    auth = request.headers.get("authorization", "").strip()
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def auth_of(key: str) -> tuple:
    """(état, AppContext | None) d'une clé présentée à info."""
    if apps.is_app_key(key):
        return apps.lookup(key)
    if key and settings.get("gateway_enabled") and access.check(key):
        return "valid", apps.GENERAL
    return "invalid", None


@router.api_route("/info", methods=["GET", "HEAD"])
def info(request: Request):
    out = base()
    key_id, challenge = request.query_params.get("key_id"), request.query_params.get("challenge")
    if key_id is not None and challenge is not None:
        if _CHALLENGE.fullmatch(challenge or ""):
            known = apps.key_hash_bytes(key_id) if apps.valid_id(key_id) else None
            out["proof"] = proof(known, challenge) if known else b64url(secrets.token_bytes(32))
    key = _given_key(request)
    if key:
        status, context = auth_of(key)
        out["auth"] = status
        if status == "valid":
            out["app"] = context.public()
            general = context.kind == "general"
            out["limits"] = {
                "requests_per_minute": limits.GENERAL_PER_MINUTE if general else limits.PER_MINUTE,
                "streams": None if general else limits.STREAMS,
                "max_body_bytes": _max_body()}
    return out


def _max_body() -> int:
    from aiodos.gateway import server
    return server.MAX_BODY
