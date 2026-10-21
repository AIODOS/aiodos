"""L'adaptateur ComfyUI (docs/conception/partage-puissance.md § 3.1, 3.4, 3.6) : rejouer une
unité dans le ComfyUI de ce PC, par son API (engines/comfy_client.py).

Sur la machine qui calcule, ce PC compris :
1. check : l'ordre revérifié avec les définitions de ce ComfyUI (I14) : forme, valeurs
   interdites, nœuds du cloud, chaque classe présente (à la même empreinte si l'ordre est
   strict), chaque modèle présent ici sous le même dossier, le même nom (et la même taille
   quand on sait lire le fichier). Un refus lève Refused(reason) : contract, class, model.
2. Les entrées vont sous input/aiodos/<run_uuid>/in-<n>.<ext> (POST /upload/image).
3. Le graphe est réécrit (engines/comfy_graph.py) : @in: → ce nom, @model: → le nom local,
   @seed → la graine, aperçus retirés, chaque sortie sous aiodos/<run_uuid>/out. Rien
   d'autre ne change.
4. POST /prompt avec prompt_id = run_uuid et client_id = aiodos-<16 hex de cette instance>,
   sans extra_data.
5. Suivi par GET /api/jobs/<run_uuid> ; stop levé, ou bounds.unit_seconds dépassé :
   POST /api/jobs/<run_uuid>/cancel, ce calcul seulement (jamais ceux de l'utilisateur).
6. Les sorties des nœuds de sortie de l'ordre, dans aiodos/<run_uuid> seulement, en flux,
   octets comptés (bounds.output_mb).
7. L'historique de ce calcul est effacé ; ses dossiers d'entrée et de sortie aussi quand la
   ligne de commande de ComfyUI les nomme (--base-directory, --input-directory,
   --output-directory) ou qu'ils sont à côté de son main.py.
"""
import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from aiodos import settings
from aiodos.engines import comfy_client, comfy_contract, comfy_find, comfy_graph, media
from aiodos.jobs.registry import Interrupted

log = logging.getLogger("aiodos.engines")

NAME = "comfyui"
API = 1
POLL = 2.0
STAGES = {None: "waiting_engine", "pending": "waiting_engine", "in_progress": "running"}
CANCEL_WAIT = 15.0
INVENTORY_TTL = 3600.0
FIRST_POLL = 0.25

_lock = threading.Lock()
_inventory = {"at": 0.0, "url": None, "value": None}


class Refused(Exception):
    """L'ordre ne peut pas être joué sur cette machine : reason (contract, class, model,
    engine) ; rendu sans essai, et ce travail ne revient pas ici pour la même raison."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class Failed(Exception):
    """Le calcul a échoué dans ComfyUI : code (engine_error, engine_timeout, output_invalid,
    output_too_large), message, nœud."""

    def __init__(self, code: str, message: str, node: str | None = None):
        super().__init__(message)
        self.code, self.node = code, node


@dataclass(frozen=True)
class Output:
    path: Path
    node: str
    type: str
    size: int
    sha256: str


# ------------------------------------------------------------------ l'instance
def enabled() -> bool:
    return bool(settings.get("engines_comfyui_enabled"))


def instance():
    """Le ComfyUI de ce PC qui répond, s'il est accordé (engines_comfyui_enabled)."""
    return comfy_find.instance() if enabled() else None


def client_id() -> str:
    """aiodos-<16 hex> : propre à cette instance d'AIODOS, le même d'un lancement à l'autre ;
    il range nos calculs à part dans l'historique de ComfyUI."""
    try:
        from aiodos.node import identity
        seed = identity.node_id()
    except Exception:
        seed = "aiodos"
    return "aiodos-" + hashlib.sha256(f"comfyui:{seed}".encode()).hexdigest()[:16]


