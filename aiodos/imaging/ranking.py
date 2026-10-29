"""« Automatique » pour les images et les vidéos (docs/conception/modeles-auto.md § 2) : le
modèle le plus puissant qu'une machine disponible a et peut faire tourner en ce moment, puis
on descend dans le classement.

Les candidats d'un genre (image, vidéo), du plus puissant au plus modeste (candidates) : les
modèles du catalogue (imaging/catalog.py : rang, puis à rang égal la plus grosse variante), et
les recettes de ComfyUI et mes workflows (chat/comfy_creations.py), classés avec eux selon leur
modèle : celui du catalogue dont ils chargent le cœur (la recette Wan 2.1 de ComfyUI a le rang
de Wan 2.1), ou OTHER_FILES (SDXL, sous tout le catalogue) ; un workflow inconnu en dernier
(UNRANKED). À rang égal, stable-diffusion.cpp avant ComfyUI.

Le choix (choose), pour chaque candidat dans cet ordre, le premier qui peut se calculer, où que
ce soit ; à modèle égal, la machine la plus proche, puis la plus puissante :
1. ce PC (stable-diffusion.cpp et le modèle complet ici ; une recette que son ComfyUI sait
   calculer) ;
2. mes machines en ligne qui l'ont et prêtent leur puissance (la plus grosse carte d'abord :
   lenders.circles) ;
3. mes amis qui le proposent, si je l'accepte (images_from_friends ; jamais une recette) ;
4. le réseau public (une machine qui le prête, même sans place en ce moment : l'unité attend
   son tour), si la création peut partir chez un inconnu (public_consent.closed : réglages,
   ni « Sur mes machines seulement », ni image jointe) et que j'ai accepté d'y faire créer mes
   images et vidéos (public_images : True ; jamais une recette).
Un modèle à licence restrictive (catalog.restricted) n'est pris par « Automatique » que sur ce
PC ou mes machines (je l'ai téléchargé, donc sa licence a été lue et acceptée), ou partout si
sa licence est acceptée ici (catalog.accepted : choisi une fois dans le menu, licence lue) ;
sinon, chez un ami ou un inconnu, seulement si je le choisis moi-même.
L'accord du réseau public reste demandé une fois : tant qu'il ne l'est pas (public_images :
None), le réseau public ne compte pas, et seulement si rien d'autre ne peut calculer, le choix
dit ce que le réseau public créerait (ask : la carte d'accord, chat/public_creations.py).
Un modèle choisi (preferred, ni vide ni « auto ») passe d'abord s'il peut se calculer quelque
part (ce même ordre des machines) ; sinon, « Automatique ».

La suggestion (suggest) : le mieux classé que la carte graphique de ce PC peut faire tourner
(catalog.vram, vram_here), pas encore ici, s'il est meilleur que le meilleur modèle déjà ici.

Le menu (menu, pour views/model-menu.js par chat/model_menu.py) :
    {"auto": {"model", "label", "where", "machine"},
     "suggest": {"model", "label", "size", "vram"} | None,
     "models": [{"id", "label", "rank", "kind", "where": [{"circle", "name"}], "ready",
                 "download": {"size", "vram", "fits_here"} | None, "license", "restricted",
                 "notice", "engine"}]}
du plus puissant au plus modeste ; size en octets (ce qui manque ici), vram en Mio ; circle et
where : HERE, MINE, FRIEND, PUBLIC ; name : celui de la machine (None pour un inconnu). Textes
dans la langue en cours (celle de la requête).
"""
import logging
import threading
import time
from dataclasses import dataclass

from aiodos import settings
from aiodos.imaging import catalog, lenders

log = logging.getLogger("aiodos.images")

AUTO = "auto"                     # un modèle demandé ainsi (ou vide) : « Automatique »
HERE, MINE, FRIEND, PUBLIC = "here", lenders.MINE, lenders.FRIEND, "public"
SDCPP, COMFYUI = "sdcpp", "comfyui"
UNRANKED = 999                    # le rang d'un workflow dont on ne connaît pas le modèle
# Les cœurs que chargent des recettes de ComfyUI sans être au catalogue : leur rang dans leur
# genre. SDXL 1.0 : sous tous les modèles d'images du catalogue (au classement d'Artificial
# Analysis, plus bas que FLUX.1 dev, 823 ; FLUX.2 [klein] 4B, le dernier ici, a le rang 6)
OTHER_FILES = {"sd_xl_base_1.0.safetensors": ("image", 7)}
# Une carte « 8 Go » en dit 8 188 Mio : la mémoire demandée est comparée avec cette marge
VRAM_SLACK = 512
VRAM_TTL = 600                    # la carte de ce PC ne change guère : relue toutes les 10 min

