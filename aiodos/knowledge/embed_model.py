"""Le modèle d'embeddings de cette machine, pour le travail de fond : indexation des fichiers
et des pages, mémoire des conversations, réindexation.

Constaté le 29/10/2026 sur le PC de l'auteur : le réglage embed_model (« embeddinggemma », le
défaut) désignait un modèle absent du moteur intégré (celui d'Ollama ne s'y charge pas), et
chaque fichier d'un dossier ajouté a échoué l'un après l'autre, deux essais chacun, pendant
plus d'un quart d'heure : des centaines d'échecs au journal, rien dans la bibliothèque.

Désormais :
- l'absence se constate au premier calcul qui en a besoin (pacing.embed lève Missing) :
  l'unité est rendue sans compter d'essai (jobs/executor.py), aucun échec par fichier ;
- premier indexage (aucun passage dans aucune bibliothèque, mémoire des conversations
  comprise) : un autre modèle d'embeddings déjà là, et qui se charge, est pris d'office,
  enregistré comme modèle d'embeddings (des vecteurs ne se comparent qu'entre ceux d'un même
  modèle) et dit : une notice gardée jusqu'à ce qu'on la ferme (NOTICE_KEY), et le journal.
  Jamais de téléchargement : seulement ce qui est déjà sur la machine ;
- sinon (des passages existent déjà : changer de modèle en silence rendrait leurs vecteurs
  incomparables, ou déferait un modèle choisi dans les Réglages dont la réindexation attend),
  cette machine ne prend plus les unités qui en demandent (crochet « here »
  des tâches : elles restent en file pour une de mes machines qui a le modèle), et chaque
  travail concerné le dit (queue._summary : « blocked »), avec ce qu'il faut faire : choisir
  un modèle présent (le réglage, puis la réindexation, jobs/kinds/reindex.py), ou télécharger
  celui-ci. Le blocage tombe de lui-même quand le réglage change ou que le modèle apparaît
  (relu toutes les RECHECK_S secondes) ; les écouteurs (on_change) le disent à l'interface.
"""
import json
import logging
import math
import threading
import time

from aiodos import db, i18n, paths, settings
from aiodos.i18n import t
from aiodos.llm import integrated, models as llm_models
from aiodos.services import ollama

log = logging.getLogger("aiodos.knowledge")

RECHECK_S = 20                # un modèle absent est-il apparu ? relu au plus aussi souvent
PROBE = "AIODOS"              # texte d'essai d'un modèle candidat (i18n: tel quel)
# Modèles d'embeddings mesurés en français (docs/embeddings.md), du meilleur au moins bon :
# le premier présent ici qui se charge est pris d'office ; tout autre modèle d'embeddings vient
# après eux
PREFERRED = ("embeddinggemma", "bge-m3", "snowflake-arctic-embed2", "qwen3-embedding",
             "nomic-embed-text")
# Ceux qu'Ollama convertit à sa façon : leur fichier d'Ollama ne se charge pas dans llama.cpp
# (mesuré le 20/10/2026, llm/models.py) ; la version téléchargée par AIODOS, si
OLLAMA_FORMAT = ("embeddinggemma", "snowflake-arctic-embed2")
NOTICE_KEY = "embed_model_auto"      # réglage interne : le modèle pris d'office, à dire
# Travaux dont les unités calculent des embeddings (le blocage s'y affiche)
EMBED_KINDS = ("index_paths", "index_urls", "index_conversation", "reindex")
ACTIVE = ("queued", "running", "paused")

_lock = threading.RLock()
# Modèle absent constaté ici, dernière relecture, et la base où il l'a été (une autre base,
# un autre dossier de données : rien de constaté)
_state = {"model": None, "checked": 0.0, "db": None}
_listeners = []


class Missing(RuntimeError):
    """Le modèle d'embeddings demandé n'est pas sur cette machine (model : son nom)."""

    def __init__(self, model):
        self.model = str(model or "")
        super().__init__(t("embed_model.missing", model=self.model))


# ------------------------------------------------------------------------ noms
def full(name) -> str:
    """« bge-m3 » → « bge-m3:latest » (les noms d'Ollama, ceux du moteur intégré aussi)."""
    return llm_models.full_name(name)


def same(a, b) -> bool:
    return bool(str(a or "").strip()) and full(a) == full(b)


def configured() -> str:
    return str(settings.get("embed_model") or "").strip()


