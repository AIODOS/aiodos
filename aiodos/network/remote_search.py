"""Chercher dans les bibliothèques ouvertes de mes machines et de mes amis (côté demandeur).

La question part chez chaque machine appairée dont le dernier profil annonce des
bibliothèques ouvertes à cette machine-ci (consult.open_count chez elle) ; les autres ne
la reçoivent pas. Chacune cherche chez elle et ne rend que quelques passages
(network/consult.py) : aucun fichier n'est copié ici. Les machines sont interrogées en
même temps, et une machine lente, injoignable ou qui refuse ne retarde ni les autres ni la
réponse. Ce qui revient est contrôlé (forme, longueurs, nombre) avant de servir.
"""
import logging
import math
from concurrent.futures import ThreadPoolExecutor, wait

from aiodos.network import client, consult, peers, sharing
from aiodos.network.client import PeerError
from aiodos.node import identity

log = logging.getLogger("aiodos.consult")

PER_PEER = 3                  # passages demandés à chaque machine pour une conversation
CHAT_TIMEOUT = 8              # s : une conversation n'attend pas une machine lente
VIEW_TIMEOUT = 20             # s : recherche lancée à la main (vue Bibliothèques)
# Chaque appel a ses délais (route.timeouts) ; au-delà de cette marge, qui couvre un réseau
# local muet puis Internet, une machine qui n'a pas répondu est laissée de côté
GRACE = 10
MAX_DOCUMENT = 300
MAX_LIBRARY = 120


def _open_for_me(peer) -> int:
    try:
        return int(((peer.get("profile") or {}).get("consult") or {}).get("open") or 0)
    except (TypeError, ValueError):
        return 0


def targets() -> list:
    """Machines à consulter : appairées, en ligne, joignables, pas mises de côté, et qui
    ont quelque chose d'ouvert pour cette machine d'après leur dernier profil."""
    return [p for p in peers.list_peers()
            if p["online"] and not p.get("set_aside_at") and (p.get("address") or p.get("net_id"))
            and _open_for_me(p) > 0]


def describe(found=None) -> list:
    """Chez qui partirait la question : [{"node_id", "name", "friend"}]."""
    return [{"node_id": p["node_id"], "name": p["name"], "friend": p["trust"] == sharing.FRIEND}
            for p in (targets() if found is None else found)]


def clean(reply, k: int) -> list:
    """Passages reçus, vérifiés : seuls ceux bien formés passent, k au plus."""
    items = reply.get("passages") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        raise PeerError("Réponse illisible.")
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
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        out.append({"text": text[:consult.MAX_TEXT], "document": document[:MAX_DOCUMENT],
                    "library": library[:MAX_LIBRARY], "score": score})
    return out


def _ask(peer, question, k, timeout) -> dict:
    full = peers.get(peer["node_id"])
    if not full:
        raise PeerError("Cette machine n'est plus appairée.")
    reply = client.call(identity.node_id(), full, "library/search",
                        {"question": question, "k": k}, timeout=timeout)
    passages = clean(reply, k)
    opened = reply.get("open")
    if isinstance(opened, int) and opened != _open_for_me(peer) and peer.get("profile"):
        # Rien (ou autre chose) d'ouvert depuis son dernier profil : on en tient compte
        # tout de suite, sans attendre le prochain
        peers.set_profile(peer["node_id"], dict(peer["profile"], consult={"open": opened}))
    return {"passages": passages, "open": opened if isinstance(opened, int) else None}


def search(question: str, k: int = PER_PEER, timeout: float = CHAT_TIMEOUT,
           found: list | None = None) -> tuple:
    """La question posée en même temps à chaque machine à consulter.

    → (passages, machines) : passages [{"text", "document", "library", "score", "peer",
    "node_id", "friend", "rank"}], du meilleur au moins bon chez chacune ; machines
    [{"node_id", "name", "friend", "count", "open", "error"}], une par machine interrogée.
    """
    question = str(question or "").strip()[:consult.MAX_QUESTION]   # rien de plus ne part
    found = targets() if found is None else found
    if not question or not found:
        return [], []
    pool = ThreadPoolExecutor(max_workers=min(8, len(found)), thread_name_prefix="aiodos-consult")
    futures = {pool.submit(_ask, p, question, k, timeout): p for p in found}
    done, _late = wait(futures, timeout=timeout + GRACE)
    pool.shutdown(wait=False, cancel_futures=True)
    passages, machines = [], []
    for future, peer in futures.items():
        status = {"node_id": peer["node_id"], "name": peer["name"],
                  "friend": peer["trust"] == sharing.FRIEND, "count": 0, "open": None,
                  "error": None}
        if future not in done:
            status["error"] = "pas de réponse à temps"
        elif future.exception() is not None:
            status["error"] = str(future.exception())[:300]
            log.info("Recherche chez « %s » : %s", peer["name"], status["error"])
        else:
            result = future.result()
            status.update(count=len(result["passages"]), open=result["open"])
            for rank, p in enumerate(result["passages"]):
                passages.append(dict(p, peer=peer["name"], node_id=peer["node_id"],
                                     friend=status["friend"], rank=rank))
        machines.append(status)
    return passages, machines