_vram = {"at": 0.0, "value": 0}
_vram_lock = threading.Lock()


@dataclass(frozen=True)
class Candidate:
    id: str
    kind: str
    rank: int
    quality: int                  # à rang égal, la plus grosse variante d'abord (vram, Mio)
    engine: str                   # SDCPP ou COMFYUI
    label: str


@dataclass(frozen=True)
class Choice:
    """Ce que prendrait « Automatique » (ou le modèle choisi)."""
    model: str | None = None
    where: str | None = None      # HERE, MINE, FRIEND, PUBLIC
    machine: str | None = None    # son nom (None pour un inconnu)
    engine: str = SDCPP
    ask: bool = False             # le réseau public, l'accord pas encore demandé (la carte)


# ------------------------------------------------------------------ le classement
def _order(c: Candidate) -> tuple:
    return c.rank, c.engine != SDCPP, -c.quality, c.id


def _kind_of(spec: dict) -> str:
    return spec.get("kind", "image")


def ordered(model_ids) -> list:
    """Des modèles du catalogue, du plus puissant au plus modeste."""
    return sorted(model_ids, key=lambda m: (catalog.rank(m), -catalog.vram(m), m))


def _recipe_rank(owner: dict, kind: str) -> int:
    """Le rang d'une recette de ComfyUI (ou d'un workflow) : celui du modèle du catalogue
    dont elle charge le cœur, ou OTHER_FILES ; UNRANKED sinon."""
    names = {m.get("name") for m in owner.get("models") or [] if isinstance(m, dict)}
    ranks = [spec.get("rank") or UNRANKED for spec in catalog.MODELS.values()
             if _kind_of(spec) == kind and names & {
                 f["name"] for part, f in spec["files"].items()
                 if part in ("diffusion", "model", "high_noise")}]
    ranks += [rank for name, (genre, rank) in OTHER_FILES.items()
              if name in names and genre == kind]
    return min(ranks, default=UNRANKED)


def _comfy_candidates(kind: str) -> list:
    from aiodos.chat import comfy_creations
    found = []
    try:
        for model_id, owner in comfy_creations._owners():
            if owner.get("kind", "image") == kind:
                found.append(Candidate(model_id, kind, _recipe_rank(owner, kind), 0, COMFYUI,
                                       comfy_creations.label(owner)))
    except Exception as exc:  # noqa: BLE001 — recettes illisibles : le catalogue seul
        log.warning("Recettes de ComfyUI illisibles pour le classement : %s", exc)
    return found


def candidates(kind: str, comfy: bool = True) -> list:
    """[Candidate] de ce genre, du plus puissant au plus modeste (cf. le début du module)."""
    found = [Candidate(m, kind, catalog.rank(m), catalog.vram(m), SDCPP, catalog.label(m))
             for m, spec in catalog.MODELS.items() if _kind_of(spec) == kind]
    if comfy:
        found += _comfy_candidates(kind)
    return sorted(found, key=_order)


