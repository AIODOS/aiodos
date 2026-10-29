"""Ce que cette machine calcule pour d'autres : l'historique de la vue Tâches, « Pour d'autres »
(demandé par l'auteur le 29/10/2026 : son PC avait créé l'image de son téléphone par le réseau
public, et la vue Tâches du PC n'en disait rien).

Une ligne par calcul fini, refusé ou arrêté, fait ici pour :
- le réseau public : conversations (network/public_lending.py), lectures pour le contrôle d'un
  demandeur (network/lend_reading.py), images (network/public_imaging.py), vidéos
  (network/public_video.py) ;
- mes machines ou mes amis : les unités de leurs travaux (network/worker.py), et les
  conversations et lectures servies par mes modèles (network/lending.py, lend_reading.py).

Ses colonnes (table lent_history, migration 31 de db.py) :

  at        la fin (time.time) ;
  kind      le genre (KINDS) : « chat » (conversation), « reading » (lecture de contrôle),
            « image », « video », « index » (un fichier à indexer), « page » (une page web à
            indexer), « engine » (un travail ComfyUI d'une appli), « unit » (une autre unité) ;
  circle    pour qui (CIRCLES) : « public » (un inconnu du réseau public), « friend » (un ami),
            « mine » (une de mes machines) ;
  peer      le nom de l'ami ou de ma machine, tel qu'il était ; jamais pour le réseau public ;
  model     le modèle ;
  seconds   la durée de calcul ;
  tokens, width, height, frames, passages : sa taille (jetons rendus d'une conversation, ou lus
            d'une lecture ; largeur × hauteur d'une image ou d'une vidéo, et son nombre
            d'images ; passages d'un fichier ou d'une page indexés) ;
  outcome   l'issue (OUTCOMES) : « done » (fait), « failed » (échec), « stopped » (arrêté en
            route), « refused » (refusé avant tout calcul) ;
  reason    sa raison courte, un code (« refused_content » : le filtre ; « busy » : la machine
            était prise ; « left » : le demandeur est parti…), jamais un message ;
  times     combien de fois : des refus ou des arrêts identiques (même genre, cercle, machine,
            modèle, issue et raison) à moins de MERGE_S secondes ne font qu'une ligne, la plus
            récente (durées additionnées). Un inconnu qui enverrait des demandes refusées à la
            chaîne (120 par minute pour tout le public) ne remplit pas l'historique.

Jamais le contenu : ni texte, ni description, ni image, ni nom de fichier ou adresse d'une page
à indexer. Pour le réseau public, jamais rien qui identifie le demandeur : ni sa clé jetable, ni
la clé d'un nœud, ni une adresse ; le modèle seulement quand il est installé ici (un nom demandé
puis refusé pourrait être n'importe quel texte de l'inconnu, cf. public_lending.Refused).

Borné : MAX_DAYS jours et MAX_ROWS lignes, dont MAX_REFUSED refus au plus (les refus ne
chassent pas le travail fait) ; les plus anciennes s'effacent à chaque écriture. Écrit par un
fil à part (_writer), depuis une file bornée : le calcul n'attend jamais la base, et une
erreur d'écriture ne va qu'au journal, en debug, sans rien arrêter.

En direct (live) : ce qui se calcule ici pour d'autres en ce moment (begin, puis Lent.end, qui
écrit la ligne), en mémoire seulement. L'interface suit l'événement {"type": "lent"} (listen :
le bus du serveur), au début et à la fin de chaque calcul.
"""
import datetime
import itertools
import logging
import math
import queue
import re
import threading
import time

from aiodos import db, paths

log = logging.getLogger("aiodos.net")

# Pour qui
PUBLIC, FRIEND, MINE = "public", "friend", "mine"
CIRCLES = (PUBLIC, FRIEND, MINE)
# Le genre
CHAT, READING, IMAGE, VIDEO = "chat", "reading", "image", "video"
INDEX, PAGE, ENGINE, UNIT = "index", "page", "engine", "unit"
KINDS = (CHAT, READING, IMAGE, VIDEO, INDEX, PAGE, ENGINE, UNIT)
# L'issue
DONE, FAILED, STOPPED, REFUSED = "done", "failed", "stopped", "refused"
OUTCOMES = (DONE, FAILED, STOPPED, REFUSED)
MERGED = (REFUSED, STOPPED)        # issues dont les lignes identiques se rejoignent
# Les unités de mes machines ou de mes amis, par tâche (jobs/registry.TASKS)
UNIT_KINDS = {"index_file": INDEX, "index_url": PAGE, "engine_unit": ENGINE}
SIZES = ("tokens", "width", "height", "frames", "passages")

