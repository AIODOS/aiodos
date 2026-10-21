"""Tunnels vers les cartes graphiques prêtées (audit de sécurité du 20/10).

Le serveur RPC de llama.cpp n'a ni mot de passe ni chiffrement : seul le llama-server
lancé par AIODOS doit le joindre. Chez la machine qui emprunte, le tunnel est « gardé »
(il ne relaie que ce programme-là, nommé au compagnon dès son lancement), ouvert juste
avant lui et fermé dès qu'il s'arrête ; un compagnon trop ancien, ou l'appli Android,
n'empruntent pas. Chez la machine qui prête, le compagnon tient le serveur RPC (lui seul y
est servi) ; le serveur s'arrête dès que la connexion du tunnel tombe, qu'un autre
programme s'y est connecté, ou que le compagnon ne le tient plus ; une machine dont le
tunnel n'est pas gardé est priée de se mettre à jour ; le cache des poids reçus reste sous
son plafond. Les derniers essais lancent deux vrais compagnons (ignorés s'il n'est pas
compilé).
"""
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from aiodos import settings
from aiodos.cluster import engine, group, helper
from aiodos.network import client, companion as companion_mod, peers, route
from aiodos.network.client import PeerError
from aiodos.network.companion import Companion, CompanionError
from aiodos.node import android_app

from tests.test_cluster import KEY, OTHER, FakeCompanion, FakeManager, FakeProcess

LLAMA_PID = 4242


