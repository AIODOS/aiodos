"""Garde d'accès de l'API locale.

Un serveur sur 127.0.0.1 reste attaquable depuis n'importe quel site ouvert
dans un navigateur (DNS rebinding, requêtes forgées). Deux verrous :
  1. l'en-tête Host doit être exactement 127.0.0.1:port ou localhost:port ;
  2. toute route /api/ exige le jeton tiré au hasard à chaque lancement
     (en-tête X-Aiodos-Token, ou paramètre t= pour le flux d'événements).
Pas d'en-têtes CORS : un autre site ne peut pas lire les réponses.
"""
import secrets
from urllib.parse import parse_qs

from starlette.responses import JSONResponse, PlainTextResponse


class Guard:
    def __init__(self, app, token: str, hosts: set):
        self.app = app
        self.token = token
        self.hosts = hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1")
        if host not in self.hosts:
            return await PlainTextResponse("Hôte refusé.", status_code=400)(scope, receive, send)
        if scope["path"].startswith("/api/"):
            given = headers.get(b"x-aiodos-token", b"").decode("latin-1")
            if not given:
                query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
                given = (query.get("t") or [""])[0]
            if not secrets.compare_digest(given, self.token):
                return await JSONResponse({"detail": "Jeton manquant ou invalide."},
                                          status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)
