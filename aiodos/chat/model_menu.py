"""Le menu des modèles (docs/conception/modeles-auto.md § 4 ; GET /api/models/menu,
api/routes/model_menu.py) : pour chaque genre, texte, images, vidéos et audio, ce que prendrait
« Automatique » en ce moment, la suggestion du meilleur modèle pour cette machine, et les
modèles, du plus puissant au plus modeste, avec où chacun peut tourner (ici, mes machines, mes
amis, le réseau public) et, s'il n'est pas sur ce PC, ce qu'il demande pour y être téléchargé.
L'interface : views/model-menu.js, sous la saisie et dans Réglages → Modèles.

Chaque genre a la même forme :

    {"auto": {"model", "label", "where", "machine"},
     "suggest": {"model", "label", "size", "vram"} | None,
     "models": [{"id", "label", "rank", "kind", "where": [{"circle", "name"}], "ready",
                 "download": {"size", "vram", "fits_here"} | None, "license", "restricted",
                 "notice"}]}

where (« auto ») et circle : « here » (cette machine), « mine », « friend » ou « public » ;
« auto » vaut {"model": None…} quand rien ne peut servir. size en octets, vram (la mémoire de la
carte graphique demandée) en Mio, comme imaging/ranking.py. L'identifiant d'un modèle est celui à
envoyer : pour le texte, « auto:<modèle> » (la machine se choisit seule), « peer:… » (un modèle
qu'une seule machine prête, sans mode automatique), « reparti:… » (gros modèle réparti, « split ») ;
pour les images et les vidéos, celui du catalogue (ou d'une recette de ComfyUI, « engine »).

- Texte : construit ici, d'après les modèles de cette machine et de mes machines
  (pipeline.own_models), ceux du réseau public qu'une autre machine peut contrôler
  (placement.public_offers), et ceux qui s'installent (services/model_install.py). « Automatique »
  dit ce que choisirait network/placement.py (« auto:best ») : le plus puissant de mes modèles et
  la machine la mieux placée, sans le réseau public qui ne passe devant que d'après ses mesures ;
  sans modèle à moi, le meilleur du réseau public.
- Images et vidéos : imaging/ranking.py (menu, avec « Sur mes machines seulement »), qui classe
  aussi les recettes de ComfyUI et mes workflows ; sans lui (une version d'avant, ou s'il
  échoue), la même forme tirée de GET /api/images/models (api/routes/images._models), dans
  l'ordre où le nœud choisissait (chat/creations.usable : les licences restrictives en dernier),
  les recettes prêtes (comfy_creations.choices) ensuite : le menu remplace la liste du mode
  « Créer ».
- Audio : audio/ranking.py (menu), la même forme, chaque modèle avec son genre (voix, musique,
  effets sonores) ; en plus, « genres » : pour chacun, ce que prendrait « Automatique » et la
  suggestion (docs/conception/audio.md).

« Sur mes machines seulement » (mine_only) : rien du réseau public. Rien n'est téléchargé, essayé
ni noté ici (placement.best_public sans note) ; un genre illisible (moteur arrêté…) reste vide
sans faire tomber les autres.
"""
import importlib
import inspect
import logging

from aiodos import settings
from aiodos.chat import pipeline, public_consent
from aiodos.i18n import t
from aiodos.network import peers, placement, public_chat, sharing

log = logging.getLogger("aiodos.chat")

KINDS = ("text", "image", "video", "audio")
# Le cercle d'une machine candidate (placement.Candidate.trust) dans le menu
CIRCLES = {"local": "here", "mine": "mine", "friend": "friend", "public": "public"}
NOBODY = {"model": None, "label": None, "where": None, "machine": None}
MIB = 2 ** 20                     # « vram » en Mio ; « size » en octets


def empty() -> dict:
    return {"auto": dict(NOBODY), "suggest": None, "models": []}


