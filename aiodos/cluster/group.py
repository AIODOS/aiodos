"""Un gros modèle réparti entre mes machines (la machine qui pose la question).

Dans le choix du modèle d'une conversation, « réparti:<modèle> » apparaît quand un modèle
d'Ollama ne tient pas sur la carte graphique de cette machine et que mes machines en ligne
prêtent la leur (profil « cluster »). Au premier message :

1. plan.make() choisit les machines et le découpage ;
2. chacune lance son serveur RPC (helper.py chez elle) ;
3. le compagnon ouvre un tunnel chiffré vers chacune, juste avant llama-server : un tunnel
   « gardé », qui ne relaie que le llama-server lancé ici (son numéro de programme est
   donné au compagnon dès son lancement) et un nombre borné de connexions. Le serveur RPC
   n'a ni mot de passe ni chiffrement : sans cette garde, n'importe quel programme de
   cette machine (un autre compte du PC) le joindrait par le tunnel. Un compagnon trop
   ancien pour cela, ou l'appli Android (aucun contrôle possible), n'empruntent pas ;
4. llama-server démarre ici avec elles, sur le fichier GGUF d'Ollama. La première fois, les
   poids traversent le réseau ; ensuite, chaque machine les garde en cache.

Le groupe reste prêt IDLE_S après la dernière réponse, puis tout est rendu ; les tunnels se
ferment dès que llama-server s'arrête. Si une machine part en route, la réponse s'arrête
avec un message, et le groupe est défait.
"""
import json
import logging
import queue
import secrets
import threading
import time
from pathlib import Path

import requests

from aiodos.cluster import engine, plan, weights
from aiodos.llm.messages import SAMPLING, for_template  # noqa: F401  (aussi cluster.group.for_template)
from aiodos.network import client, peers, route, sharing
from aiodos.network.client import PeerError
from aiodos.network.companion import CompanionError
from aiodos.node import android_app, identity
from aiodos.services import ollama

log = logging.getLogger("aiodos.cluster")

PREFIX = "reparti:"
IDLE_S = 600                  # groupe gardé 10 min après la dernière réponse
KEEP_EVERY = 30               # nouvelles données aux machines qui prêtent (cf. helper.LEASE_S)
LOAD_TIMEOUT = 1800           # premier chargement : les poids traversent le réseau
CONTEXT = 8192
# Connexions relayées en même temps par un tunnel : llama-server n'en garde qu'une par
# serveur RPC, plus quelques brèves, l'une après l'autre, au démarrage
TUNNEL_CONNECTIONS = 4
WATCH_EVERY = 1               # s : llama-server arrêté → tunnels fermés
GUARDED = "guarded"           # dit aux machines qui prêtent que le tunnel est gardé (helper.lend)
NOT_ON_PHONE = ("Sur le téléphone, AIODOS n'emprunte pas de carte graphique : il ne peut pas "
                "vérifier quel programme se connecte au tunnel vers la carte prêtée.")
NO_PROXY = {"http": None, "https": None}

_lock = threading.Lock()
_groups = {}                  # modèle -> Group
_unavailable = {"reason": None}   # dernière raison écrite au journal (cf. _note_unavailable)
_headers = {}                 # chemin du fichier -> en-tête GGUF


def model_id(name: str) -> str:
    return f"{PREFIX}{name}"


def parse(model) -> str | None:
    text = str(model or "")
    return text[len(PREFIX):] or None if text.startswith(PREFIX) else None


def _header(path: Path) -> dict:
    key = str(path)
    if key not in _headers:
        _headers[key] = weights.header(path)
    return _headers[key]


