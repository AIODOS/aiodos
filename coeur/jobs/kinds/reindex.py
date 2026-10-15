"""Réindexation quand le modèle d'embedding change.

Les vecteurs d'un modèle ne se comparent pas à ceux d'un autre, même de
dimension égale (nomic-embed-text et embeddinggemma : 768). Quand embed_model
change, les anciens vecteurs sont donc retirés tout de suite (le texte des
passages reste), puis recalculés entrée par entrée ; en attendant, la recherche
ne voit que les entrées déjà recalculées.

D'où vient le texte réindexé :
- fichiers texte et documents : relus depuis leur origine (rapide, et une
  modification faite depuis l'indexation est prise en compte) ;
- images, audio, vidéo, pages web : texte déjà extrait, gardé dans les passages
  (ni nouvel OCR, ni nouvelle transcription, ni accès au réseau sans action de
  l'utilisateur) ; de même pour une entrée sans origine (import de la v1) ou
  dont le fichier a disparu ;
- mémoire des conversations : reconstruite depuis les messages (tâche
  index_conversation).

Le modèle des vecteurs en base est noté à part (réglage interne INDEXED_KEY) ;
schedule() le compare à embed_model au démarrage (le modèle par défaut a pu
changer avec une mise à jour) et après chaque enregistrement des réglages.
Ces unités lisent la base locale : elles restent sur ce nœud.
"""
import json
import logging
from pathlib import Path

from coeur import db, settings
from coeur.jobs import queue
from coeur.jobs.models import NORMAL, PAUSED, QUEUED, RUNNING
from coeur.jobs.registry import UnitSpec, register_job_kind, task
from coeur.knowledge import chunking, search, store
from coeur.services import extract, ollama

log = logging.getLogger("coeur.reindex")

INDEXED_KEY = "indexed_embed_model"
# Modèle par défaut jusqu'au 16/10/2026 : celui des vecteurs d'une base plus
# ancienne où embed_model n'a jamais été enregistré.
PREVIOUS_DEFAULT = "nomic-embed-text"
REREAD_FAMILIES = {"text", "document", "unknown"}       # relus depuis le fichier
_ACTIVE = (QUEUED, RUNNING, PAUSED)


# ------------------------------------------------------------ déclenchement
def indexed_model() -> str:
    """Modèle qui a calculé les vecteurs en base (déduit et noté au premier appel)."""
    row = db.query_one("SELECT value FROM settings WHERE key=?", (INDEXED_KEY,))
    if row is not None:
        return json.loads(row["value"])
    if db.query_one("SELECT 1 FROM chunks LIMIT 1"):
        stored = db.query_one("SELECT value FROM settings WHERE key='embed_model'")
        model = json.loads(stored["value"]) if stored else PREVIOUS_DEFAULT
    else:
        model = settings.get("embed_model")
    _remember(model)
    return model


def _remember(model):
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (INDEXED_KEY, json.dumps(model, ensure_ascii=False)))


def _active_jobs() -> list:
    return [r["id"] for r in db.query(
        "SELECT id FROM jobs WHERE kind='reindex' AND status IN (?, ?, ?)", _ACTIVE)]


def schedule(resume=False, priority=NORMAL):
    """Lance la réindexation si embed_model n'est plus le modèle des vecteurs en
    base. resume=True (au démarrage) : relance aussi une réindexation annulée,
    échouée ou interrompue, s'il reste des passages sans vecteur.
    → id du travail créé, ou None."""
    model = settings.get("embed_model")
    job_id = None
    with db.transaction():
        changed = indexed_model() != model
        if changed:
            for previous in _active_jobs():          # visait un modèle déjà abandonné
                queue.cancel_job(previous)
            store.retire_vectors()
            _remember(model)
        waiting = db.query_one("SELECT 1 FROM chunks WHERE length(embedding) = 0 LIMIT 1")
        if waiting and (changed or (resume and not _active_jobs())):
            job_id = queue.create_job("reindex", {"model": model}, priority)
    if changed:
        search.invalidate()      # après la validation : pas de cache rempli avec l'ancien
    return job_id


# ---------------------------------------------------------------- découpage
def split_reindex(params):
    """Une unité par entrée dont des passages attendent leur vecteur."""
    rows = db.query("""SELECT e.id, e.source, e.kind, e.origin, c.system
                       FROM entries e JOIN collections c ON c.id = e.collection_id
                       WHERE EXISTS (SELECT 1 FROM chunks k WHERE k.entry_id = e.id
                                     AND length(k.embedding) = 0)
                       ORDER BY c.system, e.id""")
    units = []
    for row in rows:
        origin = row["origin"] or ""
        if row["system"] and row["kind"] == "conversation" and origin.isdigit():
            units.append(UnitSpec("index_conversation", "embed",
                                  {"conversation_id": int(origin), "rebuild": True,
                                   "source": row["source"]}))
        else:
            units.append(UnitSpec("reindex_entry", "embed",
                                  {"entry_id": row["id"], "source": row["source"],
                                   "path": _reread_path(row)}))
    return (f"Réindexer les connaissances avec {params['model']}", units,
            {"model": params["model"]})


def _reread_path(row):
    """Fichier à relire, ou None quand on garde le texte déjà extrait."""
    origin = row["origin"]
    if row["kind"] in ("file", "folder") and origin \
            and extract.classify(origin) in REREAD_FAMILIES:
        return origin
    return None


# -------------------------------------------------------------- exécution
@task("reindex_entry")
def run_reindex_entry(payload):
    """Recalcule les vecteurs d'une entrée avec le modèle d'embedding actuel."""
    stored = store.chunk_texts(payload["entry_id"])
    if not stored:
        return {"skipped": True, "reason": "retirée entre-temps"}
    fresh = _read(payload.get("path"))
    pieces = fresh or stored
    if not store.replace_chunks(payload["entry_id"], list(zip(pieces, ollama.embed(pieces)))):
        return {"skipped": True, "reason": "retirée entre-temps"}
    return {"chunks": len(pieces), "reread": bool(fresh)}


def _read(path):
    """Passages du fichier d'origine, ou None s'il a disparu, est illisible ou vide."""
    if not path or not Path(path).is_file():
        return None
    try:
        text = extract.extract_text(path)
    except Exception as exc:      # illisible désormais : le texte déjà extrait suffit
        log.info("Relecture impossible de %s (%s) : texte déjà extrait conservé", path, exc)
        return None
    return chunking.chunk_text(text) if text and text.strip() else None


def finalize_reindex(job_id, params, units):
    done = [u for u in units if u["status"] == "done"]
    conversations = sum(u["task"] == "index_conversation" for u in done)
    reread = sum(bool(u["result"].get("reread")) for u in done)
    return {"indexed": len(done),
            "chunks": sum(u["result"].get("chunks", 0) for u in done),
            "message": f"Modèle : {params['model']}. {len(done) - conversations} élément(s) "
                       f"de bibliothèque recalculé(s), dont {reread} relu(s) depuis leur "
                       f"fichier ; {conversations} conversation(s) remémorisée(s)."}


register_job_kind("reindex", split_reindex, finalize_reindex)
