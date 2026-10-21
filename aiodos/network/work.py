"""Travail confié aux machines appairées : côté propriétaire du travail.

Une machine qui a de la puissance libre demande du travail (« work/lease »).
On lui confie la première unité de la file qu'elle sait traiter : même modèle
d'embedding au bit près (sinon ses vecteurs seraient incompatibles avec ceux de
la bibliothèque), document dans un format qu'elle sait lire (un nœud minimal
peut manquer de python-pptx…), ressource libre chez elle, bibliothèque non
réservée à cette machine. Elle lit le fichier morceau par morceau (« work/blob »,
seulement celui de l'unité qui lui est confiée), calcule, puis rend sa sortie
(« work/result ») : elle est contrôlée puis intégrée ici, jamais là-bas.

Rien n'est confié si l'utilisateur a désactivé l'aide de ses machines. À un ami, seules
les bibliothèques partageables, sous des noms neutres, et seulement ce dont je sais
contrôler le résultat ici (registry.friend_checkable) : ce qu'il rend est contrôlé par
échantillon, sur un document relu ici. Une image, un son, une vidéo ou un format que je ne
sais pas relire ne lui part pas : son résultat ne pourrait pas être pris (relecture du
21/10/2026). Une machine prise en défaut est mise de côté (cf. sharing.py).

Une machine d'une version d'avant reçoit encore ce qu'elle calcule sans risque pour moi
(même WORK_API), mais rien de ce qui dépend d'une défense ajoutée depuis : sa révision
(« work_revision », cf. WORK_REVISION) le dit. Une page web, lue depuis la machine qui
calcule, ne part qu'à une machine qui ne lit pour les autres que l'Internet public
(REVISION_FOR) ; une machine plus ancienne l'apprend par un message qui lui demande de se
mettre à jour, plutôt que par un refus muet. L'ordre dit aussi la taille de résultat que
j'accepterai (« result_max », cf. network/body_limit.py) : un résultat plus gros n'est pas
envoyé pour rien, la machine qui calcule le rend en échec (« too_big »), sans que cela
compte contre un ami, et l'unité revient ici.

Un ami ne peut pas non plus faire échouer mes travaux (audit du 20/10/2026). Il détient
quelques unités à la fois (FRIEND_MAX_HELD), chacune un temps borné (_friend_lease_max), et
ses demandes restées sans unité ou ses unités rendues sont comptées par minute. Une unité
que cette machine, ou une de mes machines en ligne, sait calculer : son échec la rend sans
compter d'essai, elle ne lui revient pas, et le dernier essai ne part jamais chez un ami
(un bail qu'il laisse expirer ne la fait donc pas échouer). Une unité que seuls mes amis
savent calculer (une image, si je l'accepte) leur reste ouverte jusqu'au bout : un échec ou
un bail échu y compte un essai, un autre ami passe d'abord pendant FRIEND_RETRY_OTHERS, et
après MAX_ATTEMPTS essais elle échoue avec un message qui le dit (sinon elle attendrait
pour toujours). Un ami qui ne rend que des échecs est mis de côté.
Appelées dans un thread par le serveur réseau (cf. p2p_app.HANDLERS).
"""
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from aiodos import db, i18n, settings
from aiodos.engines import lending
from aiodos.i18n import t
from aiodos.jobs import leases, queue, registry
from aiodos.jobs.kinds import engine as engine_kind
from aiodos.jobs.models import MAX_ATTEMPTS, QUEUED, RUNNING
from aiodos.knowledge import store
from aiodos.network import envelope, peers, profile, route, sharing, wire
from aiodos.network.client import PeerError
from aiodos.services import ollama

log = logging.getLogger("aiodos.work")

