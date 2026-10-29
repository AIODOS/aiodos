"""« Automatique » pour l'audio (docs/conception/audio.md § 3), un par genre : voix, musique,
effets sonores. Comme pour les images (imaging/ranking.py) : le modèle le mieux classé qu'une
machine disponible a et peut faire tourner en ce moment, puis on descend dans le classement.

Les candidats d'un genre (candidates) : les modèles du catalogue (audio/catalog.py), du plus
puissant au plus modeste (rang, puis à rang égal le plus gros) ; pour une voix, seulement ceux
qui parlent la langue du texte.

Le choix (choose), pour chaque candidat dans cet ordre, le premier qui peut se calculer, où que
ce soit ; à modèle égal, la machine la plus proche, puis la plus puissante :
1. ce PC (audio.cpp et le modèle complet ici) ;
2. mes machines en ligne qui l'ont et prêtent leur puissance (audio/lenders.py) ;
3. mes amis qui le proposent, si je l'accepte (images_from_friends).
Le réseau public ne crée pas encore d'audio. Un modèle à licence restrictive n'est pris par
« Automatique » que sur ce PC ou mes machines (sa licence a été lue au téléchargement), ou
partout si sa licence est acceptée ici. Un modèle choisi (preferred) passe d'abord s'il peut se
calculer quelque part ; sinon, « Automatique ».

La suggestion (suggest) : le mieux classé que la carte de ce PC peut faire tourner, pas encore
ici, s'il est meilleur que le meilleur déjà ici.

Le menu (menu, pour l'onglet Audio de views/model-menu.js, par chat/model_menu.audio) a la
forme de celui des images (imaging/ranking.menu), chaque modèle avec son genre, et les trois
genres à part :
    {"auto", "suggest", "models": [{…, "genre", "voices", "seconds"}],
     "genres": [{"genre", "auto", "suggest"}]}
« auto » et « suggest » en tête : ceux de la voix.
"""
import logging
from dataclasses import dataclass

from aiodos import i18n, settings
from aiodos.audio import catalog, files, lenders
from aiodos.audio import engine as audio_engine

log = logging.getLogger("aiodos.audio")

AUTO = "auto"
HERE, MINE, FRIEND = "here", lenders.MINE, lenders.FRIEND


@dataclass(frozen=True)
class Choice:
    model: str | None = None
    where: str | None = None      # HERE, MINE, FRIEND
    machine: str | None = None


def candidates(genre: str, language: str | None = None) -> list:
    """Les modèles de ce genre (qui parlent cette langue, pour une voix), du plus puissant au
    plus modeste."""
    found = [m for m in catalog.of_genre(genre) if catalog.speaks(m, language)]
    return sorted(found, key=lambda m: (catalog.rank(m), -catalog.vram(m), m))


def language_of(language: str | None = None) -> str:
    """La langue d'une voix : celle demandée, sinon celle de l'interface (de la requête)."""
    return (language or i18n.current() or "fr").split("-")[0].lower()


def _here_name() -> str:
    return str(settings.get("node_name") or "")


def runnable_here(model_id: str) -> bool:
    return audio_engine.folder() is not None and files.ready(model_id)


class _Places:
    """Où chaque modèle peut se calculer en ce moment ; les machines lues une seule fois."""

    def __init__(self):
        self._circles = None

    def circles(self) -> dict:
        if self._circles is None:
            try:
                self._circles = lenders.circles()
            except Exception as exc:  # noqa: BLE001 — profils illisibles : personne
                log.warning("Machines de l'audio illisibles : %s", exc)
                self._circles = {}
        return self._circles

    def of(self, model_id: str, automatic: bool = False) -> list:
        """[(cercle, nom)] des machines qui peuvent calculer ce modèle, la plus proche
        d'abord. automatic : une licence restrictive, sur ce PC et mes machines seulement."""
        found = [(HERE, _here_name())] if runnable_here(model_id) else []
        found += [(m["circle"], m["name"]) for m in self.circles().get(model_id, [])]
        if automatic and not catalog.accepted(model_id):
            found = [place for place in found if place[0] in (HERE, MINE)]
        return found


