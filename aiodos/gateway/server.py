"""Serveur de la passerelle : 127.0.0.1 seulement, port fixe réglable, une clé par appli.

Il tourne dans la boucle asyncio du nœud, comme le serveur entre machines, et démarre ou
s'arrête sans relancer AIODOS (apply) : il écoute dès qu'AIODOS est ouvert aux applications
(gateway_apps, ouvert par le premier accord donné à une appli) ou aux autres outils par la
clé générale (gateway_enabled). Devant chaque requête, dans l'ordre (Guard) :
  1. l'en-tête Host : 127.0.0.1:port ou localhost:port (rebinding DNS), ou
     host.docker.internal:port (un conteneur Docker Desktop de cette machine joint ainsi
     les services de 127.0.0.1 ; aucune page web ne porte ce nom) ;
  2. Origin et Fetch Metadata (web_guard.py) : aucune page web ;
  3. une route publique (scopes.PUBLIC_ROUTES), ou bien la clé : d'appli (apps.py,
     « aiodos_app_… ») ou générale (access.py) ; une clé d'appli quand AIODOS n'est pas
     ouvert aux applis : 403 apps_closed ;
  4. pour une clé d'appli et une demande d'accès, le programme et le compte Windows de
     l'appelant (caller.py) : autre compte, 403 ; autre programme, noté, ou refusé si la clé
     lui est liée ;
  5. les limites (limits.py) et la taille du corps (MAX_BODY) ;
  6. le droit qu'exige la route (scopes.required) : 403 scope_denied ;
  7. le journal, sans contenu (usage.py).
Toutes les réponses portent X-Aiodos-Api: 1. « / » répond toujours « Ollama is running ».
"""
import asyncio
import json
import logging
import sys
import time

import uvicorn
from fastapi import FastAPI

from aiodos import paths, settings
from aiodos.gateway import (access, access_flow, apps, caller, discovery, errors, limits,
                            scopes, usage, web_guard)
from aiodos.gateway.errors import ApiError

log = logging.getLogger("aiodos.gateway")

HOST = "127.0.0.1"
PORT_ATTEMPTS = 3           # port encore tenu par l'instance qui vient de s'arrêter
PORT_RETRY_DELAY = 1
STDIO_ARGS = ("-P", "-m", "aiodos.gateway.mcp_stdio")     # pont stdio (stdio_config)
# Corps d'une requête : de quoi porter des images en base64 ; au-delà, 413 sans tout lire
MAX_BODY = 64 * 1024 ** 2
INPUT_BODY = 100 * 1024 ** 2         # une entrée de travail (input_store.MAX_INPUT)
API_HEADER = (b"x-aiodos-api", b"1")
_ROUTED = {b"x-aiodos-model": "model", b"x-aiodos-machine": "machine",
           b"x-aiodos-place": "place"}


def _given_key(headers: dict) -> str:
    auth = headers.get(b"authorization", b"").decode("latin-1").strip()
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return headers.get(b"x-api-key", b"").decode("latin-1").strip()


