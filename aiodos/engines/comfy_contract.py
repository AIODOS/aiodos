"""Le contrat des nœuds de ComfyUI (docs/conception/partage-puissance.md § 3.3) : une machine ne
rejoue le graphe d'une autre que si les classes qu'il emploie y ont la même définition.

- L'empreinte d'une classe : sha256 du JSON canonique (clés triées, séparateurs compacts,
  UTF-8) de input, input_order, output, output_is_list, output_node et python_module,
  tronquée à 16 octets. Les listes de choix qui énumèrent des fichiers (modèles, images
  d'entrée) y sont remplacées par « <fichiers> » : elles changent d'une machine à l'autre
  sans que la classe change.
- core : l'empreinte de la liste triée des empreintes des classes du cœur (python_module hors
  custom_nodes, sans api_node). Deux machines au même core acceptent les mêmes graphes du
  cœur : la voie rapide.
- Chez celui qui calcule : chaque classe du contrat doit exister ; avec strict (toujours pour
  un ami), à la même empreinte.
Fonctions pures, sur l'object_info d'un ComfyUI (defs).
"""
import hashlib
import json

from aiodos.engines import comfy_graph

FIELDS = ("input", "input_order", "output", "output_is_list", "output_node", "python_module")
FILES = "<fichiers>"


def _normal(spec):
    """La définition d'une entrée, sans la liste des fichiers d'une machine."""
    if comfy_graph.is_file_list(spec):
        options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        kept = {k: v for k, v in options.items() if k not in ("options", "default")}
        return [FILES, kept] if kept else [FILES]
    return spec


def _canonical(definition: dict) -> dict:
    out = {}
    for field in FIELDS:
        value = definition.get(field)
        if field == "input" and isinstance(value, dict):
            value = {group: {name: _normal(spec) for name, spec in (specs or {}).items()}
                     for group, specs in value.items() if isinstance(specs, dict)}
        out[field] = value
    return out


def fingerprint(definition: dict) -> str:
    """L'empreinte d'une classe (32 caractères hexadécimaux)."""
    text = json.dumps(_canonical(definition), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def is_core(definition: dict) -> bool:
    module = str(definition.get("python_module") or "")
    return (not module.startswith("custom_nodes") and definition.get("api_node") is not True
            and not module.startswith("comfy_api_nodes"))


def core(defs: dict) -> str:
    prints = sorted(fingerprint(d) for d in defs.values() if isinstance(d, dict) and is_core(d))
    return hashlib.sha256("\n".join(prints).encode("ascii")).hexdigest()[:32]


def of_graph(graph: dict, defs: dict, version: str | None = None) -> dict:
    """Le contrat d'un graphe : {"comfyui", "core", "classes": {classe: empreinte}} ; une
    classe inconnue de ces définitions n'y figure pas (elle est refusée ailleurs)."""
    classes = {}
    for node in graph.values():
        name = node["class_type"]
        definition = defs.get(name) if isinstance(defs, dict) else None
        if isinstance(definition, dict):
            classes[name] = fingerprint(definition)
    return {"comfyui": version, "core": core(defs) if defs else None, "classes": classes}


def mismatches(contract: dict, graph: dict, defs: dict, strict: bool) -> list:
    """Les classes du graphe qui manquent ici, ou qui diffèrent du contrat (strict, ou cœur
    différent). → [(classe, raison)], vide si le graphe peut se rejouer ici."""
    found = []
    same_core = (contract or {}).get("core") and (contract or {}).get("core") == core(defs)
    expected = (contract or {}).get("classes") or {}
    for name in sorted({n["class_type"] for n in graph.values()}):
        definition = defs.get(name) if isinstance(defs, dict) else None
        if not isinstance(definition, dict):
            found.append((name, "missing"))
            continue
        if strict or not same_core:
            wanted = expected.get(name)
            if wanted and wanted != fingerprint(definition):
                found.append((name, "different"))
            elif strict and not wanted:
                found.append((name, "not_in_contract"))
    return found