def menu(executor=None, mine_only: bool = False) -> dict:
    """{"text": …, "image": …, "video": …, "audio": …} (cf. le début du module)."""
    found = {}
    for kind, build in (("text", lambda: text(executor, mine_only)),
                        ("image", lambda: media("image", mine_only)),
                        ("video", lambda: media("video", mine_only)),
                        ("audio", lambda: audio(mine_only))):
        try:
            found[kind] = build()
        except Exception as exc:              # un genre illisible n'empêche pas les autres
            log.warning("Menu des modèles (%s) illisible : %s", kind, exc)
            found[kind] = empty()
    return found


def audio(mine_only: bool = False) -> dict:
    """L'audio (voix, musique, effets sonores) : audio/ranking.menu, chaque modèle avec son
    genre, et « Automatique » pour chacun des trois genres (« genres »)."""
    from aiodos.audio import ranking as audio_ranking
    return audio_ranking.menu(mine_only)


def _here() -> str:
    return t("model_menu.here")


def _public() -> dict:
    return {"circle": "public", "name": t("model_menu.public")}


# ------------------------------------------------------------------------ texte
def text(executor=None, mine_only: bool = False) -> dict:
    """Le menu du texte (cf. le début du module)."""
    open_ = public_consent.closed(None, mine_only) is None
    items = {}

    def item(name, size):
        found = items.get(name)
        if found is None:
            found = items[name] = {"id": None, "label": name, "kind": "text", "where": [],
                                   "ready": True, "download": None, "license": None,
                                   "restricted": False, "notice": None, "size": 0}
        found["size"] = max(found["size"], size or 0)
        return found

    own = pipeline.own_models()
    for m in own:
        group = m.get("group")
        if group == "auto":
            item(m["label"], m.get("size"))["id"] = m["id"]
        elif group == "local":
            item(m["label"], m.get("size"))["where"].insert(0, {"circle": "here",
                                                                "name": _here()})
        elif group == "remote":
            found = item(m["label"], m.get("size"))
            found["where"].append({"circle": "friend" if m.get("trust") == "friend" else "mine",
                                   "name": m.get("peer") or ""})
            found["first"] = found.get("first") or m["id"]
    models = []
    for name, found in items.items():
        # Sans mode automatique (prêté seulement par un ami…) : la machine qui le prête
        found["id"] = found["id"] or found.pop("first", None) or placement.auto_id(name)
        found.pop("first", None)
        models.append(found)
    models += _split(own)
    if open_:
        for name in placement.public_offers():
            known = next((m for m in models if placement.same_model(m["label"], name)
                          or placement.same_model(name, m["label"])), None)
            if known is not None:
                known["where"].append(_public())
            else:
                found = item(name, 0)
                found.update(id=placement.auto_id(name), where=[_public()])
                models.append(found)
    offered, suggest = _installable({m["label"] for m in models})
    models.sort(key=lambda m: (-placement.power(m["label"], m["size"]), m["label"]))
    models += offered
    for rank, m in enumerate(models, 1):
        m["rank"] = rank
        m.pop("size", None)
    return {"auto": _text_auto(executor, open_), "suggest": suggest, "models": models}


def _split(own: list) -> list:
    """Les gros modèles répartis entre mes machines (pipeline.own_models, « cluster »)."""
    found = []
    for m in own:
        if m.get("group") != "cluster":
            continue
        where = [{"circle": "here", "name": _here()}]
        where += [{"circle": "mine", "name": name} for name in m.get("machines") or []]
        found.append({"id": m["id"], "label": m["label"], "kind": "text", "where": where,
                      "ready": True, "download": None, "license": None, "restricted": False,
                      "notice": None, "split": True, "size": m.get("size") or 0})
    return found