def from_engine(exc, model) -> "Missing | None":
    """L'erreur d'un appel d'embeddings : Missing si elle dit que ce modèle manque ici (absent,
    ou écarté parce que llama.cpp ne sait pas le charger), sinon None."""
    if isinstance(exc, (integrated.ModelMissing, integrated.Incompatible)):
        if not exc.name or same(exc.name, model):
            return Missing(model)
    return None


# --------------------------------------------------------------- sur la machine
def models_here() -> list | None:
    """Les modèles d'embeddings de cette machine (noms complets), lus au moteur qui répond ;
    None quand on ne peut pas le savoir (Ollama injoignable…)."""
    try:
        if ollama.use_integrated():
            return [m.name for m in llm_models.available() if m.embedding]
        names = [m["name"] for m in ollama.list_models()]
        return [n for n, caps in ollama.capabilities_many(names).items() if "embedding" in caps]
    except Exception as exc:  # noqa: BLE001 — moteur injoignable : on ne sait pas
        log.debug("Modèles d'embeddings de cette machine illisibles : %s", exc)
        return None


def present(model) -> bool | None:
    names = models_here()
    return None if names is None else any(same(n, model) for n in names)


def _doubtful(name) -> bool:
    """Un modèle d'Ollama que le moteur intégré ne charge pas d'habitude (converti à la façon
    d'Ollama, llm/models.py) : proposé en dernier."""
    base = full(name).split(":")[0]
    if base not in OLLAMA_FORMAT or not ollama.use_integrated():
        return False
    found = llm_models.find(name)
    return found is not None and found.source == "ollama"


def _rank(name):
    base = full(name).split(":")[0]
    return (_doubtful(name), PREFERRED.index(base) if base in PREFERRED else len(PREFERRED),
            full(name))


def candidates(missing=None) -> list:
    """Les autres modèles d'embeddings de cette machine, du meilleur au moins bon (PREFERRED)."""
    return sorted((n for n in models_here() or [] if not same(n, missing)), key=_rank)


def indexed_anything() -> bool:
    """Quelque chose est-il déjà indexé, dans une bibliothèque ou dans la mémoire des
    conversations ? Des passages qui attendent leur réindexation (vecteur retiré : le modèle
    vient d'être choisi dans les Réglages) comptent aussi : ce choix-là ne se défait pas d'office."""
    return db.query_one("SELECT 1 FROM chunks LIMIT 1") is not None


def loads(name) -> bool:
    """Ce modèle rend-il un vecteur ici ? (un essai d'un mot ; un modèle que llama.cpp ne sait
    pas charger est écarté au passage, llm/integrated.py)"""
    try:
        vectors = ollama.embed([PROBE], model=name, timeout=120)
    except Exception as exc:  # noqa: BLE001 — absent, incompatible, moteur arrêté
        log.info("Modèle d'embeddings « %s » essayé : %s", name, exc)
        return False
    if len(vectors) != 1 or not vectors[0]:
        return False
    norm = math.sqrt(sum(float(x) * float(x) for x in vectors[0]))
    return math.isfinite(norm) and norm > 0


# ------------------------------------------------------------- absence constatée
def on_change(callback):
    """callback() quand le blocage commence ou tombe, ou qu'un modèle est pris d'office (le
    serveur le dit à l'interface et réveille la file). → de quoi le retirer (à l'arrêt)."""
    _listeners.append(callback)

    def forget():
        if callback in _listeners:
            _listeners.remove(callback)
    return forget


def _changed():
    for callback in list(_listeners):
        try:
            callback()
        except Exception:  # noqa: BLE001 — un écouteur en erreur ne bloque pas les autres
            log.exception("Écouteur du modèle d'embeddings en erreur")


def on_missing(model) -> str | None:
    """Une unité de cette machine n'a pas trouvé le modèle d'embeddings `model` (elle est
    rendue sans compter d'essai). → le modèle avec lequel elle se refera (réglage changé
    entre-temps, ou modèle pris d'office au premier indexage), ou None : bloqué, dit une fois."""
    with _lock:
        current = configured()
        if not same(current, model):
            return current
        if _state["model"] is not None and same(_state["model"], model):
            return None
        # Bloqué d'abord : pendant qu'on essaie les autres modèles (quelques secondes pour en
        # charger un), la file ne reprend pas ces unités pour les voir échouer aussitôt
        _state.update(model=model, checked=time.monotonic(), db=str(paths.db_path()))
    taken = _adopt(model)
    with _lock:
        if taken is not None and _state["model"] is not None and same(_state["model"], model):
            _state.update(model=None, checked=0.0)
    if taken is None:
        log.warning("Modèle d'embeddings « %s » absent de cette machine : ce qui en a besoin "
                    "attend (aucun échec compté) qu'on en choisisse un présent ou qu'on le "
                    "télécharge", model)
    _changed()
    return taken


