"""Qui peut calculer un modèle d'audio pour moi : cette machine (audio.cpp et modèle complet),
mes machines en ligne qui l'ont et prêtent leur puissance, et les amis qui prêtent leur carte
pour les créations (leur profil ne montre « audio » qu'alors : network/sharing.profile_for), si
je les y autorise (réglage images_from_friends, le même que pour les images)."""
from aiodos import settings
from aiodos.i18n import t
from aiodos.network import peers, sharing

MINE = "mine"                     # les mêmes cercles que imaging/lenders.py
FRIEND = "friend"


def circles(friends: bool | None = None) -> dict:
    """{modèle : [{"circle": MINE | FRIEND, "name", "power"}]} : mes machines en ligne qui
    l'ont et peuvent le calculer pour moi, puis mes amis (friends : None, selon le réglage) ;
    dans chaque cercle, la plus puissante d'abord (power, Mio)."""
    from aiodos.imaging.lenders import _power     # à la demande : imaging dépend des travaux
    if friends is None:
        friends = bool(settings.get("images_from_friends"))
    found = {}
    for peer in peers.list_peers():
        mine = sharing.level(peer["node_id"]) == sharing.MINE
        if not peer["online"] or not (mine or friends):
            continue
        profile = peer.get("profile") or {}
        audio = profile.get("audio") or {}
        if not isinstance(audio, dict) or not audio.get("engine") or not audio.get("shares"):
            continue
        for model_id in audio.get("models") or []:
            if isinstance(model_id, str):
                found.setdefault(model_id, []).append(
                    {"circle": MINE if mine else FRIEND, "name": peer["name"],
                     "power": _power(profile)})
    for machines in found.values():
        machines.sort(key=lambda m: (m["circle"] != MINE, -m["power"]))
    return found


def helpers(friends: bool | None = None) -> dict:
    """{modèle : [noms des machines en ligne qui l'ont et peuvent le calculer pour moi]} ; un
    ami est suivi de « (ami) », dans la langue en cours."""
    return {model_id: [m["name"] if m["circle"] == MINE
                       else t("imaging.friend_name", name=m["name"]) for m in machines]
            for model_id, machines in circles(friends).items()}
