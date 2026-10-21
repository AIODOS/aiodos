"""Travaux d'indexation : fichiers & dossiers, pages web, mémoire des conversations.

Chaque fichier devient une unité autonome. Fichiers et pages web sont des
tâches portables : le calcul (extraction du texte, découpage, embeddings) peut
se faire sur une autre machine, l'enregistrement dans la bibliothèque se fait
toujours ici. La mémoire des conversations, elle, ne quitte jamais ce nœud.
Les appels aux modèles suivent la cadence du travail de fond (jobs/pacing.py) :
ils attendent la fin d'une conversation, et se reposent sur batterie.
"""
import fnmatch
import json
import os
import random
import re
import stat
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

from aiodos import db, i18n, paths, settings
from aiodos.chat import conversations
from aiodos.i18n import t
from aiodos.jobs import pacing, queue
from aiodos.jobs.models import BACKGROUND, QUEUED
from aiodos.jobs.registry import (TASKS, Interrupted, SampleMismatch, TaskSpec, UnitSpec,
                                 register_job_kind, register_portable, task)
from aiodos.knowledge import chunking, store
from aiodos.services import addresses, extract, media, ollama, web

# Dossiers jamais parcourus (outils de dev, corbeille…)
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "$RECYCLE.BIN",
             "System Volume Information"}

# Attributs Windows d'un fichier ou dossier qu'un parcours ne ramasse pas : caché ou
# système (pas « point d'analyse » : les fichiers OneDrive en portent un)
_HIDDEN_ATTRS = (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0)
                 | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0))
# Fichiers de clés et de mots de passe (d'AIODOS ou d'autres logiciels) : un parcours de
# dossier ne les ramasse jamais, ils partiraient sinon chez un ami qui calcule
SECRET_NAMES = {"aiodos-net.key", "aiodos-public.key", "approbation.pem", "rclone.conf",
                "sitemanager.xml", "recentservers.xml", "filezilla.xml", "_netrc",
                "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials"}
SECRET_EXT = {".key", ".pem", ".p12", ".pfx", ".kdbx", ".ppk", ".ovpn", ".env", ".jks",
              ".keystore", ".p8", ".gpg"}
# Clés de comptes de service et jetons OAuth enregistrés en JSON (Google Cloud…)
SECRET_PATTERNS = ("client_secret*.json", "*credentials*.json", "*service*account*.json")

# Ressource utilisée selon la famille de fichier
_RESOURCE = {"text": "embed", "document": "embed", "unknown": "embed",
             "image": "gpu", "media": "cpu"}


# ----------------------------------------------------------- fichiers/dossiers
def _plan_file(path: Path, collection_id, source, kind, skipped: Counter):
    family = extract.classify(path)
    ext = path.suffix.lower() or t("index.no_extension")
    ignored = (family == "binary"
               or (family == "image" and not settings.get("media_vision"))
               or (family == "media" and not settings.get("media_transcribe"))
               # Extension inconnue : un binaire (wallet.dat…) ne donnerait aucun texte, et
               # partirait sinon tel quel chez la machine qui calcule
               or (family == "unknown" and not extract.looks_like_text(path)))
    if ignored:
        skipped[ext] += 1
        return None
    return UnitSpec("index_file", _RESOURCE[family],
                    {"path": str(path), "collection_id": collection_id,
                     "source": source, "kind": kind, "stamp": _stamp(path)})