def _adopt(missing) -> str | None:
    """Premier indexage : un autre modèle présent, qui se charge, pris d'office (cf. plus
    haut). → son nom, ou None (quelque chose est déjà indexé, ou aucun autre modèle ne
    convient)."""
    if indexed_anything():
        return None
    for name in candidates(missing):
        if not loads(name):
            continue
        settings.set_value("embed_model", name)        # le seuil de pertinence le suit
        from aiodos.jobs.kinds import reindex           # (import tardif : reindex → pacing → ici)
        reindex.schedule()                              # modèle des vecteurs en base : noté
        _remember(missing, name)
        log.warning("Modèle d'embeddings « %s » absent de cette machine : « %s », déjà là, "
                    "pris d'office et enregistré (premier indexage, aucun vecteur existant)",
                    missing, name)
        return name
    return None


def blocked() -> str | None:
    """Le modèle d'embeddings absent qui retient le travail de fond ici, ou None. Le blocage
    tombe quand le réglage change, ou quand le modèle apparaît (installé, Ollama relancé)."""
    with _lock:
        model = _state["model"]
        if model is None:
            return None
        if _state["db"] != str(paths.db_path()):
            _state.update(model=None, checked=0.0, db=None)
            return None
        lifted = not same(configured(), model)
        if not lifted and time.monotonic() - _state["checked"] > RECHECK_S:
            _state["checked"] = time.monotonic()
            lifted = present(model) is True
        if lifted:
            _state.update(model=None, checked=0.0)
    if lifted:
        log.info("Modèle d'embeddings : « %s » n'attend plus (réglage changé ou modèle "
                 "présent)", model)
        _changed()
        return None
    return model


def here(_model=None) -> bool:
    """Crochet « here » des tâches qui calculent des embeddings (registry.TaskSpec.here) :
    cette machine les prend-elle ? Pas tant que leur modèle y manque."""
    return blocked() is None


def job_note(kind, status) -> dict | None:
    """Ce qu'un travail en cours dit de ce blocage (vue Tâches, bibliothèque), ou None."""
    if kind not in EMBED_KINDS or status not in ACTIVE:
        return None
    model = blocked()
    if model is None:
        return None
    return {"model": model, "message": i18n.lazy("embed_model.job_blocked", model=model)}


def forget():
    """Essais : rien de constaté."""
    with _lock:
        _state.update(model=None, checked=0.0, db=None)


# ----------------------------------------------------------------------- notice
def _remember(missing, taken):
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (NOTICE_KEY, json.dumps({"missing": missing, "model": taken, "at": db.now()},
                                       ensure_ascii=False)))


def notice() -> dict | None:
    """Le modèle pris d'office, à dire tant qu'on ne l'a pas fermé et qu'il est toujours le
    modèle d'embeddings (changé depuis dans les Réglages : plus rien à dire)."""
    row = db.query_one("SELECT value FROM settings WHERE key=?", (NOTICE_KEY,))
    if row is None:
        return None
    try:
        found = json.loads(row["value"])
    except ValueError:
        return None
    if not isinstance(found, dict) or not same(found.get("model"), configured()):
        return None
    return found


def dismiss():
    db.execute("DELETE FROM settings WHERE key=?", (NOTICE_KEY,))


# ------------------------------------------------------------------- pour la vue
def _download_offer(model) -> dict | None:
    """Ce modèle se télécharge-t-il depuis « Installer un modèle » ? (sa taille, sa licence :
    affichées avant le clic ; rien ne se télécharge sans lui)"""
    from aiodos.services import model_install
    for entry in model_install.CATALOG:
        if entry.get("kind") == "embedding" and same(entry["name"], model):
            return {"name": entry["name"], "size": entry["size"], "license": entry["license"]}
    return None


def status() -> dict:
    """L'état du modèle d'embeddings, pour la bibliothèque et les réglages."""
    model = configured()
    names = models_here()
    here_now = None if names is None else any(same(n, model) for n in names)
    return {"model": model, "blocked": blocked(), "present": here_now,
            "candidates": sorted((n for n in names or [] if not same(n, model)), key=_rank),
            "indexed": indexed_anything(), "notice": notice(),
            "download": None if here_now else _download_offer(model)}