MAX_DAYS = 90
MAX_ROWS = 5000
MAX_REFUSED = 1000
MERGE_S = 3600                     # s : des refus ou arrêts identiques font une seule ligne
WEEK_DAYS = 7                      # les totaux : aujourd'hui, et les 7 derniers jours
PAGE_SIZE = 50                     # lignes d'une page (GET /api/lent), par défaut…
MAX_PAGE = 200                     # … et au plus
QUEUE_MAX = 1000                   # lignes en attente d'écriture, au plus (au-delà : perdues)
MAX_MODEL = 200                    # un nom de modèle, au plus (public_lending.MAX_MODEL_NAME)
MAX_NAME = 80                      # le nom d'une machine, au plus
MAX_SECONDS = 10 ** 7              # une durée de calcul, au plus (une ligne fusionnée en cumule)
MAX_SIZE = 10 ** 9                 # une taille (jetons, côtés, images, passages), au plus
_REASON = re.compile(r"[a-z][a-z_]{0,31}")
_PRINTABLE = re.compile(r"[^\x00-\x1f\x7f]+")

_lock = threading.Lock()
_live = {}                         # numéro -> Lent : les calculs pour d'autres en cours
_numbers = itertools.count(1)
_listener = {"publish": None}      # publish({"type": "lent"}) : le bus du serveur (listen)
_items = queue.Queue(maxsize=QUEUE_MAX)
_writer = {"thread": None}
_writer_lock = threading.Lock()


