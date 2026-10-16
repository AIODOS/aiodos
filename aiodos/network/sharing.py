"""Partage avec les amis : étape 2.5 de docs/phase2-internet.md, selon les décisions de
l'utilisateur du 16/10/2026.

Chaque machine appairée a un niveau de confiance, choisi à l'appairage sur chacune des
deux machines (chacune juge l'autre), modifiable ensuite dans la vue Réseau :

- « mes machines » (MINE) : confiance totale, comme avant. Tout leur est confié, sauf
  les bibliothèques gardées sur ce PC (le cadenas) ;
- « amis » (FRIEND) : seules les bibliothèques marquées « partageable avec mes amis »
  partent chez eux, et avec le minimum. Le fichier part sous un nom neutre (l'extension
  reste : elle choisit le lecteur), sans chemin ni nom de bibliothèque. Ils voient de
  cette machine un profil allégé : ni matériel, ni alimentation, ni inactivité.

Ce qu'un ami calcule est contrôlé par échantillon (spec.sample, cf. jobs/kinds/index.py) :
un passage recalculé ici. En cas d'écart, sa machine est mise de côté : plus rien ne lui
est confié jusqu'à ce que l'utilisateur la rétablisse. Chaque fichier ou page confié à un
ami est noté dans un journal (table shared_log), visible dans la vue Réseau.

Calculer pour un ami, en revanche, n'a pas de règle à part : tous les types de travaux,
aux conditions déjà en place (inactivité, secteur, température, part du GPU).
"""
import re
from pathlib import Path

from aiodos import db
from aiodos.network import peers

MINE = "mine"
FRIEND = "friend"
LEVELS = (MINE, FRIEND)
# Profil de cette machine montré à un ami : de quoi calculer, converser et se mettre à
# jour, rien de plus (les modèles seulement s'ils lui sont prêtés)
FRIEND_PROFILE = ("node_id", "name", "version", "lends_models", "updates")
_SUFFIX = re.compile(r"\.[a-z0-9]{1,10}")


def level(node_id) -> str:
    """Niveau de confiance d'une machine. Inconnue (pas appairée) : le plus étroit."""
    return peers.trust_of(node_id) or FRIEND


def is_friend(node_id) -> bool:
    return level(node_id) == FRIEND


def set_aside_reason(node_id) -> str | None:
    """Pourquoi cette machine est mise de côté, ou None si elle ne l'est pas."""
    row = db.query_one("SELECT set_aside_reason FROM peers WHERE node_id=? "
                       "AND set_aside_at IS NOT NULL", (node_id,))
    return (row["set_aside_reason"] or "contrôle échoué") if row else None


# --------------------------------------------------------- ce qui part chez un ami
def neutral_name(unit_id: int, name: str) -> str:
    """« Rapport final.PDF » → « fichier-1234.pdf » : seule l'extension reste."""
    suffix = Path(str(name or "")).suffix.lower()
    return f"fichier-{unit_id}{suffix if _SUFFIX.fullmatch(suffix) else ''}"


def order_for_friend(order: dict, spec, unit: dict) -> dict:
    """L'ordre de calcul tel qu'un ami le reçoit : du payload, les seules clés que la
    tâche déclare nécessaires chez lui (spec.shared_keys), et le fichier sous un nom
    neutre (repris comme « source » pour ses messages d'erreur)."""
    payload = {k: v for k, v in unit["payload"].items() if k in spec.shared_keys}
    reduced = dict(order, payload=payload)
    if order.get("blob"):
        neutral = neutral_name(unit["id"], order["blob"]["name"])
        reduced["blob"] = dict(order["blob"], name=neutral)
        payload["source"] = neutral
    return reduced


def restore_names(output: dict, unit: dict, spec) -> dict:
    """Chez le propriétaire : le nom neutre que l'ami a pu écrire dans le texte calculé
    (« [Image : fichier-12.png] ») redevient le vrai nom du fichier."""
    pieces = output.get("pieces")
    if not spec.file_key or not isinstance(pieces, list):
        return output
    original = Path(str(unit["payload"].get(spec.file_key) or "")).name
    neutral = neutral_name(unit["id"], original)
    if not original or neutral == original:
        return output
    output["pieces"] = [p.replace(neutral, original) if isinstance(p, str) else p
                        for p in pieces]
    return output


def profile_for(node_id, profile: dict) -> dict:
    """Le profil de cette machine tel que `node_id` le voit."""
    if not is_friend(node_id):
        return profile
    reduced = {k: profile[k] for k in FRIEND_PROFILE if k in profile}
    models = (profile.get("ollama") or {}).get("models") or []
    reduced["ollama"] = {"models": models if profile.get("lends_models") else []}
    return reduced


# ------------------------------------------------------------------- journal
def log_sent(node_id, unit_id, task: str, what: str, sent_as: str | None, size: int | None):
    db.execute("INSERT INTO shared_log(node_id, at, unit_id, task, what, sent_as, size) "
               "VALUES(?, ?, ?, ?, ?, ?, ?)",
               (node_id, db.now(), unit_id, task, str(what)[:500], sent_as, size))


def sent_summary() -> dict:
    """{machine: {"count", "bytes", "last"}} : ce qui est parti chez chaque ami."""
    return {r["node_id"]: {"count": r["count"], "bytes": r["bytes"] or 0, "last": r["last"]}
            for r in db.query("SELECT node_id, COUNT(*) AS count, SUM(size) AS bytes, "
                              "MAX(at) AS last FROM shared_log GROUP BY node_id")}


def sent_log(node_id, limit: int = 100) -> list:
    return [dict(r) for r in db.query(
        "SELECT at, task, what, sent_as, size FROM shared_log WHERE node_id=? "
        "ORDER BY at DESC, id DESC LIMIT ?", (node_id, limit))]
