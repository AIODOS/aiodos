"""Qui peut calculer un modèle d'images ou de vidéos pour moi : cette machine (moteur et
modèle complet), mes machines en ligne qui l'ont et prêtent leur puissance, et les amis qui
prêtent leur carte pour les images (leur profil ne les montre qu'alors), si je les y
autorise (réglage images_from_friends)."""
from aiodos import settings
from aiodos.jobs.kinds import images as kind
from aiodos.network import peers, sharing


def helpers(friends: bool | None = None) -> dict:
    """{modèle : [machines en ligne qui l'ont et peuvent le calculer pour moi]} ; un ami est
    suivi de « (ami) ». friends : None, selon le réglage images_from_friends."""
    if friends is None:
        friends = bool(settings.get("images_from_friends"))
    found = {}
    for peer in peers.list_peers():
        mine = sharing.level(peer["node_id"]) == sharing.MINE
        if not peer["online"] or not (mine or friends):
            continue
        images = (peer.get("profile") or {}).get("images") or {}
        if not images.get("engine") or not images.get("shares"):
            continue
        for model_id in images.get("models") or []:
            found.setdefault(model_id, []).append(peer["name"] if mine else f"{peer['name']} (ami)")
    return found


def can_compute(model_id: str, others: dict | None = None) -> tuple:
    """(ici, [autres machines]) : où ce modèle peut se calculer en ce moment."""
    others = helpers() if others is None else others
    return kind.runnable_here(model_id), others.get(model_id, [])
