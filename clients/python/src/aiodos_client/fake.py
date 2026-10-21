# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""FakeAiodos : un faux AIODOS pour les essais d'une appli (et de ce client).

Un http.server sur 127.0.0.1, sur un port libre, dans un fil, qui sert le contrat partagé
(clients/contract/) :
- `GET /aiodos/v1/info`, avec une preuve juste pour les clés qu'il a remises (aléatoire pour
  un identifiant inconnu, comme AIODOS ; `impostor=True` ne connaît aucune clé) ;
- les demandes d'accès, pilotées par l'essai : wait_request(), approve(), deny(), expire(),
  slow_down() (ou `auto_approve=True`) ; la relève, le retrait ;
- le catalogue, la conversation en flux (les flux de chat.json, « : aiodos loading »
  compris ; chat_script() en donne un autre), les embeddings ;
- les travaux ComfyUI (moteurs, entrées, travaux, événements, fichiers par plages), en
  mémoire, avec le droit « engines:comfyui » : voir _fake_jobs.py (advance(), fail_unit(),
  cut_events(), cut_content(), `job_delay`, `comfyui`) ; `features=L1_FEATURES` les retire,
  comme une version d'AIODOS qui ne les sert pas ;
- les erreurs à la demande (fail_next()), ou n'importe quelle réponse (override()).

Il écrit un faux gateway.json dans un dossier temporaire (`discovery_file`, à désigner par
AIODOS_DISCOVERY_FILE : environ()), sait fermer et rouvrir sa passerelle sur le même port
(close_gateway(), open_gateway()), et fabrique un faux dossier d'AIODOS dont le programme
d'aide (`python -m aiodos --app-request`) répond comme le vrai (helper_home()). Il ne touche
jamais au vrai fichier de découverte, ni au réseau. Il garde ce qu'il reçoit : `log`
(requêtes), `seen_keys` (clés reçues, même refusées), `token_polls`, `stream_closed`.

    with FakeAiodos(auto_approve=True) as fake:
        os.environ.update(fake.environ())
        ai = Aiodos(app="PANDORA", app_id="arkane.pandora", version="3.2.0")
        ai.connect(["chat", "engines:comfyui"])
        print(ai.chat([{"role": "user", "content": "Bonjour"}]).text)      # « Bonjour ! »
        job = ai.comfy.submit(graph=api)                # fini en 0,05 s par unité