def _installable(have: set) -> tuple:
    """(modèles à installer sur ce PC, suggestion) d'après les conseils de
    services/model_install.py : ceux qui ne sont pas déjà là, le plus puissant d'abord ; la
    suggestion, le modèle conseillé pour cette machine s'il n'y est pas."""
    from aiodos.services import model_install
    try:
        found = model_install.suggestions()
    except Exception as exc:
        log.warning("Modèles à installer illisibles : %s", exc)
        return [], None
    offered, suggest = [], None
    for spec in found.get("models") or []:
        name = spec["name"]
        if spec.get("kind") == "embedding" or spec.get("installed") or name in have \
                or any(placement.same_model(h, name) for h in have):
            continue
        # La mémoire demandée : à peu près ses fichiers (en Mio, comme imaging/ranking.py)
        vram = spec["size"] // MIB
        entry = {"id": name, "label": name, "kind": "text", "where": [], "ready": False,
                 "download": {"size": spec["size"], "vram": vram,
                              "fits_here": spec.get("fits") != "heavy"},
                 "license": spec.get("license"), "restricted": False, "notice": None,
                 "size": spec["size"]}
        offered.append(entry)
        if spec.get("recommended"):
            suggest = {"model": name, "label": name, "size": spec["size"], "vram": vram}
    offered.sort(key=lambda m: (-placement.power(m["label"], m["size"]), m["label"]))
    return offered, suggest


def _text_auto(executor, open_: bool) -> dict:
    """Ce que prendrait « Automatique » (placement.BEST) en ce moment : le plus puissant de mes
    modèles et la machine la mieux placée ; sans modèle à moi, le meilleur du réseau public, si
    la conversation peut y partir. Rien n'est noté ni essayé."""
    name = placement.best_name()
    if name is not None:
        found = placement.candidates(name, executor)
        first = found[0] if found else None
        return {"model": name, "label": name,
                "where": CIRCLES.get(first.trust) if first else None,
                "machine": first.machine if first else None}
    if open_:
        best = placement.best_public(offer=False)
        if best:
            chosen = public_chat.parse(best[0].model)[1]
            return {"model": chosen, "label": chosen, "where": "public",
                    "machine": t("model_menu.public")}
    return dict(NOBODY)


# ------------------------------------------------------------ images et vidéos
def _ranking():
    """imaging/ranking.menu, s'il existe (un autre travail l'écrit), ou None."""
    try:
        module = importlib.import_module("aiodos.imaging.ranking")
    except ImportError:
        return None
    return getattr(module, "menu", None)


def media(kind: str, mine_only: bool = False) -> dict:
    """Le menu des images ou des vidéos (cf. le début du module) : celui du classement (qui
    classe aussi les recettes de ComfyUI), sinon celui du catalogue, les recettes ensuite."""
    found, takes = None, False
    ranked = _ranking()
    if ranked is not None:
        try:
            takes = "mine_only" in inspect.signature(ranked).parameters
            found = ranked(kind, mine_only=mine_only) if takes else ranked(kind)
        except Exception as exc:
            log.warning("Classement des modèles (%s) illisible : %s", kind, exc)
    if isinstance(found, dict) and takes:
        return found              # « Sur mes machines seulement » et licences : déjà dans ranking
    if not isinstance(found, dict):
        found = _with_comfy(_from_catalog(kind), kind)
    return _without_public(found) if mine_only else found


def _lenders() -> dict:
    """{modèle : [{"circle", "name"}]} : mes machines en ligne qui calculent ce modèle d'images
    pour moi, et mes amis si je l'accepte (images_from_friends) : imaging/lenders.helpers, avec
    le cercle de chacune."""
    friends = bool(settings.get("images_from_friends"))
    found = {}
    for peer in peers.list_peers():
        mine = sharing.level(peer["node_id"]) == sharing.MINE
        if not peer["online"] or not (mine or friends):
            continue
        images = (peer.get("profile") or {}).get("images") or {}
        if not isinstance(images, dict) or not images.get("engine") or not images.get("shares"):
            continue
        for model_id in images.get("models") or []:
            if isinstance(model_id, str):
                found.setdefault(model_id, []).append(
                    {"circle": "mine" if mine else "friend", "name": peer["name"]})
    return found