def _stamp(path):
    """[taille, date de modification en ns] du fichier, ou None s'il est illisible : ce
    qu'il était à la création de l'unité. Au retour d'un ami, on sait ainsi s'il a changé
    depuis (sample_embedded) ; s'il a changé, un écart n'est pas forcément le fait de
    l'ami. Ce repère ne part pas chez lui (shared_keys)."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def _real(path) -> str:
    """Chemin réel comparable (casse et liens compris sous Windows)."""
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(str(path)))


def _aiodos_roots() -> set:
    """Les dossiers d'AIODOS : ses données (base, clés des machines appairées et du
    compagnon), et sa copie installée. Jamais parcourus, même choisis comme dossier."""
    roots = [paths.data_dir()]
    installed = paths.installed_data_dir()
    if installed is not None:
        roots += [paths.APP_ROOT, installed.parent]
    return {_real(r) for r in roots}


# Sous-dossiers du dossier de données qu'AIODOS remplit lui-même : données de sa fenêtre
# (webview : stockage local, cookies), journaux, fichiers reçus des autres machines (tmp),
# clé d'approbation des mises à jour, moteurs, modèles, images et vignettes, cache réparti
DATA_SUBDIRS = ("cluster", "engines", "images", "llm", "logs", "models", "tmp", "updates",
                "webview")


def _inside_aiodos(folder) -> bool:
    """Ce dossier choisi est-il un des dossiers d'AIODOS, ou se trouve-t-il dans l'un d'eux ?
    Son dossier de données lui-même, les sous-dossiers qu'il y remplit (DATA_SUBDIRS) et
    tout ce qu'ils contiennent, sa copie installée et tout ce qu'elle contient. Un autre
    dossier rangé dans le dossier de données n'en est pas : AIODOS_DATA_DIR peut viser un
    dossier de l'utilisateur, où il garde aussi ses documents."""
    chosen = Path(_real(folder))
    data = Path(_real(paths.data_dir()))
    if chosen == data:
        return True
    roots = [Path(_real(data / name)) for name in DATA_SUBDIRS]
    installed = paths.installed_data_dir()
    if installed is not None:
        roots += [Path(_real(paths.APP_ROOT)), Path(_real(installed.parent))]
    return any(chosen.is_relative_to(root) for root in roots)


def _app_data_roots() -> set:
    """Données des applications sous Windows (%APPDATA%, %LOCALAPPDATA%, AppData) : clés et
    jetons d'autres logiciels, invisibles dans l'Explorateur. Un parcours ne les traverse
    pas ; l'utilisateur peut encore en choisir un dossier nommément."""
    if os.name != "nt":
        return set()
    roots = [Path.home() / "AppData"]
    roots += [Path(os.environ[v]) for v in ("APPDATA", "LOCALAPPDATA") if os.environ.get(v)]
    return {_real(r) for r in roots}


def _hidden(entry) -> bool:
    """Caché : nom en « . » (.git, .ssh, .env…), ou attribut caché ou système de Windows."""
    if entry.name.startswith("."):
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & _HIDDEN_ATTRS)


def _secret(name: str) -> bool:
    lowered = name.lower()
    return (lowered in SECRET_NAMES or Path(lowered).suffix in SECRET_EXT
            or any(fnmatch.fnmatchcase(lowered, pattern) for pattern in SECRET_PATTERNS))