# ------------------------------------------------------------------ où il peut tourner
class _Places:
    """Où chaque candidat peut se calculer en ce moment ; chaque source lue une seule fois,
    au premier candidat qui en a besoin (les recettes de ComfyUI interrogent son ComfyUI)."""

    def __init__(self, kind: str, public: bool, comfy: bool = True):
        self.kind, self.public, self.comfy = kind, public, comfy
        self._circles = self._offered = self._recipes = None

    def circles(self) -> dict:
        if self._circles is None:
            self._circles = lenders.circles()
        return self._circles

    def offered(self) -> list:
        if self._offered is None:
            from aiodos.chat import public_creations
            try:
                self._offered = public_creations.offered(self.kind)
            except Exception as exc:  # noqa: BLE001 — annuaire illisible : personne
                log.warning("Réseau public illisible pour le classement : %s", exc)
                self._offered = []
        return self._offered

    def recipes(self) -> dict:
        if self._recipes is None:
            from aiodos.chat import comfy_creations
            try:
                self._recipes = {r["id"]: r for r in comfy_creations.choices(self.kind)}
            except Exception as exc:  # noqa: BLE001 — ComfyUI illisible : aucune recette
                log.warning("ComfyUI illisible pour le classement : %s", exc)
                self._recipes = {}
        return self._recipes

    def of(self, c: Candidate, automatic: bool = False) -> list:
        """[(cercle, nom)] : les machines qui peuvent calculer ce candidat, la plus proche
        d'abord. automatic : une licence restrictive, sur ce PC et mes machines seulement."""
        if c.engine == COMFYUI:
            recipe = self.recipes().get(c.id) if self.comfy else None
            if not recipe:
                return []
            return ([(HERE, _here())] if recipe.get("here") else []) \
                + [(MINE, name) for name in recipe.get("helpers") or []]
        from aiodos.jobs.kinds import images as kind
        found = [(HERE, _here())] if kind.runnable_here(c.id) else []
        found += [(m["circle"], m["name"]) for m in self.circles().get(c.id, [])]
        if self.public and c.id in self.offered():
            found.append((PUBLIC, None))
        if automatic and not catalog.accepted(c.id):
            found = [place for place in found if place[0] in (HERE, MINE)]
        return found


def _here() -> str:
    return str(settings.get("node_name") or "")


def _public_open(mine_only: bool, pictures=()) -> bool:
    """La création peut-elle partir chez un inconnu (public_consent.closed) ?"""
    from aiodos.chat import public_consent
    return public_consent.closed(None, mine_only, pictures) is None


# ------------------------------------------------------------------ le choix
def choose(kind: str, preferred=None, mine_only: bool = False, pictures=(), *,
           comfy: bool = True, public: bool = True) -> Choice:
    """Ce que prendrait « Automatique » (cf. le début du module) ; preferred : le modèle
    choisi (vide ou « auto » : aucun) ; pictures : les images jointes au message (ni le
    réseau public, ni une recette de ComfyUI n'en prennent) ; comfy, public : faux, ni les
    recettes, ni le réseau public. Choice() si rien ne peut calculer."""
    preferred = None if preferred in (None, "", AUTO) else str(preferred)
    open_ = public and _public_open(mine_only, pictures)
    consent = settings.get("public_images") if open_ else False
    comfy = comfy and not pictures
    places = _Places(kind, open_ and consent is True, comfy)
    found = candidates(kind, comfy)
    if preferred:
        for c in found:
            if c.id == preferred:
                where = places.of(c)
                if where:
                    return Choice(c.id, *where[0], engine=c.engine)
                break
    for c in found:
        where = places.of(c, automatic=True)
        if where:
            return Choice(c.id, *where[0], engine=c.engine)
    if open_ and consent is None:
        # Rien ailleurs, l'accord pas encore demandé : ce que le réseau public créerait
        asked = _Places(kind, True, False)
        offered = asked.offered()
        if preferred in offered:
            return Choice(preferred, PUBLIC, None, ask=True)
        for c in found:
            if c.engine == SDCPP and c.id in offered and catalog.accepted(c.id):
                return Choice(c.id, PUBLIC, None, ask=True)
    return Choice()


def public_choices(offered) -> list:
    """Les modèles qu'un inconnu prête (offered), ceux qu'« Automatique » peut lui demander :
    du plus puissant au plus modeste, sans licence restrictive qui ne soit acceptée ici."""
    return ordered(m for m in offered if m in catalog.MODELS and catalog.accepted(m))


# ------------------------------------------------------------------ la carte de ce PC
def vram_here(max_age=VRAM_TTL) -> int:
    """La mémoire (Mio) de la carte graphique de ce PC : la plus grosse que voit llama.cpp
    (--list-devices, déjà lu pour le profil), sinon nvidia-smi ; 0 sans carte connue."""
    with _vram_lock:
        if _vram["at"] and time.monotonic() - _vram["at"] < max_age:
            return _vram["value"]
    value = 0
    try:
        from aiodos.cluster import engine as llama
        found = llama.info()
        cards = [d for d in (found or {}).get("devices") or []
                 if d["name"].upper() != "CPU" and d.get("total_mib")]
        best = llama.best_device(cards) if cards else None
        value = int(best["total_mib"]) if best else 0
    except Exception as exc:  # noqa: BLE001 — llama.cpp absent ou illisible
        log.debug("Cartes de llama.cpp illisibles : %s", exc)
    if not value:
        try:
            from aiodos.node import identity
            value = max((g["vram_total"] for g in identity.gpus()), default=0) // 2 ** 20
        except Exception as exc:  # noqa: BLE001 — sans nvidia-smi
            log.debug("nvidia-smi illisible : %s", exc)
    with _vram_lock:
        _vram.update(at=time.monotonic(), value=value)
    return value


