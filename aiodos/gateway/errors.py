"""Erreurs de la passerelle, à codes stables (docs/api-applications.md § 3).

Une application ne lit pas les messages, faits pour l'utilisateur : elle réagit au code, qui
ne change pas d'une version à l'autre. Chaque code a son statut HTTP, un message et un
conseil en français et en anglais (Accept-Language), et dit si un nouvel essai a un sens.
Trois enveloppes, selon la famille de la route :
- /v1/* et /aiodos/v1/* : celle d'OpenAI, plus un objet « aiodos » (hint, retryable, detail) ;
- /api/* (forme d'Ollama) : {"error": "…", "code": "…", "hint": "…"} ; les clients d'Ollama
  ne lisent que « error » ;
- /mcp : une erreur JSON-RPC, le code dans error.data.code.
"""
from starlette.responses import JSONResponse

# code : (statut, message, message en anglais, conseil, conseil en anglais, réessayer ?)
CODES = {
    "host_refused": (400, "Hôte refusé.", "Host refused.", "", "", False),
    "invalid_request": (400, "Demande invalide.", "Invalid request.", "", "", False),
    "context_length_exceeded": (
        400, "La demande dépasse le contexte du modèle.", "The request exceeds the model's context.",
        "Raccourcis la demande, ou découpe-la.", "Shorten or split the request.", False),
    "tools_unavailable": (
        400, "Aucune machine permise ne reçoit les outils pour ce modèle.",
        "No allowed machine accepts tools for this model.",
        "Choisis un modèle de ce PC, ou envoie X-Aiodos-Tools: optional.",
        "Pick a model on this PC, or send X-Aiodos-Tools: optional.", False),
    "vision_unsupported": (
        400, "Ce modèle ne voit pas les images.", "This model cannot see images.",
        "Choisis un modèle qui voit les images.", "Pick a vision model.", False),
    "local_only": (
        400, "Cette demande ne se calcule que sur ce PC, qui n'a pas ce modèle.",
        "This request can only run on this PC, which does not have this model.",
        "Installe le modèle sur ce PC dans AIODOS.", "Install the model on this PC in AIODOS.",
        False),
    "unsupported_parameter": (
        400, "Paramètre non pris en charge par AIODOS.", "Parameter not supported by AIODOS.",
        "", "", False),
    # Travaux de moteur (docs/conception/partage-puissance.md § 6.9)
    "graph_not_api": (
        400, "Graphe au format de l'éditeur : il faut le format API.",
        "Editor-format graph: the API format is required.",
        "Exporte le graphe au format API dans ComfyUI.", "Export the graph in API format in ComfyUI.",
        False),
    "graph_invalid": (
        400, "Graphe mal formé.", "Malformed graph.",
        "Vérifie les liens, les nœuds et les sorties du graphe.",
        "Check the graph's links, nodes and outputs.", False),
    "patch_invalid": (
        400, "Retouche invalide.", "Invalid patch.",
        "Une retouche remplace une valeur déjà présente, du même type, jamais un lien.",
        "A patch replaces an existing value of the same type, never a link.", False),
    "forbidden_value": (
        400, "Valeur refusée (chemin, adresse ou annotation de dossier).",
        "Value refused (path, address or folder annotation).",
        "Un fichier se donne par un jeton @in:, un modèle par son nom.",
        "Give a file with an @in: token, a model by its name.", False),
    "forbidden_node": (
        400, "Nœud refusé.", "Node refused.",
        "Les nœuds qui appellent un service en ligne ne passent pas par AIODOS.",
        "Nodes that call an online service do not go through AIODOS.", False),
    "input_missing": (
        400, "Entrée absente.", "Missing input.",
        "Envoie l'entrée (PUT /aiodos/v1/inputs/{sha256}) et déclare-la dans « inputs ».",
        "Upload the input (PUT /aiodos/v1/inputs/{sha256}) and declare it in \"inputs\".",
        False),
    "input_hash_mismatch": (
        400, "L'empreinte du fichier reçu ne correspond pas.",
        "The received file's hash does not match.",
        "Calcule le SHA-256 des octets envoyés.", "Compute the SHA-256 of the bytes sent.",
        False),
    "invalid_api_key": (
        401, "Clé d'accès d'AIODOS manquante ou invalide.", "Missing or invalid AIODOS key.",
        "Connecte l'application à AIODOS.", "Connect the application to AIODOS.", False),
    "key_revoked": (
        401, "L'accès de cette application a été retiré dans AIODOS.",
        "This application's access was revoked in AIODOS.",
        "Reconnecte l'application, après un clic de l'utilisateur.",
        "Reconnect the application, after the user clicks.", False),
    "key_expired": (
        401, "L'accès de cette application a expiré.", "This application's access has expired.",
        "Reconnecte l'application, après un clic de l'utilisateur.",
        "Reconnect the application, after the user clicks.", False),
    "scope_denied": (
        403, "Cette application n'a pas ce droit dans AIODOS.",
        "This application lacks this permission in AIODOS.",
        "Demande ce droit en plus (connect avec ce droit).",
        "Ask for this permission too (connect with it).", False),
    "where_denied": (
        403, "Lieu de calcul refusé : plus large que l'accès de cette application.",
        "Compute location refused: wider than this application's access.",
        "Resserre X-Aiodos-Where, ou demande ce cercle dans AIODOS.",
        "Narrow X-Aiodos-Where, or ask for this circle in AIODOS.", False),
    "apps_closed": (
        403, "AIODOS n'est pas ouvert aux applications.", "AIODOS is not open to applications.",
        "Réglages → Applications et autres outils.", "Settings → Applications and other tools.",
        False),
    "requests_closed": (
        403, "AIODOS n'accepte pas de nouvelle application en ce moment.",
        "AIODOS does not accept new applications right now.",
        "Réglages → Applications et autres outils.", "Settings → Applications and other tools.",
        False),
    "blocked_program": (
        403, "Ce programme a été bloqué dans AIODOS.", "This program was blocked in AIODOS.",
        "Réglages → Applications et autres outils → Programmes bloqués.",
        "Settings → Applications and other tools → Blocked programs.", False),
    "other_account": (
        403, "AIODOS tourne sous un autre compte Windows.", "AIODOS runs under another Windows account.",
        "", "", False),
    "program_mismatch": (
        403, "Cette clé est liée à un autre programme.", "This key is bound to another program.",
        "Reconnecte l'application à AIODOS.", "Reconnect the application to AIODOS.", False),
    "origin_refused": (
        403, "Origine refusée : la passerelle d'AIODOS ne répond pas aux pages web.",
        "Origin refused: the AIODOS gateway does not answer web pages.",
        "Appelle AIODOS depuis le programme de l'application, pas depuis une page.",
        "Call AIODOS from the application's process, not from a page.", False),
    "model_not_found": (
        404, "Modèle inconnu du cercle AIODOS.", "Unknown model in the AIODOS circle.",
        "Installe le modèle dans AIODOS, ou choisis-en un de la liste.",
        "Install the model in AIODOS, or pick one from the list.", False),
    "not_supported": (
        404, "Fonction absente de cette version d'AIODOS.",
        "Feature not available in this AIODOS version.",
        "Mets AIODOS à jour.", "Update AIODOS.", False),
    "engine_not_found": (404, "Moteur inconnu.", "Unknown engine.", "", "", False),
    "input_not_found": (404, "Entrée introuvable.", "Input not found.", "", "", False),
    "job_not_found": (404, "Travail introuvable.", "Job not found.", "", "", False),
    "file_not_found": (404, "Fichier introuvable.", "File not found.", "", "", False),
    "no_machine": (
        409, "Aucune machine permise n'a ce modèle en ce moment.",
        "No allowed machine has this model right now.",
        "Réessaie plus tard, ou choisis un autre modèle.", "Retry later, or pick another model.",
        True),
    "model_unavailable": (
        409, "Aucune machine permise n'a ces modèles.", "No allowed machine has these models.",
        "Installe les modèles qui manquent (detail.models), ou élargis le lieu de calcul.",
        "Install the missing models (detail.models), or widen the compute location.", True),
    "model_unresolved": (
        409, "Modèle non résolu.", "Unresolved model.",
        "Déclare le modèle dans « models » : {folder, name}.",
        "Declare the model in \"models\": {folder, name}.", False),
    "model_ambiguous": (
        409, "Même nom de modèle dans plusieurs dossiers.",
        "Same model name in several folders.",
        "Déclare le modèle dans « models » avec son dossier.",
        "Declare the model in \"models\" with its folder.", False),
    "job_not_cancellable": (409, "Ce travail est déjà fini.", "This job has already finished.",
                            "", "", False),
    "job_not_pausable": (409, "Ce travail est déjà fini.", "This job has already finished.",
                         "", "", False),
    "file_too_large": (413, "Requête trop grosse.", "Request too large.", "", "", False),
    "graph_too_large": (
        413, "Graphe trop gros.", "Graph too large.",
        "2 Mo par graphe, 8 Mo et 64 graphes par travail.",
        "2 MB per graph, 8 MB and 64 graphs per job.", False),
    "input_too_large": (
        413, "Entrée trop lourde.", "Input too large.", "100 Mo au plus.", "100 MB at most.",
        False),
    "unsupported_media_type": (
        415, "Corps attendu en JSON (Content-Type: application/json).",
        "JSON body expected (Content-Type: application/json).", "", "", False),
    "rate_limited": (
        429, "Trop de demandes de cette application.", "Too many requests from this application.",
        "Réessaie dans quelques secondes.", "Retry in a few seconds.", True),
    "quota_exceeded": (
        429, "Quota de cette application dépassé.", "This application's quota is exceeded.",
        "Récupère ou supprime des fichiers et des travaux finis.",
        "Fetch or delete finished files and jobs.", True),
    "busy": (
        429, "Trop de demandes de cette application en même temps.",
        "Too many concurrent requests from this application.",
        "Réessaie dans quelques secondes.", "Retry in a few seconds.", True),
    "cancelled": (499, "Arrêté.", "Stopped.", "", "", False),
    "peer_failed": (
        502, "La machine choisie n'a pas pu répondre.", "The chosen machine could not answer.",
        "Réessaie.", "Retry.", True),
    "engine_failed": (
        502, "Le moteur n'a pas pu répondre.", "The engine could not answer.",
        "Réessaie.", "Retry.", True),
    "engine_unavailable": (
        503, "Le moteur ne répond pas encore.", "The engine is not ready yet.",
        "Réessaie dans quelques secondes.", "Retry in a few seconds.", True),
    "approval_unavailable": (
        503, "AIODOS tourne sans fenêtre : ouvre-le pour autoriser l'application.",
        "AIODOS runs without a window: open it to authorize the application.",
        "Ouvre AIODOS.", "Open AIODOS.", True),
    "timeout": (504, "Délai dépassé.", "Timed out.", "", "", True),
    "internal_error": (500, "Erreur d'AIODOS.", "AIODOS error.", "", "", True),
}

