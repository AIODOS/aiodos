"""Chercher dans les bibliothèques ouvertes de mes machines et de mes amis (côté demandeur).

La question part chez les machines appairées dont le dernier profil annonce des
bibliothèques ouvertes à cette machine-ci (consult.open_count chez elle), et seulement chez
celles que l'utilisateur a acceptées (`only` : la liste qu'il a vue avant d'envoyer, pour
qu'un ami arrivé en ligne entre-temps ne la reçoive pas sans accord). Chacune cherche chez
elle et ne rend que quelques passages (network/consult.py) : aucun fichier n'est copié ici.
Les machines sont interrogées en même temps ; une machine lente, injoignable ou qui refuse
ne retarde ni les autres ni la réponse au-delà du délai. Ce qui revient est contrôlé
(forme, longueurs, nombre), et les noms nettoyés : un passage d'ailleurs ne doit pas pouvoir
se faire passer pour un de mes fichiers.
"""
import contextvars
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, wait

from aiodos.i18n import t
from aiodos.network import client, consult, peers, sharing
from aiodos.network.client import PeerError
from aiodos.node import identity

log = logging.getLogger("aiodos.consult")

PER_PEER = 3                  # passages demandés à chaque machine pour une conversation
CHAT_TIMEOUT = 8              # s : une conversation n'attend pas une machine lente…
CHAT_GRACE = 2                # … au-delà de 10 s en tout
VIEW_TIMEOUT = 20             # s : recherche lancée à la main (vue Bibliothèques)
VIEW_GRACE = 10               # marge pour un réseau local muet puis Internet (relais : 30 s)
MAX_DOCUMENT = 300
MAX_LIBRARY = 120
MAX_OPEN = 10_000             # nombre de bibliothèques ouvertes plausible, au-delà : profil faux
_UNSAFE = re.compile(r"[\x00-\x1f\x7f\[\]«»\"<>]+")       # i18n: tel quel (motif)


def _open_for_me(peer) -> int:
    """Bibliothèques ouvertes à cette machine selon le profil de `peer` ; 0 pour un profil
    absent ou mal formé (liste, nombre non entier ou démesuré…)."""
    profile = peer.get("profile")
    found = profile.get("consult") if isinstance(profile, dict) else None
    opened = found.get("open") if isinstance(found, dict) else None
    if isinstance(opened, bool) or not isinstance(opened, int):
        return 0
    return opened if 0 < opened <= MAX_OPEN else 0


def targets(only=None) -> list:
    """Machines à consulter : appairées, en ligne, joignables, pas mises de côté, qui ont
    quelque chose d'ouvert pour cette machine d'après leur dernier profil, et, si `only` est
    donné, parmi ces identifiants-là."""
    allowed = None if only is None else set(only)
    return [p for p in peers.list_peers()
            if (allowed is None or p["node_id"] in allowed)
            and p["online"] and not p.get("set_aside_at")
            and (p.get("address") or p.get("net_id")) and _open_for_me(p) > 0]


def describe(found=None) -> list:
    """Chez qui partirait la question : [{"node_id", "name", "friend"}]."""
    return [{"node_id": p["node_id"], "name": name(p["name"]),
             "friend": p.get("trust") == sharing.FRIEND}
            for p in (targets() if found is None else found)]


def name(text, limit: int = MAX_DOCUMENT) -> str:
    """Nom venu d'une autre machine (document, bibliothèque, machine), sans ce qui pourrait
    imiter une source ou une consigne : retours à la ligne, crochets, guillemets, chevrons."""
    cleaned = " ".join(_UNSAFE.sub(" ", str(text or "")).split())[:limit]
    return cleaned or "?"


def clean(reply, k: int) -> list:
    """Passages reçus, vérifiés : seuls ceux bien formés passent, k au plus."""
    items = reply.get("passages") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        raise PeerError(t("consult.unreadable_reply"))
    out = []
    for item in items:
        if len(out) == k:
            break
        if not isinstance(item, dict):
            continue
        text, document, library = item.get("text"), item.get("document"), item.get("library")
        if not all(isinstance(v, str) for v in (text, document, library)) or not text.strip():
            continue
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(score):
            continue
        out.append({"text": text[:consult.MAX_TEXT], "document": name(document, MAX_DOCUMENT),
                    "library": name(library, MAX_LIBRARY), "score": score})
    return out


def _ask(peer, question, k, timeout) -> dict:
    full = peers.get(peer["node_id"])
    if not full:
        raise PeerError(t("consult.not_paired"))
    reply = client.call(identity.node_id(), full, "library/search",
                        {"question": question, "k": k}, timeout=timeout)
    passages = clean(reply, k)
    opened = reply.get("open")
    if isinstance(opened, bool) or not isinstance(opened, int):
        opened = None
    elif opened != _open_for_me(peer):
        # Rien (ou autre chose) d'ouvert depuis son dernier profil : on en tient compte tout
        # de suite, en ne changeant que cette clé (un profil plus récent n'est pas écrasé)
        peers.update_profile(peer["node_id"], "consult", {"open": max(0, min(opened, MAX_OPEN))})
    return {"passages": passages, "open": opened}


def search(question: str, only, k: int = PER_PEER, timeout: float = CHAT_TIMEOUT,
           grace: float = CHAT_GRACE) -> tuple:
    """La question posée en même temps à chaque machine à consulter parmi `only` (les
    identifiants acceptés par l'utilisateur).

    → (passages, machines) : passages [{"text", "document", "library", "score", "peer",
    "node_id", "friend", "rank"}], du meilleur au moins bon chez chacune ; machines
    [{"node_id", "name", "friend", "count", "open", "error"}], une par machine interrogée.
    """
    question = str(question or "").strip()[:consult.MAX_QUESTION]   # rien de plus ne part
    found = targets(only or [])
    if not question or not found:
        return [], []
    pool = ThreadPoolExecutor(max_workers=min(8, len(found)), thread_name_prefix="aiodos-consult")
    # Chaque fil garde le contexte de l'appelant : ses erreurs sont dans la langue de la
    # requête (celle de l'interface), comme le reste de la réponse
    futures = {pool.submit(contextvars.copy_context().run, _ask, p, question, k, timeout): p
               for p in found}
    done, _late = wait(futures, timeout=timeout + grace)
    pool.shutdown(wait=False, cancel_futures=True)
    passages, machines = [], []
    for future, peer in futures.items():
        status = {"node_id": peer["node_id"], "name": name(peer["name"]),
                  "friend": peer.get("trust") == sharing.FRIEND, "count": 0, "open": None,
                  "error": None}
        if future not in done:
            status["error"] = t("consult.no_answer_in_time")
        elif future.exception() is not None:
            status["error"] = str(future.exception())[:300]
            log.info("Recherche chez « %s » : %s", status["name"], status["error"])
        else:
            result = future.result()
            status.update(count=len(result["passages"]), open=result["open"])
            for rank, p in enumerate(result["passages"]):
                passages.append(dict(p, peer=status["name"], node_id=peer["node_id"],
                                     friend=status["friend"], rank=rank))
        machines.append(status)
    return passages, machines