async def _answer(scope, receive, send, path: str, exc: ApiError, lang: str):
    response = errors.response(path, exc, lang)
    response.raw_headers.append(API_HEADER)
    await response(scope, receive, send)


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
        method = scope.get("method", "GET")
        lang = errors.lang_of(headers)
        started = time.monotonic()
        host = headers.get(b"host", b"").decode("latin-1")
        if host not in self.hosts:
            return await _answer(scope, receive, send, path, ApiError("host_refused"), lang)
        needed = scopes.required(method, path)
        public = needed == scopes.PUBLIC
        try:
            web_guard.check(headers, path, public, method)
            context, who = await self._identify(scope, headers, path, method, public)
            if context is not None:
                limits.take(context.id, general=context.kind == "general")
                if needed in scopes.SCOPES and needed not in context.scopes:
                    raise ApiError("scope_denied", f"Cette application n'a pas le droit "
                                   f"« {needed} » : demande-le dans AIODOS.",
                                   detail={"scope": needed})
        except ApiError as exc:
            if exc.status == 401:
                delay = limits.invalid_delay()
                if delay:
                    await asyncio.sleep(delay)
            return await _answer(scope, receive, send, path, exc, lang)
        state = scope.setdefault("state", {})
        state["aiodos_app"] = context
        state["aiodos_caller"] = who
        state["aiodos_lang"] = lang
        declared = headers.get(b"content-length")
        limit = body_limit(method, path)
        if declared is not None:          # h11 ne laisse pas passer plus que la taille dite
            if not declared.isdigit() or int(declared) > limit:
                return await _answer(scope, receive, send, path, self._too_big(limit), lang)
            body_receive, size_in = receive, int(declared)
        else:
            body = await _read(receive, limit)   # envoi en morceaux : lu ici, jusqu'à la limite
            if body is None:
                return await _answer(scope, receive, send, path, self._too_big(limit), lang)
            body_receive, size_in = _replay(body, receive), len(body)
        seen = {"status": 500, "bytes": 0, "code": None, "first": b"", "started": False}

        async def sending(message):
            if message["type"] == "http.response.start":
                seen["started"] = True
                message = {**message,
                           "headers": [*message.get("headers", []), API_HEADER]}
                seen["status"] = message["status"]
                for name, value in message["headers"]:
                    if name.lower() in _ROUTED:
                        seen[_ROUTED[name.lower()]] = value.decode("latin-1")
            elif message["type"] == "http.response.body":
                chunk = message.get("body") or b""
                seen["bytes"] += len(chunk)
                if seen["status"] >= 400 and len(seen["first"]) < 4096:
                    seen["first"] += chunk[:4096]
            await send(message)
        try:
            # La clé générale garde ce qu'elle avait : pas de plafond de simultanéité (les
            # outils existants envoient parfois des rafales)
            if (context is not None and context.kind != "general"
                    and usage.family(path) in ("chat", "embed")):
                with limits.computing(context.id):
                    await self.app(scope, body_receive, sending)
            else:
                await self.app(scope, body_receive, sending)
        except ApiError as exc:           # limits.computing : trop de calculs en cours
            if seen["started"]:
                raise
            seen["status"] = exc.status
            return await _answer(scope, receive, send, path, exc, lang)
        finally:
            if context is not None or path.startswith("/aiodos/v1/access"):
                self._log(context, who, path, seen, started, size_in)

    @staticmethod
    def _too_big(limit: int = MAX_BODY) -> ApiError:
        if limit > MAX_BODY:
            return ApiError("input_too_large", f"Entrée trop lourde : {INPUT_BODY // 1024 ** 2} "
                            "Mio au plus.")
        return ApiError("file_too_large", f"Requête trop grosse : {MAX_BODY // 1024 ** 2} Mio "
                        "au plus.")

    async def _identify(self, scope, headers, path, method, public):
        """(AppContext | None, Caller) : l'appli qui appelle et son programme, ou ApiError."""
        key = _given_key(headers)
        context = None
        if key and apps.is_app_key(key):
            status, found = await asyncio.to_thread(apps.lookup, key)
            if status == "valid":
                context = found
            elif not public:
                raise ApiError({"revoked": "key_revoked", "expired": "key_expired"}.get(
                    status, "invalid_api_key"))
        elif key:
            general = await asyncio.to_thread(
                lambda: bool(settings.get("gateway_enabled")) and access.check(key))
            if general:
                context = apps.GENERAL
            elif not public:
                raise ApiError("invalid_api_key", "Clé d'accès d'AIODOS manquante ou invalide "
                               "(Réglages → Applications et autres outils).")
        elif not public:
            raise ApiError("invalid_api_key", "Clé d'accès d'AIODOS manquante ou invalide "
                           "(Réglages → Applications et autres outils).")
        who = caller.UNKNOWN
        asking = method == "POST" and path == "/aiodos/v1/access/requests"
        app_key = context is not None and context.kind != "general"
        if app_key or asking:
            # info, elle, dit l'état de la clé même quand AIODOS est fermé aux applis
            if app_key and (asking or not public) and not await asyncio.to_thread(
                    settings.get, "gateway_apps"):
                raise ApiError("apps_closed", "AIODOS n'est ouvert qu'aux autres outils : "
                               "ouvre-le aussi aux applications.")
            who = await asyncio.to_thread(caller.identify, scope)
        if context is not None and context.kind != "general":
            same = await asyncio.to_thread(settings.get, "gateway_same_account")
            if context.account_check and same and who.same_account is False:
                raise ApiError("other_account")
            if (context.exe_pinned and context.exe_path and who.program
                    and who.program.lower() != context.exe_path.lower()):
                raise ApiError("program_mismatch")
        if asking and context is None and not await asyncio.to_thread(settings.get,
                                                                       "gateway_apps"):
            raise ApiError("apps_closed", "AIODOS n'est pas encore ouvert aux applications : "
                           "connecte-toi par le programme d'aide d'AIODOS (--app-request).")
        return context, who

    @staticmethod
    def _log(context, who, path, seen, started, size_in):
        code = None
        if seen["status"] >= 400 and seen["first"]:
            try:
                reply = json.loads(seen["first"])
                err = reply.get("error") if isinstance(reply, dict) else None
                code = (err.get("code") if isinstance(err, dict) else
                        reply.get("code") or (err if isinstance(err, str) and " " not in err
                                              else None))
            except (ValueError, AttributeError):
                code = None
        program = None
        if context is not None and context.kind != "general" and who.program and \
                context.exe_path and who.program.lower() != context.exe_path.lower():
            program = who.program           # programme inattendu : noté
        app_id = context.id if context is not None else None
        usage.record(app_id=app_id, route=path, status=seen["status"],
                     ms=int((time.monotonic() - started) * 1000), model=seen.get("model"),
                     machine=seen.get("machine"), place=seen.get("place"),
                     code=code if isinstance(code, str) else None, bytes_in=size_in,
                     bytes_out=seen["bytes"], program=program)
        if context is not None and context.kind != "general":
            try:
                apps.touch(context.id, program)
            except Exception:
                log.debug("Dernière utilisation de %s non notée", context.id, exc_info=True)


