"""Garde d'accès de l'API locale.

Un serveur sur 127.0.0.1 reste attaquable depuis n'importe quel site ouvert
dans un navigateur (DNS rebinding, requêtes forgées). Deux verrous :
  1. l'en-tête Host doit être exactement 127.0.0.1:port ou localhost:port ;
  2. toute route /api/ exige le jeton tiré au hasard à chaque lancement
     (en-tête X-Aiodos-Token, ou paramètre t= pour le flux d'événements).
Pas d'en-têtes CORS : un autre site ne peut pas lire les réponses.

Chaque réponse porte en plus des en-têtes de protection : si un texte venu d'ailleurs
(profil d'une machine, réponse d'un modèle) glissait du HTML dans l'interface malgré
l'échappement, la politique de contenu (CSP) l'empêche d'y lancer un script ou de faire
sortir quoi que ce soit vers un autre hôte.
"""
import secrets
from urllib.parse import parse_qs

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse, PlainTextResponse

# L'interface : ses propres scripts et rien d'autre (ni script en ligne, ni gestionnaire
# onerror= glissé dans un profil), ses images et vidéos, ses requêtes vers ce nœud seul.
# Les styles en ligne restent permis : les vues en posent beaucoup (style="…"), un style
# ne lance rien, et images, polices et requêtes restant sur le nœud, rien ne sort par là.
# (Le pont de la fenêtre native, injecté par WebView2, n'est pas soumis à cette politique.)
INTERFACE_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
))
# L'API (JSON, fichiers) : jamais exécutée comme une page, même ouverte seule dans un
# onglet. Images et vidéos créées en sont dispensées : elles ne lancent rien, et s'ouvrent
# ainsi telles quelles dans un onglet du téléphone.
API_POLICY = "sandbox; frame-ancestors 'none'"
INERT_MEDIA = ("image/png", "image/jpeg", "image/webp", "image/gif", "video/", "audio/")


def protect(headers: MutableHeaders, api: bool):
    """En-têtes de protection d'une réponse, sans écraser ceux qu'une route aurait posés."""
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    if not api:
        headers.setdefault("Content-Security-Policy", INTERFACE_POLICY)
    elif not headers.get("content-type", "").lower().startswith(INERT_MEDIA):
        headers.setdefault("Content-Security-Policy", API_POLICY)


class Guard:
    def __init__(self, app, token: str, hosts: set):
        self.app = app
        self.token = token
        self.hosts = hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        api = scope["path"].startswith("/api/")

        async def send_protected(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                protect(MutableHeaders(scope=message), api)
            await send(message)

        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1")
        if host not in self.hosts:
            return await PlainTextResponse("Hôte refusé.", status_code=400)(
                scope, receive, send_protected)
        if api:
            given = headers.get(b"x-aiodos-token", b"").decode("latin-1")
            if not given:
                query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
                given = (query.get("t") or [""])[0]
            # En octets : un jeton tapé avec des accents est refusé, il ne fait pas tomber
            # la requête (compare_digest refuse les chaînes non ASCII)
            if not secrets.compare_digest(given.encode("utf-8"), self.token.encode("utf-8")):
                return await JSONResponse({"detail": "Jeton manquant ou invalide."},
                                          status_code=401)(scope, receive, send_protected)
        return await self.app(scope, receive, send_protected)