WORK_API = 1                   # contrat de calcul (sorties, options) : à incrémenter s'il change
# Révision du contrat, annoncée en plus par la machine qui calcule (« work_revision ») : ce
# qui s'ajoute sans casser l'échange avec une version d'avant, mais dont dépend ce qu'on
# peut lui confier. Une machine qui ne l'annonce pas en est à la révision 1.
#   2 (audit du 20/10/2026) : elle ne lit, pour une autre machine, que l'Internet public,
#     redirections comprises (jobs/kinds/index.compute_index_url, services/addresses.py)
WORK_REVISION = 2
# Révision qu'il faut pour recevoir une unité de cette ressource. « net » : lire le Web depuis
# la machine qui calcule ; une de mes machines restée sur une version d'avant suivrait une
# page publique qui redirige vers son réseau local, et ce qu'elle y lirait entrerait dans ma
# bibliothèque (relecture du 21/10/2026)
REVISION_FOR = {"net": 2}
MAX_BLOB = 512 * 1024 ** 2     # au-delà, le fichier reste traité sur cette machine
BLOB_CHUNK = 2 * 1024 ** 2
INTERNET_CHUNK = 512 * 1024    # par Internet (4G, relais) : chaque morceau tient dans le délai
NOTHING_RETRY = 5              # rien à confier : la machine peut redemander dans 5 s
FRIEND_MAX_HELD = 4            # unités détenues à la fois par un ami
# Demandes d'un ami restées sans unité, et unités qu'il a rendues ou rendues en échec, par
# minute, au plus : au-delà, il attend. Une demande parcourt la file, elle n'est pas gratuite
# ici ; une demande qui obtient une unité ne compte pas (un ami rapide n'est pas freiné)
FRIEND_LEASES_PER_MINUTE = 60
FRIEND_FAILURES_ASIDE = 10     # échecs à la suite d'un ami, sans résultat accepté : de côté
FAILED_REMEMBERED = 5000       # unités rendues en échec par un ami, retenues (pas redonnées)
# Unité que seuls mes amis calculent, rendue en échec par l'un d'eux : pendant ce temps (s),
# les autres passent d'abord ; ensuite, lui de nouveau
FRIEND_RETRY_OTHERS = 300
FRIEND_LEASE_MAX = 3600        # s de calcul chez un ami, au plus, pour une unité (bail compris)
FRIEND_LEASE_MAX_IMAGE = 1800  # ... pour une image
FRIEND_LEASE_MAX_VIDEO = 7200  # ... pour une vidéo
# Index d'un document (passages et vecteurs) rendu par une de mes machines, au plus : plus
# de 16 Mio dès un livre d'un millier de pages (network/body_limit.py, result_limit)
MINE_RESULT = 1024 ** 3

_friends_lock = threading.Lock()
_friend_leases = {}            # ami -> instants (monotonic) de ses demandes comptées
_friend_failed = {}            # ami -> {unité : instant (monotonic) où il l'a rendue en échec}
_friend_failures = {}          # ami -> échecs à la suite, sans un résultat accepté


def forget_friends():
    """Oublie ce qui est retenu des amis en mémoire (essais)."""
    with _friends_lock:
        _friend_leases.clear()
        _friend_failed.clear()
        _friend_failures.clear()


def _friend_too_eager(sender) -> bool:
    """Cet ami a-t-il déjà FRIEND_LEASES_PER_MINUTE demandes comptées dans la minute ?"""
    now = time.monotonic()
    with _friends_lock:
        asked = _friend_leases.setdefault(sender, deque())
        while asked and now - asked[0] > 60:
            asked.popleft()
        return len(asked) >= FRIEND_LEASES_PER_MINUTE


def _friend_counted(sender):
    """Une demande de cet ami restée sans unité, ou une unité qu'il a rendue."""
    with _friends_lock:
        _friend_leases.setdefault(sender, deque()).append(time.monotonic())


def _held_by(node_id) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM units WHERE node=? AND status=? "
                       "AND lease_until IS NOT NULL", (node_id, RUNNING))
    return row["n"] if row else 0


def _is_video(model) -> bool:
    from aiodos.imaging import catalog          # à la demande : imaging dépend du réseau
    return catalog.is_video(str(model or ""))


def _wire_size(raw: int) -> int:
    """Taille d'un résultat qui porte `raw` octets (une image, une vidéo) : base64 dans la
    sortie, puis encore base64 dans l'enveloppe scellée, et une marge."""
    return raw * 16 // 9 + 1024 ** 2


def _unit_result(task, model, mine: bool) -> int:
    """Résultat d'une unité de cette tâche accepté au-delà d'un message ordinaire (0 : rien
    de plus) : une image ou une vidéo, à la taille de ce qu'elle produit ; l'index d'un très
    long document, s'il est rendu par une de mes machines (MINE_RESULT)."""
    if task == "generate_image":
        from aiodos.jobs.kinds import images as kind
        return _wire_size(kind.MAX_WEBM if _is_video(model) else kind.MAX_PNG)
    return MINE_RESULT if mine else 0


