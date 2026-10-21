"""Les droits d'une application et le lieu de ses calculs (docs/api-applications.md § 4.5 à
4.7).

- Droits par capacité, pas par modèle : chat, embed, libraries, engines:comfyui (les
  travaux ComfyUI, leurs entrées et leurs fichiers). Chaque route de la passerelle en exige un, ou se contente d'une clé valide (catalogues, info, retrait), ou est
  publique (la liste exacte : PUBLIC). Une route inconnue de cette table exige une clé.
- Lieu : des cercles, here (ce PC), mine (mes machines), friends (mes amis), public (les
  inconnus du réseau public). L'appli a ceux qu'on lui a accordés ; une requête peut les
  resserrer (X-Aiodos-Where), jamais les élargir (403 where_denied) ; ne restent que ceux
  que cette version sert pour la capacité (SERVED) et qui ont été montrés à l'accord
  (consents.py). Jamais le cloud.
- Priorité : interactive (par défaut) ou background ; une requête peut la baisser.
"""
from aiodos.gateway.errors import ApiError
from aiodos.i18n import t

SCOPES = ("chat", "embed", "libraries", "engines:comfyui")
CIRCLES = ("here", "mine", "friends", "public")
PRIORITIES = ("interactive", "background")
# Cercles que cette version sert, par capacité : les conversations partent chez mes machines
# et mes amis (prêt de modèles), pas encore chez les inconnus ; embeddings et bibliothèques
# restent sur ce PC ; les travaux ComfyUI se calculent par le ComfyUI de ce PC et de mes
# machines (livraison L2b ; les amis et les inconnus viendront en L3 et L4)
SERVED = {"chat": frozenset({"here", "mine", "friends"}), "embed": frozenset({"here"}),
          "libraries": frozenset({"here"}), "engines:comfyui": frozenset({"here", "mine"})}
# Les routes des travaux de moteur : toutes exigent le droit du moteur
ENGINE_ROUTES = ("/aiodos/v1/engines", "/aiodos/v1/inputs/", "/aiodos/v1/jobs",
                 "/aiodos/v1/files/")

ANY = "any"                 # une clé valide suffit
PUBLIC = "public"           # sans clé
PUBLIC_ROUTES = {("GET", "/"), ("HEAD", "/"), ("GET", "/aiodos/v1/info"),
                 ("HEAD", "/aiodos/v1/info"), ("POST", "/aiodos/v1/access/requests"),
                 ("POST", "/aiodos/v1/access/token"), ("GET", "/aiodos/v1/links/{token}")}
# Un lien vers un fichier se lit sans clé : son jeton tient lieu de clé (gateway/links_api.py)
LINKS = "/aiodos/v1/links/"
_EXACT = {
    "/v1/chat/completions": "chat", "/v1/completions": "chat", "/v1/embeddings": "embed",
    "/api/chat": "chat", "/api/generate": "chat", "/api/show": "chat", "/api/ps": "chat",
    "/api/embed": "embed", "/api/embeddings": "embed",
}


def required(method: str, path: str) -> str:
    """Le droit qu'exige cette route : un nom de SCOPES, ANY ou PUBLIC. La demande d'accès
    porte parfois une clé (droits en plus) : elle reste publique."""
    if (method.upper(), path) in PUBLIC_ROUTES:
        return PUBLIC
    if method.upper() == "GET" and path.startswith(LINKS):
        return PUBLIC
    if path.startswith(ENGINE_ROUTES):
        return "engines:comfyui"
    return _EXACT.get(path, ANY)


def parse_scopes(value) -> list:
    """« chat embed » (ou une liste) → droits connus, dans l'ordre de SCOPES ; un droit
    inconnu : 400."""
    if isinstance(value, str):
        items = value.replace(",", " ").split()
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = value
    else:
        raise ApiError("invalid_request", t("gateway_access.scope_format"), param="scope")
    unknown = [s for s in items if s not in SCOPES]
    if unknown:
        raise ApiError("invalid_request", t("gateway_access.scope_unknown", scope=unknown[0],
                                            known=", ".join(SCOPES)), param="scope")
    return [s for s in SCOPES if s in items]


