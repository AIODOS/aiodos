"""Qui garde quelle tranche d'un modèle réparti.

Mesures du 17/10/2026 (docs/gros-modeles.md) : chaque machine de plus coûte environ un
aller-retour réseau par mot écrit ; le dernier étage du modèle, qui note tout le
vocabulaire (~1 Mo par mot pour Qwen), doit rester sur la machine qui pose la question,
sinon un débit Internet ordinaire suffit à tout ralentir. D'où :

- le moins de machines possible : les plus grandes cartes d'abord ;
- les machines qui prêtent gardent les premières couches, dans l'ordre ; la carte de cette
  machine garde les dernières et le dernier étage ;
- sans carte ici, le dernier étage reste sur le processeur de cette machine.
"""
MARGIN_MIB = 1536             # par carte : cache du contexte et calculs en cours
OVERHEAD = 1.05               # poids + petits tenseurs non comptés dans le fichier


def _usable(device) -> int:
    return max(0, int(device.get("free_mib") or 0) - MARGIN_MIB) if device else 0


def make(model_bytes: int, layers: int, local: dict | None, helpers: list) -> dict | None:
    """Découpage d'un modèle de `model_bytes` octets et `layers` couches entre la carte de
    cette machine (`local` : {"name", "free_mib"} ou None) et des machines qui prêtent la
    leur (`helpers` : [{"node_id", "free_mib", …}]).

    Rend {"helpers": [machines retenues, dans l'ordre], "devices": "RPC0/RPC1/Vulkan1",
    "split": "6144/4096/5632", "layers": couches confiées aux cartes, "complete": tout
    tient sur les cartes} ; None si aucune machine ne prête, ou si le modèle tient
    déjà sur la carte de cette machine."""
    need = int(model_bytes * OVERHEAD / 2**20) + 1
    if layers <= 0 or need <= 0:
        return None
    here = _usable(local)
    if here >= need:
        return None                              # tient ici : rien à répartir
    chosen, total = [], here
    for helper in sorted(helpers, key=_usable, reverse=True):
        if _usable(helper) <= 0:
            continue
        chosen.append(helper)
        total += _usable(helper)
        if total >= need:
            break
    if not chosen:
        return None
    shares = [_usable(h) for h in chosen]
    names = [f"RPC{i}" for i in range(len(chosen))]
    if here > 0:
        shares.append(here)
        names.append(local["name"])
    complete = total >= need
    if complete:
        # Tout sur les cartes ; le dernier étage aussi, sur celle d'ici s'il y en a une.
        offloaded = layers + 1 if here > 0 else layers
    else:
        # Ce qui ne tient pas reste dans la mémoire vive d'ici (les premières couches).
        offloaded = max(1, int(layers * total / need))
    return {"helpers": chosen, "devices": "/".join(names),
            "split": "/".join(str(s) for s in shares), "layers": offloaded,
            "complete": complete, "need_mib": need, "cards_mib": total}