"""

import base64
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import tempfile
import threading
import time
import http.server
from urllib.parse import parse_qs, quote, urlsplit

from . import _proof, texts
from ._fake_jobs import JOB_FEATURES, MAX_INPUT, PREFIXES, FakeJobs, png  # noqa: F401

log = logging.getLogger("aiodos_client.fake")

BASE32 = "abcdefghijklmnopqrstuvwxyz234567"
L1_FEATURES = ("access", "models", "chat", "embed", "open")
FEATURES = L1_FEATURES + JOB_FEATURES
SCOPES = ("chat", "embed", "libraries", "engines:comfyui")
CIRCLES = ("here", "mine", "friends", "public")
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
MAX_BODY = 64 * 1024 * 1024
CREATED = 1760950000
CLIENT_ID = re.compile(r"^[a-z0-9.-]{2,40}$")
OLD_401 = "Clé d'accès d'AIODOS manquante ou invalide (Réglages → Autres outils)."

# Le catalogue du contrat (clients/contract/models.json).
CHAT_MODELS = (
    {"id": "qwen3.5:9b", "kind": "chat", "label": "qwen3.5:9b", "installed": True,
     "context": 16384, "vision": True, "tools": True, "thinking": True,
     "size_bytes": 6600000000, "loaded": False, "here": True, "default": True,
     "where": [{"id": "local:qwen3.5:9b", "machine": "PC-BUREAU", "place": "here",
                "loaded": False, "context": 16384, "tools": True, "vision": True},
               {"id": "peer:9c2e41d0:qwen3.5:9b", "machine": "Portable", "place": "mine",
                "loaded": True, "context": None, "tools": False, "vision": False}],
     "license": None},
    {"id": "mistral:7b", "kind": "chat", "label": "mistral:7b", "installed": True,
     "context": 16384, "vision": False, "tools": True, "thinking": False,
     "size_bytes": 4400000000, "loaded": True, "here": True, "default": False,
     "where": [{"id": "local:mistral:7b", "machine": "PC-BUREAU", "place": "here",
                "loaded": True, "context": 16384, "tools": True, "vision": False}],
     "license": None},
)
EMBED_MODELS = (
    {"id": "embeddinggemma", "kind": "embed", "label": "embeddinggemma", "installed": True,
     "context": 2048, "dimensions": 768, "size_bytes": 334000000, "loaded": False,
     "here": True, "default": True,
     "where": [{"id": "local:embeddinggemma", "machine": "PC-BUREAU", "place": "here",
                "loaded": False, "context": 2048}],
     "license": None},
)
SUPPORTED = ["tools", "reasoning", "response_format", "max_tokens", "temperature", "top_p",
             "stop", "seed"]

# Conseils des erreurs (ceux du contrat quand il en donne un).
HINTS = {
    "invalid_api_key": "Connecte l'application à AIODOS.",
    "key_revoked": "Reconnecte l'application, après un clic de l'utilisateur.",
    "key_expired": "Reconnecte l'application, après un clic de l'utilisateur.",
    "where_denied": "Resserre X-Aiodos-Where, ou demande ce cercle dans AIODOS.",
    "model_not_found": "Installe le modèle dans AIODOS, ou choisis-en un de la liste.",
    "busy": "Réessaie dans quelques secondes.",
    "rate_limited": "Réessaie dans quelques secondes.",
    "engine_unavailable": "Réessaie dans quelques secondes.",
    "engine_failed": "Réessaie.",
    "peer_failed": "Réessaie.",
    "context_length_exceeded": "Raccourcis la demande, ou découpe-la.",
    "tools_unavailable": "Choisis un modèle de ce PC, ou envoie X-Aiodos-Tools: optional.",
    "scope_denied": "Demande ce droit en plus (connect avec ce droit).",
    "requests_closed": "Réglages → Applications et autres outils.",
    "apps_closed": "Réglages → Applications et autres outils.",
    "origin_refused": "Appelle AIODOS depuis le programme de l'application, pas depuis une page.",
}
STATUS = {"host_refused": 400, "invalid_request": 400, "context_length_exceeded": 400,
          "tools_unavailable": 400, "vision_unsupported": 400, "local_only": 400,
          "unsupported_parameter": 400, "invalid_api_key": 401, "key_revoked": 401,
          "key_expired": 401, "scope_denied": 403, "where_denied": 403, "apps_closed": 403,
          "requests_closed": 403, "blocked_program": 403, "other_account": 403,
          "program_mismatch": 403, "origin_refused": 403, "model_not_found": 404,
          "not_supported": 404, "no_machine": 409, "file_too_large": 413,
          "unsupported_media_type": 415, "rate_limited": 429, "busy": 429,
          "peer_failed": 502, "engine_failed": 502, "engine_unavailable": 503,
          "approval_unavailable": 503,
          "graph_not_api": 400, "graph_invalid": 400, "patch_invalid": 400,
          "forbidden_value": 400, "forbidden_node": 400, "input_missing": 400,
          "input_hash_mismatch": 400, "engine_not_found": 404, "job_not_found": 404,
          "file_not_found": 404, "input_not_found": 404, "model_unavailable": 409,
          "model_unresolved": 409, "model_ambiguous": 409, "job_not_cancellable": 409,
          "job_not_pausable": 409, "graph_too_large": 413, "input_too_large": 413,
          "quota_exceeded": 429}
OAUTH_DESCRIPTIONS = {
    "authorization_pending": "La demande attend la réponse de l'utilisateur dans AIODOS.",
    "slow_down": "Interroge moins souvent : 5 s de plus.",
    "access_denied": "L'accès a été refusé dans AIODOS.",
    "expired_token": "La demande a expiré : recommence.",
    "invalid_grant": "Demande inconnue, ou clé déjà relevée.",
    "invalid_request": "Demande de relève invalide.",
}

_HELPER = '''# Faux programme d'aide d'AIODOS (aiodos_client.fake.FakeAiodos.helper_home)
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = json.loads({output!r})
CODE = {code!r}

if "--app-request" in sys.argv:
    line = sys.stdin.readline()
    with open(os.path.join(HERE, "stdin.json"), "w", encoding="utf-8") as out:
        out.write(line)
    sys.stdout.write(json.dumps(OUTPUT, ensure_ascii=False) + "\\n")     # comme le vrai
    sys.stdout.flush()
    sys.exit(CODE)
if "--background" in sys.argv:
    with open(os.path.join(HERE, "launched.json"), "w", encoding="utf-8") as out:
        json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, out)
    sys.exit(0)
sys.exit(2)
'''


def new_key() -> str:
    """Une clé d'appli neuve : aiodos_app_<8 caractères base32>_<43 caractères base64url>."""
    return ("aiodos_app_" + "".join(secrets.choice(BASE32) for _ in range(8)) + "_"
            + secrets.token_urlsafe(32))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _chunk(ident, model, delta, finish=None, extra=None):
    obj = {"id": ident, "object": "chat.completion.chunk", "created": CREATED, "model": model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if extra:
        obj.update(extra)
    return "data: " + _compact(obj)


def text_stream(model="qwen3.5:9b", machine="PC-BUREAU", pieces=("Bonjour", " !"),
                thinking="L'utilisateur salue.", loading=5, usage=True, context=16384,
                ident="chatcmpl-1"):
    """Le flux d'une réponse en texte (celui de chat.json par défaut)."""
    lines = [f": aiodos loading {loading}"] if loading else []
    lines.append(_chunk(ident, model, {"role": "assistant", "content": ""}))
    if thinking:
        lines.append(_chunk(ident, model, {"reasoning_content": thinking}))
    for piece in pieces:
        lines.append(_chunk(ident, model, {"content": piece}))
    lines.append(_chunk(ident, model, {}, "stop", {"aiodos": {
        "model": model, "machine": machine, "place": "here", "stopped_by": "stop",
        "context_length": context}}))
    if usage:
        lines.append("data: " + _compact({
            "id": ident, "object": "chat.completion.chunk", "created": CREATED, "model": model,
            "choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 8,
                                     "total_tokens": 29}}))
    lines.append("data: [DONE]")
    return lines


