"""Être consulté : une machine appairée cherche dans mes bibliothèques ouvertes, sans
copie de mes fichiers (action scellée « library/search »).

La machine qui demande envoie sa question. Celle-ci calcule son vecteur avec son propre
modèle d'embedding (les vecteurs de deux modèles ne se comparent pas), cherche dans ce que
la demandeuse a le droit de voir selon son niveau de confiance (knowledge/visibility.py),
et ne rend que les passages qui passent son seuil de pertinence. Pour freiner qui voudrait
aspirer une bibliothèque entière (sans l'empêcher : de recherche en recherche, une
bibliothèque ouverte se lit passage par passage ; ce qu'on ne veut pas montrer se ferme) :

- au plus MAX_PASSAGES passages par recherche ;
- au plus MAX_PER_MINUTE recherches par minute et par machine ;
- une machine mise de côté (contrôle échoué, cf. sharing.py) n'a plus rien.

Et pour que cette machine reste disponible : au plus AT_ONCE recherches calculées à la
fois, le vecteur en EMBED_TIMEOUT secondes au plus, et aucune pendant une conversation ici
(refus immédiats, lisibles).

De chaque passage sortent son texte, le nom du document (sans ses dossiers ni son
emplacement sur ce PC ; pour une page web, son hôte et le dernier segment de son adresse),
le nom de la bibliothèque et son score. Chaque recherche reçue est notée (table
consult_log) : qui, quand, combien de passages et de quels documents (leur chemin dans la
bibliothèque). La question, elle, n'est pas gardée, comme une conversation servie par
lending.py. Appelée dans un thread (p2p_app.HANDLERS).
"""
import json
import math
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from aiodos import db, settings
from aiodos.jobs import pacing
from aiodos.knowledge import visibility
from aiodos.network import sharing
from aiodos.network.client import PeerError
from aiodos.services import ollama

MAX_PASSAGES = 8              # passages rendus au plus par recherche
MAX_PER_MINUTE = 10           # recherches servies au plus par minute et par machine
WINDOW = 60
AT_ONCE = 2                   # recherches calculées en même temps, au plus (refus au-delà)
EMBED_TIMEOUT = 10            # s : vecteur de la question (le fil sert aussi l'interface)
_busy = threading.BoundedSemaphore(AT_ONCE)
MAX_QUESTION = 2000           # caractères ; le demandeur coupe avant d'envoyer
MAX_TEXT = 4000               # un passage fait 900 caractères (knowledge/chunking.py)
KEEP_DAYS = 90                # durée du journal

# Ce que voit chaque niveau de confiance (sharing.py). Les inconnus de la phase 3 ne
# verront que visibility.EVERYONE.
_SEES = {sharing.MINE: (visibility.MINE, visibility.FRIENDS, visibility.EVERYONE),
         sharing.FRIEND: (visibility.FRIENDS, visibility.EVERYONE)}


def levels_for(node_id) -> tuple:
    """Niveaux d'ouverture visibles par cette machine (rien si elle est mise de côté)."""
    if sharing.set_aside_reason(node_id):
        return ()
    return _SEES.get(sharing.level(node_id), ())


def open_count(node_id) -> int:
    """Nombre de bibliothèques ouvertes à cette machine, annoncé dans le profil qu'elle
    reçoit : elle n'envoie sa question qu'aux machines qui ont quelque chose pour elle."""
    return len(visibility.open_collections(levels_for(node_id)))