def fits_here(model_id: str, vram: int | None = None) -> bool:
    """La carte de ce PC peut-elle faire tourner ce modèle (catalog.vram, à VRAM_SLACK près) ?"""
    vram = vram_here() if vram is None else vram
    return bool(vram) and catalog.vram(model_id) <= vram + VRAM_SLACK


def suggest(kind: str, vram: int | None = None) -> dict | None:
    """Le meilleur modèle de ce genre que ce PC peut faire tourner et n'a pas encore, s'il
    est meilleur que le meilleur qu'il a déjà : {"model", "label", "size" (octets à
    télécharger), "vram" (Mio)} ; None sinon."""
    vram = vram_here() if vram is None else vram
    if not vram:
        return None
    found = candidates(kind, comfy=False)
    here = [c for c in found if catalog.ready(c.id)]
    best = _order(here[0]) if here else None
    for c in found:
        if catalog.ready(c.id) or not fits_here(c.id, vram):
            continue
        if best is not None and _order(c) >= best:
            return None
        return {"model": c.id, "label": c.label,
                "size": catalog.state(c.id, check=False)["missing_bytes"],
                "vram": catalog.vram(c.id)}
    return None


# ------------------------------------------------------------------ le menu
def _circles(where: list) -> list:
    return [{"circle": circle, "name": name} for circle, name in where]


def _licenses(c: Candidate) -> str | None:
    """La licence d'un modèle du catalogue ; celles des fichiers d'une recette de ComfyUI."""
    if c.engine == SDCPP:
        return catalog.model(c.id)["license"]
    from aiodos.chat import comfy_creations
    try:
        owner = comfy_creations._owner(c.id)
    except Exception:  # noqa: BLE001 — disparue entre-temps
        return None
    names = sorted({m["license"] for m in owner.get("models") or []
                    if isinstance(m, dict) and m.get("license")})
    return ", ".join(names) or None


def menu(kind: str, mine_only: bool = False) -> dict:
    """Le menu de ce genre (cf. le début du module). mine_only : « Sur mes machines
    seulement », rien du réseau public."""
    open_ = _public_open(mine_only)
    # Le réseau public compte s'il n'est pas refusé : pas encore demandé, la carte viendra
    places = _Places(kind, open_ and settings.get("public_images") is not False)
    vram = vram_here()
    models = []
    for c in candidates(kind):
        where = places.of(c)
        if c.engine == COMFYUI:
            if not where:
                continue              # une recette ne se télécharge pas d'ici
            download = None
        else:
            download = None if catalog.ready(c.id) else {
                "size": catalog.state(c.id, check=False)["missing_bytes"],
                "vram": catalog.vram(c.id), "fits_here": fits_here(c.id, vram)}
        restricted = c.engine == SDCPP and catalog.restricted(c.id)
        models.append({"id": c.id, "label": c.label, "rank": c.rank, "kind": kind,
                       "where": _circles(where), "ready": bool(where), "download": download,
                       "license": _licenses(c), "restricted": restricted,
                       "notice": catalog.notice(c.id) if restricted else None,
                       "accepted": not restricted or catalog.accepted(c.id),
                       "engine": c.engine})
    chosen = choose(kind, None, mine_only)
    label = next((m["label"] for m in models if m["id"] == chosen.model), None)
    # Sans stable-diffusion.cpp ici (l'appli Android), rien à télécharger pour ce PC
    from aiodos.imaging import engine
    return {"auto": {"model": chosen.model, "label": label, "where": chosen.where,
                     "machine": chosen.machine},
            "suggest": suggest(kind, vram) if engine.folder() is not None else None,
            "models": models}