def lenders(version: str) -> list:
    """Mes machines en ligne qui prêtent leur carte avec la même version de llama.cpp."""
    found = []
    for peer in peers.list_peers():
        cluster = (peer.get("profile") or {}).get("cluster") or {}
        if (peer["online"] and peer.get("net_id") and cluster.get("lends")
                and cluster.get("version") == version
                and sharing.level(peer["node_id"]) == sharing.MINE):
            found.append({"node_id": peer["node_id"], "name": peer["name"],
                          "free_mib": int(cluster.get("free_mib") or 0),
                          # sur le réseau local : ~1 ms par mot, contre 20 à 50 par Internet
                          "near": route.lan_first(peer)})
    return found


def _guard_problem() -> str | None:
    """Pourquoi cette machine ne peut pas ouvrir de tunnel gardé vers une carte prêtée."""
    companion = route.companion()
    if companion is None or not companion.running:
        return ("le compagnon aiodos-net ne tourne pas (réglage « Relier mes machines par "
                "Internet »)")
    return companion.tunnel_guard_problem()


def _note_unavailable(reason: str | None):
    """Écrit une fois au journal pourquoi les modèles répartis ne sont pas proposés (et
    encore quand la raison change) : sans quoi ils disparaîtraient de la liste sans un mot."""
    if reason and reason != _unavailable["reason"]:
        log.warning("Modèles répartis non proposés malgré mes machines qui prêtent : %s", reason)
    _unavailable["reason"] = reason


def available() -> list:
    """Modèles de conversation d'Ollama trop gros pour la carte d'ici, que mes machines
    permettent de répartir (jamais dans l'appli Android : cf. NOT_ON_PHONE). Rien non plus
    sans compagnon capable de tunnels gardés : ces machines refuseraient le prêt, inutile
    de les proposer (le refus n'arriverait qu'à l'usage) ; la raison est écrite au journal."""
    if android_app.in_app():
        return []
    found = engine.info()
    if not found:
        return []
    helpers = lenders(found["version"])
    if not helpers:
        return []
    problem = _guard_problem()
    _note_unavailable(problem)
    if problem:
        return []
    local = engine.best_device(found["devices"])
    offers = []
    try:
        models = ollama.list_models()
    except Exception:
        return []
    caps = ollama.capabilities_many([m["name"] for m in models])
    for m in models:
        if "completion" not in (caps.get(m["name"]) or []):
            continue
        try:
            head = _header(weights.gguf_path(m["name"]))
        except (weights.WeightsError, OSError):
            continue
        cut = plan.make(head["size"], head["layers"], local, helpers)
        if cut:
            offers.append({"id": model_id(m["name"]), "label": m["name"], "group": "cluster",
                           "machines": [h["name"] for h in cut["helpers"]],
                           "complete": cut["complete"], "size": head["size"]})
    return offers