def tool_stream(name, model="qwen3.5:9b", machine="PC-BUREAU", arguments=("{", "}"),
                call_id="call_8yT2", context=16384, ident="chatcmpl-2"):
    """Le flux d'un appel d'outil, arguments en morceaux (forme de chat.json)."""
    lines = [_chunk(ident, model, {"role": "assistant", "content": None}),
             _chunk(ident, model, {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                   "function": {"name": name,
                                                                "arguments": ""}}]})]
    for piece in arguments:
        lines.append(_chunk(ident, model, {"tool_calls": [{"index": 0,
                                                           "function": {"arguments": piece}}]}))
    lines.append(_chunk(ident, model, {}, "tool_calls", {"aiodos": {
        "model": model, "machine": machine, "place": "here", "stopped_by": "tool_calls",
        "context_length": context}}))
    lines.append("data: [DONE]")
    return lines


def _vector(text: str, dimensions: int):
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    values = [random.Random(seed + i).uniform(-1.0, 1.0) for i in range(dimensions)]
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [round(v / norm, 6) for v in values]


def _has_images(body):
    for message in body.get("messages") or ():
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(isinstance(part, dict)
                                             and part.get("type") == "image_url"
                                             for part in content):
            return True
    return False


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False

    def handle_error(self, request, client_address):
        log.debug("faux AIODOS : connexion interrompue", exc_info=True)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeAiodos/0.2.0"
    sys_version = ""

    def log_message(self, format, *args):
        log.debug("faux AIODOS : %s %s", self.command, self.path.split("?", 1)[0])

    def _dispatch(self):
        try:
            self.server.fake._serve(self)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _dispatch


