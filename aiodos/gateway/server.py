"""Serveur de la passerelle : 127.0.0.1 seulement, port fixe réglable, clé d'accès.

Il tourne dans la boucle asyncio du nœud, comme le serveur entre machines, et démarre ou
s'arrête quand l'utilisateur ouvre ou ferme l'accès dans les Réglages (apply), sans
relancer AIODOS. Trois verrous devant chaque requête (Guard) :
  1. l'en-tête Host doit être 127.0.0.1:port ou localhost:port (DNS rebinding) ;
  2. un en-tête Origin, s'il est là, doit venir de la machine elle-même : une page web
     ouverte dans un navigateur ne s'en sert pas (et sans CORS, elle ne lirait rien) ;
  3. la clé d'accès (access.py) : « Authorization: Bearer <clé> », comme pour l'API
     d'OpenAI, ou « X-Api-Key: <clé> ».
Seule la racine (« / », réponse d'Ollama « Ollama is running ») se passe de clé : des
outils s'en servent pour vérifier qu'un serveur répond.
"""
import asyncio
import logging
import sys
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.responses import JSONResponse

from aiodos import paths, settings
from aiodos.gateway import access

log = logging.getLogger("aiodos.gateway")

HOST = "127.0.0.1"
PORT_ATTEMPTS = 3           # port encore tenu par l'instance qui vient de s'arrêter
PORT_RETRY_DELAY = 1
LOCAL_ORIGINS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _origin_ok(origin: str) -> bool:
    """Pas d'Origin (un programme, pas une page web) ou une page de cette machine."""
    if not origin:
        return True
    try:
        host = urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return host in LOCAL_ORIGINS


def _given_key(headers: dict) -> str:
    auth = headers.get(b"authorization", b"").decode("latin-1").strip()
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return headers.get(b"x-api-key", b"").decode("latin-1").strip()


def refusal(path: str, status: int, message: str):
    """Réponse d'erreur au format qu'attend chaque famille d'outils."""
    if path.startswith("/v1/"):           # API d'OpenAI
        code = "invalid_api_key" if status == 401 else None
        return JSONResponse({"error": {"message": message, "type": "invalid_request_error",
                                       "param": None, "code": code}}, status_code=status)
    if path.startswith("/mcp"):           # JSON-RPC sans identifiant de requête
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": message}},
                            status_code=status)
    return JSONResponse({"error": message}, status_code=status)    # API d'Ollama


class Guard:
    def __init__(self, app, hosts: set):
        self.app = app
        self.hosts = hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return                        # pas de WebSocket ici
        headers = dict(scope["headers"])
        path = scope.get("path") or "/"
        host = headers.get(b"host", b"").decode("latin-1")
        if host not in self.hosts:
            return await PlainTextResponse("Hôte refusé.", status_code=400)(scope, receive, send)
        if not _origin_ok(headers.get(b"origin", b"").decode("latin-1")):
            return await refusal(path, 403, "Origine refusée : la passerelle d'AIODOS ne "
                                 "répond pas aux pages web.")(scope, receive, send)
        if path != "/" and not access.check(_given_key(headers)):
            return await refusal(path, 401, "Clé d'accès d'AIODOS manquante ou invalide "
                                 "(Réglages → Autres outils).")(scope, receive, send)
        return await self.app(scope, receive, send)


def create_gateway_app(node, port: int):
    """node : l'état de l'application du nœud (exécuteur, réseau…)."""
    from aiodos.gateway import mcp_server, ollama_api, openai_api
    app = FastAPI(title="AIODOS — passerelle", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.node = node
    for module in (openai_api, ollama_api, mcp_server):
        app.include_router(module.router)
    return Guard(app, {f"{HOST}:{port}", f"localhost:{port}"})


async def _serve(server):
    """uvicorn « quitte » (SystemExit) quand le port est déjà pris : on le garde ici."""
    try:
        await server.serve()
    except SystemExit:
        pass


class Gateway:
    def __init__(self, node):
        self.node = node                  # app.state du nœud
        self.error = None
        self.port = None
        self._server = None
        self._task = None
        self._loop = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def apply(self):
        """Ouvre, ferme ou déplace la passerelle selon les réglages."""
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            enabled = bool(settings.get("gateway_enabled"))
            port = _port()
            if self.running and (not enabled or port != self.port):
                await self._stop()
            if enabled and not self.running:
                await self._start(port)
            if not enabled:
                self.error = None
        self.node.bus.publish({"type": "gateway"})

    def apply_from_thread(self):
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.apply()))

    async def _start(self, port: int):
        self.error = None
        access.key()                      # créée dès la première ouverture
        app = create_gateway_app(self.node, port)
        for attempt in range(PORT_ATTEMPTS):
            config = uvicorn.Config(app, host=HOST, port=port, log_config=None,
                                    log_level="warning", access_log=False)
            self._server = uvicorn.Server(config)
            self._task = asyncio.create_task(_serve(self._server), name="aiodos-passerelle")
            for _ in range(100):
                if self._server.started or self._task.done():
                    break
                await asyncio.sleep(0.05)
            if self._server.started:
                self.port = port
                log.info("Passerelle ouverte aux autres outils sur http://%s:%s", HOST, port)
                return
            if attempt + 1 < PORT_ATTEMPTS:
                await asyncio.sleep(PORT_RETRY_DELAY)
        self._task = self._server = None
        self.error = (f"Le port {port} est déjà pris sur cette machine : choisis-en un autre "
                      "dans les Réglages.")
        log.warning(self.error)

    async def _stop(self):
        if self._server:
            self._server.should_exit = True
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._server = self._task = None
        log.info("Passerelle fermée")

    async def shutdown(self):
        async with self._lock:
            if self.running:
                await self._stop()

    def snapshot(self) -> dict:
        port = self.port if self.running else _port()
        base = f"http://{HOST}:{port}"
        return {
            "enabled": bool(settings.get("gateway_enabled")), "running": self.running,
            "port": port, "error": self.error,
            "key": access.key(),
            "urls": {"openai": f"{base}/v1", "ollama": base, "mcp": f"{base}/mcp"},
            "mcp_stdio": stdio_config(),
        }


def _port() -> int:
    try:
        port = int(settings.get("gateway_port"))
    except (TypeError, ValueError):
        port = settings.DEFAULTS["gateway_port"]
    return port if 1 <= port <= 65535 else settings.DEFAULTS["gateway_port"]


def stdio_config() -> dict:
    """Configuration d'un assistant qui lance un programme (Claude Desktop…), prête à
    copier : le pont mcp_stdio.py relaie vers cette passerelle."""
    env = {"PYTHONPATH": str(paths.APP_ROOT), "PYTHONIOENCODING": "utf-8"}
    data_dir = paths.data_dir()
    if data_dir != paths.APP_ROOT / "data":
        env["AIODOS_DATA_DIR"] = str(data_dir)
    # Lancé par pythonw (double-clic) : le pont a besoin de l'interpréteur avec console
    command = sys.executable
    if command.lower().endswith("pythonw.exe"):
        command = command[:-len("pythonw.exe")] + "python.exe"
    return {"mcpServers": {"aiodos": {
        "command": command, "args": ["-m", "aiodos.gateway.mcp_stdio"], "env": env}}}