def inventory(url: str, refresh: bool = False) -> dict:
    """Ce que ce ComfyUI sait faire : version, empreintes des classes et du cœur, modèles
    par dossier, mémoire vidéo. Relu au plus une fois par heure (object_info pèse)."""
    with _lock:
        if (not refresh and _inventory["url"] == url and _inventory["value"] is not None
                and time.monotonic() - _inventory["at"] < INVENTORY_TTL):
            return _inventory["value"]
    client = comfy_client.Client(url)
    stats = client.system_stats()
    defs = client.object_info()
    models = {}
    for folder in client.model_folders():
        try:
            models[folder] = client.models(folder)
        except comfy_client.Unreachable:
            models[folder] = []
    system = stats.get("system") if isinstance(stats.get("system"), dict) else {}
    devices = [d for d in stats.get("devices") or [] if isinstance(d, dict)]
    vram = devices[0].get("vram_total") if devices else None
    value = {"version": str(system.get("comfyui_version") or ""), "defs": defs,
             "classes": {cls: comfy_contract.fingerprint(spec) for cls, spec in defs.items()
                         if isinstance(spec, dict)},
             "core": comfy_contract.core(defs), "models": models,
             "vram_mib": int(vram // 2 ** 20) if isinstance(vram, (int, float)) and vram > 0
             else None,
             "argv": [str(a) for a in system.get("argv") or []]}
    with _lock:
        _inventory.update(at=time.monotonic(), url=url, value=value)
    return value


def forget_inventory():
    with _lock:
        _inventory.update(at=0.0, url=None, value=None)


# ----------------------------------------------------------------- les modèles
def _same(a: str, b: str) -> bool:
    return a.replace("\\", "/").lower() == b.replace("\\", "/").lower()


def local_name(models: dict, folder: str | None, name: str) -> str | None:
    """Le nom exact, tel que ce ComfyUI le liste (avec la barre de son système), d'un
    modèle donné par dossier et nom relatif ; sans dossier (le propriétaire n'avait pas de
    ComfyUI pour le dire), cherché dans tous. None s'il n'est pas ici."""
    for listed_folder, listed_names in (models or {}).items():
        if folder and listed_folder != folder:
            continue
        for listed in listed_names or []:
            if _same(listed, name):
                return listed
    return None


def folders_of(models: dict, name: str) -> list:
    """Les dossiers de ce ComfyUI qui listent ce nom de modèle."""
    return sorted(f for f, names in (models or {}).items()
                  if any(_same(n, name) for n in names or []))


def _model_roots(argv) -> list:
    """Les dossiers de modèles qu'on sait lire ici : celui de Desktop, celui de la ligne de
    commande (--base-directory), celui d'à côté de main.py."""
    from aiodos.imaging import local_copies
    roots = [folder for folder, _ in local_copies._known()]
    base = _arg(argv, "--base-directory")
    if base:
        roots.insert(0, Path(base) / "models")
    main = Path(argv[0]) if argv and str(argv[0]).endswith("main.py") else None
    if main is not None and main.is_absolute():
        roots.append(main.parent / "models")
    return roots


def _arg(argv, flag: str) -> str | None:
    argv = list(argv or ())
    for i, value in enumerate(argv):
        if value == flag and i + 1 < len(argv):
            return argv[i + 1]
        if value.startswith(flag + "="):
            return value.split("=", 1)[1]
    return None


def _size_of(roots, folder: str, name: str) -> int | None:
    for root in roots:
        path = Path(root) / folder / name.replace("\\", "/")
        try:
            if path.is_file():
                return path.stat().st_size
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- vérifier
def check(order: dict, inv: dict) -> dict:
    """L'ordre revérifié ici (I14) → {"models": {jeton: nom local}} ; Refused sinon.
    Les erreurs de forme et de valeurs lèvent comfy_graph.GraphError (ordre invalide : il
    n'aurait jamais dû partir)."""
    graph = order.get("graph")
    comfy_graph.check_shape(graph, "mine")
    defs = inv.get("defs") or {}
    comfy_graph.check_values(graph, defs)
    cloud = comfy_graph.cloud_nodes(graph, defs)
    if cloud:
        raise comfy_graph.GraphError("forbidden_node", "Nœud d'un service en ligne : refusé.",
                                     node=cloud[0], **{"class": graph[cloud[0]]["class_type"]})
    missing = sorted({n["class_type"] for n in graph.values()} - set(inv.get("classes") or {}))
    if missing:
        raise Refused("class", f"Ce ComfyUI n'a pas le nœud « {missing[0]} ».")
    contract = order.get("contract") or {}
    if order.get("strict") and contract.get("classes"):
        for cls, fingerprint in contract["classes"].items():
            if inv["classes"].get(cls) != fingerprint:
                raise Refused("contract", f"Le nœud « {cls} » n'a pas la même définition ici.")
    roots = _model_roots(inv.get("argv"))
    names = {}
    for model in order.get("models") or []:
        folder, name = str(model.get("folder") or ""), str(model.get("name") or "")
        found = local_name(inv.get("models") or {}, folder, name) if name else None
        if found is None:
            raise Refused("model", f"Il manque le modèle « {name or model.get('sha256')} » "
                                   f"({folder or 'tout dossier'}) sur cette machine.")
        where = folder or (folders_of(inv.get("models") or {}, found) or [""])[0]
        size = _size_of(roots, where, found) if where else None
        if size is not None and model.get("size") and size != int(model["size"]):
            raise Refused("model", f"Le modèle « {name} » n'est pas le même ici (taille).")
        names[str(model["token"]).removeprefix(comfy_graph.TOKEN_MODEL)] = found
    return {"models": names}


# ------------------------------------------------------------------- calculer
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(order: dict, uploaded: dict, models: dict, run_id: str) -> dict:
    """Le graphe à donner à ComfyUI : jetons remplacés, aperçus retirés, sorties sous
    aiodos/<run_id>/out."""
    graph = comfy_graph.without_previews(order["graph"])
    graph = comfy_graph.replace_tokens(graph, uploaded, models, seed=order.get("seed"))
    outputs = [n for n in order.get("outputs") or [] if n in graph
               and "filename_prefix" in graph[n].get("inputs", {})]
    return comfy_graph.set_prefix(graph, outputs, f"aiodos/{run_id}/out")


def run(order: dict, inputs: dict, folder: Path, stop: threading.Event, progress=None,
        url: str | None = None) -> list:
    """Joue l'unité dans le ComfyUI de ce PC → [Output] ; Refused, Failed, ou Interrupted
    (arrêt demandé, ComfyUI fermé en route : l'unité est rendue sans essai)."""
    progress = progress or (lambda stage, **info: None)
    if url is None:
        found = instance()
        if found is None:
            raise Interrupted("ComfyUI n'est pas ouvert sur ce PC.")
        url = found.url
    client = comfy_client.Client(url)
    try:
        inv = inventory(url)
        checked = check(order, inv)
    except comfy_client.Unreachable as exc:
        raise Interrupted(str(exc)) from exc
    run_id = str(order.get("run_uuid") or comfy_client.new_run_id())
    bounds = order.get("bounds") or {}
    limit = int(float(bounds.get("output_mb") or 200) * 1024 ** 2)
    seconds = float(bounds.get("unit_seconds") or 1800)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        progress("sending_inputs")
        uploaded = {}
        for n, item in enumerate(order.get("inputs") or [], 1):
            slot = str(item["slot"])
            path = Path(inputs[slot])
            name = f"in-{n}{media.extension(media.sniff_file(path))}"
            uploaded[slot] = client.upload(path.read_bytes(), name, f"aiodos/{run_id}")
        graph = prepare(order, uploaded, checked["models"], run_id)
        if stop.is_set():
            raise Interrupted("arrêt demandé")
        try:
            client.queue(graph, run_id, client_id())
        except comfy_client.Refused as exc:
            raise Failed("engine_error", str(exc), node=exc.node) from exc
        job = _follow(client, run_id, stop, seconds, progress)
        progress("receiving")
        return _collect(client, run_id, order, folder, limit, job)
    except comfy_client.Unreachable as exc:
        raise Interrupted(f"ComfyUI ne répond plus : {exc}") from exc
    finally:
        client.forget(run_id)
        _clean(inv.get("argv"), run_id)


def _follow(client, run_id, stop, seconds, progress) -> dict:
    started = time.monotonic()
    wait, last = FIRST_POLL, None
    while True:
        if stop.wait(wait):
            _cancel(client, run_id)
            raise Interrupted("arrêt demandé")
        wait = POLL
        job = client.job(run_id)
        status = (job or {}).get("status")
        if status != last and status in STAGES:
            progress(STAGES[status])            # la file de ComfyUI (ses travaux d'abord)
            last = status
        if status == "completed":
            return job
        if status == "failed":
            error = job.get("execution_error") if isinstance(job.get("execution_error"), dict) \
                else {}
            message = str(error.get("exception_message") or "le calcul a échoué")[:300]
            raise Failed("engine_error", message, node=_text(error.get("node_id")))
        if status == "cancelled":
            raise Interrupted("calcul annulé dans ComfyUI")
        if time.monotonic() - started > seconds:
            _cancel(client, run_id)
            raise Failed("engine_timeout", f"Calcul plus long que permis ({int(seconds)} s).")


def _text(value):
    return str(value) if value not in (None, "") else None


def _cancel(client, run_id):
    """Ce calcul seulement ; on attend qu'il soit vraiment arrêté (CANCEL_WAIT au plus)."""
    client.cancel(run_id)
    deadline = time.monotonic() + CANCEL_WAIT
    while time.monotonic() < deadline:
        try:
            job = client.job(run_id)
        except comfy_client.Unreachable:
            return
        if job is None or job.get("status") in ("completed", "failed", "cancelled"):
            return
        time.sleep(0.25)


def _collect(client, run_id, order, folder, limit, job) -> list:
    entry = client.history(run_id) or {}
    produced = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
    wanted = set(order.get("outputs") or [])
    prefix = f"aiodos/{run_id}"
    found, total = [], 0
    for node_id in sorted(produced, key=comfy_graph._order_key):
        if node_id not in wanted or not isinstance(produced[node_id], dict):
            continue
        for items in produced[node_id].values():
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or item.get("type") != "output":
                    continue
                sub = str(item.get("subfolder") or "").replace("\\", "/")
                name = str(item.get("filename") or "")
                if not (sub == prefix or sub.startswith(prefix + "/")) or not name \
                        or "/" in name or "\\" in name or name.startswith("."):
                    continue
                dest = folder / f"{len(found) + 1:03d}-{name}"
                try:
                    size = client.download(name, sub, "output", dest, limit - total)
                except comfy_client.TooLarge as exc:
                    raise Failed("output_too_large", str(exc), node=node_id) from exc
                total += size
                found.append(Output(dest, node_id, media.sniff_file(dest), size,
                                    _sha256(dest)))
    if not found:
        raise Failed("output_invalid", "Le calcul n'a produit aucun fichier.")
    return found


def _clean(argv, run_id):
    """Les dossiers aiodos/<run_id> d'entrée et de sortie de ce ComfyUI, quand on les
    connaît : jamais rien d'autre."""
    argv = list(argv or ())
    base = _arg(argv, "--base-directory")
    main = Path(argv[0]).parent if argv and str(argv[0]).endswith("main.py") \
        and Path(argv[0]).is_absolute() else None
    root = Path(base) if base else main
    for flag, kind in (("--input-directory", "input"), ("--output-directory", "output")):
        chosen = _arg(argv, flag)
        top = Path(chosen) if chosen else (root / kind if root else None)
        if top is None:
            continue
        target = top / "aiodos" / run_id
        try:
            if target.is_dir() and os.path.commonpath([str(top.resolve()),
                                                       str(target.resolve())]) == str(top.resolve()):
                shutil.rmtree(target, ignore_errors=True)
        except OSError:
            pass
