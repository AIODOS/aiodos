"""Serveur MCP d'AIODOS : les bibliothèques et les modèles du cercle, pour les assistants.

Transport « Streamable HTTP » (POST /mcp), réponses JSON (ni flux, ni session), derrière la
clé d'accès de la passerelle. Les deux époques du protocole, vérifiées le 17/10/2026 dans la
spécification (modelcontextprotocol.io) :
- 2026-07-28, l'actuelle : sans état ni poignée de main. Chaque requête porte sa version et
  les capacités du client dans params._meta ; le serveur se présente par server/discover ;
  les en-têtes MCP-Protocol-Version, Mcp-Method et Mcp-Name doivent correspondre au message
  (sinon erreur -32020, version inconnue -32022) ; chaque résultat porte resultType, les
  listes ttlMs et cacheScope ;
- 2025-11-25, 2025-06-18 et 2025-03-26 : poignée de main initialize, ping, et (2025-03-26)
  lots de messages ; sans en-tête MCP-Protocol-Version, 2025-03-26 (règle de ces versions).
Les assistants qui lancent un programme (Claude Desktop…) passent par le pont mcp_stdio.py.

Outils : lister_bibliotheques, rechercher_bibliotheques, converser. Les passages trouvés
partent vers l'assistant, et de là vers son modèle : dans le cloud si c'en est un. D'où :
seules les bibliothèques actives qui ne sont pas gardées sur ce PC (le cadenas, portée
« local ») lui sont montrées ; une conversation ne les envoie pas même à mes machines. Et
jamais celles que mes machines et mes amis m'ouvrent (network/remote_search.py) : leurs
passages ne vont ni au cloud, ni chez un ami.
"""
import asyncio
import base64
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from aiodos import __version__, settings
from aiodos.chat import pipeline
from aiodos.gateway import answer, catalog
from aiodos.gateway.answer import GatewayError
from aiodos.knowledge import search, store
from aiodos.network import placement
from aiodos.services import ollama

router = APIRouter()

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN, *LEGACY)
WITHOUT_HEADER = "2025-03-26"
META = "io.modelcontextprotocol/"
TTL_MS = 300_000                    # liste des outils : la même pendant 5 min au moins

_running = {}                       # identifiant JSON-RPC -> réponse de « converser » en cours

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022

INSTRUCTIONS = (
    "AIODOS est l'atelier d'IA local de l'utilisateur. Pour répondre à partir de ses "
    "documents, cherche-les avec rechercher_bibliotheques (lister_bibliotheques donne les "
    "noms des bibliothèques). Pour faire répondre un modèle de sa machine ou de ses machines "
    "appairées, utilise converser.")
CAPABILITIES = {"tools": {"listChanged": False}}
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
             "openWorldHint": False}

TOOLS = [
    {"name": "lister_bibliotheques", "title": "Bibliothèques d'AIODOS",
     "description": "Liste les bibliothèques de connaissance d'AIODOS ouvertes aux assistants "
                    "(documents de l'utilisateur indexés sur sa machine) : nom, nombre "
                    "d'éléments et de passages. Utile pour limiter une recherche à une "
                    "bibliothèque.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": READ_ONLY},
    {"name": "rechercher_bibliotheques", "title": "Rechercher dans les bibliothèques d'AIODOS",
     "description": "Recherche par le sens dans les documents de l'utilisateur indexés par "
                    "AIODOS (PDF, Word, pages web, notes, code, transcriptions…). Renvoie les "
                    "passages les plus proches de la question, avec leur source et un score de "
                    "0 à 1. Pose une question précise, en langage naturel.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "description": "Ce que l'on cherche, en langage naturel."},
         "bibliotheque": {"type": "string",
                          "description": "Nom d'une bibliothèque pour s'y limiter "
                                         "(voir lister_bibliotheques)."},
         "nombre": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                    "description": "Nombre maximal de passages (5 par défaut)."}},
         "required": ["question"], "additionalProperties": False},
     "annotations": READ_ONLY},
    {"name": "converser", "title": "Converser avec un modèle d'AIODOS",
     "description": "Fait répondre un modèle de langage du cercle AIODOS (cette machine, ou une "
                    "machine appairée qui prête ses modèles) et renvoie sa réponse complète. "
                    "Sans historique : chaque appel est indépendant. Peut prendre une minute.",
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "La question ou la consigne."},
         "modele": {"type": "string",
                    "description": "Nom d'un modèle (ex. qwen3.5:9b), la machine étant choisie "
                                   "seule, ou peer:<machine>:<modèle>. Par défaut, celui "
                                   "d'AIODOS."},
         "consignes": {"type": "string", "description": "Consignes système, facultatives."}},
         "required": ["message"], "additionalProperties": False},
     "annotations": {**READ_ONLY, "idempotentHint": False}},
]