# Code par défaut d'un statut, pour une erreur qui n'en donne pas (answer.GatewayError)
BY_STATUS = {400: "invalid_request", 401: "invalid_api_key", 403: "scope_denied",
             404: "model_not_found", 409: "no_machine", 413: "file_too_large",
             415: "unsupported_media_type", 429: "rate_limited", 499: "cancelled",
             502: "engine_failed", 503: "engine_unavailable", 504: "timeout"}


class ApiError(Exception):
    """Refus d'une requête, avec son code stable (CODES). message : à la place de celui du
    code (plus précis) ; detail : données pour l'appli ; retry_after : en secondes."""

    def __init__(self, code: str, message: str | None = None, *, param=None, detail=None,
                 retry_after: int | None = None, status: int | None = None):
        known = CODES.get(code) or CODES["internal_error"]
        super().__init__(message or known[1])
        self.code = code if code in CODES else "internal_error"
        self.status = status or known[0]
        self.custom = message
        self.param = param
        self.detail = detail or {}
        self.retry_after = retry_after


def lang_of(headers) -> str:
    """« en » si Accept-Language le demande d'abord, sinon le français."""
    raw = ""
    if isinstance(headers, dict):
        raw = headers.get(b"accept-language", b"")
        raw = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
    elif headers is not None:
        raw = headers.get("accept-language") or ""
    first = raw.split(",")[0].strip().lower()
    return "en" if first.startswith("en") else "fr"