class Group:
    """llama-server ici, et les cartes prêtées par mes machines."""

    def __init__(self, name: str):
        self.name = name
        self.loans = []           # [[peer, service, port du tunnel]]
        self.server = None
        self.api_key = secrets.token_urlsafe(24)
        self.last_used = time.monotonic()
        self.lock = threading.Lock()
        self.closed = False
        self.loading = False      # premier chargement en cours : jamais « inactif »
        self.status = None        # où en est la mise en place (relayé à qui l'attend)
        self.ready = False        # mise en place finie : llama-server a chargé le modèle
        self._closing = threading.Lock()

    # ------------------------------------------------------------- mise en place
    def start(self, stop=None, progress=None):
        """Réunit les cartes et lance llama-server. `stop` (bouton Stop) interrompt la mise
        en place : tout ce qui était pris est rendu. `progress(texte)` dit où elle en est."""
        report = progress or (lambda text: None)

        def progress(text):
            self.status = text
            report(text)
        if android_app.in_app():
            raise RuntimeError(NOT_ON_PHONE)
        found = engine.info(max_age=0)
        if not found:
            raise RuntimeError("llama.cpp n'est pas installé sur cette machine "
                               "(réglage « Dossier de llama.cpp »).")
        companion = route.companion()
        if companion is None or not companion.running:
            raise RuntimeError("Le compagnon aiodos-net ne tourne pas : il relie les machines "
                               "du groupe (réglage « Relier mes machines par Internet »).")
        problem = companion.tunnel_guard_problem()    # avant de demander la moindre carte
        if problem:
            raise RuntimeError(problem)
        path = weights.gguf_path(self.name)
        head = _header(path)
        helpers = lenders(found["version"])
        cut = plan.make(head["size"], head["layers"], engine.best_device(found["devices"]),
                        helpers)
        if not cut:
            raise RuntimeError("Aucune de mes machines ne prête sa carte graphique en ce "
                               "moment (réglage « Prêter ma carte graphique » chez elles).")
        try:
            for helper in cut["helpers"]:
                if stop is not None and stop.is_set():
                    raise RuntimeError("Mise en place du modèle réparti interrompue.")
                peer = peers.get(helper["node_id"])
                progress(f"Demande de la carte graphique de « {peer['name']} »…")
                reply = client.call(identity.node_id(), peer, "cluster/lend",
                                    {"version": found["version"], "model": self.name,
                                     "tunnel": GUARDED},
                                    timeout=90)
                # Noté avant le tunnel : la carte est rendue même s'il ne s'ouvre pas
                self.loans.append([peer, reply["service"], None])
            # Les tunnels juste avant llama-server, réservés à lui seul (cf. _spawned)
            for loan in self.loans:
                if stop is not None and stop.is_set():
                    raise RuntimeError("Mise en place du modèle réparti interrompue.")
                loan[2] = companion.open_tunnel(loan[0]["net_id"], loan[1],
                                                max_connections=TUNNEL_CONNECTIONS)
            # Des nouvelles dès maintenant : un premier chargement dure souvent plus que les
            # 90 s au bout desquelles une machine sans nouvelles reprend sa carte
            self.loading = True
            threading.Thread(target=self._keep, daemon=True,
                             name=f"aiodos-groupe-{self.name}").start()
            machines = len(self.loans) + 1
            progress(f"Chargement de {self.name} sur {machines} machines : la première fois, "
                     "les poids traversent le réseau, puis chaque machine les garde…")
            self.server = engine.llama_server(
                Path(found["folder"]), path, [port for _, _, port in self.loans],
                cut["devices"], cut["split"], self.api_key, CONTEXT, cut["layers"])
            self.server.start(timeout=LOAD_TIMEOUT, ready=lambda: engine.loaded(self.server.port),
                              stop=stop, waiting=lambda seconds: progress(
                                  f"Chargement du modèle réparti… {seconds} s"),
                              spawned=lambda pid: self._spawned(companion, pid))
        except Exception:
            self.close()
            raise
        finally:
            self.loading = False
        self.ready = True
        self.last_used = time.monotonic()
        log.info("Modèle %s réparti : %s, découpage %s", self.name, cut["devices"], cut["split"])

    def _spawned(self, companion, pid: int):
        """llama-server vient d'être lancé : les tunnels ne relaient que lui (ses connexions
        arrivées entre-temps attendaient), et se ferment dès qu'il s'arrête."""
        for _, _, port in self.loans:
            try:
                companion.set_tunnel_owner(port, pid)
            except CompanionError as exc:
                raise RuntimeError("Le compagnon aiodos-net ne réserve pas le tunnel à "
                                   f"llama-server : {exc}") from exc
        threading.Thread(target=self._watch_server, args=(self.server,), daemon=True,
                         name=f"aiodos-groupe-{self.name}-serveur").start()

    def _watch_server(self, server):
        """Pendant le chargement, start() voit lui-même llama-server s'arrêter ; ensuite,
        c'est ici : le groupe est défait aussitôt, tunnels compris."""
        while not self.closed and (self.loading or server.alive):
            time.sleep(WATCH_EVERY)
        if not self.closed:
            log.info("llama-server du modèle %s arrêté : groupe défait", self.name)
            _forget(self)
            self.close()

    def _keep(self):
        """Donne des nouvelles aux machines qui prêtent ; défait le groupe après IDLE_S
        sans conversation, si llama-server s'est arrêté, ou si l'une d'elles a rendu sa
        carte."""
        while not self.closed:
            time.sleep(KEEP_EVERY)
            if self.closed:
                return
            if self.loading:                  # pendant le chargement : garder les cartes
                self._still_lent()
                continue
            idle = time.monotonic() - self.last_used > IDLE_S
            if idle or not (self.server and self.server.alive) or not self._still_lent():
                break
        _forget(self)
        self.close()

    def _still_lent(self) -> bool:
        for peer, service, _ in self.loans:
            try:
                # « loaded » : la connexion de llama-server est en place ; la machine qui
                # prête arrête son serveur RPC dès qu'elle tombe (cf. helper._sweep)
                kept = client.call(identity.node_id(), peer, "cluster/keep",
                                   {"service": service, "loaded": self.ready},
                                   timeout=15).get("ok")
            except PeerError:
                continue                      # réseau absent un instant : tour suivant
            if kept is False:
                log.info("%s a rendu sa carte : groupe %s défait", peer["name"], self.name)
                return False
        return True

    def close(self):
        """Défait le groupe, une seule fois : les tunnels d'abord (plus rien ne joint les
        cartes prêtées), puis llama-server, puis les cartes rendues."""
        with self._closing:
            self.closed = True
            loans, self.loans = self.loans, []
        companion = route.companion()
        for _, _, port in loans:
            try:
                if port and companion is not None and companion.running:
                    companion.close_tunnel(port)
            except Exception:
                pass
        if self.server:
            self.server.stop()
        for peer, service, _ in loans:
            try:
                client.call(identity.node_id(), peer, "cluster/release",
                            {"service": service}, timeout=10)
            except PeerError:
                pass                      # elle le rendra seule (helper.LEASE_S)

    # ------------------------------------------------------------- conversation
    def chat_stream(self, messages, think=True, stop=None, options=None, stats=False):
        """options : réglages d'échantillonnage à la façon d'Ollama ; stats : à la fin,
        ("stats", {"prompt_tokens", "completion_tokens", "done_reason"}) (passerelle)."""
        self.last_used = time.monotonic()
        body = {"messages": for_template(messages), "stream": True,
                "chat_template_kwargs": {"enable_thinking": bool(think)}}
        for key, value in (options or {}).items():
            if key in SAMPLING:
                body[SAMPLING[key]] = value
        if stats:
            body["stream_options"] = {"include_usage": True}
        finish = "stop"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"http://127.0.0.1:{self.server.port}/v1/chat/completions"
        try:
            with requests.post(url, json=body, headers=headers, stream=True,
                               timeout=(10, 900), proxies=NO_PROXY) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"llama-server : {r.text[:300]}")
                r.encoding = "utf-8"          # flux sans encodage déclaré : pas de Latin-1
                for line in r.iter_lines(decode_unicode=True):
                    if stop is not None and stop.is_set():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                    usage = chunk.get("usage")
                    if stats and isinstance(usage, dict):
                        yield "stats", {"prompt_tokens": usage.get("prompt_tokens") or 0,
                                        "completion_tokens": usage.get("completion_tokens") or 0,
                                        "done_reason": finish}
                    self.last_used = time.monotonic()
                    if delta.get("reasoning_content"):
                        yield "thinking", delta["reasoning_content"]
                    if delta.get("content"):
                        yield "content", delta["content"]
        except requests.RequestException as exc:
            _forget(self)
            self.close()
            raise RuntimeError("Le modèle réparti ne répond plus : une machine du groupe est "
                               "peut-être partie. Réessaie, il sera remonté.") from exc
        finally:
            self.last_used = time.monotonic()