class RpcError(Exception):
    """Erreur du protocole (JSON-RPC), avec le statut HTTP qui va avec."""

    def __init__(self, code: int, message: str, data=None, status: int = 200):
        super().__init__(message)
        self.code = code
        self.data = data
        self.status = status


class ToolError(Exception):
    """L'outil n'a pas pu servir la demande : résultat isError, lisible par l'assistant."""


def server_info() -> dict:
    return {"name": "aiodos", "title": "AIODOS", "version": __version__}


# ------------------------------------------------------------------- transport
@router.post("/mcp")
async def mcp(request: Request):
    try:
        message = json.loads(await request.body())
    except ValueError:
        return _reply(_error(None, RpcError(PARSE_ERROR, "Message JSON illisible.")), 400)
    header = request.headers.get("mcp-protocol-version")
    if isinstance(message, list):         # lot de messages : versions 2025-03-26 seulement
        if not message or header not in (None, WITHOUT_HEADER):
            return _reply(_error(None, RpcError(INVALID_REQUEST, "Lot de messages refusé.")),
                          400)
        replies = []
        for item in message:
            reply, _ = await _handle(request, item, header)
            if reply is not None:
                replies.append(reply)
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply, status = await _handle(request, message, header)
    return _reply(reply, status)


@router.api_route("/mcp", methods=["GET", "DELETE"])
def no_stream():
    """Ni flux ouvert par le serveur, ni session à fermer."""
    return Response(status_code=405, headers={"Allow": "POST"})


def _reply(reply, status: int):
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply, status_code=status)


def _error(message_id, exc: RpcError) -> dict:
    error = {"code": exc.code, "message": str(exc)}
    if exc.data is not None:
        error["data"] = exc.data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


async def _handle(request: Request, message, header) -> tuple:
    """(réponse JSON-RPC ou None, statut HTTP)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, RpcError(INVALID_REQUEST, "Message JSON-RPC 2.0 attendu.")), 400
    if "method" not in message:           # réponse du client à une demande : rien à en faire
        return None, 202
    message_id = message.get("id")
    notification = "id" not in message
    method = message.get("method")
    params = message.get("params") if message.get("params") is not None else {}
    try:
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "« params » doit être un objet.")
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        version = meta.get(META + "protocolVersion")
        if method == "notifications/cancelled":     # l'assistant n'attend plus la réponse
            running = _running.get(str(params.get("requestId")))
            if running is not None:
                running.close()
            return None, 202
        if method == "initialize":
            result = _initialize(params)
        elif version is not None or header == MODERN:
            _check_modern(request, method, params, meta, version)
            request.state.rpc_id = message_id
            result = await _modern(request, method, params)
        else:
            if header is not None and header not in LEGACY:
                raise RpcError(UNSUPPORTED_VERSION, f"Version du protocole MCP inconnue : "
                               f"{header}.", {"supported": list(SUPPORTED), "requested": header},
                               status=400)
            request.state.rpc_id = message_id
            result = await _legacy(request, method, params)
    except RpcError as exc:
        return (None, 202) if notification else (_error(message_id, exc), exc.status)
    if notification:
        return None, 202
    return {"jsonrpc": "2.0", "id": message_id, "result": result}, 200


def _header(request: Request, name: str):
    """Valeur d'un en-tête, décodée si elle est en « =?base64?…?= »."""
    value = request.headers.get(name)
    if value and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except ValueError:
            return None
    return value


