# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Les travaux de l'appli (docs/api-applications.md § 5.7 ; conception « partager la
puissance », § 2.5 et 6.7) : `ai.jobs()`, `ai.job(id)`, `Job`.

- L'objet travail : `id`, `status` (queued, in_progress, pausing, paused, cancelling,
  completed, failed, cancelled), `label`, `client_ref`, `progress` (Progress : `percent`,
  `message`…), `units` (Unit), `files` (File), `error`, `cancelled_by`… ; `refresh()` le relit.
- `watch()` le suit en direct (`GET …/events`, SSE) : un JobEvent par nouvel état (« job »),
  par nouveau fichier (« file »), puis « end ». Coupé, le suivi reprend seul et relit l'état
  complet (AIODOS ne rejoue rien) ; tant qu'AIODOS ne répond pas, un événement
  « unreachable », puis de nouveaux essais. Si `cancel()` devient vrai, le travail est annulé
  dans AIODOS (Job.cancel()) et watch() lève Cancelled ; `timeout` dépassé : Timeout, et le
  travail continue dans AIODOS.
- `wait()`, les commandes (`pause`, `resume`, `cancel`, `retry_failed`, `delete`) et les
  fichiers (`files`, `download()`).

Une appli n'agit que sur ses propres travaux : celui d'une autre lui est introuvable
(JobNotFound). Tant que `features` n'annonce pas « jobs » (« jobs.pause » pour la pause,
« files » pour les fichiers), ces fonctions lèvent NotSupported sans rien essayer.
"""

import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import quote, urlencode

from . import _transport, errors
from .files import Downloads, File, FileList, names_for

log = logging.getLogger("aiodos_client")

FEATURE = "jobs"
PAUSE = "jobs.pause"
FILES = "files"
STATUSES = ("queued", "in_progress", "pausing", "paused", "cancelling", "completed", "failed",
            "cancelled")
FINISHED = ("completed", "failed", "cancelled")
PAUSE_WHEN = ("after_current", "now")
LIST_LIMIT = 200
WATCH_IDLE = 45.0               # le flux d'un travail parle au moins toutes les 15 s (« : ping »)
RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0)     # entre deux essais, quand AIODOS ne répond pas
# Ce qui coupe le suivi sans l'arrêter : AIODOS s'arrête, redémarre, ou ne suit plus
_TRANSIENT = (errors.Unreachable, errors.Timeout, errors.NotRunning, errors.GatewayClosed,
              errors.RateLimited, errors.EngineUnavailable, errors.PeerFailed)


def _transient(exc) -> bool:
    return isinstance(exc, _TRANSIENT) or (isinstance(exc.status, int) and exc.status >= 500)


def _int(value, default=None):
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    return value if isinstance(value, str) else None


def _circles(value):
    return tuple(str(c) for c in value) if isinstance(value, list) else ()


@dataclass(frozen=True)
class Machine:
    """Une machine qui calcule le travail : son nom, son cercle (`place` : here, mine,
    friend, public), ses unités en cours et faites."""
    name: str | None
    place: str | None = None
    running: int = 0
    done: int = 0
    raw: dict = field(default_factory=dict, repr=False, compare=False, hash=False)


@dataclass(frozen=True)
class Progress:
    """L'avancement : `percent` (0 à 100), `message` (« 1 sur 2 · 1 en cours sur 1
    machine »), les unités par état, l'étape la plus avancée (`stage`), les machines. Une
    estimation (`estimated`), `eta_s` quand AIODOS sait le dire."""
    total: int = 0
    done: int = 0
    running: int = 0
    queued: int = 0
    failed: int = 0
    percent: int | None = None
    estimated: bool = True
    elapsed_s: float | None = None
    eta_s: float | None = None
    stage: str | None = None
    machines: tuple = ()
    message: str = ""
    raw: dict = field(default_factory=dict, repr=False, compare=False, hash=False)


@dataclass(frozen=True)
class Unit:
    """Une unité du travail (un élément d'`items`) : son état, son étape (waiting_machine,
    sending_inputs, waiting_engine, running, receiving, checking), sa machine et son cercle
    (`place` : here, mine…), ses fichiers (identifiants), sa durée, et en échec son erreur
    ({"code", "message"} ; code engine_error, engine_timeout, output_invalid,
    output_too_large, ou no_machine : aucune machine ne l'a prise avant `bounds.wait_s`)."""
    index: int | None
    label: str | None = None
    status: str | None = None
    stage: str | None = None
    place: str | None = None
    machine: str | None = None
    files: tuple = ()
    seconds: float | None = None
    error: dict | None = field(default=None, compare=False, hash=False)
    raw: dict = field(default_factory=dict, repr=False, compare=False, hash=False)


@dataclass(frozen=True)
class JobEvent:
    """Un événement du suivi d'un travail. `kind` : « job » (un nouvel état), « file » (un
    nouveau fichier : `file`), « end » (la fin : `status`), « unreachable » (AIODOS ne répond
    plus : `error` ; le suivi reprend seul). `job` : le travail, à jour (`job.progress`…)."""
    kind: str
    job: object
    file: object = None
    status: str | None = None
    error: object = field(default=None, compare=False)
    data: object = field(default=None, repr=False, compare=False)


def _machine(entry):
    return Machine(name=_text(entry.get("name")), place=_text(entry.get("place")),
                   running=_int(entry.get("running"), 0), done=_int(entry.get("done"), 0),
                   raw=dict(entry))


def _progress(entry):
    if not isinstance(entry, dict):
        return Progress()
    machines = tuple(_machine(m) for m in entry.get("machines") or () if isinstance(m, dict))
    return Progress(total=_int(entry.get("total"), 0), done=_int(entry.get("done"), 0),
                    running=_int(entry.get("running"), 0), queued=_int(entry.get("queued"), 0),
                    failed=_int(entry.get("failed"), 0), percent=_int(entry.get("percent")),
                    estimated=entry.get("estimated") is not False,
                    elapsed_s=_number(entry.get("elapsed_s")), eta_s=_number(entry.get("eta_s")),
                    stage=_text(entry.get("stage")), machines=machines,
                    message=_text(entry.get("message")) or "", raw=dict(entry))


def _unit(entry):
    files = tuple(f for f in entry.get("files") or () if isinstance(f, str))
    return Unit(index=_int(entry.get("index")), label=_text(entry.get("label")),
                status=_text(entry.get("status")), stage=_text(entry.get("stage")),
                place=_text(entry.get("place")), machine=_text(entry.get("machine")),
                files=files, seconds=_number(entry.get("seconds")),
                error=dict(entry["error"]) if isinstance(entry.get("error"), dict) else None,
                raw=dict(entry))


class Job:
    """Un travail d'AIODOS. Ses champs sont ceux de l'objet travail, au dernier état connu
    (`refresh()`, `watch()` et les commandes le mettent à jour) : `id`, `status`, `label`,
    `client_ref`, `engine`, `priority`, `where`, `where_effective`, `where_reason`,
    `progress` (Progress), `waiting`, `units` (Unit), `files` (File ; appelée,
    `job.files(unfetched=False)` relit la liste dans AIODOS), `error`, `cancelled_by`,
    `created_at`, `started_at`, `completed_at` ; le champ « pause » ({"when", "pending"}
    pendant une pause) s'appelle `pause_info`, `pause()` étant la commande ; `data` : l'objet
    tel quel."""

    def __init__(self, client, job_id, data=None):
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id : l'identifiant d'un travail (« job_… »)")
        self._client = client
        self.id = job_id
        self._apply(data or {})

    def _apply(self, data):
        if not isinstance(data, dict) or data.get("id") not in (None, self.id):
            raise errors.make("protocol", self._client.lang)
        get = data.get
        files = FileList((File(self._client, f["id"], f, job_id=self.id)
                          for f in get("files") or ()
                          if isinstance(f, dict) and isinstance(f.get("id"), str)), job=self)
        fields = {
            "data": dict(data), "engine": _text(get("engine")), "label": _text(get("label")),
            "client_ref": _text(get("client_ref")), "status": _text(get("status")),
            "pause_info": dict(get("pause")) if isinstance(get("pause"), dict) else None,
            "priority": _text(get("priority")), "where": _circles(get("where")),
            "where_effective": _circles(get("where_effective")),
            "where_reason": _text(get("where_reason")), "progress": _progress(get("progress")),
            "waiting": dict(get("waiting")) if isinstance(get("waiting"), dict) else None,
            "units": tuple(_unit(u) for u in get("units") or () if isinstance(u, dict)),
            "files": files,
            "error": dict(get("error")) if isinstance(get("error"), dict) else None,
            "cancelled_by": _text(get("cancelled_by")), "created_at": _number(get("created_at")),
            "started_at": _number(get("started_at")),
            "completed_at": _number(get("completed_at"))}
        self.__dict__.update(fields)            # d'un seul tenant, pour les autres fils

    def __repr__(self):
        return f"Job({self.id!r}, status={self.status!r}, label={self.label!r})"

    @property
    def finished(self) -> bool:
        """Fini : completed (au moins une unité faite), failed (aucune) ou cancelled."""
        return self.status in FINISHED

    @property
    def _path(self):
        return f"/aiodos/v1/jobs/{quote(self.id, safe='')}"

    # --- L'état et les commandes ------------------------------------------------------
    def refresh(self):
        """Relit le travail dans AIODOS ; rend le Job. JobNotFound s'il n'existe plus."""
        self._apply(self._client._call("GET", self._path, feature=FEATURE))
        return self

    def _command(self, action, body=None, feature=FEATURE):
        data = self._client._call("POST", f"{self._path}/{action}",
                                  body=body if body is not None else {}, feature=feature)
        self._apply(data)
        return self

    def pause(self, when="after_current"):
        """Met le travail en pause : après l'unité en cours (rien ne se perd), ou « now »
        (le calcul en cours s'arrête dans ComfyUI, l'unité repartira sans compter d'essai).
        Rend le Job (« pausing », puis « paused »). JobNotPausable s'il est fini."""
        if when not in PAUSE_WHEN:
            raise ValueError("when : « after_current » ou « now »")
        return self._command("pause", {"when": when}, PAUSE)

    def resume(self):
        """Reprend un travail en pause, à son rang ; rend le Job."""
        return self._command("resume", None, PAUSE)

    def cancel(self):
        """Annule le travail : le calcul en cours s'arrête dans ComfyUI (ce calcul seulement),
        les fichiers déjà rendus restent (`cancelled_by` : « app »). Rend le Job
        (« cancelling », puis « cancelled »). JobNotCancellable s'il est déjà fini."""
        return self._command("cancel")

    def retry_failed(self):
        """Relance les unités en échec ; rend le Job."""
        return self._command("retry")

    def delete(self, files=False):
        """Oublie un travail fini (JobNotCancellable s'il ne l'est pas) ; ses fichiers suivent
        leur garde, sauf `files=True` qui les efface aussi. Rend vrai."""
        mode = "delete" if files else "keep"
        data = self._client._call("DELETE", f"{self._path}?files={mode}", feature=FEATURE)
        return isinstance(data, dict) and data.get("deleted") is True

    def set_priority(self, priority):
        """Change la priorité (high, normal, low) : pas encore servi par AIODOS,
        NotSupported (« jobs.priority »)."""
        self._client._refuse_unserved("jobs.priority")

    def report(self, unit, reason):
        """Signale un fichier douteux d'une unité (livraison 3) : NotSupported
        (« jobs.report »)."""
        self._client._refuse_unserved("jobs.report")

    # --- Les fichiers -----------------------------------------------------------------
    def _list_files(self, unfetched=False):
        query = "?unfetched=1" if unfetched else ""
        data = self._client._call("GET", f"{self._path}/files{query}", feature=FEATURE)
        entries = data.get("files") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise errors.make("protocol", self._client.lang)
        return FileList((File(self._client, f["id"], f, job_id=self.id) for f in entries
                         if isinstance(f, dict) and isinstance(f.get("id"), str)), job=self)

    def download(self, dest_dir, stem=None, on_file=None, cancel=None):
        """Télécharge les fichiers du travail dans `dest_dir` (créé s'il manque) : chacun en
        flux, repris par Range s'il a été coupé, son empreinte vérifiée ; un fichier servi en
        entier est récupéré pour AIODOS. Noms : « <stem>_1.png », « <stem>_2.png »… (un seul
        fichier : « <stem>.png ») ; sans `stem`, l'identifiant du fichier. Un fichier déjà là
        et identique n'est pas téléchargé de nouveau. `on_file(file, chemin)` après chacun.
        Rend les chemins (Downloads : une liste de Path, avec `files` et `job`)."""
        self._client._gate(FEATURE, FILES)
        stop = _transport.as_cancel(cancel)
        folder = Path(dest_dir)
        folder.mkdir(parents=True, exist_ok=True)
        files = self._list_files()
        paths = []
        for file, name in zip(files, names_for(files, stem)):
            if stop is not None and stop():
                raise errors.make("cancelled", self._client.lang)
            path = file.save(folder / name, cancel=cancel)
            paths.append(path)
            if on_file is not None:
                on_file(file, path)
        return Downloads(paths, files, self)

    # --- Le suivi ---------------------------------------------------------------------
    def watch(self, cancel=None, timeout=None):
        """Les événements du travail (JobEvent), jusqu'à sa fin : « job » (l'état, d'abord
        complet puis à chaque changement), « file » (chaque fichier pas encore récupéré, une
        fois), « end », et « unreachable » quand AIODOS ne répond plus (le suivi reprend
        seul, l'état complet relu). Si `cancel()` devient vrai (fonction ou
        threading.Event), le travail est annulé dans AIODOS et Cancelled est levée ; au-delà
        de `timeout` secondes, Timeout (le travail continue). Vérifie d'emblée ce qu'AIODOS
        sert (NotSupported…), puis se lit en itérant."""
        self._client._gate(FEATURE)
        stop = _transport.as_cancel(cancel)
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        return self._follow(stop, deadline, timeout)

    def wait(self, timeout=None, cancel=None):
        """Attend la fin du travail (completed, failed ou cancelled) et rend le Job, à jour.
        `cancel` et `timeout` : comme watch()."""
        for _ in self.watch(cancel=cancel, timeout=timeout):
            pass
        return self

    def _halt(self, stop, deadline, timeout):
        """L'arrêt demandé (le travail est annulé dans AIODOS, puis Cancelled), ou le délai
        dépassé (Timeout) ; rien sinon."""
        lang = self._client.lang
        if stop is not None and stop():
            try:
                self.cancel()
            except errors.JobNotCancellable:
                pass                                    # déjà fini
            except errors.AiodosError as exc:
                log.warning("Annulation de %s impossible (%s) : il continue dans AIODOS.",
                            self.id, exc.code)
            raise errors.make("cancelled", lang, detail={"job": self.id})
        if deadline is not None and time.monotonic() >= deadline:
            raise errors.make("timeout", lang, detail={"seconds": timeout, "job": self.id})

    def _follow(self, stop, deadline, timeout):
        client = self._client
        announced = set()
        down = False                    # « unreachable » déjà dit pour cette panne
        failures = 0

        def halted():                   # interrogé par le fil de surveillance du transport
            return ((stop is not None and stop())
                    or (deadline is not None and time.monotonic() >= deadline))

        while True:
            self._halt(stop, deadline, timeout)
            fresh = False               # au moins un état reçu sur cette connexion
            try:
                with client._stream(f"{self._path}/events", method="GET",
                                    timeout=WATCH_IDLE, cancel=halted,
                                    feature=FEATURE) as exchange:
                    for kind, name, value in exchange.messages():
                        if kind == "comment":
                            continue                    # « : ping »
                        data = _event_data(value, client.lang)
                        if name == "job":
                            self._apply(data)
                            fresh, down, failures = True, False, 0
                            yield JobEvent("job", self, data=data)
                            for file in list(self.files):
                                if file.id not in announced and not file.fetched:
                                    announced.add(file.id)
                                    yield JobEvent("file", self, file=file, data=file.data)
                        elif name == "file":
                            if not isinstance(data.get("id"), str):
                                raise errors.make("protocol", client.lang)
                            if data["id"] not in announced:
                                announced.add(data["id"])
                                file = File(client, data["id"], data, job_id=self.id)
                                yield JobEvent("file", self, file=file, data=data)
                        elif name == "end":
                            status = _text(data.get("status"))
                            if status in FINISHED and status != self.status:
                                self.refresh()
                            yield JobEvent("end", self, status=status, data=data)
                            return
                        else:                           # un événement d'une version suivante
                            yield JobEvent(name, self, data=data)
                        exchange.touch()                # le temps de l'appli ne compte pas
            except errors.Cancelled:
                self._halt(stop, deadline, timeout)
                raise
            except errors.AiodosError as exc:
                if not _transient(exc):
                    raise
                log.info("Suivi de %s interrompu (%s) : nouvel essai.", self.id, exc.code)
                if not fresh:
                    if not down:
                        down = True
                        yield JobEvent("unreachable", self, error=exc)
                    self._rest(failures, deadline, halted)
                    failures += 1
                continue
            if not fresh:                               # fermé sans rien dire : on souffle
                self._rest(failures, deadline, halted)
                failures += 1

    @staticmethod
    def _rest(failures, deadline, halted):
        delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
        if deadline is not None:
            delay = max(0.0, min(delay, deadline - time.monotonic()))
        _transport.pause(delay, halted)


def _event_data(value, lang):
    try:
        data = json.loads(value)
    except ValueError:
        raise errors.make("protocol", lang) from None
    if not isinstance(data, dict):
        raise errors.make("protocol", lang)
    return data


def list_jobs(client, active=True, client_ref=None, status=None):
    """Les travaux de l'appli (`GET /aiodos/v1/jobs`), les plus récents d'abord : `active`
    True (en cours), False (finis) ou None (tous) ; `client_ref` ; `status` : un état ou
    plusieurs."""
    query = {"limit": str(LIST_LIMIT)}
    if active is True:
        query["active"] = "1"
    elif active is False:
        query["active"] = "0"
    elif active is not None:
        raise ValueError("active : True, False ou None")
    if client_ref is not None:
        query["client_ref"] = str(client_ref)
    wanted = None
    if status is not None:
        wanted = {status} if isinstance(status, str) else set(status)
        unknown = wanted - set(STATUSES)
        if unknown:
            raise ValueError(f"status : parmi {', '.join(STATUSES)}")
        query["status"] = ",".join(s for s in STATUSES if s in wanted)
    data = client._call("GET", "/aiodos/v1/jobs?" + urlencode(query), feature=FEATURE)
    entries = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise errors.make("protocol", client.lang)
    jobs = [Job(client, entry["id"], entry) for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]]
    # filtré ici aussi : un AIODOS qui ne lit pas « status » les rend tous
    return [job for job in jobs if wanted is None or job.status in wanted]


def get_job(client, job_id):
    """Un travail par son identifiant (« job_… »)."""
    return Job(client, job_id).refresh()