def result_limit(node_id) -> int:
    """Taille d'un résultat que cette machine peut m'envoyer en ce moment (0 : rien de
    particulier ; network/body_limit.py). Plus que la taille d'un message ordinaire
    seulement si elle calcule pour moi (bail en cours, ou échu sans que l'unité ait été
    redonnée : son résultat est encore accepté) une image ou une vidéo, à la taille de ce
    qu'elle produit ; ou, si c'est une de mes machines, l'index d'un très long document
    (MINE_RESULT). Un ami qui indexe pour moi n'envoie qu'un message ordinaire : au-delà,
    il rend l'unité en échec sans l'envoyer (result_max dans l'ordre), elle revient ici."""
    best = 0
    mine = None
    for r in db.query("SELECT task, json_extract(payload, '$.model') AS model FROM units "
                      "WHERE node=? AND status IN (?, ?)", (node_id, RUNNING, QUEUED)):
        if r["task"] != "generate_image" and mine is None:
            mine = sharing.level(node_id) == sharing.MINE
        best = max(best, _unit_result(r["task"], r["model"], bool(mine)))
    return best


def _result_max(sender, unit) -> int:
    """Taille de « work/result » que j'accepterai de `sender` pour cette unité, au moins
    (network/body_limit.py : un message ordinaire, davantage selon result_limit). Dite dans
    l'ordre : un résultat plus gros n'est pas envoyé pour rien (des dizaines de Mio, trois
    essais, puis le bail qui expire), il est rendu en échec sans reproche (fail, too_big)."""
    from aiodos.network import body_limit       # à la demande : il importe ce module-ci
    extra = _unit_result(unit["task"], unit["payload"].get("model"),
                         sharing.level(sender) == sharing.MINE)
    return min(body_limit.HUGE_BODY, max(body_limit.SEALED_BODY, extra))


def _friend_lease_max(unit) -> int:
    """Temps de calcul accordé à un ami pour cette unité, renouvellements compris."""
    if unit["task"] == "generate_image":
        return FRIEND_LEASE_MAX_VIDEO if _is_video(unit["payload"].get("model")) \
            else FRIEND_LEASE_MAX_IMAGE
    return FRIEND_LEASE_MAX


class _Mine:
    """Modèles d'images que mes machines en ligne savent calculer, lus une fois par demande
    (imaging/lenders.py)."""

    def __init__(self):
        self._models = None

    def models(self) -> set:
        if self._models is None:
            from aiodos.imaging import lenders
            try:
                self._models = set(lenders.helpers(friends=False))
            except Exception as exc:  # noqa: BLE001 — profils illisibles : aucune
                log.debug("Machines capables de calculer les images inconnues : %s", exc)
                self._models = set()
        return self._models


def _kept_for_me(unit, mine: _Mine) -> bool:
    """Cette machine, ou une de mes machines en ligne, sait-elle calculer cette unité ?
    Alors un ami n'en a jamais le dernier essai, et son échec ne compte pas d'essai."""
    model = unit["payload"].get("model")
    if registry.runnable_here(unit["task"], model):
        return True
    return model is not None and model in mine.models()


def _friend_checkable(spec, payload) -> bool:
    """registry.friend_checkable : ce qu'un ami rendrait pour cette unité pourrait-il être
    pris ici ? Dans le doute (le crochet de la tâche échoue), non. Regardé en dernier par
    lease (acceptable), après les contrôles qui ne coûtent rien : le crochet d'un document
    regarde le disque et les lecteurs installés, pour chacune des unités parcourues."""
    try:
        return registry.friend_checkable(spec, payload)
    except Exception as exc:  # noqa: BLE001 — crochet en erreur : rien chez un ami
        log.warning("Unité %s non confiée à un ami : contrôle impossible à prévoir (%s)",
                    spec.name, exc)
        return False