def _forget(group: Group):
    with _lock:
        if _groups.get(group.name) is group:
            del _groups[group.name]


def _group(name: str, retry: bool = True, stop=None, progress=None) -> Group:
    with _lock:
        group = _groups.get(name)
        if group is None or group.closed:
            group = _groups[name] = Group(name)
    # Un seul lancement à la fois par modèle : si une autre conversation (ou la
    # préparation d'avance) le met déjà en place, on l'attend en relayant ses nouvelles
    said = None
    while not group.lock.acquire(timeout=1):
        if stop is not None and stop.is_set():
            raise RuntimeError("Mise en place du modèle réparti interrompue.")
        if progress is not None and group.status and group.status != said:
            said = group.status
            progress(said)
    try:
        if not group.closed and group.server is None:
            try:
                group.start(stop, progress)
            except Exception:
                _forget(group)
                raise
        if group.closed or not group.server.alive:   # arrêté entre deux conversations
            _forget(group)
            group.close()
            if retry:
                return _group(name, retry=False, stop=stop, progress=progress)
            raise RuntimeError("Le modèle réparti ne démarre pas : réessaie plus tard.")
    finally:
        group.lock.release()
    return group


_prepared = {}                    # nom -> préparation d'avance : {"state", "status", "error"}


def prepare(model, changed=None) -> dict:
    """Réunit le groupe d'avance, dès que le modèle est choisi dans la conversation : le
    temps d'écrire la question, les cartes sont prêtées et le modèle chargé. `changed()` à
    chaque nouvelle. Sans conversation ensuite, le groupe se défait IDLE_S après, comme
    après une réponse. → {"state": "preparing" | "ready" | "error", "status", "error"}."""
    name = parse(model)
    if not name:
        raise RuntimeError("Modèle réparti inconnu.")
    with _lock:
        if _usable(_groups.get(name)):
            return {"state": "ready", "status": None, "error": None}
        current = _prepared.get(name)
        if current and current["state"] == "preparing":
            return dict(current)
        record = _prepared[name] = {"state": "preparing", "status": None, "error": None}

    def note(text):
        record["status"] = text
        if changed:
            changed()

    def run():
        try:
            _group(name, progress=note)
            record.update(state="ready", status=None)
        except Exception as exc:
            log.info("Préparation de %s impossible : %s", name, exc)
            record.update(state="error", error=str(exc))
        if changed:
            changed()
    threading.Thread(target=run, daemon=True, name=f"aiodos-groupe-{name}-avance").start()
    return dict(record)