def _texts(code: str, lang: str):
    status, fr, en, hint_fr, hint_en, retryable = CODES.get(code) or CODES["internal_error"]
    return (en if lang == "en" else fr), (hint_en if lang == "en" else hint_fr), retryable


def body(path: str, error: ApiError, lang: str = "fr") -> dict:
    """Le corps de l'erreur, dans l'enveloppe de la famille de la route."""
    default, hint, retryable = _texts(error.code, lang)
    # Les messages précis (nom du modèle, de la machine…) sont en français : en anglais,
    # celui du code
    message = error.custom if (error.custom and lang == "fr") else default
    if path.startswith("/api/"):
        return {"error": message, "code": error.code, "hint": hint}
    if path.startswith("/mcp"):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": message,
                          "data": {"code": error.code, "hint": hint}}}
    kind = "invalid_request_error" if error.status < 500 else "api_error"
    return {"error": {"message": message, "type": kind, "param": error.param,
                      "code": error.code,
                      "aiodos": {"hint": hint, "retryable": retryable, "detail": error.detail}}}


def response(path: str, error: ApiError, lang: str = "fr") -> JSONResponse:
    headers = {}
    if error.retry_after is not None:
        headers["Retry-After"] = str(int(error.retry_after))
    return JSONResponse(body(path, error, lang), status_code=error.status, headers=headers)


def from_status(status: int, message: str | None = None, code: str | None = None,
                **kw) -> ApiError:
    """L'ApiError d'une erreur qui n'avait qu'un statut (answer.GatewayError…)."""
    code = code or BY_STATUS.get(status) or ("internal_error" if status >= 500 else "invalid_request")
    return ApiError(code, message, status=status, **kw)