def body_limit(method: str, path: str) -> int:
    """Le corps le plus gros permis : MAX_BODY, sauf l'envoi d'une entrée de travail
    (PUT /aiodos/v1/inputs/…, 100 Mo, gateway/input_store.py, lue au fil de l'eau)."""
    if method == "PUT" and path.startswith("/aiodos/v1/inputs/"):
        return INPUT_BODY
    return MAX_BODY


async def _read(receive, limit: int = MAX_BODY) -> bytes | None:
    """Le corps envoyé en morceaux, ou None au-delà de la limite (ou client parti)."""
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        body += message.get("body") or b""
        if len(body) > limit:
            return None
        if not message.get("more_body"):
            return bytes(body)


def _replay(body: bytes, receive):
    """receive qui rend d'abord le corps déjà lu, puis la suite (déconnexion du client)."""
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return replay


def create_gateway_app(node, port: int):
    """node : l'état de l'application du nœud (exécuteur, réseau…)."""
    from aiodos.gateway import (access_api, engines_api, files_api, info, inputs_api, jobs_api,
                                links_api, mcp_server, models_api, ollama_api, open_page,
                                openai_api)
    app = FastAPI(title="AIODOS — passerelle", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.node = node
    for module in (info, access_api, models_api, open_page, openai_api, ollama_api, mcp_server,
                   engines_api, inputs_api, jobs_api, files_api, links_api):
        app.include_router(module.router)
    return Guard(app, hosts(port))


def hosts(port: int) -> set:
    return {f"{HOST}:{port}", f"localhost:{port}", f"host.docker.internal:{port}"}


async def _serve(server):
    """uvicorn « quitte » (SystemExit) quand le port est déjà pris : on le garde ici."""
    try:
        await server.serve()
    except SystemExit:
        pass


def wanted() -> bool:
    """La passerelle doit-elle écouter ? Ouverte aux applis ou à la clé générale."""
    return bool(settings.get("gateway_enabled")) or bool(settings.get("gateway_apps"))


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
            enabled = await asyncio.to_thread(wanted)
            port = _port()
            if self.running and (not enabled or port != self.port):
                await self._stop()
            if enabled and not self.running:
                await self._start(port)
            if not enabled:
                self.error = None
            await asyncio.to_thread(self._discovery)
        self.node.bus.publish({"type": "gateway"})

    def apply_from_thread(self):
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.apply()))

    async def _start(self, port: int):
        self.error = None
        if await asyncio.to_thread(settings.get, "gateway_enabled"):
            await asyncio.to_thread(access.key)          # créée à la première ouverture
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
                log.info("Passerelle ouverte sur http://%s:%s", HOST, port)
                return
            if attempt + 1 < PORT_ATTEMPTS:
                await asyncio.sleep(PORT_RETRY_DELAY)
        self._task = self._server = None
        holder = await asyncio.to_thread(caller.holder_of, port)
        held = f" ({holder})" if holder else ""
        self.error = (f"Le port {port} est déjà pris sur cette machine{held} : choisis-en un "
                      "autre dans les Réglages.")
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
        await asyncio.to_thread(discovery.mark_stopped)

    def _discovery(self):
        state = "error" if self.error else ("open" if self.running else "closed")
        discovery.write(discovery.content(
            running=True, state=state, url=f"http://{HOST}:{self.port or _port()}",
            apps=bool(settings.get("gateway_apps")),
            requests=bool(settings.get("gateway_requests")),
            approval_ui=access_flow.approval_ui(), error=self.error))

    def snapshot(self) -> dict:
        port = self.port if self.running else _port()
        base = f"http://{HOST}:{port}"
        general = bool(settings.get("gateway_enabled"))
        return {
            "enabled": general, "running": self.running, "port": port, "error": self.error,
            "apps": bool(settings.get("gateway_apps")),
            "requests": bool(settings.get("gateway_requests")),
            "same_account": bool(settings.get("gateway_same_account")),
            "key": access.key() if general else access.existing(),
            "urls": {"openai": f"{base}/v1", "ollama": base, "mcp": f"{base}/mcp"},
            "mcp_stdio": stdio_config(),
        }


