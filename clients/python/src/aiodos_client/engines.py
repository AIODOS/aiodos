# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Les moteurs vus par l'appli : `GET /aiodos/v1/engines` (docs/api-applications.md § 5.7),
filtrés par AIODOS selon les cercles accordés à l'appli.

- `Engine.here` : le moteur de ce PC (`enabled`, `version`, `vram_mib`), accordé ou non dans
  Réglages → Moteurs des applications ;
- `Engine.machines` : combien de machines de chaque cercle l'ont (`None` : inconnu d'avance,
  ou cercle pas encore servi) ;
- `Engine.models` : ses modèles, par dossier et par nom (`EngineModel`), avec où ils sont.

Cette version d'AIODOS calcule les travaux ComfyUI sur le ComfyUI de ce PC (`here`) ; mes
machines suivront.
"""

from dataclasses import dataclass
from dataclasses import field

from . import errors

FEATURE = "engines"


@dataclass(frozen=True)
class EngineModel:
    """Un modèle d'un moteur : son dossier (« checkpoints »…), son nom, et où il est."""
    folder: str | None
    name: str
    sha256: str | None = None
    size: int | None = None
    where: dict = field(default_factory=dict, compare=False, hash=False)
    license: object = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class Engine:
    """Un moteur (« comfyui ») des machines permises."""
    id: str
    label: str
    here: dict | None = field(default=None, compare=False, hash=False)
    machines: dict = field(default_factory=dict, compare=False, hash=False)
    models: tuple = ()
    object_info: dict | None = field(default=None, compare=False, hash=False)
    recipes: int | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def enabled(self) -> bool:
        """Le moteur de ce PC est-il accordé à AIODOS et ouvert ?"""
        return isinstance(self.here, dict) and self.here.get("enabled") is True

    def has_model(self, name, folder=None) -> bool:
        """Ce modèle est-il connu des machines permises (son nom, et son dossier s'il est
        donné) ? Les séparateurs « \\ » et « / » se valent."""
        wanted = str(name).replace("\\", "/")
        return any(model.name.replace("\\", "/") == wanted
                   and (folder is None or model.folder == folder) for model in self.models)


class EngineList(list):
    """La liste des moteurs, avec `by_id`."""

    @property
    def by_id(self) -> dict:
        """Les moteurs par identifiant : `engines.by_id["comfyui"]`."""
        return {engine.id: engine for engine in self}


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value):
    return dict(value) if isinstance(value, dict) else None


def model_from(entry: dict) -> EngineModel:
    return EngineModel(folder=entry.get("folder") if isinstance(entry.get("folder"), str)
                       else None, name=str(entry.get("name")),
                       sha256=entry.get("sha256") if isinstance(entry.get("sha256"), str)
                       else None, size=_int(entry.get("size")),
                       where=_dict(entry.get("where")) or {}, license=entry.get("license"))


def engine_from(entry: dict) -> Engine:
    """Une fiche de `GET /aiodos/v1/engines` en Engine."""
    models = tuple(model_from(m) for m in entry.get("models") or ()
                   if isinstance(m, dict) and isinstance(m.get("name"), str))
    return Engine(id=str(entry["id"]), label=str(entry.get("label") or entry["id"]),
                  here=_dict(entry.get("here")), machines=_dict(entry.get("machines")) or {},
                  models=models, object_info=_dict(entry.get("object_info")),
                  recipes=_int(entry.get("recipes")), raw=dict(entry))


def list_engines(client) -> EngineList:
    """Les moteurs des machines permises."""
    data = client._call("GET", "/aiodos/v1/engines", feature=FEATURE)
    entries = data.get("engines") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise errors.make("protocol", client.lang)
    return EngineList(engine_from(entry) for entry in entries
                      if isinstance(entry, dict) and isinstance(entry.get("id"), str))