def choose(genre: str, preferred=None, language: str | None = None,
           places: _Places | None = None) -> Choice:
    """Ce que prendrait « Automatique » (ou le modèle choisi) pour ce genre ; Choice() si rien
    ne peut calculer."""
    preferred = None if preferred in (None, "", AUTO) else str(preferred)
    places = places or _Places()
    if preferred in catalog.MODELS and catalog.genre(preferred) == genre:
        where = places.of(preferred)
        if where:
            return Choice(preferred, *where[0])
    for model_id in candidates(genre, language_of(language) if genre == catalog.VOICE else None):
        where = places.of(model_id, automatic=True)
        if where:
            return Choice(model_id, *where[0])
    return Choice()


def fits_here(model_id: str, vram: int) -> bool:
    from aiodos.imaging.ranking import VRAM_SLACK
    return bool(vram) and catalog.vram(model_id) <= vram + VRAM_SLACK


def suggest(genre: str, vram: int | None = None) -> dict | None:
    """Le meilleur modèle de ce genre que ce PC peut faire tourner et n'a pas encore, s'il est
    meilleur que le meilleur qu'il a déjà : {"model", "label", "size", "vram"} ; None sinon."""
    from aiodos.imaging.ranking import vram_here
    vram = vram_here() if vram is None else vram
    if not vram:
        return None
    language = language_of() if genre == catalog.VOICE else None
    found = candidates(genre, language)
    here = [m for m in found if files.ready(m)]
    best = (catalog.rank(here[0]), -catalog.vram(here[0])) if here else None
    for model_id in found:
        if files.ready(model_id) or not fits_here(model_id, vram):
            continue
        if best is not None and (catalog.rank(model_id), -catalog.vram(model_id)) >= best:
            return None
        return {"model": model_id, "label": catalog.label(model_id),
                "size": files.state(model_id, check=False)["missing_bytes"],
                "vram": catalog.vram(model_id)}
    return None


def _auto(choice: Choice) -> dict:
    return {"model": choice.model,
            "label": catalog.label(choice.model) if choice.model else None,
            "where": choice.where, "machine": choice.machine}


def menu(mine_only: bool = False) -> dict:
    """L'onglet Audio du menu des modèles (cf. le début du module). mine_only ne change rien
    tant que le réseau public ne crée pas d'audio."""
    from aiodos.imaging.ranking import vram_here
    places = _Places()
    vram = vram_here()
    engine_here = audio_engine.folder() is not None
    language = language_of()
    models, genres = [], []
    for genre in catalog.GENRES:
        for model_id in candidates(genre):
            where = places.of(model_id)
            state = files.state(model_id, check=False)
            restricted = catalog.restricted(model_id)
            models.append({
                "id": model_id, "label": catalog.label(model_id),
                "rank": catalog.rank(model_id), "kind": "audio", "genre": genre,
                "where": [{"circle": circle, "name": name} for circle, name in where],
                "ready": bool(where),
                "download": None if state["state"] == "ready" else {
                    "size": state["missing_bytes"], "vram": catalog.vram(model_id),
                    "fits_here": fits_here(model_id, vram)},
                "license": catalog.model(model_id)["license"], "restricted": restricted,
                "notice": catalog.notice(model_id) if restricted else None,
                "accepted": catalog.accepted(model_id), "engine": "audiocpp",
                "languages": catalog.languages(model_id),
                "voices": catalog.voices(model_id, language),
                "seconds": catalog.seconds(model_id),
                "default_seconds": catalog.model(model_id).get("default_seconds"),
                "lyrics": catalog.model(model_id).get("lyrics"),
                "experimental": bool(catalog.model(model_id).get("experimental"))})
        genres.append({"genre": genre,
                       "auto": _auto(choose(genre, places=places)),
                       "suggest": suggest(genre, vram) if engine_here else None})
    return {"auto": genres[0]["auto"], "suggest": genres[0]["suggest"], "models": models,
            "genres": genres}