# ------------------------------------------------------------------ nettoyage
def _model(value) -> str | None:
    """Un nom de modèle borné, sans caractère de contrôle."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_MODEL or not _PRINTABLE.fullmatch(value):
        return None
    return value


def _name(value) -> str | None:
    """Le nom d'une machine appairée, borné (il vient d'elle, un ami peut-être)."""
    if not isinstance(value, str):
        return None
    value = " ".join(_PRINTABLE.findall(value)).strip()[:MAX_NAME]
    return value or None


def _reason(value) -> str | None:
    """Une raison courte : un code (« busy », « refused_content »…), jamais un message."""
    return value if isinstance(value, str) and _REASON.fullmatch(value) else None


def _seconds(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(min(max(0.0, float(value)), MAX_SECONDS), 1)


def _size(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_SIZE else None


def circle_of(node_id) -> str:
    """Le cercle d'une machine appairée : une de mes machines, ou un ami (sharing.level :
    inconnue, le plus étroit ; illisible aussi, sans faire échouer le calcul)."""
    try:
        from aiodos.network import sharing       # à la demande : sharing importe peers
        return MINE if sharing.level(node_id) == sharing.MINE else FRIEND
    except Exception:  # noqa: BLE001 — l'historique ne fait jamais échouer un calcul
        return FRIEND


def _peer_name(node_id) -> str | None:
    if not isinstance(node_id, str) or not node_id:
        return None
    row = db.query_one("SELECT name FROM peers WHERE node_id=?", (node_id,))
    return _name(row["name"]) if row else None


def _row(kind, circle, outcome, reason, peer_id, peer, model, seconds, sizes, at) -> dict:
    """La ligne, nettoyée (cf. le début du module). Le réseau public : ni machine, ni nom."""
    if circle not in CIRCLES:
        raise ValueError(circle)                    # i18n: tel quel (jamais montré)
    public = circle == PUBLIC
    return {"at": float(at) if at is not None else time.time(),
            "kind": kind if kind in KINDS else UNIT, "circle": circle,
            "peer_id": None if public else peer_id, "peer": None if public else _name(peer),
            "model": _model(model), "seconds": _seconds(seconds),
            **{key: _size(sizes.get(key)) for key in SIZES},
            "outcome": outcome if outcome in OUTCOMES else FAILED, "reason": _reason(reason)}


# ---------------------------------------------------------------- enregistrer
def record(kind, circle, outcome, reason=None, *, peer_id=None, peer=None, model=None,
           seconds=None, at=None, **sizes):
    """Une ligne de plus à l'historique, écrite en fond : ne lève jamais, n'attend jamais la
    base. peer_id : la machine appairée dont le nom est cherché à l'écriture, si `peer` ne le
    donne pas ; sizes : tokens, width, height, frames, passages."""
    try:
        row = _row(kind, circle, outcome, reason, peer_id, peer, model, seconds, sizes, at)
        _push((str(paths.db_path()), row))
    except Exception:  # noqa: BLE001 — l'historique ne fait jamais échouer un calcul
        # i18n: tel quel (journal)
        log.debug("Historique du travail pour d'autres : ligne perdue", exc_info=True)


def refused(kind, circle, reason, *, peer_id=None, peer=None, model=None):
    """Un refus avant tout calcul (le filtre, la machine prise, le prêt coupé…)."""
    record(kind, circle, REFUSED, reason, peer_id=peer_id, peer=peer, model=model)


class Lent:
    """Un calcul pour d'autres, en cours (live), puis inscrit une fois à l'historique (end)."""

    def __init__(self, kind, circle, peer_id=None, peer=None, model=None):
        self.kind, self.circle, self.model = kind, circle, model
        self.peer_id = None if circle == PUBLIC else peer_id
        self.peer = None if circle == PUBLIC else _name(peer)
        self.since = time.time()
        self.number = next(_numbers)
        self.ended = False

    def view(self) -> dict:
        return {"kind": self.kind if self.kind in KINDS else UNIT, "circle": self.circle,
                "peer": self.peer if self.peer or self.peer_id is None
                else _peer_name(self.peer_id),
                "model": _model(self.model), "since": self.since}

    def end(self, outcome, reason=None, seconds=None, **sizes):
        """La fin du calcul : plus en cours, une ligne à l'historique. Une seule fois (les
        appels suivants ne font rien) ; ne lève jamais."""
        with _lock:
            if self.ended:
                return
            self.ended = True
            _live.pop(self.number, None)
        record(self.kind, self.circle, outcome, reason, peer_id=self.peer_id, peer=self.peer,
               model=self.model, seconds=seconds, **sizes)


def begin(kind, circle, *, peer_id=None, peer=None, model=None) -> Lent:
    """Un calcul pour d'autres commence : noté en cours, l'interface prévenue. → Lent, dont
    end() écrit la ligne. Ne lève jamais (le calcul ne dépend pas de l'historique)."""
    lent = Lent(kind, circle, peer_id, peer, model)
    with _lock:
        _live[lent.number] = lent
    _tell()
    return lent


def live() -> list:
    """Ce qui se calcule ici pour d'autres en ce moment, les plus anciens d'abord :
    [{"kind", "circle", "peer", "model", "since"}]."""
    with _lock:
        going = sorted(_live.values(), key=lambda lent: lent.since)
    return [lent.view() for lent in going]


def unit_fields(order) -> dict:
    """Le genre et le modèle d'une unité de mes machines ou de mes amis (worker.Assignment) :
    {"kind", "model"}. L'ordre vient de l'autre machine : rien n'en est cru que ce qui se
    vérifie ici (un modèle d'images du catalogue), jamais son contenu. Ne lève jamais (appelé
    pendant le calcul) : une unité, sans modèle, faute de mieux."""
    try:
        return _unit_fields(order)
    except Exception:  # noqa: BLE001 — l'historique ne fait jamais échouer un calcul
        # i18n: tel quel (journal)
        log.debug("Historique du travail pour d'autres : ordre illisible", exc_info=True)
        return {"kind": UNIT, "model": None}


def _unit_fields(order) -> dict:
    from aiodos.imaging import catalog        # à la demande : imaging dépend du réseau
    order = order if isinstance(order, dict) else {}
    task = order.get("task")
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    options = order.get("options") if isinstance(order.get("options"), dict) else {}
    if task == "generate_image":
        model = payload.get("model")
        known = isinstance(model, str) and model in catalog.MODELS
        return {"kind": VIDEO if known and catalog.is_video(model) else IMAGE,
                "model": model if known else None}
    kind = UNIT_KINDS.get(task, UNIT) if isinstance(task, str) else UNIT
    return {"kind": kind, "model": options.get("embed_model") if kind in (INDEX, PAGE) else None}


def unit_sizes(order, output=None) -> dict:
    """La taille d'une unité : largeur × hauteur (et images) d'une image ou d'une vidéo,
    d'après son ordre ; passages d'un fichier ou d'une page indexés, d'après sa sortie. Ne
    lève jamais (appelé à la fin du calcul) : rien, faute de mieux."""
    try:
        return _unit_sizes(order, output)
    except Exception:  # noqa: BLE001 — l'historique ne fait jamais échouer un calcul
        return {}


def _unit_sizes(order, output) -> dict:
    order = order if isinstance(order, dict) else {}
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    if order.get("task") == "generate_image":
        return {key: payload.get(key) for key in ("width", "height", "frames")}
    pieces = output.get("pieces") if isinstance(output, dict) else None
    return {"passages": len(pieces)} if isinstance(pieces, list) else {}


# ---------------------------------------------------------- l'écriture, en fond
def listen(publish):
    """publish(événement) : prévenir l'interface (le bus du serveur) ; None : plus personne."""
    _listener["publish"] = publish


def _tell():
    publish = _listener["publish"]
    if publish is None:
        return
    try:
        publish({"type": "lent"})
    except Exception:  # noqa: BLE001 — l'interface se remettra à jour au prochain
        # i18n: tel quel (journal)
        log.debug("Historique du travail pour d'autres : interface non prévenue", exc_info=True)


def _push(item):
    _start()
    try:
        _items.put_nowait(item)
    except queue.Full:
        # i18n: tel quel (journal)
        log.debug("Historique du travail pour d'autres : file pleine, ligne perdue")
        _tell()


def _start():
    with _writer_lock:
        thread = _writer["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_loop, daemon=True, name="aiodos-historique-prets")
            _writer["thread"] = thread
            thread.start()


def _loop():
    while True:
        item = _items.get()
        try:
            _write(*item)
        except Exception:  # noqa: BLE001 — au journal seulement, le fil continue
            # i18n: tel quel (journal)
            log.debug("Historique du travail pour d'autres : écriture impossible", exc_info=True)
        try:
            if _items.empty():
                db.close()    # aucune base gardée ouverte entre deux écritures
                _tell()       # avant task_done : flush() rendu, l'interface est prévenue
        except Exception:  # noqa: BLE001 — au journal seulement
            # i18n: tel quel (journal)
            log.debug("Historique du travail pour d'autres : base non fermée", exc_info=True)
        finally:
            _items.task_done()


def _write(path: str, row: dict):
    """La ligne dans la base où elle a été notée (une autre, pendant les essais : laissée),
    jointe à sa pareille récente s'il le faut (MERGED, MERGE_S), puis les bornes."""
    if path != str(paths.db_path()):
        return
    row = dict(row)
    peer_id = row.pop("peer_id")
    if row["peer"] is None and peer_id is not None:
        row["peer"] = _peer_name(peer_id)
    with db.transaction() as conn:
        if row["outcome"] in MERGED:
            same = conn.execute(
                "SELECT id, seconds, times FROM lent_history WHERE kind=? AND circle=? "
                "AND peer IS ? AND model IS ? AND outcome=? AND reason IS ? AND at >= ? "
                "ORDER BY id DESC LIMIT 1",
                (row["kind"], row["circle"], row["peer"], row["model"], row["outcome"],
                 row["reason"], row["at"] - MERGE_S)).fetchone()
            if same is not None:
                conn.execute("DELETE FROM lent_history WHERE id=?", (same["id"],))
                if same["seconds"] is not None or row["seconds"] is not None:
                    row["seconds"] = _seconds((same["seconds"] or 0) + (row["seconds"] or 0))
                row["times"] = same["times"] + 1
        columns = list(row)
        conn.execute(f"INSERT INTO lent_history({', '.join(columns)}) "
                     f"VALUES ({', '.join('?' * len(columns))})", [row[c] for c in columns])
        _prune(conn, time.time())


def _prune(conn, now: float):
    """Les bornes : MAX_DAYS jours, MAX_REFUSED refus, MAX_ROWS lignes ; les plus anciennes
    s'effacent."""
    conn.execute("DELETE FROM lent_history WHERE at < ?", (now - MAX_DAYS * 86400,))
    conn.execute("DELETE FROM lent_history WHERE outcome=? AND id <= (SELECT id FROM "
                 "lent_history WHERE outcome=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                 (REFUSED, REFUSED, MAX_REFUSED))
    conn.execute("DELETE FROM lent_history WHERE id <= (SELECT id FROM lent_history "
                 "ORDER BY id DESC LIMIT 1 OFFSET ?)", (MAX_ROWS,))


def flush(timeout: float = 5.0) -> bool:
    """Attend que les lignes notées soient écrites (essais) : vrai si c'est fait à temps."""
    deadline = time.monotonic() + timeout
    while _items.unfinished_tasks:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def forget():
    """Plus rien en cours (essais) ; les lignes notées sont écrites d'abord."""
    flush()
    with _lock:
        _live.clear()


# ------------------------------------------------------------------- lire
def page(before: int | None = None, limit: int = PAGE_SIZE) -> dict:
    """Une page de l'historique, les plus récents d'abord (par leur fin, puis par leur
    identifiant) : {"items": […], "more"}. before : l'identifiant de la dernière ligne de la
    page précédente ; la page suivante part d'elle (de son identifiant seul, si elle a été
    effacée ou réunie à une autre entre-temps)."""
    limit = max(1, min(MAX_PAGE, int(limit)))
    cutoff = time.time() - MAX_DAYS * 86400
    order = "ORDER BY at DESC, id DESC LIMIT ?"
    if before is None:
        rows = db.query(f"SELECT * FROM lent_history WHERE at >= ? {order}", (cutoff, limit + 1))
    else:
        mark = db.query_one("SELECT at FROM lent_history WHERE id=?", (int(before),))
        if mark is None:
            rows = db.query(f"SELECT * FROM lent_history WHERE at >= ? AND id < ? {order}",
                            (cutoff, int(before), limit + 1))
        else:
            rows = db.query(f"SELECT * FROM lent_history WHERE at >= ? AND (at < ? OR "
                            f"(at = ? AND id < ?)) {order}",
                            (cutoff, mark["at"], mark["at"], int(before), limit + 1))
    return {"items": [dict(r) for r in rows[:limit]], "more": len(rows) > limit}


def _midnight(now: float, days_back: int = 0) -> float:
    """Minuit, heure de cette machine, `days_back` jours avant le jour de `now`."""
    day = datetime.date.fromtimestamp(now) - datetime.timedelta(days=days_back)
    return time.mktime(day.timetuple())


def totals(now: float | None = None) -> dict:
    """Aujourd'hui (depuis minuit) et les WEEK_DAYS derniers jours (aujourd'hui compris), par
    cercle et en tout : {"n" : calculs faits, "seconds" : temps de calcul donné, arrêtés et
    échecs compris}. {"since": {"today", "week"}, "today": {cercle: …, "all": …}, "week": …}."""
    now = time.time() if now is None else now
    since = {"today": _midnight(now), "week": _midnight(now, WEEK_DAYS - 1)}
    out = {"since": since}
    for name, start in since.items():
        found = {circle: {"n": 0, "seconds": 0.0} for circle in (*CIRCLES, "all")}
        for r in db.query("SELECT circle, SUM(CASE WHEN outcome=? THEN times ELSE 0 END) AS n, "
                          "SUM(COALESCE(seconds, 0)) AS s FROM lent_history WHERE at >= ? "
                          "GROUP BY circle", (DONE, start)):
            if r["circle"] not in CIRCLES:
                continue
            found[r["circle"]] = {"n": int(r["n"] or 0), "seconds": round(r["s"] or 0.0, 1)}
            found["all"]["n"] += found[r["circle"]]["n"]
            found["all"]["seconds"] = round(found["all"]["seconds"] + (r["s"] or 0.0), 1)
        out[name] = found
    return out
