# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Le catalogue des modèles : `GET /aiodos/v1/models?kind=chat|embed` (docs/api-applications.md
§ 5.2), filtré par AIODOS selon les droits et le lieu de calcul de l'appli.

- `Model.id` se donne tel quel à chat() ou embed() : AIODOS choisit alors la machine ;
  `Model.where[].id` désigne une machine précise.
- `tools` et `vision` valent vrai si **ce PC** les sert (une machine appairée ne reçoit en
  v1 qu'une conversation en texte) ; `context` est le contexte réel du moteur de ce PC.
- `default` : le modèle qu'AIODOS prend lui-même (ses conversations, ses bibliothèques) ;
  `best()` le préfère, et embed() sans modèle le prend.
"""

from dataclasses import dataclass
from dataclasses import field

from . import errors

KINDS = ("chat", "embed")
NEEDS = ("vision", "tools", "thinking")


@dataclass(frozen=True)
class Placement:
    """Une machine qui a le modèle."""
    id: str
    machine: str | None = None
    place: str | None = None            # here, mine, friend
    loaded: bool = False
    context: int | None = None
    tools: bool = False
    vision: bool = False


@dataclass(frozen=True)
class Model:
    """Un modèle logique et les machines qui l'ont."""
    id: str
    kind: str
    label: str
    installed: bool = True
    context: int | None = None
    vision: bool = False
    tools: bool = False
    thinking: bool = False
    size_bytes: int | None = None
    loaded: bool = False
    here: bool = False
    default: bool = False
    where: tuple = ()
    license: object = field(default=None, compare=False, hash=False)
    dimensions: int | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False, hash=False)

    def can(self, need: str) -> bool:
        """Le modèle sert-il ce besoin (« vision », « tools », « thinking ») ?"""
        if need not in NEEDS:
            raise ValueError(f"besoin inconnu : {need!r} (parmi {', '.join(NEEDS)})")
        return bool(getattr(self, need))


class ModelList(list):
    """La liste des modèles, avec `best()` et `by_id`."""

    @property
    def by_id(self) -> dict:
        """Les modèles par identifiant : `models.by_id["qwen3.5:9b"]`."""
        return {model.id: model for model in self}

    def best(self, needs=(), prefer_local=True):
        """Le meilleur modèle installé qui sert tous les besoins (« vision », « tools »,
        « thinking »), ou None : d'abord ceux de ce PC (si `prefer_local`), puis celui
        qu'AIODOS prend lui-même (`default`), puis ceux déjà chargés, puis le plus grand
        contexte, puis le plus gros."""
        if isinstance(needs, str):
            needs = [needs]
        needs = list(needs or ())
        for need in needs:
            if need not in NEEDS:
                raise ValueError(f"besoin inconnu : {need!r} (parmi {', '.join(NEEDS)})")
        fitting = [m for m in self if m.installed and all(m.can(need) for need in needs)]
        if not fitting:
            return None

        def rank(model):
            return (model.here if prefer_local else True, model.default, model.loaded,
                    model.context or 0, model.size_bytes or 0)

        return max(fitting, key=rank)


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _placement(entry):
    return Placement(id=str(entry.get("id")), machine=entry.get("machine"),
                     place=entry.get("place"), loaded=entry.get("loaded") is True,
                     context=_int(entry.get("context")), tools=entry.get("tools") is True,
                     vision=entry.get("vision") is True)


def model_from(entry: dict) -> Model:
    """Une fiche du catalogue d'AIODOS en Model."""
    where = tuple(_placement(w) for w in entry.get("where") or ()
                  if isinstance(w, dict) and isinstance(w.get("id"), str))
    return Model(id=entry["id"], kind=str(entry.get("kind") or ""),
                 label=str(entry.get("label") or entry["id"]),
                 installed=entry.get("installed") is not False,
                 context=_int(entry.get("context")), vision=entry.get("vision") is True,
                 tools=entry.get("tools") is True, thinking=entry.get("thinking") is True,
                 size_bytes=_int(entry.get("size_bytes")), loaded=entry.get("loaded") is True,
                 here=entry.get("here") is True, default=entry.get("default") is True,
                 where=where, license=entry.get("license"),
                 dimensions=_int(entry.get("dimensions")), raw=dict(entry))


def list_models(client, kind="chat") -> ModelList:
    """Le catalogue d'un genre de modèles (« chat » ou « embed »)."""
    if kind not in KINDS:
        raise ValueError(f"kind : {' ou '.join(KINDS)}")
    data = client._call("GET", f"/aiodos/v1/models?kind={kind}", feature="models")
    entries = data.get("models") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise errors.make("protocol", client.lang)
    result = ModelList(model_from(entry) for entry in entries
                       if isinstance(entry, dict) and isinstance(entry.get("id"), str))
    client._remember_models(kind, result)
    return result