def _revision(caps) -> int:
    """Révision du contrat annoncée par la machine qui demande du travail (WORK_REVISION) ;
    1 si elle n'en dit rien (version d'avant) ou rien de lisible."""
    value = caps.get("work_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def full_name(model: str) -> str:
    """« nomic-embed-text » et « nomic-embed-text:latest » désignent le même modèle."""
    return model if ":" in model else model + ":latest"


def local_models() -> dict:
    """Modèles Ollama de ce nœud : nom complet -> empreinte (digest)."""
    try:
        return {full_name(m["name"]): m.get("digest", "") for m in ollama.list_models()}
    except Exception:
        return {}


def can_serve(need: dict, caps: dict, mine: dict) -> bool:
    """La machine décrite par `caps` peut-elle calculer une unité qui demande `need` ?
    Une machine qui ne dit pas quels formats elle lit (« readers » absent : version
    précédente) est supposée tous les lire, comme avant."""
    theirs = caps.get("models") or {}
    embed = need.get("embed")
    if embed:
        digest = mine.get(full_name(embed))
        if not digest or theirs.get(full_name(embed)) != digest:
            return False
    vision = need.get("vision")
    if vision and full_name(vision) not in theirs:
        return False
    if need.get("whisper") and not caps.get("whisper"):
        return False
    readers = caps.get("readers")
    if need.get("format") and isinstance(readers, list) and need["format"] not in readers:
        return False
    images = need.get("images")
    if images:
        # Une image : stable-diffusion.cpp là-bas, et tous les fichiers du modèle aux mêmes
        # empreintes (jobs/kinds/images.py)
        theirs_images = caps.get("images") or {}
        if (not theirs_images.get("engine")
                or not set(images["files"]) <= set(theirs_images.get("files") or [])):
            return False
    engine = need.get("engine")
    if engine and not lending.can_serve(engine, caps):
        return False              # un travail ComfyUI : le moteur, ses classes, ses modèles
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _peer_name(node_id) -> str:
    peer = peers.get(node_id)
    return peer["name"] if peer else t("work.another_machine")


def _unit_id(payload) -> int:
    try:
        return int(payload["unit"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PeerError(t("work.unit_missing")) from exc


# --------------------------------------------------------------------- bail
def lease(manager, sender, payload):
    """Une unité pour la machine qui demande du travail ; sinon quand redemander, et
    pourquoi (« reason », dans la langue d'ici : elle ne va qu'au journal de l'autre)."""
    if not settings.get("offload_enabled"):
        return {"unit": None, "retry_after": 60}
    caps = payload.get("caps") or {}
    if caps.get("work_api") != WORK_API:
        return {"unit": None, "retry_after": 600, "reason": t("work.versions_differ")}
    aside = sharing.set_aside_reason(sender)
    if aside:
        return {"unit": None, "retry_after": 600,
                "reason": t("work.set_aside", name=manager.name(), reason=aside)}
    friend = sharing.is_friend(sender)
    if friend and _friend_too_eager(sender):
        return {"unit": None, "retry_after": NOTHING_RETRY}
    if friend and _held_by(sender) >= FRIEND_MAX_HELD:
        _friend_counted(sender)
        return {"unit": None, "retry_after": NOTHING_RETRY}
    with _friends_lock:
        failed_here = dict(_friend_failed.get(sender, {})) if friend else {}
    now = time.monotonic()
    mine = _Mine()
    offers = friend and _offers_images(sender)
    shareable = store.shareable_ids() if friend else set()
    local_only = store.local_only_ids()
    max_blob = _max_blob(sender)
    options = {}
    # Modèles d'Ollama d'ici, lus au premier besoin : une image n'en demande aucun
    ollama = {"models": None}
    revision = _revision(caps)
    outdated = 0                  # unités qu'elle aurait eues, à une révision près

    def acceptable(unit):
        nonlocal outdated
        spec = registry.TASKS.get(unit["task"])
        if spec is None or not spec.portable:
            return False
        if friend and not registry.friends_may_take(spec):
            return False                  # images : chez un ami seulement si je l'accepte
        if friend and spec.mine_only:
            if not offers:
                return False              # ... et s'il les propose (son profil le dit)
            if spec.here is not None and spec.here(unit["payload"].get("model")):
                return False              # ... pour ce que cette machine ne calcule pas
        collection = unit["payload"].get("collection_id")
        if collection is not None:
            if collection in local_only or (friend and collection not in shareable):
                return False
        elif friend and not spec.mine_only:
            return False                  # sans bibliothèque, chez un ami : images acceptées seulement
        failed_at = failed_here.get(unit["id"])
        if friend and (failed_at is not None or unit["attempts"] >= MAX_ATTEMPTS - 1):
            if _kept_for_me(unit, mine):
                return False              # rendue en échec par cet ami, ou dernier essai : à moi
            if failed_at is not None and now - failed_at < FRIEND_RETRY_OTHERS:
                return False              # seuls mes amis la calculent : un autre d'abord
        if unit["task"] not in options:
            options[unit["task"]] = spec.options() if spec.options else {}
        if not spec.requires:
            fits = _fits(unit, spec, max_blob)
        else:
            need = spec.requires(unit, options[unit["task"]])
            if need.get("embed") or need.get("vision"):
                if ollama["models"] is None:
                    ollama["models"] = local_models()
                if not ollama["models"]:
                    return False
            fits = can_serve(need, caps, ollama["models"] or {}) and _fits(unit, spec, max_blob)
        if not fits:
            return False
        # En dernier : le seul contrôle qui coûte (4,8 ms par document, sur jusqu'à
        # leases.SCAN_MAX unités à chaque demande d'un ami, relecture finale du 21/10/2026)
        if friend and not _friend_checkable(spec, unit["payload"]):
            return False                  # son résultat ne se contrôlerait pas ici : à moi
        if revision < REVISION_FOR.get(unit["resource"], 1):
            outdated += 1                 # elle la calculerait, mais sans une défense d'après
            return False
        return True

    # Une unité ne part que dans les cercles qui lui sont permis (units.reach : travaux des
    # applis, unités qu'un ami a perdues deux fois)
    unit = leases.claim_remote(payload.get("resources"), sender, acceptable,
                               circle="friends" if friend else "mine")
    if unit is None:
        if friend:
            _friend_counted(sender)
        if ollama["models"] == {}:
            # Sans Ollama ici, impossible de vérifier que l'autre machine a exactement le
            # même modèle : rien n'est confié. On le dit plutôt que de refuser en silence
            # (essai du 16/10/2026 : Ollama arrêté après un redémarrage du PC).
            log.warning("Ollama ne répond pas : aucune unité confiée à %s", _peer_name(sender))
            return {"unit": None, "retry_after": 30,
                    "reason": t("work.ollama_down", name=manager.name())}
        if outdated:
            # Refus dit, plutôt que muet : la machine qui demande le note dans son journal
            # (une fois par cause) ; ici, elle redemande toutes les quelques secondes
            log.debug("%s unité(s) gardée(s) ici : « %s » est d'une version d'avant",
                      outdated, _peer_name(sender))
            return {"unit": None, "retry_after": NOTHING_RETRY,
                    "reason": t("work.outdated", name=manager.name())}
        return {"unit": None, "retry_after": NOTHING_RETRY}
    return _order(manager, sender, unit, options, friend, caps, max_blob)


def _offers_images(node_id) -> bool:
    """Cet ami prête-t-il sa carte pour les images ? Son profil, tel qu'il me le montre, ne
    contient « images » qu'alors (sharing.profile_for, chez lui)."""
    peer = peers.get(node_id) or {}
    try:
        profile = json.loads(peer.get("profile") or "null") or {}
    except ValueError:
        return False
    images = profile.get("images") if isinstance(profile, dict) else None
    return isinstance(images, dict) and bool(images.get("engine")) and bool(images.get("shares"))


def _fits(unit, spec, max_blob) -> bool:
    """Le fichier de l'unité (s'il y en a un) n'est pas trop gros pour cette machine."""
    if spec.file_optional is not None:
        return True               # l'ordre partira sans le fichier s'il le faut (_order)
    if spec.file_key and unit["payload"].get(spec.file_key):
        try:
            return os.path.getsize(unit["payload"][spec.file_key]) <= max_blob
        except OSError:
            return False          # fichier disparu : l'exécution locale le signalera
    return True


def _order(manager, sender, unit, options, friend, caps=None, max_blob=MAX_BLOB) -> dict:
    """L'ordre de calcul envoyé à la machine qui a obtenu l'unité."""
    spec = registry.TASKS[unit["task"]]
    # Un travail de moteur : l'ordre complet, sans ce qui ne quitte jamais ce PC (l'appli,
    # le travail, les chemins de ses entrées, qui se demandent par empreinte : blob)
    payload = registry.payload_for(unit, here=False) if spec.expand else unit["payload"]
    order = {"id": unit["id"], "task": unit["task"], "resource": unit["resource"],
             "payload": {k: v for k, v in payload.items() if k != spec.file_key},
             "options": options[unit["task"]], "lease_s": leases.LEASE_S, "blob": None,
             "result_max": _result_max(sender, unit)}
    if spec.file_key and unit["payload"].get(spec.file_key):
        path = Path(unit["payload"][spec.file_key])
        try:
            if spec.file_optional is not None and (friend or not spec.file_optional(caps or {})
                                                   or path.stat().st_size > max_blob):
                raise OSError("fichier facultatif gardé ici")   # i18n: tel quel (repris aussitôt)
            order["blob"] = {"name": path.name, "size": path.stat().st_size,
                             "sha256": _sha256(path)}
        except OSError:
            if spec.file_optional is None:
                leases.release(unit["id"], sender)
                return {"unit": None, "retry_after": NOTHING_RETRY}
            # Facultatif (image de départ) : l'ordre part sans lui, la tâche fait sans
            for key in spec.file_extras:
                order["payload"].pop(key, None)
    if friend:
        order = sharing.order_for_friend(order, spec, unit)
        blob = order.get("blob") or {}
        sharing.log_sent(sender, unit["id"], unit["task"],
                         unit["payload"].get("source") or unit["payload"].get("url") or "?",
                         blob.get("name") or unit["payload"].get("url"), blob.get("size"))
    log.info("Unité %s (%s) confiée à %s", unit["id"], unit["task"], _peer_name(sender))
    manager.executor.publish_job(unit["job_id"], force=True)
    return {"unit": order}


def _max_blob(sender) -> int:
    """Taille de fichier qu'on confie à cette machine : moindre si elle n'est joignable
    que par Internet (4G, relais : le plus gros reste aux machines du réseau local)."""
    peer = peers.get(sender)
    if peer is None or route.lan_first(peer):
        return MAX_BLOB
    try:
        megabytes = float(settings.get("internet_max_blob_mb"))
    except (TypeError, ValueError):
        megabytes = 64.0
    return min(MAX_BLOB, max(0, int(megabytes * 1024 ** 2)))


def renew(manager, sender, payload):
    """Bail prolongé. Un ami ne garde pas une unité indéfiniment en renouvelant sans rien
    rendre : au-delà de _friend_lease_max, elle lui est reprise comme s'il avait échoué.
    state dit à la machine où en est l'unité (leases.renew) : running, resumed (son bail
    avait échu, elle se raccroche), paused, cancelled, reassigned ; « ok » reste pour les
    machines d'une version précédente, qui ne lisent que lui."""
    unit_id = _unit_id(payload)
    if sharing.is_friend(sender):
        unit = leases.held(unit_id, sender)
        if unit is not None:
            longest = _friend_lease_max(unit)
            if db.now() - float(unit["started"] or db.now()) > longest:
                _friend_failed_unit(manager, sender, unit_id,
                                    t("work.too_long", name=_peer_name(sender),
                                      minutes=longest // 60))
                return {"ok": False, "state": "reassigned"}
    state = leases.renew(unit_id, sender)
    if state == "resumed":
        manager.changed()
    progress = payload.get("progress")
    if state in leases.STATES_GOING and isinstance(progress, dict) \
            and progress.get("stage") in engine_kind.STAGES:
        # L'étape d'un calcul de moteur, pour l'appli qui suit son travail (elle seule)
        queue.set_progress(unit_id, {"stage": progress["stage"], "at": db.now()})
    return {"ok": state in leases.STATES_GOING, "state": state}


def blob(manager, sender, payload):
    """Un morceau du fichier de l'unité confiée à cette machine (et d'aucun autre). Un
    fichier facultatif (l'image de départ d'une retouche) ne part jamais chez un ami, même
    s'il le demande : l'ordre est parti sans lui (_order ; audit du 20/10/2026)."""
    unit = leases.held(_unit_id(payload), sender)
    spec = registry.TASKS.get(unit["task"]) if unit else None
    if spec is not None and spec.expand and payload.get("sha256"):
        return _input_chunk(unit, payload)
    if spec is None or not spec.file_key or not unit["payload"].get(spec.file_key):
        raise PeerError(t("work.not_yours"))
    if spec.file_optional is not None and sharing.is_friend(sender):
        raise PeerError(t("work.file_stays"))
    try:
        offset = int(payload.get("offset") or 0)
        size = min(BLOB_CHUNK, max(1, int(payload.get("size") or BLOB_CHUNK)))
    except (TypeError, ValueError) as exc:
        raise PeerError(t("work.request_unreadable")) from exc
    path = unit["payload"][spec.file_key]
    try:
        total = os.path.getsize(path)
    except OSError as exc:
        raise PeerError(t("work.file_gone")) from exc
    if not 0 <= offset <= total:
        raise PeerError(t("work.chunk_outside"))
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(size)
    return {"data": envelope.b64e(data)}


def _input_chunk(unit, payload) -> dict:
    """Un morceau d'une entrée d'un travail de moteur, par son empreinte : seulement une
    entrée de l'unité confiée à cette machine (jamais une autre entrée de l'appli)."""
    wanted = str(payload.get("sha256") or "")
    try:
        offset = int(payload.get("offset") or 0)
        size = min(BLOB_CHUNK, max(1, int(payload.get("size") or BLOB_CHUNK)))
        order = registry.payload_for(unit, here=True, fresh=False)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise PeerError(t("work.input_request_unreadable")) from exc
    entry = next((e for e in order.get("inputs") or [] if e.get("sha256") == wanted), None)
    if entry is None:
        raise PeerError(t("work.input_not_yours"))
    path = order["local"]["files"][entry["slot"]]
    try:
        total = os.path.getsize(path)
    except OSError as exc:
        raise PeerError(t("work.input_gone")) from exc
    if not 0 <= offset <= total:
        raise PeerError(t("work.chunk_outside"))
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(size)
    return {"data": envelope.b64e(data)}


# ---------------------------------------------------------------- résultats
def result(manager, sender, payload):
    unit_id = _unit_id(payload)
    compute_s = max(0.0, min(float(payload.get("compute_s") or 0), 86400.0))
    unit = leases.take_back(unit_id, sender)
    if unit is None:
        return {"accepted": False, "reason": t("work.taken_back")}
    spec = registry.TASKS[unit["task"]]
    friend = sharing.is_friend(sender)
    checked = False                   # résultat lu et contrôlé : une erreur ensuite est d'ici
    try:
        output = wire.unpack(payload.get("output") or {})
        # Un travail de moteur se contrôle et s'intègre avec l'ordre de cette remise
        # (bornes, sorties attendues, appli et travail, qui sont restés ici)
        given = (registry.payload_for(unit, here=True, fresh=False) if spec.expand
                 else unit["payload"])
        if spec.validate:
            spec.validate(given, output)
        if friend and spec.sample:
            if not _sampled(manager, sender, unit, spec, output):
                return {"accepted": False, "reason": t("work.check_impossible")}
            output = sharing.restore_names(output, unit, spec)
        checked = True
        outcome = spec.assimilate(given, output) or {}
    except registry.SampleMismatch as exc:
        reason = t("work.sample_mismatch", error=exc)
        peers.set_aside(sender, reason[:300])
        # Un résultat faux ne dépend pas de la machine à qui il a été rendu : mes autres
        # machines la mettent de côté aussi (appairage par groupe, network/group.py), avec la
        # raison dans la langue d'ici et à traduire chez elles (reason_t)
        group = getattr(manager, "group", None)
        if group is not None:
            told = i18n.lazy("work.sample_mismatch", error=str(exc)[:300])
            threading.Thread(target=group.spread, daemon=True, name="aiodos-mise-de-cote",
                             args=("group/set_aside", {"node_id": sender, "reason": reason[:300],
                                                       "reason_t": told})
                             ).start()
        peers.account(sender, refused_units=1)
        log.warning("« %s » mise de côté : %s", _peer_name(sender), reason)
        job_id, finished = queue.fail_unit(
            unit_id, t("work.result_refused", name=_peer_name(sender), error=reason)[:500],
            compute_s)
        manager.executor.settle(job_id, finished)
        manager.changed()
        return {"accepted": False, "reason": t("work.sample_failed")}
    except Exception as exc:
        log.warning("Résultat de %s refusé pour l'unité %s : %s", _peer_name(sender), unit_id, exc)
        if not checked:               # sa fiabilité ne compte que ses résultats, pas nos pannes
            peers.account(sender, refused_units=1)
        job_id, finished = queue.fail_unit(
            unit_id, t("work.result_refused", name=_peer_name(sender), error=exc)[:500],
            compute_s)
        manager.executor.settle(job_id, finished)
        return {"accepted": False, "reason": str(exc)[:200]}
    try:
        job_id, finished = queue.finish_unit(unit_id, outcome, compute_s)
    except Exception as exc:  # noqa: BLE001 — l'unité ne reste pas prise sans bail
        # Reprise par take_back (sans bail) : sans ceci, elle restait « en cours » jusqu'au
        # redémarrage, que leases.expire ne voit pas (relecture finale du 21/10/2026). Ce
        # n'est pas le fait de la machine qui a calculé : rien ne lui est reproché
        log.warning("Résultat de %s pour l'unité %s impossible à enregistrer : %s",
                    _peer_name(sender), unit_id, exc)
        job_id, finished = queue.fail_unit(
            unit_id, t("work.result_not_saved", name=_peer_name(sender),
                       kind=exc.__class__.__name__), compute_s)
        manager.executor.settle(job_id, finished)
        return {"accepted": False, "reason": t("work.result_not_saved_owner")}
    if friend:
        with _friends_lock:
            _friend_failures.pop(sender, None)
    peers.account(sender, received_s=compute_s, received_units=1,
                  checked_units=1 if friend and spec.sample else 0)
    manager.executor.settle(job_id, finished)
    manager.changed()
    return {"accepted": True}


def _sampled(manager, sender, unit, spec, output) -> bool:
    """Contrôle par échantillon d'un résultat d'ami (SampleMismatch s'il ne correspond pas).
    Faux si le contrôle est impossible ici pour l'instant (Ollama arrêté…) : ce n'est pas
    sa faute, l'unité retourne en file sans compter d'essai."""
    try:
        spec.sample(unit["payload"], output, spec.options() if spec.options else {})
        return True
    except registry.SampleMismatch:
        raise
    except Exception as exc:
        log.warning("Résultat de %s non vérifiable ici pour l'instant : %s",
                    _peer_name(sender), exc)
        queue.release_unit(unit["id"])
        manager.executor.publish_job(unit["job_id"], force=True)
        manager.executor.wake()
        return False


def fail(manager, sender, payload):
    unit_id = _unit_id(payload)
    # L'échec tel que l'a écrit la machine qui calcule (error, dans sa langue), traduit ici
    # quand il vient avec sa clé (error_t, i18n.lazy : worker._told)
    told = i18n.render(profile.lazy(payload.get("error_t"), "worker."))
    error = (told or str(payload.get("error") or t("work.unknown_error")))[:400]
    message = t("work.failed_on", name=_peer_name(sender), error=error)[:500]
    try:
        compute_s = max(0.0, min(float(payload.get("compute_s") or 0), 86400.0))
    except (TypeError, ValueError):
        compute_s = 0.0
    if sharing.is_friend(sender):
        return _friend_failed_unit(manager, sender, unit_id, message, compute_s,
                                   too_big=payload.get("too_big") is True)
    settled = leases.fail(unit_id, sender, message, compute_s)
    if settled:
        peers.account(sender, failed_units=1)
        manager.executor.settle(*settled)
    return {"ok": bool(settled)}


def _friend_failed_unit(manager, sender, unit_id, message, compute_s=0.0,
                        too_big=False) -> dict:
    """Échec annoncé par un ami (ou calcul trop long chez lui) : rien ne le distingue d'un
    échec inventé. Une unité que cette machine ou une de mes machines sait calculer revient
    en file sans compter d'essai et ne lui sera plus confiée. Une unité que seuls mes amis
    calculent compte un essai : un autre ami passe d'abord (FRIEND_RETRY_OTHERS), et après
    MAX_ATTEMPTS essais elle échoue, avec un message qui le dit. Trop d'échecs à la suite,
    sans un résultat accepté : l'ami est mis de côté.
    Un résultat trop gros pour ce que j'accepte d'un ami (too_big : result_max, network/
    body_limit.py) n'est pas son fait quand l'unité n'a droit qu'à un message ordinaire
    (un index : un dossier de longs journaux le mettrait sinon de côté) : ni sa fiabilité ni
    la mise de côté n'en tiennent compte, et l'unité ne lui revient pas. Le dire à tort ne lui
    sert à rien : c'est rendre l'unité sans pouvoir la reprendre. Pour une image ou une
    vidéo, dont la limite est taillée pour ce qu'elles produisent, ce n'est pas crédible :
    un échec comme un autre (sinon il ferait échouer mes images sans jamais être écarté)."""
    unit = leases.held(unit_id, sender)
    if unit is None:
        return {"ok": False}
    blamed = not (too_big and not _unit_result(unit["task"], unit["payload"].get("model"),
                                               mine=False))
    _friend_counted(sender)
    if blamed:
        peers.account(sender, failed_units=1)
    with _friends_lock:
        failed = _friend_failed.setdefault(sender, {})
        if len(failed) >= FAILED_REMEMBERED:
            failed.clear()
        failed[unit_id] = time.monotonic()
        if blamed:
            _friend_failures[sender] = _friend_failures.get(sender, 0) + 1
        aside = blamed and _friend_failures[sender] >= FRIEND_FAILURES_ASIDE
        if aside:
            _friend_failures.pop(sender, None)
    if aside:
        reason = t("work.failures_aside", count=FRIEND_FAILURES_ASIDE)
        peers.set_aside(sender, reason)
        log.warning("« %s » mise de côté : %s", _peer_name(sender), reason)
        manager.changed()
    if not _kept_for_me(unit, _Mine()):
        settled = leases.fail(unit_id, sender,
                              t("work.nobody_can", message=message)[:500],
                              compute_s)
        if settled is None:
            return {"ok": False}
        manager.executor.settle(*settled)
        return {"ok": True}
    job_id = leases.release(unit_id, sender)
    if job_id is None:
        return {"ok": False}
    db.execute("UPDATE units SET error=? WHERE id=?", (message, unit_id))
    manager.executor.publish_job(job_id, force=True)
    manager.executor.wake()
    return {"ok": True}


def release(manager, sender, payload):
    job_id = leases.release(_unit_id(payload), sender)
    if job_id is not None:
        if sharing.is_friend(sender):
            _friend_counted(sender)
        manager.executor.publish_job(job_id, force=True)
        manager.executor.wake()
    return {"ok": job_id is not None}


HANDLERS = {
    "work/lease": lease,
    "work/renew": renew,
    "work/blob": blob,
    "work/result": result,
    "work/fail": fail,
    "work/release": release,
}
