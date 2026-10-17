"""Pont stdio vers le serveur MCP d'AIODOS, pour les assistants qui lancent un programme au
lieu d'appeler une adresse (Claude Desktop…) :

    python -m aiodos.gateway.mcp_stdio

Lit les messages JSON-RPC sur l'entrée standard, un par ligne (transport stdio de MCP), les
envoie au serveur MCP de la passerelle (http://127.0.0.1:<port>/mcp) avec la clé d'accès, et
écrit les réponses sur la sortie standard ; ses propres messages vont sur la sortie d'erreur.
Il ajoute les en-têtes que le transport HTTP exige (MCP-Protocol-Version, Mcp-Method,
Mcp-Name) et retient la version convenue par initialize pour les clients plus anciens.

L'adresse et la clé sont lues dans les réglages d'AIODOS (dossier de données habituel, ou
AIODOS_DATA_DIR) ; AIODOS_MCP_URL et AIODOS_KEY les remplacent. AIODOS doit tourner, avec la
passerelle ouverte (Réglages → Autres outils). Une clé renouvelée est relue d'elle-même.
"""
import base64
import json
import os
import sys

import requests

META_VERSION = "io.modelcontextprotocol/protocolVersion"
TIMEOUT = (5, 900)                   # une réponse de modèle peut prendre plusieurs minutes


def target() -> tuple:
    """(adresse du serveur MCP, clé d'accès) ; RuntimeError si AIODOS n'est pas prêt."""
    url, key = os.environ.get("AIODOS_MCP_URL"), os.environ.get("AIODOS_KEY")
    if url and key:
        return url, key
    from aiodos import paths, settings
    from aiodos.gateway import access
    if not paths.db_path().exists():
        raise RuntimeError("AIODOS n'a pas encore été lancé sur cette machine.")
    key = key or access.existing()
    if not key:
        raise RuntimeError("La passerelle d'AIODOS n'a jamais été ouverte : active-la dans "
                           "Réglages → Autres outils.")
    return url or f"http://127.0.0.1:{int(settings.get('gateway_port'))}/mcp", key


def _header_value(value) -> str:
    text = str(value)
    if text.isascii() and text.isprintable():
        return text
    return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


class Bridge:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False   # 127.0.0.1 : jamais par un mandataire
        self.url = self.key = None
        self.version = None              # version convenue par initialize (anciens clients)

    def headers(self, message) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.key}"}
        if not isinstance(message, dict):
            return headers
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        method = message.get("method")
        if meta.get(META_VERSION):               # époque 2026-07-28
            headers["MCP-Protocol-Version"] = _header_value(meta[META_VERSION])
            if method:
                headers["Mcp-Method"] = _header_value(method)
            name = params.get("uri") if method == "resources/read" else params.get("name")
            if method in ("tools/call", "prompts/get", "resources/read") and name is not None:
                headers["Mcp-Name"] = _header_value(name)
        elif self.version and method != "initialize":
            headers["MCP-Protocol-Version"] = self.version
        return headers

    def post(self, message):
        if self.url is None:
            self.url, self.key = target()
        response = self.session.post(self.url, json=message, headers=self.headers(message),
                                     timeout=TIMEOUT)
        if response.status_code == 401:          # clé renouvelée entre-temps : relue
            self.url, self.key = target()
            response = self.session.post(self.url, json=message,
                                         headers=self.headers(message), timeout=TIMEOUT)
        return response

    def forward(self, message):
        """La réponse à écrire, ou None (notification, réponse du client)."""
        try:
            response = self.post(message)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            reason = (f"AIODOS ne répond pas sur {self.url} : lance AIODOS et ouvre la "
                      "passerelle (Réglages → Autres outils)."
                      if isinstance(exc, requests.RequestException) else str(exc))
            self.url = None                      # adresse relue au prochain message
            return _failure(message, reason)
        if response.status_code == 202 or not response.content:
            return None
        if response.status_code == 401:
            return _failure(message, "Clé d'accès refusée par AIODOS (Réglages → Autres outils).")
        if "text/event-stream" in response.headers.get("content-type", ""):
            replies = [json.loads(line[5:]) for line in response.text.splitlines()
                       if line.startswith("data:") and line[5:].strip()]
            reply = replies[-1] if replies else None
        else:
            try:
                reply = response.json()
            except ValueError:
                return _failure(message, f"Réponse illisible d'AIODOS (HTTP "
                                         f"{response.status_code}).")
        if isinstance(message, dict) and message.get("method") == "initialize":
            self.version = ((reply or {}).get("result") or {}).get("protocolVersion")
        return reply


def _failure(message, reason: str):
    """Erreur JSON-RPC pour une requête (rien pour une notification)."""
    if isinstance(message, list):
        replies = [_failure(m, reason) for m in message]
        return [r for r in replies if r is not None] or None
    if not isinstance(message, dict) or "id" not in message or "method" not in message:
        return None
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": reason}}


def main():
    bridge = Bridge()
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Message JSON illisible."}}
        else:
            reply = bridge.forward(message)
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
