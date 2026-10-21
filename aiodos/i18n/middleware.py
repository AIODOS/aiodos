"""Middleware ASGI : les textes qu'une requête produit (erreurs, réponses) sont dans sa
langue. L'interface envoie la sienne dans Accept-Language ; le flux d'événements, qu'un
EventSource ne peut pas munir d'en-têtes, la passe en paramètre (lang=)."""
from urllib.parse import parse_qs

from aiodos.i18n import language


def requested(scope) -> str | None:
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    asked = language.supported((query.get("lang") or [""])[0])
    if asked:
        return asked
    for name, value in scope.get("headers") or []:
        if name == b"accept-language":
            return language.from_header(value.decode("latin-1"))
    return None


class Language:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        lang = requested(scope) if scope["type"] in ("http", "websocket") else None
        if lang is None:
            return await self.app(scope, receive, send)
        token = language.set_request(lang)
        try:
            await self.app(scope, receive, send)
        finally:
            language.reset_request(token)
