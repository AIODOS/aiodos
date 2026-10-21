"""Aucune page web n'atteint la passerelle (invariant I6 de la conception,
docs/api-applications.md § 2).

Une page ouverte dans un navigateur peut envoyer des requêtes à 127.0.0.1 (CSRF, rebinding
DNS) ; un programme, lui, n'envoie ni Origin ni Fetch Metadata (sauf le fetch de Node, qui
envoie toujours « Sec-Fetch-Mode: cors » seul). D'où, après le contrôle de Host (server.py) :
- sous /aiodos/v1/ et sur les routes sans clé : tout Origin est refusé ;
- ailleurs : un Origin qui n'est pas cette machine est refusé (règle d'avant, pour les outils
  existants) ;
- Sec-Fetch-Site « cross-site » ou « same-site », ou un Sec-Fetch-Dest autre que « empty » :
  refusé partout ;
- une demande d'accès (route sans clé qui écrit) exige du JSON : une page ne peut envoyer un
  POST « simple » qu'en text/plain ou en formulaire, et n'obtient alors que 415 ;
- jamais par host.docker.internal : un conteneur sert les outils, pas les demandes d'accès.
Pas de CORS : rien ne s'ajoute pour un navigateur.
"""
from urllib.parse import urlsplit

from aiodos.gateway.errors import ApiError
from aiodos.i18n import t

LOCAL_ORIGINS = {"127.0.0.1", "localhost", "[::1]", "::1"}
DOCKER_HOST = "host.docker.internal"


def _header(headers: dict, name: bytes) -> str:
    return headers.get(name, b"").decode("latin-1").strip()


def _local_origin(origin: str) -> bool:
    try:
        host = urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return host in LOCAL_ORIGINS


def check(headers: dict, path: str, public: bool, method: str):
    """Lève ApiError(origin_refused | unsupported_media_type) pour ce qui vient d'une page."""
    origin = _header(headers, b"origin")
    strict = public or path.startswith("/aiodos/")
    if origin and (strict or not _local_origin(origin)):
        raise ApiError("origin_refused")
    site = _header(headers, b"sec-fetch-site").lower()
    if site in ("cross-site", "same-site"):
        raise ApiError("origin_refused")
    dest = _header(headers, b"sec-fetch-dest").lower()
    if dest and dest != "empty":
        raise ApiError("origin_refused")
    if public and method == "POST":
        if _header(headers, b"host").lower().startswith(DOCKER_HOST):
            raise ApiError("origin_refused", t("gateway_access.docker_refused"))
        kind = _header(headers, b"content-type").split(";")[0].strip().lower()
        if kind != "application/json":
            raise ApiError("unsupported_media_type")