def _walk(folder: Path):
    """Fichiers d'un dossier, récursivement, dans l'ordre des noms. Ce qu'un parcours
    ramasse en passant ne comprend pas ce qui contient souvent des secrets : fichiers et
    dossiers cachés (nom en « . », ou attribut caché ou système de Windows), fichiers de
    clés (SECRET_NAMES, SECRET_EXT, SECRET_PATTERNS), dossiers d'AIODOS et données des
    applications. Les liens symboliques et jonctions ne sont pas suivis, ni vers un dossier
    ni vers un fichier (un lien dans un dépôt cloné peut viser une clé ou AppData). Un
    dossier d'AIODOS (ou l'un de ses sous-dossiers, cf. _inside_aiodos) n'est jamais
    parcouru, même choisi directement. Un fichier que l'utilisateur choisit nommément,
    lui, est indexé (split_index_paths)."""
    protected = _aiodos_roots() | _app_data_roots()
    if _inside_aiodos(folder):
        return
    stack = [Path(folder)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as found:
                entries = sorted(found, key=lambda e: e.name)
        except OSError:
            continue
        subfolders = []
        for entry in entries:
            if _hidden(entry):
                continue
            try:
                link = entry.is_symlink() or getattr(entry, "is_junction", lambda: False)()
                if entry.is_dir(follow_symlinks=False):
                    if not link and entry.name not in SKIP_DIRS \
                            and _real(entry.path) not in protected:
                        subfolders.append(Path(entry.path))
                elif not link and entry.is_file(follow_symlinks=False) \
                        and not _secret(entry.name):
                    yield Path(entry.path)
            except OSError:
                continue
        stack.extend(reversed(subfolders))


def split_index_paths(params):
    collection_id = params["collection_id"]
    paths = [Path(p) for p in params["paths"]]
    units, skipped = [], Counter()
    folders = [p for p in paths if p.is_dir()]
    for path in paths:
        if path.is_dir():
            root = path.name or str(path)
            for f in _walk(path):
                unit = _plan_file(f, collection_id,
                                  f"{root}/{f.relative_to(path).as_posix()}",
                                  "folder", skipped)
                if unit:
                    units.append(unit)
        elif path.is_file():
            unit = _plan_file(path, collection_id, path.name, "file", skipped)
            if unit:
                units.append(unit)
    target = store.collection_name(collection_id)
    # Le titre, traduit pour qui le lit (i18n.lazy, cf. jobs/queue.py)
    if len(paths) == 1 and folders:
        title = i18n.lazy("index.job_index_folder", name=folders[0].name, target=target)
    else:
        title = i18n.lazy("index.job_index_items", n=len(paths), target=target)
    info = {"skipped": sum(skipped.values()), "skipped_types": dict(skipped)}
    if not units:
        info["message"] = t("index.no_usable_file")
    return title, units, info


def index_options() -> dict:
    """Réglages du propriétaire, figés au moment du calcul : une machine qui
    calcule pour une autre applique ceux de l'autre, pas les siens. Sa langue aussi
    (lang) : description d'une image et en-têtes des passages, dans la sienne ; une
    version précédente l'ignore."""
    vision = None
    if settings.get("media_vision"):
        vision = {"model": settings.get("vision_model"),
                  "mode": settings.get("media_vision_mode")}
    return {"embed_model": settings.get("embed_model"), "vision": vision,
            "whisper": settings.get("whisper_model") if settings.get("media_transcribe") else None,
            "lang": i18n.current()}


def requires_index(unit, options) -> dict:
    """Ce qu'il faut à une machine pour calculer cette unité. Un document demande
    en plus de savoir lire son format : le paquet de lecture (python-pptx…) peut
    manquer là-bas. Texte et extensions inconnues se lisent partout."""
    need = {"embed": options["embed_model"]}
    if unit["resource"] == "gpu" and options.get("vision"):
        need["vision"] = options["vision"]["model"]
    if unit["resource"] == "cpu" and options.get("whisper"):
        need["whisper"] = options["whisper"]
    if unit["task"] == "index_file":
        path = Path(unit["payload"]["path"])
        if extract.classify(path) == "document":
            need["format"] = path.suffix.lower()
    return need


def _embed(pieces, options, stop) -> dict:
    vectors = pacing.embed(pieces, model=options["embed_model"], stop=stop)
    return {"pieces": pieces, "vectors": np.asarray(vectors, dtype=np.float32)}


def compute_index_file(payload, file, options, stop=None):
    with i18n.using(options.get("lang")):          # la langue du propriétaire
        return _compute_index_file(payload, file, options, stop)


def _compute_index_file(payload, file, options, stop):
    path = Path(file)
    if not path.exists():
        raise FileNotFoundError(t("index.file_not_found",
                                  path=payload.get("path") or payload["source"]))
    vision = transcribe = None
    pace = lambda seconds: pacing.step(seconds, stop)       # entre deux pages, deux segments
    if options.get("vision"):
        model, mode = options["vision"]["model"], options["vision"]["mode"]
        vision = lambda p: pacing.paced(lambda: media.vision_extract(p, model, mode), stop)
    if options.get("whisper"):
        transcribe = lambda p: media.transcribe(
            p, options["whisper"], stop=stop, threads=pacing.threads(media.WHISPER_THREADS),
            pace=pace)
    text = extract.extract_text(path, vision=vision, transcribe=transcribe, pace=pace)
    if stop is not None and stop.is_set():
        raise Interrupted()
    if not text or not text.strip():
        return _nothing_in(path)
    return _embed(chunking.chunk_text(text), options, stop)


def _nothing_in(path) -> dict:
    """Unité sautée faute de texte, avec la raison quand c'est une limite d'AIODOS (archive
    refusée, fichier trop gros : extract.why_unread) plutôt que le fichier qui est vide."""
    return {"skipped": True, "ext": path.suffix.lower() or t("index.no_extension"),
            "reason": extract.why_unread(path) or t("index.no_usable_text")}


def assimilate_index_file(payload, output):
    _refuse_unchecked(output)
    if output.get("skipped"):
        return _skipped(output)
    store.upsert_entry(payload["collection_id"], payload["source"], payload["kind"],
                       payload["path"], list(zip(output["pieces"], output["vectors"])))
    return {"chunks": len(output["pieces"])}


def _skipped(output) -> dict:
    """Une unité sautée, telle que la file la garde (units.result) : son extension et sa
    raison, en texte et bornées, rien d'autre. Une sortie peut venir d'une autre machine :
    un tableau joint faisait échouer son enregistrement (json.dumps) après la reprise de
    l'unité, qui restait prise jusqu'au redémarrage, et une longue chaîne était gardée telle
    quelle à chaque unité (relecture finale du 21/10/2026)."""
    return {"skipped": True, "ext": str(output.get("ext") or "?")[:40],
            "reason": str(output.get("reason") or t("index.no_usable_text"))[:500]}


SAMPLE_MIN = 0.999     # cosinus minimal d'un passage recalculé ici (0,9999 mesuré PC/S9)
SAMPLE_COUNT = 3       # passages revectorisés ici pour chaque résultat d'ami
# Posée par le contrôle (sample_embedded) sur la sortie d'un ami qu'on ne peut pas vérifier
# ici : assimilate la refuse, sans mettre l'ami de côté (cf. _refuse_unchecked)
REFUSED = "refused_here"
# Espaces et césures ignorés en comparant un passage au document relu ici
_IGNORED = re.compile(r"[\s\u00ad\u200b-\u200d\u2060\ufeff]+")


def checkable_here(payload) -> bool:
    """Sais-je relire ici, à bas coût, le texte de cette unité pour contrôler ce qu'un ami
    en rend ? Oui pour un fichier texte, une extension inconnue, un document dont j'ai le
    lecteur. Non pour une page web (elle ne se relit pas à l'identique), une image, un son
    ou une vidéo (OCR, description et transcription coûtent autant que le calcul confié),
    un document que je ne sais pas lire, un fichier disparu. Seules les unités vérifiables
    partent chez un ami : c'est le crochet « checkable » de la tâche (registry.
    friend_checkable, network/work.lease)."""
    path = payload.get("path")
    if not path:
        return False
    p = Path(path)
    family = extract.classify(p)
    if family == "document" and p.suffix.lower() not in extract.readable_documents():
        return False
    return family in ("text", "unknown", "document") and p.is_file()


def _normalized(text: str) -> str:
    """Le texte sans espaces ni césures, en forme NFKC : deux versions d'un lecteur
    (pypdf…) peuvent espacer ou lier les lettres autrement (« ﬁ »), pas changer les mots."""
    return _IGNORED.sub("", unicodedata.normalize("NFKC", text))


def _all_from_document(pieces, local_text) -> bool:
    """Chaque passage se trouve-t-il dans le document relu ici, dans l'ordre (espaces mis
    à part) ? Sinon l'ami a glissé sous le nom de mon document un texte à lui."""
    document, position = _normalized(local_text), 0
    for piece in pieces:
        wanted = _normalized(piece)
        found = document.find(wanted, position)
        if found < 0:
            return False
        position = found
    return True


def _differs(output, reason, excuse):
    """Écart entre ce que rend l'ami et le découpage refait ici. Un fichier texte (ou
    d'extension inconnue) inchangé depuis qu'il lui a été confié se relit à l'identique
    partout : l'écart est son fait (excuse None : SampleMismatch, il est mis de côté).
    Sinon (excuse : un document peut se lire un peu autrement d'une version de lecteur à
    l'autre, un fichier a pu changer entre-temps) : refusé sans reproche, recalculé chez
    moi."""
    if excuse is None:
        raise SampleMismatch(reason)
    output[REFUSED] = t("index.refused_recompute", reason=reason, excuse=excuse)


def _excuse(path, stamp, before, after):
    """Pourquoi un écart pourrait ne pas être le fait de l'ami, None s'il n'y a aucune
    excuse. Le fichier a changé depuis la création de l'unité (journal qui grandit,
    document modifié pendant le calcul : relecture du 21/10/2026), ou pendant ma relecture ;
    l'unité ne porte pas de repère (créée par une version d'avant) ; c'est un document."""
    if not (isinstance(stamp, list) and len(stamp) == 2):
        return t("index.excuse_old_unit")
    if not (before == after == stamp):
        return t("index.excuse_file_changed")
    if extract.classify(path) == "document":
        return t("index.excuse_reader")
    return None


def _sample_vectors(pieces, vectors, options):
    """SAMPLE_COUNT passages tirés au hasard, revectorisés ici avec le même modèle : leurs
    vecteurs doivent être ceux reçus. Un ami qui joindrait à un passage un vecteur de son
    choix (pour le faire remonter à chaque question) a ainsi bien plus d'une chance sur n
    d'être pris."""
    picks = random.sample(range(len(pieces)), min(SAMPLE_COUNT, len(pieces)))
    mine = ollama.embed([pieces[i] for i in picks], model=options["embed_model"])
    if len(mine) != len(picks):
        raise RuntimeError(t("index.vector_count_mismatch"))
    for i, vector in zip(picks, mine):
        here = np.asarray(vector, dtype=np.float32)
        theirs = np.asarray(vectors[i], dtype=np.float32)
        cosine = float(np.dot(here, theirs) / (np.linalg.norm(here) * np.linalg.norm(theirs)))
        if not cosine >= SAMPLE_MIN:
            raise SampleMismatch(t("index.sample_mismatch", number=i + 1,
                                   similarity=f"{cosine:.4f}", minimum=SAMPLE_MIN))


def sample_embedded(payload, output, options):
    """Contrôle d'une sortie calculée par un ami, avant de l'intégrer (network/work.result).
    Le document est relu et découpé ici (peu coûteux comparé aux embeddings) : les passages
    rendus doivent être exactement ce découpage. Ni morceaux choisis, ni oublis, ni
    répétitions, ni espaces déplacés ; un document qui a du texte ne peut pas être
    « sauté ». Puis SAMPLE_COUNT vecteurs sont recalculés ici.
    - Écart sur un fichier texte inchangé depuis la création de l'unité : SampleMismatch,
      l'ami est mis de côté. Sur un document, ou un fichier qui a changé entre-temps (cf.
      _excuse) : refusé sans reproche (cf. _differs).
    - Ce qui ne se relit pas ici (checkable_here : image, son, vidéo, page web, format sans
      lecteur ici, fichier illisible ici) n'est jamais intégré : marqué REFUSED, refusé par
      assimilate sans reproche, et recalculé sur mes machines (l'unité ne revient pas à un
      ami : il ne reçoit jamais le dernier essai).
    - Un document « sauté » à raison (sans texte ici non plus) garde la raison dite ici : le
      texte de l'ami n'est pas montré à l'utilisateur.
    Une autre exception (Ollama injoignable ici) : vérification impossible pour l'instant."""
    output.pop(REFUSED, None)
    if not checkable_here(payload):
        output[REFUSED] = t("index.refused_uncheckable")
        return
    path = Path(payload["path"])
    before = _stamp(path)
    try:
        local_text = extract.extract_text(path) or ""
    except Exception as exc:          # noqa: BLE001  (PDF chiffré, abîmé…)
        output[REFUSED] = t("index.refused_unreadable", error=exc.__class__.__name__)
        return
    excuse = _excuse(path, payload.get("stamp"), before, _stamp(path))
    expected = chunking.chunk_text(local_text)
    if output.get("skipped"):
        if expected:
            _differs(output, t("index.skipped_by_friend"), excuse)
        else:
            # Refaite ici, pas complétée : rien de ce que l'ami y a joint ne reste
            output.clear()
            output.update(_nothing_in(path))
        return
    pieces = output["pieces"]
    if pieces != expected:
        reason = (t("index.passage_not_in_document")
                  if not _all_from_document(pieces, local_text) else
                  t("index.passages_cut_differently"))
        _differs(output, reason, excuse)
        return
    _sample_vectors(pieces, output["vectors"], options)


def _refuse_unchecked(output):
    """La sortie d'un ami que le contrôle n'a pas pu vérifier (REFUSED) n'entre pas dans
    la bibliothèque : l'unité échoue pour cet essai, avec la raison, et se refait ici."""
    if isinstance(output, dict) and output.get(REFUSED):
        raise ValueError(str(output[REFUSED]))


MAX_PIECE = 2 * chunking.CHUNK_SIZE    # un passage découpé ici en fait au plus CHUNK_SIZE


def check_embedded(payload, output):
    """Contrôle d'une sortie calculée sur une autre machine, avant de l'intégrer :
    forme, valeurs finies, et même dimension que les vecteurs déjà stockés."""
    if output.get("skipped"):
        return
    pieces, vectors = output.get("pieces"), output.get("vectors")
    if not isinstance(pieces, list) or not pieces or not all(isinstance(p, str) for p in pieces):
        raise ValueError(t("index.invalid_passages"))
    if any(len(p) > MAX_PIECE for p in pieces):
        raise ValueError(t("index.passage_too_long", max=MAX_PIECE))
    if not isinstance(vectors, np.ndarray) or vectors.ndim != 2 or len(vectors) != len(pieces):
        raise ValueError(t("index.invalid_vector_count"))
    if not np.isfinite(vectors).all() or (np.linalg.norm(vectors, axis=1) < 1e-6).any():
        raise ValueError(t("index.invalid_vectors"))
    dim = store.embedding_dim(payload["collection_id"])
    if dim and vectors.shape[1] != dim:
        raise ValueError(t("index.wrong_dimension", got=vectors.shape[1], expected=dim))


register_portable("index_file", compute_index_file, assimilate_index_file, file_key="path",
                  options=index_options, requires=requires_index, validate=check_embedded,
                  sample=sample_embedded, checkable=checkable_here)


def finalize_index(job_id, params, units):
    skipped = Counter()
    for unit in units:
        if unit["status"] == "skipped":
            skipped[unit["result"].get("ext", "?")] += 1
    job = db.query_one("SELECT result FROM jobs WHERE id=?", (job_id,))
    planned = json.loads(job["result"] or "{}").get("skipped_types", {})
    for ext, n in planned.items():
        skipped[ext] += n
    return {"indexed": sum(1 for u in units if u["status"] == "done"),
            "chunks": sum(u["result"].get("chunks", 0) for u in units),
            "skipped_types": dict(skipped.most_common())}


register_job_kind("index_paths", split_index_paths, finalize_index)


# ------------------------------------------------------------------------ web
def split_index_urls(params):
    """Une unité par page. Une page qui vise le réseau local ou cette machine (le wiki du
    NAS, un service local), demandée ici par l'utilisateur, devient une unité
    « index_url_here » : lue ici seulement. Elle se reconnaît sans résolution de nom, à son
    adresse IP privée ou à son nom local (addresses.is_private_target). Les autres,
    « index_url », ne lisent que des adresses publiques, où qu'elles soient calculées."""
    urls = [u.strip() for u in params["urls"] if u.strip()]
    units = [UnitSpec("index_url_here" if addresses.is_private_target(u) else "index_url",
                      "net", {"url": u, "collection_id": params["collection_id"]})
             for u in urls]
    target = store.collection_name(params["collection_id"])
    return i18n.lazy("index.job_read_pages", n=len(units), target=target), units, {}


def _read_page(url, options, stop, public_only):
    try:
        title, text = web.fetch_url_text(url, public_only=public_only, stop=stop)
    except web.FetchStopped as exc:
        raise Interrupted() from exc
    if not text.strip():
        return {"skipped": True, "ext": "url", "reason": t("index.empty_page")}
    output = _embed(chunking.chunk_text(f"{title}\n\n{text}"), options, stop)
    output["title"] = title
    return output


def compute_index_url(payload, file, options, stop=None):
    """Une page web, adresses publiques seulement : l'ordre peut venir d'une autre machine,
    et il ne doit jamais faire lire notre réseau local ni cette machine (adresse ou
    redirection : services/addresses.py)."""
    return _read_page(payload["url"], options, stop, public_only=True)


def assimilate_index_url(payload, output):
    _refuse_unchecked(output)
    if output.get("skipped"):
        return _skipped(output)
    store.upsert_entry(payload["collection_id"], payload["url"], "url", payload["url"],
                       list(zip(output["pieces"], output["vectors"])))
    return {"chunks": len(output["pieces"]), "title": output.get("title")}


# Entre mes machines seulement (mine_only, sans friends_out ni friends_in : aucun réglage ne
# l'ouvre). Jamais confiée à un ami : le texte d'une page ne se relit pas ici à
# l'identique, il pourrait y substituer le sien. Jamais calculée pour un ami non plus : une
# version d'AIODOS d'après le 20/10/2026 ne m'en confie pas, et seul un ami malveillant (ou
# resté sur une version d'avant) m'en confierait, pour lire des pages depuis mon adresse
# IP (services qui n'ouvrent leur accès qu'à l'adresse de mon entreprise…) et en recevoir
# le texte. Ma machine refuse alors l'unité (network/worker.py).
register_portable("index_url", compute_index_url, assimilate_index_url,
                  options=index_options, requires=requires_index, validate=check_embedded,
                  shared_keys=("url",), sample=sample_embedded, mine_only=True)


def run_index_url_here(payload, file=None, options=None, stop=None):
    """Une page du réseau local ou de cette machine, demandée par l'utilisateur ici :
    tâche non portable, donc jamais confiée ailleurs ni calculée pour une autre machine.
    C'est ce qui permet de lever la garde des adresses publiques. stop arrête la lecture et
    les embeddings, comme pour une page publique."""
    output = _read_page(payload["url"], index_options(), stop, public_only=False)
    return assimilate_index_url(payload, output)


# Tâche simple (sans assimilate : non portable), enregistrée sans le décorateur task, qui
# ne transmet pas l'arrêt (stop) à la fonction
TASKS["index_url_here"] = TaskSpec("index_url_here", run_index_url_here)


register_job_kind("index_urls", split_index_urls, finalize_index)


# ---------------------------------------------- mémoire des conversations
def split_index_conversation(params):
    row = db.query_one("SELECT title FROM conversations WHERE id=?",
                       (params["conversation_id"],))
    title = row["title"] if row else f"#{params['conversation_id']}"
    return (i18n.lazy("index.job_memorize", title=title),
            [UnitSpec("index_conversation", "embed", dict(params))], {})


@task("index_conversation")
def run_index_conversation(payload):
    """Met la mémoire de la conversation à jour, message par message : un message déjà
    là (même numéro, mêmes passages) n'est pas recalculé ; un message retiré
    (« Régénérer », « Modifier », « Répondre en texte ») perd ses passages, et un numéro
    repris par SQLite pour un autre message est recalculé. En reconstruction (payload
    « rebuild », après un changement de modèle d'embedding), tout est recalculé.
    Ce qui change pendant le calcul (message retiré ou remplacé, conversation supprimée)
    n'entre pas en mémoire : tout est revérifié au moment d'écrire.
    Chaque passage porte l'id du message dans son index : idx = id×1000 + n."""
    conv_id = payload["conversation_id"]
    rebuild = bool(payload.get("rebuild"))
    collection_id = store.conversation_collection_id()
    conv = db.query_one("SELECT title FROM conversations WHERE id=?", (conv_id,))
    if conv is None:
        store.remove_by_origin(collection_id, str(conv_id))     # mémoire orpheline
        return {"skipped": True, "ext": "conversation", "reason": t("index.conversation_deleted")}
    entry = store.entry_by_origin(collection_id, str(conv_id))
    if rebuild and entry is None:
        return {"skipped": True, "ext": "conversation", "reason": t("index.memory_cleared")}
    known = store.message_chunks(entry["id"]) if entry and not rebuild else {}
    messages = db.query("SELECT id, role, content, sources, created FROM messages "
                        "WHERE conversation_id=? ORDER BY id", (conv_id,))
    read = {m["id"]: m["content"] for m in messages}
    pieces, kept, changed = [], set(), []
    for m in messages:
        if m["role"] == "assistant" and conversations.cites_others(m["sources"]):
            continue          # appuyée sur des passages d'autres machines : pas en mémoire
        if m["role"] == "assistant" and conversations.is_error(m):
            continue          # une erreur, pas une réponse
        # « Utilisateur : … » ou « User: … », selon la langue du nœud : changée, elle fait
        # refaire les passages de la conversation à sa mise à jour suivante (ils diffèrent)
        said = "index.memory_user" if m["role"] == "user" else "index.memory_assistant"
        texts = chunking.chunk_text(t(said, text=m["content"]))[:999]
        if not texts:
            continue
        kept.add(m["id"])
        if m["id"] in known:
            if known[m["id"]] == texts:
                continue                  # déjà en mémoire
            changed.append(m["id"])       # numéro repris par un autre message
        pieces.extend((m["id"] * 1000 + n, piece) for n, piece in enumerate(texts))
    drop = changed + [mid for mid in known if mid not in kept]
    if not pieces:
        if rebuild:                       # plus aucun message : rien à garder en mémoire
            store.remove_by_origin(collection_id, str(conv_id))
        elif drop:
            store.drop_message_chunks(collection_id, str(conv_id), drop)
        return {"chunks": 0}
    vectors = pacing.embed([p for _, p in pieces])
    with db.transaction() as c:           # revérifié d'un seul geste avec l'écriture
        if c.execute("SELECT 1 FROM conversations WHERE id=?", (conv_id,)).fetchone() is None:
            store.remove_by_origin(collection_id, str(conv_id))
            return {"skipped": True, "ext": "conversation",
                    "reason": t("index.conversation_deleted")}
        now = {r["id"]: r["content"] for r in c.execute(
            "SELECT id, content FROM messages WHERE conversation_id=?", (conv_id,))}
        still = [(idx, text, vec) for (idx, text), vec in zip(pieces, vectors)
                 if now.get(idx // 1000) == read[idx // 1000]]
        gone = [mid for mid in set(known) | set(read) if mid not in now]
        if still:
            store.append_chunks(collection_id, f"{conv['title']} · #{conv_id}", "conversation",
                                str(conv_id), still, replace=rebuild, drop_messages=drop + gone)
        elif rebuild:
            store.remove_by_origin(collection_id, str(conv_id))
        elif drop or gone:
            store.drop_message_chunks(collection_id, str(conv_id), drop + gone)
    return {"chunks": len(still)}


def queue_conversation(conv_id) -> int | None:
    """Met à jour la mémoire de cette conversation : un travail, sauf s'il y en a déjà un en
    attente pour elle (il lira aussi la nouvelle réponse). → son numéro, ou None."""
    waiting = db.query_one("SELECT id FROM jobs WHERE kind='index_conversation' AND status=? "
                           "AND json_extract(params, '$.conversation_id')=?", (QUEUED, conv_id))
    if waiting is not None:
        return None
    return queue.create_job("index_conversation", {"conversation_id": conv_id},
                            priority=BACKGROUND)


register_job_kind("index_conversation", split_index_conversation)