def _check_modern(request: Request, method, params: dict, meta: dict, version):
    if version is None:
        raise RpcError(INVALID_PARAMS, "params._meta doit porter "
                       "« io.modelcontextprotocol/protocolVersion ».", status=400)
    if version != MODERN:
        raise RpcError(UNSUPPORTED_VERSION, f"Version du protocole MCP non prise en charge : "
                       f"{version}.", {"supported": list(SUPPORTED), "requested": version},
                       status=400)
    if not isinstance(meta.get(META + "clientCapabilities"), dict):
        raise RpcError(INVALID_PARAMS, "params._meta doit porter "
                       "« io.modelcontextprotocol/clientCapabilities ».", status=400)
    expected = [("MCP-Protocol-Version", version), ("Mcp-Method", method)]
    if method in ("tools/call", "prompts/get"):
        expected.append(("Mcp-Name", params.get("name")))
    elif method == "resources/read":
        expected.append(("Mcp-Name", params.get("uri")))
    for name, value in expected:
        given = _header(request, name.lower())
        if given != value:
            raise RpcError(HEADER_MISMATCH, f"Header mismatch: {name} header value "
                           f"'{given}' does not match body value '{value}'", status=400)


def _complete(result: dict, cacheable=False) -> dict:
    """Résultat de l'époque 2026-07-28 : resultType, identité du serveur, et pour une
    liste, la durée pendant laquelle le client peut la garder."""
    result = {"resultType": "complete", **result,
              "_meta": {META + "serverInfo": server_info()}}
    if cacheable:
        result.update(ttlMs=TTL_MS, cacheScope="private")
    return result


async def _modern(request: Request, method, params: dict) -> dict:
    if method == "server/discover":
        return _complete({"supportedVersions": list(SUPPORTED), "capabilities": CAPABILITIES,
                          "instructions": INSTRUCTIONS}, cacheable=True)
    if method == "tools/list":
        return _complete({"tools": TOOLS}, cacheable=True)
    if method == "tools/call":
        return _complete(await _call(request, params))
    raise RpcError(METHOD_NOT_FOUND, f"Méthode inconnue : {method}", status=404)


def _initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    return {"protocolVersion": requested if requested in LEGACY else LEGACY[0],
            "capabilities": CAPABILITIES, "serverInfo": server_info(),
            "instructions": INSTRUCTIONS}


async def _legacy(request: Request, method, params: dict) -> dict:
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return await _call(request, params)
    raise RpcError(METHOD_NOT_FOUND, f"Méthode inconnue : {method}")


# --------------------------------------------------------------------- outils
async def _call(request: Request, params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") if params.get("arguments") is not None else {}
    tool = {"lister_bibliotheques": _list, "rechercher_bibliotheques": _search,
            "converser": _converse}.get(name)
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"Outil inconnu : {name}")
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "« arguments » doit être un objet.")
    try:
        text, structured = await tool(request, arguments)
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured,
            "isError": False}


def _active_libraries() -> list:
    """Bibliothèques montrées aux assistants : actives, et pas gardées sur ce PC."""
    return [c for c in store.list_collections() if c["enabled"] and c["scope"] != "local"]


async def _list(request: Request, arguments: dict):
    libraries = await asyncio.to_thread(_active_libraries)
    structured = {"bibliotheques": [{"nom": c["name"], "elements": c["entries"],
                                     "passages": c["chunks"]} for c in libraries]}
    text = "\n".join(f"- {c['name']} : {c['entries']} éléments, {c['chunks']} passages"
                     for c in libraries) or "Aucune bibliothèque active dans AIODOS."
    return text, structured


