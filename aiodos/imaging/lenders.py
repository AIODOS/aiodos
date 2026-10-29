"""Qui peut calculer un modèle d'images ou de vidéos pour moi : cette machine (moteur et
modèle complet), mes machines en ligne qui l'ont et prêtent leur puissance, et les amis qui
prêtent leur carte pour les images (leur profil ne les montre qu'alors), si je les y
autorise (réglage images_from_friends)."""
from aiodos import settings
from aiodos.i18n import t
from aiodos.jobs.kinds import images as kind
from aiodos.network import peers, sharing

MINE = "mine"
FRIEND = "friend"


def _power(profile: dict) -> int:
    """La mémoire de sa plus grosse carte graphique (Mio), seule idée de sa puissance pour
    les images que dise son profil : ses cartes (nvidia-smi), sinon celle qu'il prête aux
    gros modèles répartis."""
    cards = [g.get("vram_total") for g in profile.get("gpus") or [] if isinstance(g, dict)]
    found = max((int(v) for v in cards if isinstance(v, int)), default=0) // 2 ** 20
    cluster = profile.get("cluster") if isinstance(profile.get("cluster"), dict) else {}
    free = cluster.get("free_mib")
    return found or (int(free) if isinstance(free, int) else 0)


def circles(friends: bool | None = None) -> dict:
    """{modèle : [{"circle": MINE | FRIEND, "name", "power"}]} : mes machines en ligne qui
    l'ont et peuvent le calculer pour moi, puis mes amis (friends : None, selon le réglage
    images_from_friends) ; dans chaque cercle, la plus puissante d'abord (power, Mio)."""
    if friends is None:
        friends = bool(settings.get("images_from_friends"))
    found = {}
    for peer in peers.list_peers():
        mine = sharing.level(peer["node_id"]) == sharing.MINE
        if not peer["online"] or not (mine or friends):
            continue
        profile = peer.get("profile") or {}
        images = profile.get("images") or {}
        if not images.get("engine") or not images.get("shares"):
            continue
        for model_id in images.get("models") or []:
            found.setdefault(model_id, []).append(
                {"circle": MINE if mine else FRIEND, "name": peer["name"],
                 "power": _power(profile)})
    for machines in found.values():
        machines.sort(key=lambda m: (m["circle"] != MINE, -m["power"]))
    return found


def helpers(friends: bool | None = None) -> dict:
    """{modèle : [machines en ligne qui l'ont et peuvent le calculer pour moi]} ; un ami est
    suivi de « (ami) », dans la langue en cours. friends : None, selon le réglage
    images_from_friends."""
    return {model_id: [m["name"] if m["circle"] == MINE
                       else t("imaging.friend_name", name=m["name"]) for m in machines]
            for model_id, machines in circles(friends).items()}


def can_compute(model_id: str, others: dict | None = None) -> tuple:
    """(ici, [autres machines]) : où ce modèle peut se calculer en ce moment."""
    others = helpers() if others is None else others
    return kind.runnable_here(model_id), others.get(model_id, [])