def _port() -> int:
    try:
        port = int(settings.get("gateway_port"))
    except (TypeError, ValueError):
        port = settings.DEFAULTS["gateway_port"]
    return port if 1024 <= port <= 65535 else settings.DEFAULTS["gateway_port"]


def stdio_config() -> dict:
    """Configuration d'un assistant qui lance un programme (Claude Desktop…), prête à
    copier : le pont mcp_stdio.py relaie vers cette passerelle. -P (et PYTHONSAFEPATH, pour
    un assistant qui réécrirait les arguments) : sans lui, « python -m » met en tête de
    sys.path le dossier où l'assistant le lance, souvent le projet ouvert, et un requests.py
    ou un dossier aiodos/ de ce projet s'exécuterait à la place du pont (faille 3 de l'audit
    du 20/10/2026)."""
    env = {"PYTHONPATH": str(paths.APP_ROOT), "PYTHONIOENCODING": "utf-8",
           "PYTHONSAFEPATH": "1"}
    data_dir = paths.data_dir()
    if data_dir != paths.APP_ROOT / "data":
        env["AIODOS_DATA_DIR"] = str(data_dir)
    # Lancé par pythonw (double-clic) : le pont a besoin de l'interpréteur avec console
    command = sys.executable
    if command.lower().endswith("pythonw.exe"):
        command = command[:-len("pythonw.exe")] + "python.exe"
    return {"mcpServers": {"aiodos": {
        "command": command, "args": list(STDIO_ARGS), "env": env}}}