def preparation(model) -> dict:
    """Où en est la préparation d'avance de ce modèle (vue Conversation)."""
    name = parse(model) or ""
    with _lock:
        if _usable(_groups.get(name)):
            return {"state": "ready", "status": None, "error": None}
        return dict(_prepared.get(name) or {"state": "none", "status": None, "error": None})


def _usable(group) -> bool:
    """Groupe en place : modèle chargé (pas seulement llama-server lancé), toujours là."""
    return (group is not None and group.ready and not group.closed
            and group.server is not None and group.server.alive)


def chat_stream(model, messages, think=True, stop=None, options=None, stats=False):
    """Couples (genre, texte), comme ollama.chat_stream, calculés par le groupe (options,
    stats : cf. Group.chat_stream). Pendant sa mise en place (plusieurs minutes la première
    fois), des couples ("status", texte) disent où elle en est ; un groupe déjà prêt répond
    aussitôt."""
    name = parse(model)
    if not name:
        raise RuntimeError("Modèle réparti inconnu.")
    news, result = queue.Queue(), {}

    def prepare():
        try:
            result["group"] = _group(name, stop=stop, progress=news.put)
        except Exception as exc:          # remonte dans la conversation, ci-dessous
            result["error"] = exc
        finally:
            news.put(None)
    threading.Thread(target=prepare, daemon=True, name=f"aiodos-groupe-{name}-prepa").start()
    while (text := news.get()) is not None:
        yield "status", text
    if "error" in result:
        raise result["error"]
    yield from result["group"].chat_stream(messages, think, stop, options, stats)


def stop_all():
    with _lock:
        groups = list(_groups.values())
        _groups.clear()
    for group in groups:
        group.close()