def _from_catalog(kind: str) -> dict:
    """La forme du menu tirée de GET /api/images/models, sans classement : l'ordre du
    catalogue, les licences restrictives en dernier (chat/creations.usable, ce que le nœud
    choisit sans modèle demandé)."""
    from aiodos.api.routes import images as image_routes
    info = image_routes._models()
    engine = bool(info["engine"]["found"])
    lenders = _lenders()
    listed = [m for m in info["models"] if (m.get("kind") or "image") == kind]
    listed.sort(key=lambda m: bool(m.get("notice")))
    models = []
    for rank, m in enumerate(listed, 1):
        here = engine and m.get("state") == "ready"
        where = ([{"circle": "here", "name": _here()}] if here else []) + lenders.get(m["id"], [])
        if m.get("public"):
            where.append(_public())
        models.append({
            "id": m["id"], "label": m["label"], "rank": rank, "kind": kind, "where": where,
            "ready": bool(where),
            "download": None if m.get("state") == "ready" else {
                "size": m.get("missing_bytes") or m.get("size"), "vram": None, "fits_here": None},
            "license": m.get("license"), "restricted": bool(m.get("notice")),
            "notice": m.get("notice")})
    return {"auto": _media_auto(models), "suggest": None, "models": models}


def _media_auto(models: list) -> dict:
    """Le premier modèle qui peut calculer ici ou chez mes machines (ou mes amis), sinon chez
    une machine du réseau public."""
    for public in (False, True):
        for m in models:
            where = [w for w in m.get("where") or []
                     if (w.get("circle") == "public") == public]
            if m.get("ready") and where:
                return {"model": m["id"], "label": m["label"], "where": where[0]["circle"],
                        "machine": where[0]["name"]}
    return dict(NOBODY)


def _with_comfy(found: dict, kind: str) -> dict:
    """Les recettes de ComfyUI et mes workflows qui peuvent calculer ce genre (ici ou chez mes
    machines), après les modèles du catalogue ; « Automatique » les prend quand rien d'autre ne
    peut calculer (chat/creations.prepare)."""
    from aiodos.chat import comfy_creations
    try:
        recipes = comfy_creations.choices(kind)
    except Exception as exc:
        log.warning("Recettes de ComfyUI illisibles : %s", exc)
        recipes = []
    models = list(found.get("models") or [])
    known = {m.get("id") for m in models}
    rank = max((m.get("rank") or 0 for m in models), default=0)
    for r in recipes:
        if r["id"] in known:
            continue
        rank += 1
        where = ([{"circle": "here", "name": _here()}] if r.get("here") else []) \
            + [{"circle": "mine", "name": name} for name in r.get("helpers") or []]
        models.append({"id": r["id"], "label": r["label"], "rank": rank, "kind": kind,
                       "where": where, "ready": bool(where), "download": None, "license": None,
                       "restricted": False, "notice": None, "engine": "comfyui"})
    auto = found.get("auto") or dict(NOBODY)
    first = next((m for m in models if m.get("engine") == "comfyui" and m["ready"]), None)
    if (auto.get("model") is None or auto.get("where") == "public") and first is not None:
        auto = {"model": first["id"], "label": first["label"],
                "where": first["where"][0]["circle"], "machine": first["where"][0]["name"]}
    return {**found, "auto": auto, "models": models}


def _without_public(found: dict) -> dict:
    """« Sur mes machines seulement » : rien du réseau public (ni où, ni « Automatique »)."""
    models = []
    for m in found.get("models") or []:
        where = [w for w in m.get("where") or [] if w.get("circle") != "public"]
        models.append({**m, "where": where, "ready": bool(m.get("ready")) and bool(where)})
    auto = found.get("auto") or dict(NOBODY)
    if auto.get("where") == "public":
        auto = _media_auto(models)
    return {**found, "auto": auto, "models": models}