def parse_where(value, param: str = "where") -> list:
    """Une liste de cercles ou « here,mine » → cercles connus, dans l'ordre de CIRCLES ;
    absent : les quatre ; vide ou inconnu : 400."""
    if value is None:
        return list(CIRCLES)
    if isinstance(value, str):
        items = [v.strip() for v in value.replace(" ", ",").split(",") if v.strip()]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        items = [v.strip() for v in value]
    else:
        raise ApiError("invalid_request", t("gateway_access.where_format"), param=param)
    unknown = [c for c in items if c not in CIRCLES]
    if unknown or not items:
        raise ApiError("invalid_request", t("gateway_access.where_unknown",
                                            circle=unknown[0] if unknown else ""), param=param)
    return [c for c in CIRCLES if c in items]


def where_text(circles) -> str:
    return ",".join(c for c in CIRCLES if c in set(circles))


def effective(granted, header, capability: str, consented=None) -> frozenset:
    """Les cercles où cette requête peut calculer. granted : ceux de l'appli ; header :
    X-Aiodos-Where (None : rien de plus) ; consented : ceux montrés à l'accord pour cette
    capacité (None : pas de restriction, clé générale ou clé à la main)."""
    granted = frozenset(granted)
    if header:
        asked = frozenset(parse_where(header, param="X-Aiodos-Where"))
        wider = asked - granted
        if wider:
            raise ApiError("where_denied", t("gateway_access.where_refused",
                                             circle=sorted(wider)[0]),
                           detail={"allowed": [c for c in CIRCLES if c in granted]})
        granted = asked
    served = SERVED.get(capability, frozenset({"here"}))
    allowed = granted & served
    if consented is not None:
        allowed &= frozenset(consented)
    return allowed


def for_request(request, capability: str):
    """Les cercles permis à cette requête pour cette capacité ; None : pas de filtre (clé
    générale sans X-Aiodos-Where, comme avant les applis). ApiError where_denied."""
    context = getattr(request.state, "aiodos_app", None)
    header = request.headers.get("x-aiodos-where")
    if context is None:
        return None
    if context.kind == "general":
        return effective(context.reach, header, capability) if header else None
    return effective(context.reach, header, capability, context.consented(capability))


def for_job(request, capability: str, asked) -> list:
    """Les cercles d'un travail : ceux de la requête (for_request), resserrés par « where » du
    corps, qui comme X-Aiodos-Where ne peut pas dépasser l'accord (403 where_denied). Un cercle
    accordé que cette version ne sert pas encore est laissé de côté, sans erreur."""
    allowed = for_request(request, capability)
    allowed = set(CIRCLES) if allowed is None else set(allowed)
    if asked is not None:
        wanted = set(parse_where(asked))
        context = getattr(request.state, "aiodos_app", None)
        granted = set(context.reach) if context is not None else set(CIRCLES)
        wider = [c for c in CIRCLES if c in wanted - granted]
        if wider:
            raise ApiError("where_denied", t("gateway_access.where_refused", circle=wider[0]),
                           detail={"allowed": [c for c in CIRCLES if c in granted]})
        allowed &= wanted
    return [c for c in CIRCLES if c in allowed]


def priority_of(request) -> str:
    """interactive ou background pour cette requête."""
    context = getattr(request.state, "aiodos_app", None)
    return priority(context.priority if context is not None else "interactive",
                    request.headers.get("x-aiodos-priority"))


def has(request, scope: str) -> bool:
    context = getattr(request.state, "aiodos_app", None)
    return context is not None and scope in context.scopes


def priority(app_priority: str, header) -> str:
    """La priorité de la requête : celle de l'appli, baissée par X-Aiodos-Priority."""
    if str(header or "").strip().lower() == "background":
        return "background"
    return app_priority if app_priority in PRIORITIES else "interactive"