def _find(question: str, library, count: int) -> list:
    shown = _active_libraries()
    only = {c["id"] for c in shown}
    if library:
        match = [c for c in shown if c["name"] == library]
        if not match:
            raise ToolError(f"Bibliothèque inconnue, désactivée ou gardée sur ce PC : "
                            f"« {library} » (voir lister_bibliotheques).")
        only = {match[0]["id"]}
    if not only:
        return []
    try:
        vector = ollama.embed([question])[0]
    except Exception as exc:
        raise ToolError(f"Recherche impossible : le modèle d'embedding "
                        f"« {settings.get('embed_model')} » ne répond pas ({exc}).") from exc
    hits = search.search(vector, k=count, libraries=True, conversations=False, only=only)
    return pipeline.select_relevant(hits, float(settings.get("rag_min_score") or 0), count)


async def _search(request: Request, arguments: dict):
    question = arguments.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ToolError("Il manque la question (« question »).")
    try:
        count = max(1, min(20, int(arguments.get("nombre") or 5)))
    except (TypeError, ValueError) as exc:
        raise ToolError("« nombre » doit être un entier de 1 à 20.") from exc
    library = arguments.get("bibliotheque")
    if library is not None and not isinstance(library, str):
        raise ToolError("« bibliotheque » doit être un nom de bibliothèque.")
    hits = await asyncio.to_thread(_find, question.strip(), library, count)
    passages = [{"texte": h["text"], "source": h["source"], "bibliotheque": h["collection"],
                 "score": round(h["score"], 3)} for h in hits]
    if not passages:
        return ("Aucun passage assez proche de la question dans les bibliothèques d'AIODOS.",
                {"passages": []})
    text = "\n\n".join(f"[{i}] {p['source']} ({p['bibliotheque']}, score {p['score']})\n"
                       f"{p['texte']}" for i, p in enumerate(passages, 1))
    return text, {"passages": passages}


def _default_model() -> str:
    chosen = str(settings.get("default_model") or "")
    if chosen and not chosen.startswith("cloud:"):
        return chosen
    entries = catalog.entries()
    if not entries:
        raise ToolError("Aucun modèle de conversation dans le cercle AIODOS.")
    return entries[0]["id"]


async def _converse(request: Request, arguments: dict):
    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ToolError("Il manque le message (« message »).")
    instructions = arguments.get("consignes")
    messages = ([{"role": "system", "content": instructions}]
                if isinstance(instructions, str) and instructions.strip() else [])
    messages.append({"role": "user", "content": message})
    executor = request.app.state.node.executor
    think = bool(settings.get("think"))
    try:
        model_id = arguments.get("modele") or await asyncio.to_thread(_default_model)
        models = await asyncio.to_thread(answer.plan, str(model_id), executor)
    except GatewayError as exc:
        raise ToolError(str(exc)) from exc

    def attempt(model, stop):
        yield from pipeline.generate(model, messages, think, stop)

    stream = answer.Stream(answer.run(models, attempt, executor))
    key = str(getattr(request.state, "rpc_id", None))
    _running[key] = stream            # notifications/cancelled l'arrête
    parts, used = [], models[0]
    try:
        while (item := await stream.next(request)) is not None:
            if item[0] == "route":
                used = item[1]
            elif item[0] == "content":
                parts.append(item[1])
    except Exception as exc:
        raise ToolError(f"Le modèle n'a pas pu répondre : {exc}") from exc
    finally:
        stream.close()
        if _running.get(key) is stream:
            del _running[key]
    if stream.stop.is_set() and not parts:
        raise ToolError("Réponse annulée.")
    machine = await asyncio.to_thread(placement.machine_of, used)
    reply = "".join(parts).strip()
    return reply or "(réponse vide)", {"reponse": reply, "modele": used,
                                       "machine": machine or "cette machine"}