# -------------------------------------------------------------------- limites
class Limiter:
    """Au plus `limit` recherches servies par fenêtre glissante de `window` secondes,
    pour chaque machine."""

    def __init__(self, limit=MAX_PER_MINUTE, window=WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._seen = {}               # machine -> instants des recherches servies
        self._told = {}               # machine -> instant du dernier refus noté au journal
        self._lock = threading.Lock()

    def take(self, node_id) -> tuple:
        """(attente, à noter) : attente nulle si la recherche peut être servie (elle est
        alors comptée) ; sinon les secondes avant la prochaine, et s'il faut noter ce refus
        (un seul par fenêtre : une machine qui insiste ne remplit pas le journal)."""
        now = self.clock()
        with self._lock:
            seen = self._seen.setdefault(node_id, deque())
            while seen and now - seen[0] >= self.window:
                seen.popleft()
            if len(seen) < self.limit:
                seen.append(now)
                return 0.0, False
            told = self._told.get(node_id)
            note = told is None or now - told >= self.window
            if note:
                self._told[node_id] = now
            return self.window - (now - seen[0]), note


LIMITER = Limiter()


# ------------------------------------------------------------- ce qui sort
def shown_name(source: str, kind: str) -> str:
    """Nom du document montré à la machine qui cherche : sans les dossiers ; pour une
    page web, son hôte et le dernier segment de son chemin (« exemple.org/…/page ») : ni
    le reste du chemin, ni identifiants, paramètres ou ancre, qui peuvent porter un jeton."""
    source = str(source or "")
    if kind == "url":
        parts = urlsplit(source)
        if parts.scheme in ("http", "https") and parts.hostname:
            host = parts.netloc.rpartition("@")[2]
            segments = [s for s in parts.path.split("/") if s]
            if not segments:
                return host[:300]
            middle = "/" if len(segments) == 1 else "/…/"
            return f"{host}{middle}{segments[-1]}"[:300]
    return source.replace("\\", "/").rsplit("/", 1)[-1][:300]


def _passage(hit) -> dict:
    return {"text": hit["text"][:MAX_TEXT], "document": shown_name(hit["source"], hit["kind"]),
            "library": hit["collection"], "score": round(float(hit["score"]), 4)}


def _question(payload) -> str:
    question = (payload or {}).get("question")
    if not isinstance(question, str) or not question.strip():
        raise PeerError("Question vide.")
    if len(question) > MAX_QUESTION:
        raise PeerError(f"Question trop longue ({MAX_QUESTION} caractères au plus).")
    return question.strip()


def _wanted(payload) -> int:
    try:
        k = int((payload or {}).get("k") or 4)
    except (TypeError, ValueError):
        k = 4
    return max(1, min(MAX_PASSAGES, k))


# ---------------------------------------------------------------- recherche
def search(manager, sender, payload):
    """Action scellée « library/search » : {question, k} → {passages, open}."""
    question, k = _question(payload), _wanted(payload)
    # La limite d'abord : elle compte toute recherche reçue, et borne ainsi ce qui est noté
    wait, note = LIMITER.take(sender)
    if wait:
        if note:
            log(sender, refused=f"plus de {LIMITER.limit} recherches par minute")
        raise PeerError(f"« {manager.name()} » limite les recherches à {LIMITER.limit} par "
                        f"minute : réessaie dans {math.ceil(wait)} s.")
    aside = sharing.set_aside_reason(sender)
    if aside:
        log(sender, refused="machine mise de côté")
        raise PeerError(f"« {manager.name()} » ne te laisse plus chercher dans ses "
                        f"bibliothèques : {aside}.")
    from aiodos.network import worker
    if not worker.welcomes(sender):
        log(sender, refused="téléphone hors charge ou hors Wi-Fi")
        raise PeerError(f"« {manager.name()} » est un téléphone : il ne cherche pour ses amis "
                        "qu'en charge et en Wi-Fi.")
    levels = levels_for(sender)
    opened = visibility.open_collections(levels)
    if not opened:                        # rien d'ouvert : la question n'est même pas lue
        log(sender)
        return {"passages": [], "open": 0}
    # Une conversation ici passe d'abord, et les recherches des autres restent peu
    # nombreuses à la fois : refus tout de suite, plutôt qu'une file qui bloque le fil
    if pacing.chatting():
        raise PeerError(f"« {manager.name()} » répond à une conversation : réessaie dans un "
                        "instant.")
    if not _busy.acquire(blocking=False):
        raise PeerError(f"« {manager.name()} » est occupée à d'autres recherches : réessaie "
                        "dans un instant.")
    try:
        try:
            vector = ollama.embed([question], timeout=EMBED_TIMEOUT)[0]
        except Exception as exc:
            raise PeerError(f"« {manager.name()} » ne peut pas chercher pour l'instant : "
                            "Ollama n'y répond pas à temps.") from exc
        threshold = float(settings.get("rag_min_score") or 0)
        hits = [h for h in visibility.search(vector, k, levels) if h["score"] >= threshold]
    finally:
        _busy.release()
    # Au journal, le chemin du document dans sa bibliothèque : c'est le mien, je le vois
    log(sender, len(hits), [(h["collection"], h["source"]) for h in hits])
    return {"passages": [_passage(h) for h in hits], "open": len(opened)}


HANDLERS = {"library/search": search}


# ------------------------------------------------------------------ journal
def log(node_id, passages: int = 0, documents=(), refused: str | None = None):
    """Recherche reçue de `node_id` : nombre de passages rendus, (bibliothèque, document)
    d'où ils viennent, ou raison du refus."""
    unique = list(dict.fromkeys((str(lib), str(doc)) for lib, doc in documents))
    db.execute("INSERT INTO consult_log(node_id, at, passages, documents, refused) "
               "VALUES(?, ?, ?, ?, ?)",
               (node_id, db.now(), passages,
                json.dumps([{"library": l, "document": d} for l, d in unique],
                           ensure_ascii=False), refused))
    db.execute("DELETE FROM consult_log WHERE at < ?", (db.now() - KEEP_DAYS * 86400,))


def summary() -> dict:
    """{machine: {"count", "passages", "refused", "last"}} : ses recherches dans mes
    bibliothèques (servies et refusées)."""
    return {r["node_id"]: {"count": r["count"], "passages": r["passages"] or 0,
                           "refused": r["refused"], "last": r["last"]}
            for r in db.query("SELECT node_id, COUNT(refused) AS refused, "
                              "COUNT(*) - COUNT(refused) AS count, SUM(passages) AS passages, "
                              "MAX(at) AS last FROM consult_log GROUP BY node_id")}


def entries(node_id, limit: int = 100) -> list:
    return [dict(r, documents=json.loads(r["documents"] or "[]")) for r in db.query(
        "SELECT at, passages, documents, refused FROM consult_log WHERE node_id=? "
        "ORDER BY at DESC, id DESC LIMIT ?", (node_id, limit))]