class FakeAiodos(FakeJobs):
    """Un faux AIODOS sur 127.0.0.1 (voir l'en-tête du module). `job_delay` : les secondes
    de chaque unité d'un travail ComfyUI (None : il n'avance que par advance()) ;
    `ping_every` : la cadence des « : ping » de ses événements."""

    def __init__(self, *, features=FEATURES, version="2.1.0", instance=None, apps=True,
                 requests=True, approval_ui=True, interval=2, expires_in=600,
                 user_code="482 197", impostor=False, mode="aiodos", other_account=False,
                 auto_approve=False, granted_where=("here", "mine"), machine="PC-BUREAU",
                 chat_models=CHAT_MODELS, embed_models=EMBED_MODELS, job_delay=0.05,
                 ping_every=5.0):
        if mode not in ("aiodos", "old", "ollama"):
            raise ValueError("mode : aiodos, old ou ollama")
        self.features = list(features)
        self.version = version
        self.instance = instance or _b64(secrets.token_bytes(6))
        self.apps = apps
        self.requests = requests
        self.approval_ui = approval_ui
        self.interval = interval
        self.expires_in = expires_in
        self.user_code = user_code
        self.impostor = impostor
        self.mode = mode
        self.other_account = other_account
        self.auto_approve = auto_approve
        self.granted_where = tuple(granted_where)
        self.machine = machine
        self.chat_models = [dict(m) for m in chat_models]
        self.embed_models = [dict(m) for m in embed_models]
        self.log = []
        self.seen_keys = []
        self.token_polls = []
        self.stream_closed = threading.Event()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._keys = {}
        self._requests = {}
        self._overrides = []
        self._failures = []
        self._script = None
        self._slow_down = 0
        self._server = None
        self._thread = None
        self._dir = None
        self.port = None
        self.discovery_file = None
        self._init_jobs(job_delay, ping_every)

    # --- Démarrage ----------------------------------------------------------------------
    def start(self):
        self._dir = tempfile.mkdtemp(prefix="aiodos-fake-")
        self.discovery_file = os.path.join(self._dir, "gateway.json")
        self._listen(0)
        self.write_discovery()
        return self

    def stop(self):
        self._close_server()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _listen(self, port):
        server = _Server(("127.0.0.1", port), _Handler)
        server.fake = self
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="faux-aiodos", daemon=True)
        self._thread.start()

    def _close_server(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def environ(self) -> dict:
        """Les variables d'environnement qui désignent ce faux AIODOS au client."""
        return {"AIODOS_DISCOVERY_FILE": self.discovery_file}

    def write_discovery(self, **changes) -> dict:
        """Écrit le faux gateway.json (forme de clients/contract/discovery.json). `changes` :
        state, url, apps, requests, approval_ui, error (dans « gateway »), ou running, pid,
        instance, version, api…"""
        data = {"product": "aiodos", "format": 1, "version": self.version, "api": 1,
                "pid": os.getpid(), "instance": self.instance, "running": True,
                "gateway": {"state": "open", "url": self.url, "apps": self.apps,
                            "requests": self.requests, "approval_ui": self.approval_ui,
                            "error": None},
                "updated": time.time()}
        for name, value in changes.items():
            if name in ("state", "url", "apps", "requests", "approval_ui", "error"):
                data["gateway"][name] = value
            else:
                data[name] = value
        self.write_discovery_file(data)
        return data

    def write_discovery_file(self, data) -> None:
        """Écrit tel quel le faux gateway.json (écriture atomique)."""
        temporary = self.discovery_file + ".tmp"
        with open(temporary, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False)
        os.replace(temporary, self.discovery_file)

    def close_gateway(self, discovery=True):
        """La passerelle cesse d'écouter (connexion refusée) ; le port reste réservé."""
        self._close_server()
        if discovery:
            self.write_discovery(state="closed", apps=False)

    def open_gateway(self, discovery=True):
        """La passerelle écoute de nouveau, sur le même port."""
        if self._server is None:
            self._listen(self.port)
        if discovery:
            self.write_discovery(state="open")

    # --- Clés et demandes d'accès ---------------------------------------------------------
    def add_key(self, scopes=("chat", "embed"), where=("here", "mine"), name="PANDORA",
                priority="interactive", expires=None) -> str:
        """Une clé valide, comme si l'utilisateur avait déjà autorisé l'appli."""
        key = new_key()
        with self._lock:
            self._keys[key] = {"id": key[11:19], "name": name, "scopes": list(scopes),
                               "where": list(where), "priority": priority, "expires": expires,
                               "state": "valid"}
        return key

    def revoke(self, key):
        with self._lock:
            self._keys[key]["state"] = "revoked"

    def expire_key(self, key):
        with self._lock:
            self._keys[key]["state"] = "expired"

    def key_record(self, key):
        with self._lock:
            record = self._keys.get(key)
            return dict(record) if record else None

    def _new_request(self, body, upgrade_key=None, stdin_file=None):
        scopes = str(body.get("scope") or "").split()
        where = body.get("where")
        if isinstance(where, str):
            where = [c.strip() for c in where.split(",") if c.strip()]
        request = {"device_code": secrets.token_urlsafe(32), "user_code": self.user_code,
                   "request_id": "req_" + _b64(secrets.token_bytes(6)), "body": dict(body),
                   "scopes": scopes, "where": list(where) if where else list(CIRCLES),
                   "status": "pending", "upgrade_key": upgrade_key, "key": None,
                   "taken": False, "seen": False, "created": time.monotonic(),
                   "stdin_file": stdin_file}
        with self._changed:
            self._requests[request["device_code"]] = request
            if self.auto_approve:
                self._approve(request, None, None)
            self._changed.notify_all()
        return request

    def _refresh(self, request):
        """Une demande passée par le faux programme d'aide : son corps est sur son stdin."""
        if request.get("stdin_file") and not request["body"]:
            try:
                with open(request["stdin_file"], encoding="utf-8") as handle:
                    body = json.loads(handle.read() or "{}")
            except (OSError, ValueError):
                return
            request["body"] = body
            request["scopes"] = str(body.get("scope") or "").split()
            where = body.get("where")
            request["where"] = list(where) if isinstance(where, list) else list(CIRCLES)

    def wait_request(self, timeout=5.0) -> dict:
        """La prochaine demande d'accès arrivée (chacune n'est rendue qu'une fois)."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for request in self._requests.values():
                    if not request["seen"] and request["status"] == "pending":
                        request["seen"] = True
                        self._refresh(request)
                        return dict(request)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("faux AIODOS : aucune demande d'accès")
                self._changed.wait(left)

    def _pick(self, device_code):
        if device_code is not None:
            return self._requests[device_code]
        waiting = [r for r in self._requests.values() if r["status"] == "pending"]
        if not waiting:
            raise LookupError("faux AIODOS : aucune demande en attente")
        return waiting[-1]

    def _approve(self, request, scopes, where):
        self._refresh(request)
        granted = list(scopes) if scopes is not None else list(request["scopes"])
        if where is not None:
            circles = list(where)
        else:
            circles = [c for c in request["where"] if c in self.granted_where] or ["here"]
        request.update(status="approved", granted_scopes=granted, granted_where=circles)
        if request["upgrade_key"]:                  # droits en plus pour la même clé
            record = self._keys[request["upgrade_key"]]
            record["scopes"] = list(dict.fromkeys(record["scopes"] + granted))
            request["key"] = request["upgrade_key"]
        else:
            key = new_key()
            self._keys[key] = {"id": key[11:19], "name": request["body"].get("client_name"),
                               "scopes": granted, "where": circles, "priority": "interactive",
                               "expires": None, "state": "valid"}
            request["key"] = key
        self.apps = True                    # le premier accord ouvre la passerelle aux applis

    def approve(self, scopes=None, where=None, device_code=None):
        """L'utilisateur autorise l'appli (la dernière demande, par défaut). Rend la clé."""
        with self._changed:
            request = self._pick(device_code)
            self._approve(request, scopes, where)
            self._changed.notify_all()
            return request["key"]

    def deny(self, device_code=None):
        with self._changed:
            self._pick(device_code)["status"] = "denied"

    def expire(self, device_code=None):
        with self._changed:
            self._pick(device_code)["status"] = "expired"

    def slow_down(self, times=1):
        """Les `times` relèves suivantes répondent slow_down."""
        with self._lock:
            self._slow_down += times

    def helper_home(self, error=None) -> str:
        """Un faux dossier d'AIODOS (copie de développement), à désigner par AIODOS_HOME (et
        AIODOS_PYTHON = sys.executable). Son « python -m aiodos --app-request » écrit son
        entrée standard dans `stdin.json`, puis la ligne de stdout_ok du contrat (adresse de ce
        faux AIODOS, demande en attente ici) et sort en 0 ; avec `error` (un code, ou l'objet
        d'erreur), la ligne de stdout_error et le code 1. « --background » écrit
        `launched.json`."""
        home = tempfile.mkdtemp(prefix="aiodos-home-", dir=self._dir)
        package = os.path.join(home, "aiodos")
        os.makedirs(package)
        with open(os.path.join(package, "__init__.py"), "w", encoding="utf-8"):
            pass
        if error is None:
            request = self._new_request({}, stdin_file=os.path.join(home, "stdin.json"))
            output = {"device_code": request["device_code"], "user_code": request["user_code"],
                      "interval": self.interval, "expires_in": self.expires_in,
                      "request_id": request["request_id"], "url": self.url}
            code = 0
        else:
            detail = error if isinstance(error, dict) else {
                "code": error, "message": texts.error(error, "fr") or error}
            output = {"error": detail}
            code = 1
        with open(os.path.join(package, "__main__.py"), "w", encoding="utf-8") as out:
            out.write(_HELPER.format(output=json.dumps(output, ensure_ascii=False), code=code))
        return home

    @staticmethod
    def helper_stdin(home):
        """Ce que le faux programme d'aide a reçu sur son entrée standard (ou None)."""
        try:
            with open(os.path.join(home, "stdin.json"), encoding="utf-8") as handle:
                return json.loads(handle.read())
        except (OSError, ValueError):
            return None

    @staticmethod
    def launched(home):
        """Ce qu'a reçu le faux « python -m aiodos --background » (ou None)."""
        try:
            with open(os.path.join(home, "launched.json"), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    # --- Réponses imposées --------------------------------------------------------------
    def override(self, path, response, method=None, times=1):
        """Les `times` prochaines requêtes sur `path` (le chemin, ou le chemin et sa requête
        « ?… ») reçoivent `response`, à la forme du contrat : {"status", "headers",
        "json" | "sse" | "raw"}. Dans "sse", un nombre est une pause (s)."""
        with self._lock:
            self._overrides.append({"path": path, "method": method, "response": response,
                                    "times": times})

    def fail_next(self, code, status=None, message=None, hint=None, retryable=None,
                  detail=None, retry_after=None, path=None, times=1, param=None):
        """La prochaine requête (sur `path` s'il est donné) reçoit l'erreur `code`, dans la
        forme de sa famille de routes (OpenAI + aiodos, Ollama, OAuth)."""
        with self._lock:
            self._failures.append({"code": code, "status": status, "message": message,
                                   "hint": hint, "retryable": retryable, "detail": detail,
                                   "retry_after": retry_after, "path": path, "times": times,
                                   "param": param})

    def chat_script(self, sse, headers=None, hold=False):
        """Le flux des conversations suivantes (lignes SSE ; un nombre : une pause). `hold` :
        après les lignes, des morceaux toutes les 0,05 s jusqu'à ce que le client parte."""
        with self._lock:
            self._script = None if sse is None else {"sse": list(sse), "headers": headers or {},
                                                     "hold": hold}

    def requests_to(self, path):
        """Les requêtes reçues sur ce chemin (sans la requête « ?… »)."""
        with self._lock:
            return [entry for entry in self.log if entry["path"].split("?", 1)[0] == path]

    # --- Service ------------------------------------------------------------------------
    def _hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}

    @staticmethod
    def _given_key(headers):
        auth = headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip() or None
        return (headers.get("X-Api-Key") or "").strip() or None

    def _serve(self, h):
        split = urlsplit(h.path)
        path, query = split.path, parse_qs(split.query)
        length = h.headers.get("Content-Length")
        uploading = h.command == "PUT" and path.startswith("/aiodos/v1/inputs/")
        limit = MAX_INPUT if uploading else MAX_BODY        # une entrée : 100 Mo
        too_big = length is not None and (not length.isdigit() or int(length) > limit)
        raw = h.rfile.read(int(length)) if length and not too_big else b""
        parsed = None
        if raw and not uploading:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                parsed = None
        key = self._given_key(h.headers)
        with self._lock:
            self.log.append({"method": h.command, "path": h.path, "headers": dict(h.headers),
                             "json": parsed, "bytes": len(raw), "at": time.monotonic()})
            if key:
                self.seen_keys.append(key)
        if self.mode == "old":
            if path == "/":
                return self._send(h, 200, b"Ollama is running", "text/plain", api=False)
            return self._send_json(h, 401, {"error": OLD_401}, api=False)
        if self.mode == "ollama":
            if path == "/":
                return self._send(h, 200, b"Ollama is running", "text/plain", api=False)
            return self._send(h, 404, b"404 page not found", "text/plain", api=False)
        if h.headers.get("Host") not in self._hosts():
            return self._send(h, 400, "Hôte refusé.".encode("utf-8"), "text/plain")
        if (h.headers.get("Origin") or h.headers.get("Sec-Fetch-Site") in ("cross-site",
                                                                          "same-site")
                or h.headers.get("Sec-Fetch-Dest") not in (None, "empty")):
            return self._error(h, path, "origin_refused",
                               message="Origine refusée : la passerelle d'AIODOS ne répond "
                                       "pas aux pages web.")
        override = self._take(self._overrides, lambda o: (o["method"] in (None, h.command)
                                                          and o["path"] in (path, h.path)))
        if override is not None:
            return self._respond(h, override["response"])
        failure = self._take(self._failures, lambda f: f["path"] in (None, path))
        if failure is not None:
            return self._error(h, path, failure["code"], status=failure["status"],
                               message=failure["message"], hint=failure["hint"],
                               retryable=failure["retryable"], detail=failure["detail"],
                               retry_after=failure["retry_after"], param=failure["param"])
        if too_big:
            return self._error(h, path, "input_too_large" if uploading else "file_too_large")
        if (h.command == "POST" and path.startswith("/aiodos/v1/") and raw     # un corps : JSON
                and (h.headers.get("Content-Type") or "").split(";")[0].strip()
                != "application/json"):
            return self._error(h, path, "unsupported_media_type")
        if path.startswith(PREFIXES):                   # les travaux ComfyUI (_fake_jobs.py)
            return self._jobs_route(h, h.command, path, query, parsed, raw, key)
        return self._route(h, h.command, path, query, parsed, key)

    def _take(self, queue, match):
        with self._lock:
            for entry in queue:
                if entry["times"] > 0 and match(entry):
                    entry["times"] -= 1
                    if entry["times"] <= 0:
                        queue.remove(entry)
                    return entry
        return None

    def _route(self, h, method, path, query, body, key):
        if path == "/" and method == "GET":
            return self._send(h, 200, b"Ollama is running", "text/plain")
        if path == "/aiodos/v1/info" and method == "GET":
            return self._info(h, query, key)
        if path == "/aiodos/v1/access/requests" and method == "POST":
            return self._access_request(h, body, key)
        if path == "/aiodos/v1/access/token" and method == "POST":
            return self._access_token(h, body)
        keyed = {("POST", "/aiodos/v1/access/revoke"): None, ("GET", "/aiodos/v1/models"): None,
                 ("GET", "/v1/models"): None, ("POST", "/aiodos/v1/open"): None,
                 ("POST", "/v1/chat/completions"): "chat", ("POST", "/v1/embeddings"): "embed"}
        if (method, path) not in keyed:
            return self._error(h, path, "not_supported",
                               message="Cette version d'AIODOS ne sert pas cette route.")
        record = self._keyed(h, path, key, keyed[(method, path)])
        if record is None:
            return None
        if path == "/aiodos/v1/access/revoke":
            with self._lock:
                record["state"] = "revoked"
            return self._send_json(h, 200, {"revoked": True})
        if path == "/aiodos/v1/models":
            kind = (query.get("kind") or ["chat"])[0]
            models = self.chat_models if kind == "chat" else self.embed_models \
                if kind == "embed" else []
            return self._send_json(h, 200, {"object": "list", "models": models})
        if path == "/v1/models":
            return self._send_json(h, 200, {"object": "list", "data": self._openai_models()})
        if path == "/aiodos/v1/open":
            return self._send_json(h, 202, {"opened": bool(self.approval_ui)})
        if path == "/v1/chat/completions":
            return self._chat(h, record, body)
        return self._embed(h, body)

    def _keyed(self, h, path, key, scope):
        with self._lock:
            record = None if self.impostor else self._keys.get(key) if key else None
        if record is None:
            self._error(h, path, "invalid_api_key")
            return None
        if record["state"] != "valid":
            self._error(h, path, "key_revoked" if record["state"] == "revoked" else "key_expired")
            return None
        if self.other_account:
            self._error(h, path, "other_account")
            return None
        if not self.apps:
            self._error(h, path, "apps_closed")
            return None
        if scope and scope not in record["scopes"]:
            self._error(h, path, "scope_denied",
                        message=f"Cette application n'a pas le droit « {scope} » : demande-le "
                                "dans AIODOS.", detail={"scope": scope})
            return None
        return record

    def _info(self, h, query, key):
        body = {"object": "aiodos.info", "product": "aiodos", "version": self.version, "api": 1,
                "instance": self.instance, "features": list(self.features),
                "access": {"apps": self.apps, "requests": self.requests,
                           "approval_ui": self.approval_ui}, "auth": "none"}
        key_id = (query.get("key_id") or [None])[0]
        challenge = (query.get("challenge") or [None])[0]
        if key_id and challenge:
            with self._lock:
                known = None if self.impostor else next(
                    (k for k, r in self._keys.items() if r["id"] == key_id), None)
            try:
                body["proof"] = _proof.compute(known, challenge) if known else \
                    _b64(secrets.token_bytes(32))
            except UnicodeEncodeError:
                body["proof"] = _b64(secrets.token_bytes(32))
        if key:
            if self.other_account:
                return self._error(h, "/aiodos/v1/info", "other_account")
            with self._lock:
                record = None if self.impostor else self._keys.get(key)
            if record is None:
                body["auth"] = "invalid"
            elif record["state"] != "valid":
                body["auth"] = record["state"]
            else:
                body["auth"] = "valid"
                body["app"] = {"id": record["id"], "name": record["name"], "kind": "app",
                               "scopes": list(record["scopes"]), "where": list(record["where"]),
                               "priority": record["priority"], "expires": record["expires"]}
                body["limits"] = {"requests_per_minute": 120, "streams": 4,
                                  "max_body_bytes": MAX_BODY}
        return self._send_json(h, 200, body)

    def _access_request(self, h, body, key):
        path = "/aiodos/v1/access/requests"
        if not isinstance(body, dict):
            return self._error(h, path, "invalid_request", message="Corps JSON attendu.")
        if self.other_account:
            return self._error(h, path, "other_account")
        upgrade = None
        if key:
            with self._lock:
                record = self._keys.get(key)
            if record is None:
                return self._error(h, path, "invalid_api_key")
            if record["state"] != "valid":
                return self._error(h, path, "key_revoked" if record["state"] == "revoked"
                                   else "key_expired")
            upgrade = key
        if not self.apps:
            return self._error(h, path, "apps_closed",
                               message="AIODOS n'est ouvert qu'aux autres outils.")
        if not self.requests:
            return self._error(h, path, "requests_closed",
                               message="AIODOS n'accepte pas de nouvelle application en ce "
                                       "moment.")
        if not self.approval_ui:
            return self._error(h, path, "approval_unavailable")
        name = body.get("client_name")
        scopes = str(body.get("scope") or "").split()
        if (not isinstance(name, str) or not 1 <= len(name) <= 40
                or not isinstance(body.get("client_id"), str)
                or not CLIENT_ID.match(body["client_id"])
                or not scopes or any(s not in SCOPES for s in scopes)):
            return self._error(h, path, "invalid_request", message="Demande d'accès invalide.")
        request = self._new_request(body, upgrade_key=upgrade)
        return self._send_json(h, 200, {
            "device_code": request["device_code"], "user_code": request["user_code"],
            "expires_in": self.expires_in, "interval": self.interval,
            "request_id": request["request_id"],
            "message": "Une demande d'accès t'attend dans AIODOS. Vérifie que le code est "
                       f"{request['user_code']}."})

    def _oauth(self, h, code):
        return self._send_json(h, 400, {"error": code,
                                        "error_description": OAUTH_DESCRIPTIONS.get(code, code)})

    def _access_token(self, h, body):
        with self._changed:
            self.token_polls.append(time.monotonic())
            if not isinstance(body, dict) or body.get("grant_type") != GRANT_TYPE:
                return self._oauth(h, "invalid_request")
            request = self._requests.get(body.get("device_code"))
            if request is None or request["taken"]:
                return self._oauth(h, "invalid_grant")
            if self._slow_down > 0:
                self._slow_down -= 1
                return self._oauth(h, "slow_down")
            if (request["status"] == "pending"
                    and time.monotonic() - request["created"] > self.expires_in):
                request["status"] = "expired"
            status = request["status"]
            if status == "pending":
                return self._oauth(h, "authorization_pending")
            if status == "denied":
                return self._oauth(h, "access_denied")
            if status == "expired":
                return self._oauth(h, "expired_token")
            request["taken"] = True
            record = self._keys[request["key"]]
            ext = {"app_id": record["id"], "where": list(record["where"]),
                   "priority": record["priority"], "expires": record["expires"]}
            if request["upgrade_key"]:
                ext["upgraded"] = True
                answer = {"token_type": "Bearer", "scope": " ".join(record["scopes"]),
                          "aiodos": ext}
            else:
                answer = {"access_token": request["key"], "token_type": "Bearer",
                          "scope": " ".join(record["scopes"]), "aiodos": ext}
        return self._send_json(h, 200, answer)

    def _openai_models(self):
        data = []
        for model in self.chat_models:
            data.append({"id": model["id"], "object": "model", "created": 1760000000,
                         "owned_by": "aiodos", "name": model["id"],
                         "context_length": model.get("context"),
                         "architecture": {"input_modalities": ["text", "image"]
                                          if model.get("vision") else ["text"],
                                          "output_modalities": ["text"]},
                         "supported_parameters": list(SUPPORTED),
                         "aiodos": {"kind": "chat", "machine": None, "place": "here",
                                    "loaded": bool(model.get("loaded"))}})
        return data

    def _chat(self, h, record, body):
        path = "/v1/chat/completions"
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return self._error(h, path, "invalid_request", message="messages manquants.")
        asked = [c.strip() for c in (h.headers.get("X-Aiodos-Where") or "").split(",")
                 if c.strip()]
        if any(c not in record["where"] for c in asked):
            return self._error(h, path, "where_denied", detail={"allowed": record["where"]},
                               message="Lieu de calcul refusé : cette application n'a pas "
                                       f"accès à « {','.join(asked)} ».")
        entry = next((m for m in self.chat_models if m["id"] == body.get("model")), None)
        if entry is None:
            return self._error(h, path, "model_not_found", param="model",
                               message=f"Aucune machine ne peut répondre avec "
                                       f"« {body.get('model')} » en ce moment.")
        tools = body.get("tools")
        if tools and not entry.get("tools") and h.headers.get("X-Aiodos-Tools") != "optional":
            return self._error(h, path, "tools_unavailable", param="tools")
        if _has_images(body) and not entry.get("vision"):
            return self._error(h, path, "vision_unsupported")
        model = entry["id"]
        headers = {"X-Aiodos-Api": "1", "Content-Type": "text/event-stream",
                   "X-Aiodos-Model": quote(model, safe=":@"),
                   "X-Aiodos-Machine": quote(self.machine, safe=""),
                   "X-Aiodos-Place": "here"}
        if not body.get("stream"):
            return self._send_json(h, 200, {
                "id": "chatcmpl-5", "object": "chat.completion", "created": CREATED,
                "model": model, "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "Bonjour !"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
                "aiodos": {"model": model, "machine": self.machine, "place": "here",
                           "stopped_by": "stop", "context_length": entry.get("context")}},
                headers={"X-Aiodos-Model": model})
        with self._lock:
            script = dict(self._script) if self._script else None
        if script is not None:
            headers.update(script["headers"])
            return self._send_sse(h, 200, headers, script["sse"], script["hold"])
        usage = isinstance(body.get("stream_options"), dict) and \
            body["stream_options"].get("include_usage") is True
        if tools and entry.get("tools"):            # sinon (optional) : en texte, sans outils
            first = tools[0].get("function", {}) if isinstance(tools[0], dict) else {}
            lines = tool_stream(first.get("name") or "outil", model, self.machine,
                                context=entry.get("context"))
        else:
            lines = text_stream(model, self.machine, usage=usage, context=entry.get("context"),
                                loading=0 if entry.get("loaded") else 5)
        return self._send_sse(h, 200, headers, lines, False)

    def _embed(self, h, body):
        path = "/v1/embeddings"
        if not isinstance(body, dict):
            return self._error(h, path, "invalid_request", message="Corps JSON attendu.")
        entry = next((m for m in self.embed_models if m["id"] == body.get("model")), None)
        if entry is None:
            return self._error(h, path, "model_not_found", param="model")
        items = body.get("input")
        items = [items] if isinstance(items, str) else items
        if not isinstance(items, list) or not all(isinstance(t, str) for t in items):
            return self._error(h, path, "invalid_request", message="input : des textes.")
        dimensions = entry.get("dimensions") or 8
        return self._send_json(h, 200, {
            "object": "list", "model": entry["id"],
            "data": [{"object": "embedding", "index": i, "embedding": _vector(text, dimensions)}
                     for i, text in enumerate(items)],
            "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    # --- Envoi --------------------------------------------------------------------------
    def _error(self, h, path, code, status=None, message=None, hint=None, retryable=None,
               detail=None, retry_after=None, param=None):
        status = status or STATUS.get(code, 400)
        message = message or texts.error(code, "fr") or code
        hint = hint if hint is not None else HINTS.get(code)
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        if path.startswith("/aiodos/v1/access/token"):
            body = {"error": code, "error_description": message}
        elif path.startswith("/api/"):
            body = {"error": message, "code": code, "hint": hint}
        else:
            body = {"error": {"message": message,
                              "type": "invalid_request_error" if status < 500 else "api_error",
                              "param": param, "code": code,
                              "aiodos": {"hint": hint,
                                         "retryable": bool(retryable) if retryable is not None
                                         else status in (429, 502, 503),
                                         "detail": detail or {}}}}
        return self._send_json(h, status, body, headers=headers)

    def _send_json(self, h, status, obj, headers=None, api=True):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(h, status, data, "application/json", headers=headers, api=api)

    def _send(self, h, status, data, content_type, headers=None, api=True):
        h.send_response(status)
        if api:
            h.send_header("X-Aiodos-Api", "1")
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            h.send_header(name, value)
        h.end_headers()
        if h.command != "HEAD" and data:
            h.wfile.write(data)
        h.close_connection = True

    def _respond(self, h, response):
        """Une réponse du contrat, telle quelle (sans X-Aiodos-Api si elle n'en a pas)."""
        status = response.get("status", 200)
        headers = dict(response.get("headers") or {})
        if "sse" in response:
            return self._send_sse(h, status, headers, response["sse"], response.get("hold", False),
                                  cut=response.get("cut", False))
        h.send_response(status)
        if "raw" in response:
            data = response["raw"] if isinstance(response["raw"], bytes) else \
                str(response["raw"]).encode("utf-8")
            kind = "text/plain"
        elif response.get("json") is None:
            data, kind = b"", None
        else:
            data = json.dumps(response["json"], ensure_ascii=False).encode("utf-8")
            kind = "application/json"
        if kind and not any(name.lower() == "content-type" for name in headers):
            h.send_header("Content-Type", kind)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Connection", "close")
        for name, value in headers.items():
            h.send_header(name, value)
        h.end_headers()
        if data and h.command != "HEAD":
            h.wfile.write(data)
        h.close_connection = True

    def _send_sse(self, h, status, headers, items, hold, cut=False):
        """Un flux SSE découpé (chunked). `hold` : des morceaux jusqu'au départ du client ;
        `cut` : la connexion se ferme après les lignes, sans le morceau final. Une ligne
        « event: … » nomme l'événement qui la suit (sans ligne vide entre les deux)."""
        h.send_response(status)
        if not any(name.lower() == "content-type" for name in headers):
            h.send_header("Content-Type", "text/event-stream")
        for name, value in headers.items():
            h.send_header(name, value)
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Transfer-Encoding", "chunked")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True
        try:
            for item in items:
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    time.sleep(item)
                    continue
                line = str(item)
                end = "\n" if line.startswith("event:") and "\n" not in line else "\n\n"
                self._chunk(h, (line + end).encode("utf-8"))
            if hold:
                piece = _chunk("chatcmpl-hold", "qwen3.5:9b", {"content": "."})
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    time.sleep(0.05)
                    self._chunk(h, (piece + "\n\n").encode("utf-8"))
                return
            if not cut:
                h.wfile.write(b"0\r\n\r\n")
        except OSError:
            self.stream_closed.set()
            log.debug("faux AIODOS : le client a fermé le flux")

    @staticmethod
    def _chunk(h, data):
        h.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        h.wfile.flush()