def _wait(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# ------------------------------------------------------------ compagnon (côté Python)
class ScriptedCompanion(Companion):
    """Le vrai Companion, dont l'API locale répond comme un compagnon donné."""

    def __init__(self, features, replies=None):
        super().__init__("127.0.0.1:9")
        self.features = features
        self.replies = replies or {}
        self.calls = []

    def api(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        return self.replies.get((method, path.split("/")[3]), {})


def test_an_old_companion_opens_no_tunnel_to_a_lent_card():
    old = ScriptedCompanion(None)                      # ligne « ready » sans « features »
    assert "Mets AIODOS à jour" in old.tunnel_guard_problem()
    with pytest.raises(CompanionError, match="Mets AIODOS à jour"):
        old.open_tunnel("c" * 64, "rpc-1")
    assert old.calls == []                             # rien demandé au compagnon
    # Compagnon récent, sur un système qui ne dit pas quel programme se connecte
    blind = ScriptedCompanion(frozenset({"service-usage"}))
    assert "ne peut pas vérifier" in blind.tunnel_guard_problem()
    with pytest.raises(CompanionError):
        blind.open_tunnel("c" * 64, "rpc-1")
    assert blind.calls == []


def test_a_tunnel_left_unguarded_by_the_companion_is_closed_at_once():
    """Le compagnon a été remplacé par un plus ancien entre-temps : il ignore la garde
    demandée et ouvre un tunnel à toute la machine. AIODOS le referme aussitôt."""
    replaced = ScriptedCompanion(frozenset({"tunnel-owner"}),
                                 {("POST", "tunnels"): {"port": 51001}})
    with pytest.raises(CompanionError, match="mets AIODOS à jour"):
        replaced.open_tunnel("c" * 64, "rpc-1", max_connections=4)
    assert replaced.calls[0] == ("POST", "/net/v1/tunnels",
                                 {"peer": "c" * 64, "name": "rpc-1", "await_owner": True,
                                  "max_connections": 4})
    assert replaced.calls[-1][:2] == ("DELETE", "/net/v1/tunnels/51001")


def test_a_guarded_tunnel_is_reserved_to_one_program():
    current = ScriptedCompanion(frozenset({"tunnel-owner", "service-usage"}),
                                {("POST", "tunnels"): {"port": 51001, "guarded": True}})
    assert current.tunnel_guard_problem() is None
    assert current.open_tunnel("c" * 64, "rpc-1") == 51001
    current.set_tunnel_owner(51001, LLAMA_PID)
    assert current.calls[-1] == ("PUT", "/net/v1/tunnels/51001", {"owner_pid": LLAMA_PID})


def test_the_companion_features_come_from_its_ready_line():
    assert companion_mod._features({"event": "ready"}) is None
    assert companion_mod._features({"features": ["tunnel-owner", 3]}) == {"tunnel-owner"}


# ------------------------------------------------------------ machine qui emprunte
class FakeServer:
    """llama-server : lancé, nommé au compagnon, puis chargé."""

    def __init__(self, events):
        self.events, self.port, self.alive = events, 40999, False

    def start(self, timeout, ready=None, stop=None, waiting=None, spawned=None):
        self.alive = True
        self.events.append("llama-server lancé")
        spawned(LLAMA_PID)
        self.events.append("llama-server chargé")
        return self

    def stop(self):
        if self.alive:
            self.events.append("llama-server arrêté")
        self.alive = False


class RecordingCompanion(FakeCompanion):
    headers = {}

    def __init__(self, events, features=("tunnel-owner", "service-usage")):
        super().__init__(features)
        self.events = events

    def proxy_base(self, net_id):                       # chemins vers les machines (route.py)
        return f"http://127.0.0.1:9/peer/{net_id}"

    def open_tunnel(self, net_id, name, guarded=True, max_connections=None):
        port = super().open_tunnel(net_id, name, guarded, max_connections)
        self.events.append(("tunnel", name, guarded, max_connections))
        return port

    def set_tunnel_owner(self, port, pid):
        self.events.append(("réservé", pid))

    def close_tunnel(self, port):
        self.events.append("tunnel fermé")


@pytest.fixture
def borrowing(monkeypatch, tmp_path):
    """Cette machine emprunte la carte de « PC fixe » : llama.cpp, poids, compagnon et
    machine qui prête remplacés par des doublures. → (événements, compagnon)."""
    events = []
    monkeypatch.setattr(engine, "info", lambda max_age=0: {
        "folder": str(tmp_path), "version": "11707-abc",
        "devices": [{"name": "Vulkan1", "label": "RTX", "total_mib": 8000, "free_mib": 7000}]})
    monkeypatch.setattr(group.weights, "gguf_path", lambda name: tmp_path / "poids")
    monkeypatch.setattr(group, "_header", lambda path: {"size": 40 * 2 ** 30, "layers": 80})
    monkeypatch.setattr(engine, "llama_server", lambda *args: FakeServer(events))
    monkeypatch.setattr(group, "KEEP_EVERY", 3600)   # nouvelles aux prêteurs : appelées à la main
    monkeypatch.setattr(group, "WATCH_EVERY", 0.02, raising=False)

    def call(me, peer, action, payload, timeout=None):
        events.append((action, payload))
        if action == "cluster/lend":
            return {"service": "rpc-1", "device": "Vulkan1", "label": "RTX", "free_mib": 48000}
        return {"ok": True}
    monkeypatch.setattr(client, "call", call)
    peers.save(OTHER, "PC fixe", KEY, "127.0.0.1", 1)
    peers.set_net(OTHER, "c" * 64, {})
    peers.set_profile(OTHER, {"cluster": {"lends": True, "version": "11707-abc",
                                          "free_mib": 48000}})
    companion = RecordingCompanion(events)
    route.attach(companion)
    yield events, companion
    route.detach()


def test_the_tunnel_relays_only_llama_server_and_closes_with_it(borrowing):
    events, _ = borrowing
    g = group.Group("gros:70b")
    g.start()
    lend = next(payload for action, payload in events if action == "cluster/lend")
    assert lend["tunnel"] == "guarded"                 # la machine qui prête le vérifie
    steps = [e if isinstance(e, str) else e[0] for e in events]
    # Carte demandée, puis tunnel gardé, puis llama-server, aussitôt nommé au compagnon
    assert steps[:5] == ["cluster/lend", "tunnel", "llama-server lancé", "réservé",
                         "llama-server chargé"]
    assert events[1] == ("tunnel", "rpc-1", True, group.TUNNEL_CONNECTIONS)
    assert events[3] == ("réservé", LLAMA_PID)
    # Modèle chargé : la machine qui prête le sait (son serveur RPC s'arrêtera dès que la
    # connexion de llama-server tombera)
    assert g._still_lent()
    assert events[-1] == ("cluster/keep", {"service": "rpc-1", "loaded": True})
    # llama-server s'arrête (planté, tué) : tunnels fermés aussitôt, puis la carte rendue
    events.clear()
    g.server.alive = False
    assert _wait(lambda: g.closed and ("cluster/release", {"service": "rpc-1"}) in events)
    assert events[0] == "tunnel fermé"
    g.close()                                          # une deuxième fois : rien de plus
    assert events.count("tunnel fermé") == 1


def test_a_tunnel_that_cannot_be_reserved_stops_llama_server(borrowing, monkeypatch):
    events, companion = borrowing

    def refused(port, pid):
        raise CompanionError("Aucun tunnel sur ce port.")
    monkeypatch.setattr(companion, "set_tunnel_owner", refused)
    g = group.Group("gros:70b")

    def start(timeout, ready=None, stop=None, waiting=None, spawned=None):
        events.append("llama-server lancé")
        try:
            spawned(LLAMA_PID)
        except BaseException:
            events.append("llama-server arrêté")       # comme engine.Process.start
            raise
    monkeypatch.setattr(engine, "llama_server", lambda *args: type(
        "Server", (), {"start": staticmethod(start), "stop": lambda self: None,
                       "alive": False, "port": 1})())
    with pytest.raises(RuntimeError, match="ne réserve pas le tunnel"):
        g.start()
    assert "llama-server arrêté" in events and "tunnel fermé" in events
    assert ("cluster/release", {"service": "rpc-1"}) in events and g.closed


def test_an_outdated_companion_borrows_no_card(borrowing):
    events, _ = borrowing
    route.attach(RecordingCompanion(events, features=("service-usage",)))
    with pytest.raises(RuntimeError, match="compagnon trop ancien"):
        group.Group("gros:70b").start()
    assert events == []                                # aucune carte demandée


def test_a_phone_borrows_no_card(borrowing, monkeypatch):
    events, _ = borrowing
    monkeypatch.setattr(group.ollama, "list_models", lambda: [{"name": "gros:70b", "size": 1}])
    monkeypatch.setattr(group.ollama, "capabilities_many",
                        lambda names: {"gros:70b": ["completion"]})
    assert [o["id"] for o in group.available()] == ["reparti:gros:70b"]
    monkeypatch.setenv(android_app.APP_ENV, "1")
    assert group.available() == []
    with pytest.raises(RuntimeError, match="téléphone"):
        group.Group("gros:70b").start()
    assert events == []


def test_no_distributed_model_without_a_guarded_companion(borrowing, monkeypatch):
    """available() ne propose pas de « réparti: » si le compagnon ne garde pas ses tunnels
    (plus ancien) ou s'il manque : ces machines refuseraient le prêt à l'usage."""
    events, _ = borrowing
    monkeypatch.setattr(group.ollama, "list_models", lambda: [{"name": "gros:70b", "size": 1}])
    monkeypatch.setattr(group.ollama, "capabilities_many",
                        lambda names: {"gros:70b": ["completion"]})
    assert [o["id"] for o in group.available()] == ["reparti:gros:70b"]
    route.attach(RecordingCompanion(events, features=("service-usage",)))  # tunnels non gardés
    assert group.available() == []
    route.detach()                                     # aucun compagnon
    assert group.available() == []


def test_why_distributed_models_are_hidden_is_logged_once(borrowing, monkeypatch, caplog):
    """Sans compagnon capable de tunnels gardés, les « réparti: » disparaissent : la raison
    est écrite au journal, une fois (pas à chaque liste des modèles)."""
    events, _ = borrowing
    monkeypatch.setattr(group.ollama, "list_models", lambda: [{"name": "gros:70b", "size": 1}])
    monkeypatch.setattr(group.ollama, "capabilities_many",
                        lambda names: {"gros:70b": ["completion"]})
    monkeypatch.setattr(group, "_unavailable", {"reason": None})
    route.attach(RecordingCompanion(events, features=("service-usage",)))
    with caplog.at_level(logging.WARNING, logger="aiodos.cluster"):
        assert group.available() == [] and group.available() == []
    said = [r.getMessage() for r in caplog.records if "non proposés" in r.getMessage()]
    assert len(said) == 1 and "compagnon trop ancien" in said[0]


def test_the_real_launch_names_the_program_before_waiting(tmp_path):
    """engine.Process.start donne le numéro du programme dès son lancement ; un refus
    l'arrête aussitôt."""
    seen = []
    slow = engine.Process("essai-nomme", Path(sys.executable),
                          ["-c", "import time; time.sleep(60)"], engine.free_port())

    def spawned(pid):
        seen.append(pid)
        raise RuntimeError("tunnel refusé")
    with pytest.raises(RuntimeError, match="tunnel refusé"):
        slow.start(timeout=60, spawned=spawned)
    assert seen and seen[0] > 0 and not slow.alive


# ------------------------------------------------------------ machine qui prête
@pytest.fixture
def lending(monkeypatch, tmp_path):
    companion = FakeCompanion()
    route.attach(companion)
    processes = []

    def rpc_server(where, device, cache):
        processes.append(FakeProcess())
        return processes[-1]
    monkeypatch.setattr(engine, "info", lambda max_age=0: {
        "folder": str(tmp_path), "version": "11707-abc",
        "devices": [{"name": "Vulkan1", "label": "RTX", "total_mib": 8000, "free_mib": 7000}]})
    monkeypatch.setattr(engine, "rpc_server", rpc_server)
    peers.save(OTHER, "PC portable", KEY, "127.0.0.1", 1)
    peers.set_net(OTHER, "c" * 64, {})
    settings.set_value("lend_gpu", True)
    yield companion, processes
    helper.stop_all()
    route.detach()


ASK = {"version": "11707-abc", "tunnel": "guarded"}
HELD = {"held": True, "holding": 2, "foreign": 0, "blocked": None}


def _flows(**flows):
    """État d'un service tenu par le compagnon, avec ces flux."""
    return dict(HELD, **flows)


def test_a_borrower_with_an_unguarded_tunnel_is_asked_to_update(lending):
    companion, processes = lending
    with pytest.raises(PeerError, match="Mets AIODOS à jour sur « PC portable »"):
        helper.lend(FakeManager(), OTHER, {"version": "11707-abc"})
    assert processes == [] and companion.exposed == []


def test_an_old_local_companion_lends_nothing(lending):
    _, processes = lending
    for features in (("tunnel-owner",), ("tunnel-owner", "service-usage")):
        # Le second sait dire les flux, mais pas tenir le serveur RPC : il ne prête pas
        route.attach(FakeCompanion(features=features))
        with pytest.raises(PeerError, match="trop ancien pour prêter"):
            helper.lend(FakeManager(), OTHER, ASK)
    assert processes == []


def test_the_companion_holds_the_lent_rpc_server(lending):
    """Le service est exposé « tenu » : le compagnon garde ses connexions au serveur RPC, et
    aucun autre programme de la machine n'y est servi (compagnon/src/hold.rs)."""
    companion, _ = lending
    service = helper.lend(FakeManager(), OTHER, ASK)["service"]
    assert companion.exposed[-1][0]["name"] == service
    assert companion.exposed[-1][0]["hold"] is True


def test_the_rpc_server_stops_when_another_program_connects(lending, caplog):
    companion, processes = lending
    manager = FakeManager()
    service = helper.lend(manager, OTHER, ASK)["service"]
    companion.usage = {service: _flows(active=1, total=1)}
    assert helper.sweep_once(manager) == 1 and processes[-1].alive
    # Le compagnon a vu un autre programme connecté au serveur : il ne le sert pas, et le dit
    companion.usage = {service: _flows(active=1, total=1, foreign=1,
                                       blocked="un autre programme de cette machine…")}
    with caplog.at_level(logging.WARNING, logger="aiodos.cluster"):
        assert helper.sweep_once(manager) == 0
    assert not processes[-1].alive and companion.exposed[-1] == []
    assert any("autre programme" in r.getMessage() for r in caplog.records)


def test_the_rpc_server_stops_when_the_companion_no_longer_holds_it(lending):
    companion, processes = lending
    manager = FakeManager()
    # Compagnon relancé : il ne connaît plus le service (plus rien ne tient le serveur)
    service = helper.lend(manager, OTHER, ASK)["service"]
    companion.usage = {}
    assert helper.sweep_once(manager) == 0 and not processes[-1].alive
    # Service exposé, mais pas tenu
    service = helper.lend(manager, OTHER, ASK)["service"]
    companion.usage = {service: dict(_flows(), held=False)}
    assert helper.sweep_once(manager) == 0 and not processes[-1].alive
    # Compagnon arrêté
    helper.lend(manager, OTHER, ASK)
    companion.running = False
    try:
        assert helper.sweep_once(manager) == 0 and not processes[-1].alive
    finally:
        companion.running = True
    assert manager.executor.interactive == 0


def _cache_file(path: Path, size: int, age: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def test_the_weights_cache_is_trimmed_where_the_rpc_server_writes():
    """ggml-rpc-server range ses fichiers dans <cache>/rpc/ : le plafond s'y applique, en
    retirant les plus anciens."""
    rpc = helper.cache_dir() / "rpc"
    for i in range(5):                                 # du plus ancien (0) au plus récent (4)
        _cache_file(rpc / f"{i:016x}", 1000, age=100 - i)
    assert helper.trim_cache(cap=2500) == 2000
    assert sorted(f.name for f in rpc.iterdir()) == [f"{3:016x}", f"{4:016x}"]


def test_the_weights_cache_is_trimmed_during_a_loan(lending, monkeypatch):
    """Pendant le prêt aussi : le client du serveur RPC ne remplit pas le disque sans fin."""
    companion, processes = lending
    manager = FakeManager()
    service = helper.lend(manager, OTHER, ASK)["service"]
    monkeypatch.setattr(helper, "CACHE_MAX_BYTES", 1500)
    for i in range(4):
        _cache_file(helper.cache_dir() / "rpc" / f"{i:016x}", 1000, age=10 - i)
    companion.usage = {service: _flows(active=1, total=1)}
    helper._sweeper["trimmed"] = time.monotonic() - helper.CACHE_TRIM_S - 1
    assert helper.sweep_once(manager) == 1 and processes[-1].alive
    assert len(list((helper.cache_dir() / "rpc").iterdir())) == 1


def test_the_rpc_server_stops_when_the_tunnel_connection_drops(lending):
    companion, processes = lending
    manager = FakeManager()
    service = helper.lend(manager, OTHER, ASK)["service"]
    rpc = processes[-1]
    # llama-server pas encore là (aucun flux) : le prêt l'attend, borné par LEASE_S
    companion.usage = {service: _flows(active=0, total=0, idle_s=None)}
    assert helper.sweep_once(manager) == 1 and rpc.alive
    # Sa connexion en cours, puis un trou bref entre deux connexions du démarrage
    companion.usage = {service: _flows(active=1, total=3, idle_s=None)}
    assert helper.sweep_once(manager) == 1 and rpc.alive
    companion.usage = {service: _flows(active=0, total=3, idle_s=1.0)}
    assert helper.sweep_once(manager) == 1 and rpc.alive
    # Plus aucune connexion depuis DROP_S : serveur arrêté, service retiré du compagnon
    companion.usage = {service: _flows(active=0, total=3, idle_s=helper.DROP_S + 1)}
    assert helper.sweep_once(manager) == 0
    assert not rpc.alive and companion.exposed[-1] == []
    assert manager.executor.interactive == 0
    # Modèle chargé (dit par la machine qui emprunte) : arrêté presque aussitôt
    service = helper.lend(manager, OTHER, ASK)["service"]
    rpc = processes[-1]
    assert helper.keep(manager, OTHER, {"service": service, "loaded": True}) == {"ok": True}
    companion.usage = {service: _flows(active=0, total=5, idle_s=helper.DROP_LOADED_S + 0.5)}
    assert helper.sweep_once(manager) == 0 and not rpc.alive
    # Un compagnon remplacé par un plus ancien, qui ne tient plus le serveur : prêt arrêté
    # (il servirait n'importe quel programme de la machine)
    helper.lend(manager, OTHER, ASK)
    companion.features = frozenset({"tunnel-owner"})
    assert helper.sweep_once(manager) == 0 and not processes[-1].alive


# ------------------------------------------------------------ deux vrais compagnons
needs_companion = pytest.mark.skipif(companion_mod.find_binary() is None,
                                     reason="compagnon aiodos-net non compilé")
LOCAL = {"relays": ["none"], "directory": "none", "bind": "127.0.0.1:0", "portmap": False}


def _echo_service():
    """Service d'essai : renvoie ce qu'il reçoit (comme le serveur RPC répond)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve(conn):
        with conn:
            while data := conn.recv(4096):
                conn.sendall(data)

    def accept():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return server


def _echoed(port, data=b"llama") -> bool:
    """Une connexion au tunnel reçoit-elle l'écho de ce qu'elle envoie ?"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as tcp:
            tcp.settimeout(5)
            tcp.sendall(data)
            got = b""
            while len(got) < len(data):
                piece = tcp.recv(64)
                if not piece:
                    return False
                got += piece
            return got == data
    except OSError:
        return False


@pytest.fixture
def two_companions(tmp_path):
    nodes = [Companion("127.0.0.1:9", dict(LOCAL, data_dir=tmp_path / name))
             for name in ("emprunte", "prete")]
    for node in nodes:
        assert node.start(), node.error
    for me, other in ((nodes[0], nodes[1]), (nodes[1], nodes[0])):
        info = other.info()
        me.set_peers([{"id": info["id"], "addrs": info["addrs"]}])
    yield nodes
    for node in nodes:
        node.stop()


@needs_companion
def test_a_real_tunnel_relays_only_the_named_program(two_companions):
    borrower, lender = two_companions
    assert borrower.tunnel_guard_problem() is None
    assert borrower.supports(companion_mod.SERVICE_USAGE)
    service = _echo_service()
    try:
        lender.expose([{"name": "rpc-essai", "port": service.getsockname()[1],
                        "peers": [borrower.id]}])
        # Réservé à un autre programme (un faux llama-server) : ce programme-ci est refusé
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            port = borrower.open_tunnel(lender.id, "rpc-essai", max_connections=4)
            borrower.set_tunnel_owner(port, other.pid)
            assert not _echoed(port)
        finally:
            other.kill()
            other.wait(10)
        seen = next(t for t in borrower.status()["tunnels"]["tunnels"] if t["port"] == port)
        assert seen["guarded"] and seen["owner_pid"] == other.pid and seen["refused"] >= 1
        assert seen["max_connections"] == 4
        assert lender.service_usage()["rpc-essai"]["total"] == 0     # rien n'a atteint B
        borrower.close_tunnel(port)
        # Réservé à ce programme-ci : relayé
        port = borrower.open_tunnel(lender.id, "rpc-essai", max_connections=4)
        borrower.set_tunnel_owner(port, os.getpid())
        assert _echoed(port)
        usage = lender.service_usage()["rpc-essai"]
        assert usage["total"] == 1
        assert _wait(lambda: lender.service_usage()["rpc-essai"]["active"] == 0)
        assert isinstance(lender.service_usage()["rpc-essai"]["idle_s"], (int, float))
        borrower.close_tunnel(port)
    finally:
        service.close()


def _serial_service():
    """Service d'essai qui sert un client à la fois, dans l'ordre d'arrivée, jusqu'à ce qu'il
    ferme, et lui renvoie ce qu'il reçoit : comme ggml-rpc-server."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    while data := conn.recv(4096):
                        conn.sendall(data)
                except OSError:
                    pass
    threading.Thread(target=serve, daemon=True).start()
    return server


@needs_companion
def test_a_real_held_rpc_server_never_serves_another_program(two_companions):
    """Le compagnon de la machine qui prête tient le serveur : llama-server (par le tunnel)
    est servi ; un autre programme de cette machine, connecté directement, ne l'est jamais,
    et le compagnon le dit (« blocked ») : AIODOS arrête alors le prêt."""
    borrower, lender = two_companions
    assert lender.supports(companion_mod.SERVICE_HOLD), "compagnon compilé trop ancien"
    service = _serial_service()
    try:
        lender.expose([{"name": "rpc-tenu", "port": service.getsockname()[1],
                        "peers": [borrower.id], "hold": True}])
        port = borrower.open_tunnel(lender.id, "rpc-tenu", max_connections=4)
        borrower.set_tunnel_owner(port, os.getpid())
        for word in (b"un", b"deux"):                   # connexions brèves du démarrage
            assert _echoed(port, word)
        usage = lender.service_usage()["rpc-tenu"]
        assert usage["held"] is True and usage["blocked"] is None
        # Un autre programme se connecte directement au serveur RPC
        intruder = socket.create_connection(service.getsockname(), timeout=5)
        try:
            intruder.sendall(b"intrus")
            assert _wait(lambda: lender.service_usage()["rpc-tenu"]["blocked"], timeout=10)
            assert lender.service_usage()["rpc-tenu"]["foreign"] >= 1
            intruder.settimeout(2)
            with pytest.raises(socket.timeout):
                intruder.recv(16)                      # jamais servi
            assert not _echoed(port, b"trois")         # plus rien ne passe
        finally:
            intruder.close()
        borrower.close_tunnel(port)
    finally:
        service.close()
