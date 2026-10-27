"""Failles du réseau entre machines relevées par l'audit du 20/10/2026, et leurs garde-fous :
profil d'une machine appairée typé à la réception, balises UDP qui ne réécrivent plus une
machine appairée sans preuve, corps des requêtes bornés avant d'être lus, fichier de travail
gardé ici pour un ami, téléchargement borné chez la machine qui calcule, ami qui ne peut plus
faire échouer mes travaux. Sans Ollama ni réseau (sauf 127.0.0.1)."""
import asyncio
import gzip
import http.server
import json
import re
import socket
import socketserver
import threading
import time
import tracemalloc
import unicodedata

import pytest
from starlette.testclient import TestClient

from aiodos import db, settings
from aiodos.jobs import kinds, leases, queue, registry  # noqa: F401  (enregistre les tâches)
from aiodos.knowledge import store
from aiodos.network import (activity, body_limit, client, connections, crypto, envelope,
                            p2p_app, peers, profile, sharing, work, worker)
from aiodos.network import manager as manager_module
from aiodos.network.client import PeerError
from aiodos.network.envelope import PROTOCOL, ReplayGuard
from aiodos.network.manager import NetworkManager
from aiodos.network.worker import Assignment, Worker
from aiodos.node import identity
from aiodos.services import ollama
from tests.test_images import tiny_model  # noqa: F401  (modèle d'images d'essai)

ME = "0" * 32
MINE = "a" * 32
FRIEND = "f" * 32
FRIEND2 = "e" * 32
KEY = b"k" * 32
FRIEND_KEY = b"a" * 32
HOSTILE = "<img src=x onerror=\"window.__xss=sessionStorage.getItem('aiodos-token')\">"


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _manager() -> NetworkManager:
    return NetworkManager(ME, Bus(), {"host": "127.0.0.1", "discovery": False})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ============================================================ profil reçu (0, 16)
def _refresh_with(monkeypatch, reply, node_id=FRIEND):
    """Les nouvelles d'une machine appairée reçues comme dans la boucle du nœud."""
    net = _manager()

    async def no_exchange(peer):
        return None
    monkeypatch.setattr(net.internet, "exchange", no_exchange)
    monkeypatch.setattr(client, "call", lambda *args, **kw: json.loads(json.dumps(reply)))
    asyncio.run(net._refresh_peer(peers.get(node_id)))
    return net


def _clean(text, limit):
    return re.sub(r"[\x00-\x1f\x7f<>]", " ", text)[:limit]


def test_a_hostile_profile_is_typed_before_being_kept(monkeypatch):
    """Faille 0 (XSS sans clic) et 16 (NaN qui fait tomber /api/network) : le profil d'un
    ami est réduit à ce qui a la forme attendue avant d'être gardé."""
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    hostile = {
        "name": f"Léa\n{HOSTILE}" + "x" * 200,
        "version": HOSTILE,
        "memory": {"total": float("inf"), "available": "beaucoup"},
        "gpus": [{"name": HOSTILE, "vram_total": float("nan"), "utilization": 5}, "carte"],
        "power": {"mobile": True, "level": HOSTILE, "temperature": float("nan"),
                  "plugged": "oui"},
        "idle_seconds": -5,
        "ollama": {"models": {"length": HOSTILE}},
        "lends_models": "vrai",
        "updates": {"offer": {"version": "9.9.9"}, "files": {"9.9.9": "archive"}},
        "consult": {"open": 10 ** 30},
        "sharing": {"enabled": True, "detail": HOSTILE},
        "script": HOSTILE,
    }
    _refresh_with(monkeypatch, hostile)
    seen = next(p for p in peers.list_peers() if p["node_id"] == FRIEND)
    kept = seen["profile"]
    assert kept == {
        "name": _clean(hostile["name"], profile.NAME),
        "version": _clean(HOSTILE, 40),
        "memory": {},
        "gpus": [{"name": _clean(HOSTILE, 200), "utilization": 5}],
        "power": {"mobile": True},
        "ollama": {},
        "updates": {"files": {}},
        "consult": {},
        "sharing": {"enabled": True, "detail": _clean(HOSTILE, 300)},
    }
    assert "<" not in seen["name"] and "\n" not in seen["name"]
    assert len(seen["name"]) <= profile.NAME
    json.dumps(peers.list_peers(), allow_nan=False)         # ce que sert GET /api/network
    heard = activity._heard[FRIEND]["sharing"]
    assert "<" not in heard["detail"]


def test_a_profile_stored_before_the_fix_is_read_back_clean():
    """Une base écrite avant la correction (NaN, objet à la place d'une liste) ne casse plus
    ni l'API ni l'interface : le profil est relu nettoyé."""
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    db.execute("UPDATE peers SET profile=? WHERE node_id=?",
               ('{"memory": {"total": NaN}, "ollama": {"models": {"length": "<img>"}}, '
                '"power": {"level": Infinity}, "lends_models": true}', FRIEND))
    kept = next(p for p in peers.list_peers() if p["node_id"] == FRIEND)["profile"]
    assert kept == {"memory": {}, "ollama": {}, "power": {}, "lends_models": True}
    json.dumps(peers.list_peers(), allow_nan=False)
    db.execute("UPDATE peers SET profile=? WHERE node_id=?", ("[1, 2]", FRIEND))
    assert next(p for p in peers.list_peers() if p["node_id"] == FRIEND)["profile"] is None


def test_set_profile_never_writes_a_non_finite_number():
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    peers.set_profile(FRIEND, {"idle_seconds": float("nan"), "ollama": {"models": [
        {"name": "qwen3:8b", "size": float("inf"), "caps": ["completion"]},
        {"size": 3}]}})
    raw = db.query_one("SELECT profile FROM peers WHERE node_id=?", (FRIEND,))["profile"]
    assert "NaN" not in raw and "Infinity" not in raw
    assert json.loads(raw) == {"ollama": {"models": [{"name": "qwen3:8b",
                                                      "caps": ["completion"]}]}}


def test_numbers_and_flags_keep_their_type():
    assert profile.clean({"power": {"level": True, "temperature": 41.5, "plugged": None},
                          "cpu": {"threads": 16.0}, "gpus": [{"utilization": 101}]}) == {
        "power": {"temperature": 41.5, "plugged": None}, "cpu": {"threads": 16},
        "gpus": [{}]}
    assert profile.clean("texte") is None and profile.clean([1]) is None
    assert profile.name(" \x00PC <fixe>\n ") == "PC fixe" and profile.name(5) is None
    assert profile.clean({"ollama": {"models": [{"name": "m"}] * 1000}})["ollama"]["models"] \
        == [{"name": "m"}] * profile.MAX_MODELS


def test_a_profile_that_is_not_an_object_keeps_the_previous_one():
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    peers.set_profile(FRIEND, {"lends_models": True})
    peers.set_profile(FRIEND, ["pas", "un", "profil"])
    assert next(p for p in peers.list_peers() if p["node_id"] == FRIEND)["profile"] == {
        "lends_models": True}


def test_a_name_from_elsewhere_is_one_short_line():
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    peers.touch(FRIEND, name="Léa\n<script>x</script>" + "y" * 100)
    name = peers.get(FRIEND)["name"]
    assert "\n" not in name and "<" not in name and len(name) <= profile.NAME


HIDDEN = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def test_invisible_characters_cannot_disguise_a_name():
    """Relecture du 21/10/2026 : U+202E retourne le texte qui suit (« PC‮txt.exe » s'affiche
    autrement qu'il s'écrit), U+200B et U+2066 à U+2069 sont invisibles, U+0085 est un
    contrôle C1, et un demi-codet isolé fait échouer l'écriture en base."""
    hostile = "PC‮exe.txt​⁦x⁩\x85 \ud800"
    cleaned = profile.clean({"name": hostile, "version": hostile,
                             "ollama": {"models": [{"name": "llama" + hostile}]}})
    for text in (cleaned["name"], cleaned["version"], cleaned["ollama"]["models"][0]["name"]):
        assert not any(unicodedata.category(ch) in HIDDEN for ch in text)
    assert profile.name(hostile) == "PC exe.txt x"
    assert profile.name("Salon de Léa — 🎮") == "Salon de Léa — 🎮"     # rien d'autre ne change
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    peers.set_profile(FRIEND, {"name": hostile, "os": hostile})        # s'écrit en base
    peers.touch(FRIEND, name=hostile)
    kept = peers.list_peers()[0]
    assert kept["name"] == "PC exe.txt x" and "‮" not in kept["profile"]["os"]


def test_the_profile_keeps_everything_a_real_machine_sends(monkeypatch):
    """Ce que nettoie la réception ne retire rien d'un vrai profil : une clé ajoutée à
    identity.profile sans être décrite dans network/profile.py ferait échouer cet essai."""
    peers.save(MINE, "PC fixe", KEY, "127.0.0.1", 1)
    settings.set_value("lend_gpu", True)
    settings.set_value("lend_models", True)
    monkeypatch.setattr(identity, "gpus", lambda: [
        {"name": "NVIDIA GeForce RTX 4070", "vram_total": 12 << 30, "vram_used": 1 << 30,
         "utilization": 3}])
    monkeypatch.setattr(ollama, "version", lambda: "0.12.3")
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": "qwen3:8b", "digest": "sha256:abc", "size": 5 << 30, "family": "qwen3",
         "parameters": "8.2B", "quantization": "Q4_K_M"}])
    monkeypatch.setattr(ollama, "loaded_models", lambda: [{"name": "qwen3:8b"}])
    monkeypatch.setattr(ollama, "capabilities_many",
                        lambda names: {n: ["completion", "thinking"] for n in names})
    monkeypatch.setattr(identity.engine, "info", lambda: {"version": "b6500", "devices": []})
    monkeypatch.setattr(identity.engine, "lent_device",
                        lambda devices: {"label": "RTX 4070", "free_mib": 11000})
    monkeypatch.setattr(identity.images_engine, "info", lambda: {"version": "f0aac52"})
    monkeypatch.setattr(identity.images_catalog, "available", lambda: ["z-image-turbo"])
    identity._cache.update(at=0.0, profile=None)

    class Lender:
        node_id = ME
        executor = type("Executor", (), {"interactive": 0, "busy": {"gpu": 1}})()
        worker = type("Worker", (), {"state": {"active": True, "detail": "Prêt."}})()

        def name(self):
            return "PC portable"
    sent = json.loads(json.dumps(p2p_app._profile(Lender(), MINE, {})))
    assert set(sent) >= {"cpu", "memory", "gpus", "power", "ollama", "cluster", "images",
                         "updates", "consult", "load", "sharing"}
    assert profile.clean(sent) == sent


# ======================================================== balises UDP (15)
def _beacon(node_id, port, name="Faux nom <b>"):
    return {"aiodos": PROTOCOL, "id": node_id, "name": name, "port": port}


def _wait_checks(net):
    for thread in list(net.beacons.threads):
        thread.join(10)


def test_a_beacon_never_rewrites_a_paired_machine_without_proof():
    """Faille 15 : n'importe quel appareil du réseau local envoie une balise au nom d'une de
    mes machines (son identifiant circule en clair). Personne ne répond avec la clé de la
    paire à l'adresse annoncée : rien ne change."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    net = _manager()
    net._on_beacon(_beacon(MINE, _free_port()), "127.0.0.1")
    _wait_checks(net)
    kept = peers.get(MINE)
    assert (kept["address"], kept["port"], kept["name"]) == ("192.168.1.20", 47701, "PC fixe")


class _FakePeer(http.server.BaseHTTPRequestHandler):
    """La machine appairée, jointe à une nouvelle adresse : répond au ping scellé, et dit où
    la requête lui est arrivée (p2p_app._arrived_at)."""
    key = KEY
    body = None                     # réponse imposée (octets), sinon la vraie réponse scellée
    where = "ici"                   # « ici » : son adresse et son port ; None : version d'avant

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        env = json.loads(self.rfile.read(length))
        if self.body is not None:
            data = self.body
        else:
            asked = envelope.unwrap(KEY, "ping", MINE, env, ReplayGuard())
            reply = {"name": "PC fixe (Wi-Fi)"}
            if asked.get("where") and self.where == "ici":
                reply["where"] = list(self.connection.getsockname()[:2])
            elif asked.get("where") and self.where is not None:
                reply["where"] = self.where
            data = json.dumps(envelope.wrap_reply(self.key, "ping", MINE, ME, env["ts"],
                                                  reply)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def moved_peer():
    """Un faux nœud sur 127.0.0.1 : la machine appairée partie à une autre adresse."""
    servers = []

    def start(key=KEY, body=None, where="ici"):
        handler = type("Handler", (_FakePeer,), {"key": key, "body": body, "where": where})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_a_paired_machine_that_moved_is_followed_after_a_sealed_answer(moved_peer):
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    port = moved_peer()
    net = _manager()
    net._on_beacon(_beacon(MINE, port), "127.0.0.1")
    _wait_checks(net)
    kept = peers.get(MINE)
    # Le nom vient de la réponse scellée, pas de la balise
    assert (kept["address"], kept["port"], kept["name"]) == ("127.0.0.1", port, "PC fixe (Wi-Fi)")


def test_an_answer_sealed_with_another_key_or_too_big_proves_nothing(moved_peer):
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    net = _manager()
    for port in (moved_peer(key=b"z" * 32), moved_peer(body=b" " * (2 * 1024 ** 2))):
        net._on_beacon(_beacon(MINE, port), "127.0.0.1")
        _wait_checks(net)
        kept = peers.get(MINE)
        assert (kept["address"], kept["port"]) == ("192.168.1.20", 47701)


def test_beacons_are_checked_sparingly():
    """Un flot de balises vers la même adresse ne déclenche qu'une vérification."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    port = _free_port()
    net = _manager()
    for _ in range(20):
        net._on_beacon(_beacon(MINE, port), "127.0.0.1")
    _wait_checks(net)
    assert net.beacons.started == 1
    for _ in range(5):                                # à l'adresse qu'on lui connaît : rien
        net._on_beacon(_beacon(MINE, 47701), "192.168.1.20")
    assert net.beacons.started == 1


def test_a_malformed_beacon_is_ignored():
    net = _manager()
    for message in (_beacon("../../x", 47701), _beacon("A" * 32, 47701), _beacon(MINE, 0),
                    _beacon(MINE, 99999), _beacon(MINE, True), _beacon(MINE, "port"),
                    _beacon(ME, 47701)):
        net._on_beacon(message, "192.168.1.66")
    assert net.visible == {}
    net._on_beacon(_beacon(MINE, 47701, name="Salon\n<b>gras</b>" + "x" * 100), "192.168.1.66")
    assert set(net.visible) == {MINE}
    seen = net.visible[MINE]
    assert "<" not in seen["name"] and "\n" not in seen["name"] and len(seen["name"]) <= 60


def _relay(target_port):
    """L'appareil d'un attaquant : relaie chaque connexion, octet pour octet, vers la vraie
    machine (qui répond donc bien avec la clé de la paire)."""
    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.create_connection(("127.0.0.1", target_port)) as up:
                up.settimeout(5)
                self.request.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    data += self.request.recv(65536)
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(re.search(rb"(?i)content-length:\s*(\d+)", head).group(1))
                while len(body) < length:
                    body += self.request.recv(65536)
                up.sendall(head + b"\r\n\r\n" + body)
                while True:
                    part = up.recv(65536)
                    if not part:
                        break
                    self.request.sendall(part)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Relay)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_relaying_device_does_not_take_over_the_address(moved_peer):
    """Relecture du 21/10/2026 : un appareil annonce l'identifiant d'une de mes machines
    depuis sa propre adresse et relaie la connexion vers elle. La réponse est bien scellée,
    mais la machine dit où la requête lui est arrivée : pas là où l'appareil l'annonçait."""
    real_port = moved_peer()
    relay = _relay(real_port)
    try:
        peers.save(MINE, "PC fixe", KEY, "127.0.0.1", real_port)
        net = _manager()
        net._on_beacon(_beacon(MINE, relay.server_address[1]), "127.0.0.1")
        _wait_checks(net)
        assert net.beacons.started == 1
        assert peers.get(MINE)["port"] == real_port
    finally:
        relay.shutdown()
        relay.server_close()


def test_a_machine_of_a_previous_version_is_not_followed_on_a_beacon(moved_peer, caplog):
    """Une machine d'avant ne dit pas où la requête lui est arrivée : rien ne prouve que
    l'adresse est la sienne. Elle n'est pas suivie sur balise (ses propres requêtes scellées
    apprennent toujours sa nouvelle adresse), et le journal dit quoi faire."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    port = moved_peer(where=None)
    net = _manager()
    with caplog.at_level("INFO", logger="aiodos.network"):
        net._on_beacon(_beacon(MINE, port), "127.0.0.1")
        _wait_checks(net)
    assert (peers.get(MINE)["address"], peers.get(MINE)["port"]) == ("192.168.1.20", 47701)
    assert any("mets AIODOS à jour sur « PC fixe »" in r.getMessage() for r in caplog.records)


def test_a_ping_says_where_it_arrived(server):
    peers.save(MINE, "PC fixe", KEY, "testclient", 1)
    for asked, expected in (({"where": True}, ["testserver", 80]), ({}, None)):
        env = envelope.wrap(KEY, "ping", MINE, "moi", asked)
        reply = envelope.unwrap_reply(KEY, "ping", "moi", MINE, env["ts"],
                                      server.post("/p2p/v1/ping", json=env).json())
        assert reply.get("where") == expected


def test_invented_ids_do_not_hide_a_paired_machine_that_moved():
    """64 identifiants inventés, renouvelés sans cesse, remplissaient la liste des machines
    visibles avant qu'on regarde si la balise venait d'une machine appairée."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    net = _manager()
    for n in range(manager_module.MAX_VISIBLE + 10):
        net._on_beacon(_beacon(f"{n + 1:032x}", 47701), "192.168.1.66")
    assert len(net.visible) == manager_module.MAX_VISIBLE
    net._on_beacon(_beacon(MINE, _free_port()), "127.0.0.1")
    _wait_checks(net)
    assert net.beacons.started == 1 and MINE in net.visible


def test_a_slow_machine_does_not_delay_the_news_of_the_others(monkeypatch):
    """Une balise ne marque plus une machine « en ligne » : ses nouvelles viennent de la
    boucle du nœud. Une machine injoignable (30 s par Internet) n'y retarde plus les autres,
    qui passeraient sinon pour hors ligne (peers.ONLINE_WINDOW)."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "192.168.1.30", 47701, trust=sharing.FRIEND)
    net = _manager()
    news = []

    async def refresh(peer):
        if peer["node_id"] == FRIEND:
            await asyncio.sleep(3600)          # injoignable : le délai n'en finit pas
        news.append(peer["node_id"])

    async def nothing():
        return None
    monkeypatch.setattr(net, "_refresh_peer", refresh)
    monkeypatch.setattr(net.internet, "sync", nothing)
    monkeypatch.setattr(net.internet, "refresh_status", nothing)
    monkeypatch.setattr(net.public, "sync", nothing)
    monkeypatch.setattr(manager_module, "REFRESH_EVERY", 0.05)

    async def scenario():
        loop = asyncio.create_task(net._refresh_loop())
        await asyncio.sleep(0.5)
        loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
    assert news.count(MINE) >= 3 and FRIEND not in news


# ======================================== corps des requêtes bornés (17)
class _P2PManager:
    node_id = "moi"

    def __init__(self):
        self.guard = ReplayGuard()

    def name(self):
        return "PC portable"

    def changed(self):
        pass


@pytest.fixture
def server():
    return TestClient(p2p_app.create_p2p_app(_P2PManager()))


def _call_app(app, path, headers, chunks, client=("192.168.1.66", 5000)):
    """Appel direct de l'application ASGI : ce qu'elle a lu du corps, et sa réponse."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "client": client,
             "server": ("127.0.0.1", 47701),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    pending, read, sent = list(chunks), [], []

    async def receive():
        if pending:
            chunk = pending.pop(0)
            read.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body or b"null"), sum(read)


def test_an_announced_oversized_body_is_refused_before_being_read():
    """Faille 17 : un appareil du réseau local, sans appairage, annonçait quelques centaines
    de Mio ; tout était lu (et analysé) avant de savoir qui il était."""
    app = p2p_app.create_p2p_app(_P2PManager())
    for path, size in (("/p2p/v1/ping", 400 * 1024 ** 2), ("/p2p/v1/hello", 10_000),
                       ("/p2p/v1/pair/start", 10 ** 6), ("/p2p/v1/nimporte", 10 ** 9)):
        status, reply, read = _call_app(app, path, {"content-length": str(size)},
                                        [b"x" * 65536] * 4)
        assert status == 413 and read == 0, path
        assert "trop volumineuse" in reply["detail"]


def _from(node_id) -> bytes:
    return b'{"from": "' + node_id.encode() + b'", "to": "moi", "ts": 1, "box": "'


def test_a_big_body_from_an_unpaired_machine_is_cut_while_reading():
    """Sans Content-Length (envoi en morceaux) : la lecture s'arrête peu après OPEN_BODY."""
    app = p2p_app.create_p2p_app(_P2PManager())
    status, reply, read = _call_app(app, "/p2p/v1/ping", {},
                                    [_from("1" * 32)] + [b"A" * 65536] * 200)
    assert status == 413 and "non appairée" in reply["detail"]
    assert read < body_limit.OPEN_BODY + 2 * 65536


def test_an_unpaired_machine_announcing_a_big_body_is_refused_at_once():
    """Taille annoncée plus grande qu'OPEN_BODY : seul le début de l'enveloppe est lu."""
    app = p2p_app.create_p2p_app(_P2PManager())
    status, reply, read = _call_app(app, "/p2p/v1/ping", {"content-length": str(10 ** 6)},
                                    [_from("1" * 32) + b"A" * 8192] + [b"A" * 65536] * 15)
    assert status == 413 and "non appairée" in reply["detail"]
    assert read < 2 * body_limit.PREFIX + 8192


def test_a_paired_machine_may_send_a_bigger_request(server):
    peers.save(MINE, "PC fixe", KEY, "testclient", 1)       # son adresse (TestClient)
    env = envelope.wrap(KEY, "ping", MINE, "moi", {"pad": "x" * (2 * body_limit.OPEN_BODY)})
    response = server.post("/p2p/v1/ping", json=env)
    assert response.status_code == 200
    reply = envelope.unwrap_reply(KEY, "ping", "moi", MINE, env["ts"], response.json())
    assert reply["name"] == "PC portable"


def test_a_paired_machine_is_bounded_too(monkeypatch):
    monkeypatch.setattr(body_limit, "SEALED_BODY", 4 * body_limit.OPEN_BODY)
    peers.save(MINE, "PC fixe", KEY, "192.168.1.66", 1)
    app = p2p_app.create_p2p_app(_P2PManager())
    status, reply, read = _call_app(app, "/p2p/v1/ping", {},
                                    [_from(MINE)] + [b"A" * 65536] * 40)
    assert status == 413 and "trop volumineuse" in reply["detail"]
    assert read < body_limit.SEALED_BODY + 2 * 65536


def _computing_for_me(node_id, task="index_file", model=None):
    """Une unité confiée à cette machine (elle calcule pour moi)."""
    store.ensure_defaults()
    collection = store.list_collections()[0]["id"]
    job = queue.create_job("index_paths", {"paths": [], "collection_id": collection})
    db.execute("INSERT INTO units(job_id, task, resource, payload, status, node, lease_until, "
               "created) VALUES(?, ?, 'gpu', ?, 'running', ?, ?, ?)",
               (job, task, json.dumps({"model": model} if model else {}), node_id,
                db.now() + 60, db.now()))


def test_a_huge_result_comes_only_from_a_machine_computing_a_video_for_me(monkeypatch):
    """Un résultat (une vidéo) peut dépasser SEALED_BODY : seulement de la machine qui
    calcule pour celle-ci une image ou une vidéo, et depuis l'adresse qu'on lui connaît.
    Relecture du 21/10/2026 : une unité à indexer chez un ami ne l'ouvre plus."""
    monkeypatch.setattr(body_limit, "SEALED_BODY", 2 * body_limit.OPEN_BODY)
    monkeypatch.setattr(body_limit, "HUGE_BODY", 16 * body_limit.OPEN_BODY)
    peers.save(MINE, "PC de Léa", KEY, "192.168.1.66", 1, trust=sharing.FRIEND)
    app = p2p_app.create_p2p_app(_P2PManager())
    big = [_from(MINE)] + [b"A" * 65536] * 8

    status, _, read = _call_app(app, "/p2p/v1/work/result", {}, big)
    assert status == 413 and read < body_limit.SEALED_BODY + 2 * 65536   # rien à calculer

    _computing_for_me(MINE)                  # un fichier à indexer : un message ordinaire
    status, _, read = _call_app(app, "/p2p/v1/work/result", {}, big)
    assert status == 413 and read < body_limit.SEALED_BODY + 2 * 65536

    _computing_for_me(MINE, "generate_image", "wan2.1-t2v-1.3b")     # une vidéo
    status, reply, read = _call_app(app, "/p2p/v1/work/result", {}, big)
    assert status == 400 and read == sum(map(len, big))   # lu en entier (enveloppe fausse)
    status, _, read = _call_app(app, "/p2p/v1/work/result", {},
                                [_from(MINE)] + [b"A" * 65536] * 20)
    assert status == 413 and read < body_limit.HUGE_BODY + 2 * 65536

    db.execute("UPDATE peers SET address='192.168.1.99' WHERE node_id=?", (MINE,))
    status, _, read = _call_app(app, "/p2p/v1/work/result", {}, big)
    assert status == 413                     # même identifiant, autre adresse : pas la sienne


def test_the_size_of_a_result_follows_what_is_computed():
    from aiodos.jobs.kinds import images as kind
    peers.save(FRIEND, "PC de Léa", FRIEND_KEY, "192.168.1.30", 1, trust=sharing.FRIEND)
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 1)
    assert work.result_limit(FRIEND) == 0
    _computing_for_me(FRIEND)                        # un ami indexe : un message ordinaire
    assert work.result_limit(FRIEND) == 0
    _computing_for_me(FRIEND, "generate_image", "z-image-turbo")      # une image
    image = work.result_limit(FRIEND)
    assert kind.MAX_PNG * 16 // 9 < image < body_limit.HUGE_BODY // 8
    _computing_for_me(FRIEND, "generate_image", "wan2.1-t2v-1.3b")    # une vidéo
    assert kind.MAX_WEBM * 16 // 9 < work.result_limit(FRIEND) <= body_limit.HUGE_BODY
    _computing_for_me(MINE)          # une de mes machines : l'index d'un très long document
    assert work.result_limit(MINE) == work.MINE_RESULT


def test_an_unpaired_device_lying_about_from_gets_no_big_body():
    """Relecture du 21/10/2026 : l'identifiant d'une de mes machines (diffusé en clair par
    ses balises), envoyé depuis une autre adresse, ouvrait 16 Mio, lus ET analysés."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    app = p2p_app.create_p2p_app(_P2PManager())
    status, reply, read = _call_app(app, "/p2p/v1/ping", {},
                                    [_from(MINE)] + [b"A" * 65536] * 64)
    assert status == 413 and "pas encore reconnue à cette adresse" in reply["detail"]
    assert read < body_limit.OPEN_BODY + 2 * 65536
    status, _, read = _call_app(app, "/p2p/v1/ping", {"content-length": str(8 * 1024 ** 2)},
                                [_from(MINE) + b"A" * 8192] + [b"A" * 65536] * 64)
    assert status == 413 and read < 2 * body_limit.PREFIX + 8192


def test_a_forged_internet_header_from_the_network_proves_nothing(monkeypatch):
    """X-Aiodos-Peer n'est ajouté que par le compagnon, depuis cette machine : venu du
    réseau local, il ne fait pas passer un appareil pour une de mes machines jointe par
    Internet (un ami connaît leurs clés Internet par les présentations de groupe)."""
    monkeypatch.setattr(body_limit, "SEALED_BODY", 2 * body_limit.OPEN_BODY)
    monkeypatch.setattr(body_limit, "HUGE_BODY", 16 * body_limit.OPEN_BODY)
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    db.execute("UPDATE peers SET net_id=? WHERE node_id=?", ("ab" * 32, MINE))
    _computing_for_me(MINE, "generate_image", "wan2.1-t2v-1.3b")
    app = p2p_app.create_p2p_app(_P2PManager())
    big = [_from(MINE)] + [b"A" * 65536] * 8
    status, _, read = _call_app(app, "/p2p/v1/work/result", {"x-aiodos-peer": "ab" * 32}, big)
    assert status == 413 and read < body_limit.OPEN_BODY + 2 * 65536
    # Depuis cette machine (le compagnon), la même clé ouvre la limite
    status, _, read = _call_app(app, "/p2p/v1/work/result", {"x-aiodos-peer": "ab" * 32}, big,
                                client=("127.0.0.1", 5000))
    assert status == 400 and read == sum(map(len, big))


def test_the_internet_key_is_believed_only_from_this_machine():
    def scope(host, key="ab" * 32, server="0.0.0.0"):
        return {"client": (host, 5000), "server": (server, 47701),
                "headers": [(b"x-aiodos-peer", key.encode())]}
    assert connections.internet_key(scope("127.0.0.1")) == "ab" * 32
    assert connections.internet_key(scope("::1")) == "ab" * 32
    assert connections.internet_key(scope("::ffff:127.0.0.1")) == "ab" * 32
    # Serveur lancé sur une adresse précise (--p2p-host) : le compagnon s'y connecte
    assert connections.internet_key(scope("192.168.1.20", server="192.168.1.20")) == "ab" * 32
    for host in ("192.168.1.66", "10.0.0.2", "testclient", None):
        assert connections.internet_key(scope(host)) == ""
        assert connections.internet_key(scope(host, server="192.168.1.20")) == ""


def test_a_forged_internet_header_is_ignored_by_the_routes(server):
    """Le réseau local, même avec l'en-tête : l'adresse de l'appelant est retenue, et la clé
    n'est pas comparée (une machine appairée sur le réseau local reste joignable)."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    db.execute("UPDATE peers SET net_id=? WHERE node_id=?", ("ab" * 32, MINE))
    env = envelope.wrap(KEY, "ping", MINE, "moi", {})
    response = server.post("/p2p/v1/ping", json=env, headers={"x-aiodos-peer": "cd" * 32})
    assert response.status_code == 200
    assert peers.get(MINE)["address"] == "testclient"


def test_a_big_body_is_parsed_only_if_it_looks_like_an_envelope(server, monkeypatch):
    """Une machine appairée, à son adresse, envoie des Mio de [0, 0, …] ou de [{}, {}, …] :
    des centaines de Mio d'objets une fois analysés. Refusés sans être analysés."""
    peers.save(MINE, "PC fixe", KEY, "testclient", 1)
    parsed = []
    real_loads = p2p_app.json.loads

    def loads(raw, *args, **kwargs):
        if len(raw) > body_limit.OPEN_BODY:
            parsed.append(len(raw))
        return real_loads(raw, *args, **kwargs)
    monkeypatch.setattr(p2p_app.json, "loads", loads)
    for filler in (b"0,", b"{},"):
        hostile = (b'{"from": "' + MINE.encode() + b'", "to": "moi", "ts": 1, "x": ['
                   + filler * (1024 ** 2) + b'0]}')
        response = server.post("/p2p/v1/ping", content=hostile,
                               headers={"content-type": "application/json"})
        assert response.status_code == 400 and "illisible" in response.text
    assert parsed == []
    env = envelope.wrap(KEY, "ping", MINE, "moi", {"pad": "x" * (2 * body_limit.OPEN_BODY)})
    assert server.post("/p2p/v1/ping", json=env).status_code == 200      # une vraie, oui
    assert parsed and parsed[0] > body_limit.OPEN_BODY


def test_big_bodies_are_read_a_few_at_a_time(monkeypatch):
    """Au-delà d'OPEN_BODY, une machine n'a que quelques corps en lecture à la fois : les
    suivants attendent leur tour (SLOT_WAIT), puis reçoivent un 503 lisible."""
    monkeypatch.setattr(body_limit, "SLOT_WAIT", 0.2)
    peers.save(MINE, "PC fixe", KEY, "192.168.1.66", 1)
    seen = []

    async def inner(scope, receive, send):
        seen.append((await receive())["body"][:9])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    limiter = body_limit.BodyLimit(inner, {"ping"})
    big = [_from(MINE)] + [b"A" * 65536] * 2

    async def busy():
        for _ in range(body_limit.BIG_PER_SENDER):
            assert await limiter.big.acquire(MINE, 1)
    asyncio.run(busy())
    status, reply, _ = _call_app(limiter, "/p2p/v1/ping", {}, big)
    assert status == 503 and "réessaie" in reply["detail"] and seen == []
    limiter.big.release(MINE)
    status, _, read = _call_app(limiter, "/p2p/v1/ping", {}, big)
    assert status == 200 and read == sum(map(len, big)) and len(seen) == 1
    assert limiter.big.held == {MINE: body_limit.BIG_PER_SENDER - 1}   # son tour est rendu


def test_a_waiting_body_gets_its_turn():
    slots = body_limit._Slots(per_sender=1, total=1)

    async def scenario():
        assert await slots.acquire(MINE, 1)
        waiting = asyncio.create_task(slots.acquire(FRIEND, 2))
        await asyncio.sleep(0.05)
        assert not waiting.done()                 # en tout : un seul à la fois
        slots.release(MINE)
        assert await waiting
        assert not await slots.acquire(FRIEND, 0.05)
    asyncio.run(scenario())


def test_an_unreadable_body_gets_a_short_answer_without_echo(server):
    response = server.post("/p2p/v1/ping", content=b"[1, 2]",
                           headers={"content-type": "application/json"})
    assert response.status_code == 400 and "[1, 2]" not in response.text
    response = server.post("/p2p/v1/ping", content="pas du JSON <b>".encode())
    assert response.status_code == 400 and "<b>" not in response.text


# ================================== connexions muettes ou trop nombreuses (17)
@pytest.fixture
def p2p_server(data_dir, monkeypatch):
    """Le vrai serveur entre machines du nœud, sur 127.0.0.1, délais raccourcis."""
    monkeypatch.setattr(connections, "HEADER_S", 1)
    monkeypatch.setattr(connections, "BODY_GRACE", 1)
    monkeypatch.setattr(connections, "WATCH_EVERY", 0.2)
    connections.forget_known()
    db.connect()
    net = NetworkManager(ME, Bus(), {"force": True, "host": "127.0.0.1", "port": _free_port(),
                                     "discovery": False})
    ready, stop = threading.Event(), threading.Event()

    def run():
        async def serve():
            await net._start()
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.05)
            await net._stop()
        asyncio.run(serve())
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(30)
    yield net.p2p_port
    stop.set()
    thread.join(15)
    connections.forget_known()


def _hello(port) -> int:
    import requests
    return requests.post(f"http://127.0.0.1:{port}/p2p/v1/hello", json={}, timeout=5).status_code


def _closed(sock, wait=5) -> bool:
    """La connexion a-t-elle été fermée par le serveur ?"""
    sock.settimeout(wait)
    try:
        return sock.recv(1024) == b""
    except (ConnectionResetError, ConnectionAbortedError):
        return True
    except TimeoutError:
        return False


def test_muted_connections_no_longer_deny_the_server(p2p_server):
    """Relecture du 21/10/2026 : avec le plafond global d'uvicorn (64), 64 connexions aux
    en-têtes inachevés donnaient 503 à tout le monde, aussi longtemps qu'elles restaient
    ouvertes. Désormais, ni plafond global, et elles sont fermées au bout de HEADER_S."""
    socks = []
    try:
        for _ in range(80):
            s = socket.create_connection(("127.0.0.1", p2p_server))
            s.sendall(b"POST /p2p/v1/hello HTTP/1.1\r\nHost: x\r\n")      # inachevés
            socks.append(s)
        assert _hello(p2p_server) == 200
        assert all(_closed(s) for s in socks[:5])
    finally:
        for s in socks:
            s.close()


def test_a_body_sent_drop_by_drop_is_cut(p2p_server, monkeypatch):
    monkeypatch.setattr(connections, "same_machine", lambda *hosts: False)   # réseau local
    with socket.create_connection(("127.0.0.1", p2p_server)) as s:
        s.sendall(b"POST /p2p/v1/ping HTTP/1.1\r\nHost: x\r\nContent-Length: 60000\r\n\r\n{")
        closed = False
        for _ in range(30):
            try:
                s.sendall(b" ")
            except OSError:
                closed = True
                break
            if _closed(s, 0.2):
                closed = True
                break
        assert closed


def test_one_address_has_only_its_share_of_connections(p2p_server, monkeypatch):
    monkeypatch.setattr(connections, "same_machine", lambda *hosts: False)   # réseau local
    monkeypatch.setattr(connections, "PER_ADDRESS", 4)
    socks = [socket.create_connection(("127.0.0.1", p2p_server)) for _ in range(4)]
    try:
        extra = socket.create_connection(("127.0.0.1", p2p_server))
        assert _closed(extra, 2)                      # fermée aussitôt
        extra.close()
        assert not any(_closed(s, 0.1) for s in socks)
    finally:
        for s in socks:
            s.close()


def test_my_machines_have_a_larger_share_of_connections(p2p_server, monkeypatch):
    """Une de mes machines (ou un ami) à son adresse connue : PER_KNOWN connexions, pas
    PER_ADDRESS. Relecture finale du 21/10/2026 : une rafale de conversations d'une de mes
    machines voyait ses connexions en trop fermées sans réponse (« Machine injoignable »)."""
    monkeypatch.setattr(connections, "same_machine", lambda *hosts: False)   # réseau local
    monkeypatch.setattr(connections, "HEADER_S", 30)       # elles restent, le temps de compter
    monkeypatch.setattr(connections, "PER_ADDRESS", 4)
    monkeypatch.setattr(connections, "PER_KNOWN", 8)
    peers.save(MINE, "PC fixe", KEY, "127.0.0.1", 1)       # une de mes machines, là
    connections.forget_known()
    socks = [socket.create_connection(("127.0.0.1", p2p_server)) for _ in range(8)]
    try:
        assert not any(_closed(s, 0.1) for s in socks)
        extra = socket.create_connection(("127.0.0.1", p2p_server))
        assert _closed(extra, 1)                      # au-delà de sa part : fermée aussitôt
        extra.close()
    finally:
        for s in socks:
            s.close()


def test_strangers_do_not_take_the_places_of_my_machines(p2p_server, monkeypatch):
    monkeypatch.setattr(connections, "same_machine", lambda *hosts: False)   # réseau local
    monkeypatch.setattr(connections, "MAX_STRANGERS", 3)
    socks = [socket.create_connection(("127.0.0.1", p2p_server)) for _ in range(3)]
    try:
        extra = socket.create_connection(("127.0.0.1", p2p_server))
        assert _closed(extra, 2)                      # un inconnu de trop
        extra.close()
        peers.save(MINE, "PC fixe", KEY, "127.0.0.1", 1)      # une de mes machines, là
        connections.forget_known()
        mine = socket.create_connection(("127.0.0.1", p2p_server))
        assert not _closed(mine, 0.3)
        mine.close()
    finally:
        for s in socks:
            s.close()


def test_the_server_between_machines_uses_the_guarded_protocol(data_dir):
    db.connect()
    net = NetworkManager(ME, Bus(), {"force": True, "host": "127.0.0.1", "port": _free_port(),
                                     "discovery": False})

    async def scenario():
        await net._start()
        try:
            return net._server.config
        finally:
            await net._stop()
    config = asyncio.run(scenario())
    assert config.http is connections.GuardedH11 and config.limit_concurrency is None


# =================================== fichier facultatif gardé ici pour un ami (18)
def test_the_starting_image_of_a_retouch_never_reaches_a_friend(tmp_path):
    """Faille 18 : l'ordre envoyé à un ami ne contient pas l'image de départ d'une retouche ;
    work/blob ne la lui donne pas non plus, même s'il la demande."""
    peers.save(FRIEND, "PC d'un ami", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    peers.save(MINE, "PC fixe", KEY, "127.0.0.1", 1)
    # Hors du dossier de données d'AIODOS (tmp_path ici), jamais indexé
    (tmp_path / "docs").mkdir()
    start = tmp_path / "docs" / "depart.txt"         # une unité, retouchée en image ci-dessous
    start.write_bytes(b"\x89PNG photo de depart")
    store.ensure_defaults()
    collection = store.list_collections()[0]["id"]
    queue.create_job("index_paths", {"paths": [str(start)], "collection_id": collection})
    unit_id = db.query_one("SELECT id FROM units")["id"]

    def lend_to(node):
        db.execute("UPDATE units SET task='generate_image', payload=?, status='running', "
                   "node=?, lease_until=? WHERE id=?",
                   (json.dumps({"model": "essai", "init_path": str(start)}), node,
                    db.now() + 60, unit_id))
    lend_to(FRIEND)
    with pytest.raises(PeerError, match="reste"):
        work.blob(None, FRIEND, {"unit": unit_id, "offset": 0})
    lend_to(MINE)                          # une de mes machines, elle, la reçoit
    piece = work.blob(None, MINE, {"unit": unit_id, "offset": 0})
    assert envelope.b64d(piece["data"]) == start.read_bytes()


# ===================================== téléchargement borné chez qui calcule (19)
OWNER, LENDER = "machine-a", "machine-b"


class _OwnerExecutor:
    def settle(self, job_id, finished):
        if finished:
            queue.finalize_job(job_id)

    def publish_job(self, job_id, force=False):
        pass

    def wake(self):
        pass


class _OwnerManager:
    executor = _OwnerExecutor()

    def changed(self):
        pass

    def name(self):
        return "PC portable"


class _LenderManager:
    node_id = LENDER

    def __init__(self):
        self.executor = type("Executor", (), {"interactive": 0, "cooldown": {}, "slots": {},
                                              "busy": {}})()

    def changed(self):
        pass


@pytest.fixture
def lending(monkeypatch, tmp_path):
    """Propriétaire et machine qui calcule dans la même base (comme tests/test_worker.py)."""
    store.ensure_defaults()
    settings.set_value("embed_model", "nomic-embed-text")
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": "nomic-embed-text:latest", "digest": "sha256:x", "size": 1}])
    peers.save(LENDER, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2)
    (tmp_path / "docs").mkdir()                      # hors du dossier de données d'AIODOS
    note = tmp_path / "docs" / "note.txt"
    note.write_text("Le crachin breton est une pluie fine.", encoding="utf-8")
    collection = store.list_collections()[0]["id"]
    queue.create_job("index_paths", {"paths": [str(note)], "collection_id": collection})
    owner = _OwnerManager()
    lender = Worker(_LenderManager())
    asked = []

    def call(a, action, payload, timeout=30):
        asked.append((action, payload))
        return work.HANDLERS[action](owner, LENDER, payload)
    monkeypatch.setattr(lender, "_call", call)
    order = work.lease(owner, LENDER, {"resources": ["embed"],
                                       "caps": {"work_api": work.WORK_API,
                                                "models": {"nomic-embed-text:latest": "sha256:x"}}})
    monkeypatch.setattr(registry.TASKS["index_file"], "compute",
                        lambda *args: pytest.fail("rien à calculer"))
    return {"lender": lender, "asked": asked, "assignment": Assignment(OWNER, "PC portable",
                                                                       order["unit"])}


def _blob_calls(asked):
    return [payload for action, payload in asked if action == "work/blob"]


def test_an_announced_file_bigger_than_the_limit_is_not_downloaded(lending):
    """Faille 19 : la taille vient de l'autre machine ; au-delà de work.MAX_BLOB, rien n'est
    demandé ni écrit."""
    a = lending["assignment"]
    a.order["blob"]["size"] = 1000 * work.MAX_BLOB
    outcome = lending["lender"]._process(a)
    assert "trop gros" in outcome and _blob_calls(lending["asked"]) == []


def test_a_piece_longer_than_asked_is_refused(lending, monkeypatch):
    lender, a = lending["lender"], lending["assignment"]
    owner_call = lender._call

    def bloated(assignment, action, payload, timeout=30):
        reply = owner_call(assignment, action, payload, timeout)
        if action == "work/blob":
            reply = {"data": envelope.b64e(envelope.b64d(reply["data"]) * 50)}
        return reply
    monkeypatch.setattr(lender, "_call", bloated)
    a.order["blob"]["size"] = 100                     # plus que le fichier, moins que le morceau
    outcome = lender._process(a)
    assert "plus long" in outcome and len(_blob_calls(lending["asked"])) == 1


def test_without_room_on_the_disk_the_unit_is_given_back(lending, monkeypatch):
    lender, a = lending["lender"], lending["assignment"]
    monkeypatch.setattr(worker.shutil, "disk_usage",
                        lambda path: type("Usage", (), {"free": 10 ** 6})())
    outcome = lender._process(a)
    assert "place" in outcome and _blob_calls(lending["asked"]) == []
    unit = db.query_one("SELECT status, attempts, node FROM units WHERE id=?", (a.order["id"],))
    assert (unit["status"], unit["attempts"], unit["node"]) == ("queued", 0, None)


_MIB = b"A" * 1024 ** 2               # écrit par le faux propriétaire, alloué une fois


class _Owner(http.server.BaseHTTPRequestHandler):
    """Le propriétaire d'une unité (un ami), qui répond à work/blob ou work/lease par
    64 Mio annoncés, 64 Mio sans taille annoncée, ou une bombe gzip (64 Mio de zéros
    compressés en quelques dizaines de Kio)."""
    mode = "annoncée"
    bomb = b""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if self.mode == "annoncée":
                self.send_header("Content-Length", str(64 * len(_MIB) + 2))
            elif self.mode == "gzip":
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(self.bomb)))
            else:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.mode == "gzip":
                self.wfile.write(self.bomb)
                return
            self.wfile.write(b'"')
            for _ in range(64):
                self.wfile.write(_MIB)
        except OSError:
            pass                                 # la machine qui calcule a coupé : bien

    def log_message(self, *args):
        pass


@pytest.fixture
def owner_answering():
    servers = []

    def start(mode):
        bomb = gzip.compress(bytes(64 * 1024 ** 2), 9) if mode == "gzip" else b""
        handler = type("Handler", (_Owner,), {"mode": mode, "bomb": bomb})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        peers.save(FRIEND, "PC d'un ami", FRIEND_KEY, "127.0.0.1", server.server_address[1],
                   trust=sharing.FRIEND)
        return peers.get(FRIEND)
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode", ["annoncée", "sans taille", "gzip"])
def test_an_owner_cannot_flood_the_memory_of_the_machine_computing(owner_answering, monkeypatch,
                                                                   mode):
    """Faille 19, partie mémoire (relecture du 21/10/2026) : client.call lisait toute la
    réponse, décompressée, avant de la refuser. Un ami propriétaire d'unités répondait à
    work/blob par 64 Mio, ou par une bombe gzip, et la mémoire de la machine qui calcule
    (un téléphone…) y passait. La réponse est maintenant lue en flux et bornée ; une réponse
    compressée est refusée sans être décompressée."""
    owner = owner_answering(mode)
    monkeypatch.setattr(worker, "CHUNK_RETRY_DELAYS", ())        # pas de nouvel essai
    lender = Worker(_LenderManager())
    a = Assignment(FRIEND, "PC d'un ami", {"id": 1, "task": "index_file", "resource": "embed",
                                           "blob": {"size": work.BLOB_CHUNK}})
    tracemalloc.start()
    try:
        with pytest.raises(PeerError):
            lender._chunk(a, 0, work.BLOB_CHUNK)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 ** 2                 # 64 Mio annoncés, envoyés ou décompressés


@pytest.mark.parametrize("mode,why", [("annoncée", "trop volumineuse"),
                                      ("sans taille", "trop volumineuse"),
                                      ("gzip", "compressée")])
def test_a_bounded_answer_says_why_it_is_refused(owner_answering, mode, why):
    owner = owner_answering(mode)
    with pytest.raises(PeerError, match=why):
        client.call(ME, owner, "work/blob", {"unit": 1, "offset": 0}, limit=1024 ** 2)


class _Answering(http.server.BaseHTTPRequestHandler):
    """Un propriétaire qui répond à work/lease : en distillant sa réponse (« goutte »), par
    une réponse scellée qui contient NaN (« nan »), ou honnêtement, sans unité (« rien »)."""
    protocol_version = "HTTP/1.1"
    mode, node, key = "rien", FRIEND, FRIEND_KEY
    stop = threading.Event()

    def do_POST(self):
        env = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self.close_connection = True
        if self.mode == "goutte":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            for _ in range(64):
                if self.stop.wait(0.5):
                    return
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except OSError:
                    return
            return
        if self.mode == "nan":
            box = crypto.seal(self.key, b'{"unit": NaN}',
                              envelope._aad("rep", "work/lease", self.node, LENDER, env["ts"]))
            reply = {"box": envelope.b64e(box)}
        else:
            reply = envelope.wrap_reply(self.key, "work/lease", self.node, LENDER, env["ts"],
                                        {"unit": None, "retry_after": 60})
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def owners():
    """Des propriétaires d'unités (_Answering), appairés à la machine qui calcule."""
    servers = []

    def start(node, key, mode, trust):
        handler = type("Handler", (_Answering,), {"mode": mode, "node": node, "key": key,
                                                  "stop": threading.Event()})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append((server, handler))
        peers.save(node, f"PC {node[:4]}", key, "127.0.0.1", server.server_address[1],
                   trust=trust)
        peers.touch(node)
        return peers.get(node)
    yield start
    for server, handler in servers:
        handler.stop.set()
        server.shutdown()
        server.server_close()
    client._lingering.clear()


def test_an_owner_that_drips_its_answer_is_left_at_the_deadline(owners, monkeypatch):
    """Relecture finale du 21/10/2026 : reply_limit.call ne tenait pas l'échéance (vérifiée
    entre deux lectures de 64 Kio, sans coupure) ; un ami qui distillait work/lease figeait
    toute la boucle du prêt, qui attend chaque machine à son tour, pour mes machines aussi.
    client.call rend la main à l'échéance, et ce propriétaire n'est pas redemandé avant un
    moment."""
    monkeypatch.setattr(worker, "LEASE_TIMEOUT", 1)
    peer = owners(FRIEND, FRIEND_KEY, "goutte", sharing.FRIEND)
    lender = Worker(_LenderManager())
    began = time.monotonic()
    reply = lender._ask(peer, ["embed"])
    assert time.monotonic() - began < 10                 # 64 octets à 2 par seconde : 32 s
    assert reply == {"unit": None, "retry_after": worker.SLOW_RETRY}


def test_a_sealed_answer_with_nan_is_a_refusal_and_the_next_owner_is_asked(owners,
                                                                          monkeypatch):
    """Une réponse scellée qui contient NaN levait SealError, que _ask ne rattrapait pas :
    le tour du prêt s'interrompait sur ce même ami, toutes les 2 s, et les machines suivantes
    (en tête : celle qui m'a le plus donné) n'étaient jamais demandées."""
    friend = owners(FRIEND, FRIEND_KEY, "nan", sharing.FRIEND)
    mine = owners(MINE, KEY, "rien", sharing.MINE)
    lender = Worker(_LenderManager())
    with pytest.raises(PeerError):
        client.call(LENDER, friend, "work/lease", {}, timeout=2)
    monkeypatch.setattr(worker, "sharing_state",
                        lambda previous=None: {"active": True, "state": "ready", "detail": ""})
    monkeypatch.setattr(lender, "_offered", lambda: ["embed"])
    monkeypatch.setattr(lender, "_candidates", lambda: [friend, mine])
    assert asyncio.run(lender._tick()) == worker.CHECK_EVERY
    assert set(lender._next_ask) == {FRIEND, MINE}       # l'ami, puis ma machine


def test_an_honest_answer_is_read_as_before(server_thread_reply):
    owner, expected = server_thread_reply
    reply = client.call(ME, owner, "work/renew", {"unit": 1}, limit=worker.SMALL_REPLY)
    assert reply == expected


@pytest.fixture
def server_thread_reply():
    """Un propriétaire qui répond normalement (réponse scellée), et l'erreur lisible d'un
    refus (HTTP 400 avec « detail »)."""
    expected = {"ok": True}

    class Honest(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            env = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            data = json.dumps(envelope.wrap_reply(FRIEND_KEY, "work/renew", FRIEND, ME,
                                                  env["ts"], expected)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Honest)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    peers.save(FRIEND, "PC d'un ami", FRIEND_KEY, "127.0.0.1", server.server_address[1],
               trust=sharing.FRIEND)
    yield peers.get(FRIEND), expected
    server.shutdown()
    server.server_close()


def test_the_worker_bounds_the_answers_of_the_owner(lending, monkeypatch, tmp_path):
    lender, a = lending["lender"], lending["assignment"]
    limits = []

    def bounded(my_id, peer, action, payload, key=None, timeout=10, limit=None):
        limits.append((action, limit))
        return work.HANDLERS[action](_OwnerManager(), LENDER, payload)
    monkeypatch.setattr(lender, "_call", Worker._call.__get__(lender))   # le vrai, cette fois
    monkeypatch.setattr(worker.client, "call", bounded)
    received = lender._download(a, tmp_path / "recu")
    assert "crachin" in open(received, encoding="utf-8").read()
    size = a.order["blob"]["size"]
    assert limits == [("work/blob", worker._reply_size("work/blob", {"size": size}))]
    assert worker._reply_size("work/blob", {"size": work.BLOB_CHUNK}) < 4 * work.BLOB_CHUNK
    assert worker._reply_size("work/renew", {}) == worker.SMALL_REPLY


# ===================================== ami qui ferait échouer mes travaux (29)
@pytest.fixture
def friend_library(monkeypatch, tmp_path):
    """Une bibliothèque ouverte aux amis, huit fichiers à indexer, et un ami appairé."""
    store.ensure_defaults()
    settings.set_value("embed_model", "nomic-embed-text")
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": "nomic-embed-text:latest", "digest": "sha256:nomic", "size": 1}])
    peers.save(FRIEND, "PC d'un ami", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    folder = tmp_path / "docs"
    folder.mkdir()
    for n in range(8):
        (folder / f"note{n}.txt").write_text(f"Note numéro {n}.", encoding="utf-8")
    collection = store.list_collections()[0]["id"]
    store.set_scope(collection, "friends")
    job_id = queue.create_job("index_paths", {"paths": [str(folder)], "collection_id": collection})
    work.forget_friends()
    return {"manager": _OwnerManager(), "job": job_id}


def _friend_lease(lib):
    return work.lease(lib["manager"], FRIEND, {"resources": ["embed"], "caps": {
        "work_api": work.WORK_API, "models": {"nomic-embed-text:latest": "sha256:nomic"}}})["unit"]


def test_a_friend_holds_only_a_few_units_at_once(friend_library):
    orders = [_friend_lease(friend_library) for _ in range(8)]
    given = [o for o in orders if o]
    assert len(given) == work.FRIEND_MAX_HELD < 8


def test_a_friend_cannot_make_my_units_fail(friend_library):
    """Faille 29 : bail puis échec annoncé, deux fois, et l'unité finissait en échec sans avoir
    été calculée. L'échec d'un ami rend l'unité sans compter d'essai, et elle ne lui revient
    pas : une autre machine (ou celle-ci) la calculera."""
    lib = friend_library
    seen = set()
    for _ in range(20):
        order = _friend_lease(lib)
        if order is None:
            break
        assert order["id"] not in seen                 # jamais la même unité deux fois
        seen.add(order["id"])
        assert work.fail(lib["manager"], FRIEND, {"unit": order["id"], "error": "panne"}) == {"ok": True}
    assert len(seen) == 8
    rows = db.query("SELECT status, attempts, error FROM units")
    assert all(r["status"] == "queued" and r["attempts"] == 0 for r in rows)
    assert all("PC d'un ami" in r["error"] for r in rows)
    assert queue.job_summary(lib["job"])["failed"] == 0


def test_a_friend_that_only_fails_is_set_aside(friend_library, monkeypatch):
    monkeypatch.setattr(work, "FRIEND_FAILURES_ASIDE", 3)
    lib = friend_library
    for _ in range(3):
        order = _friend_lease(lib)
        work.fail(lib["manager"], FRIEND, {"unit": order["id"], "error": "panne"})
    assert sharing.set_aside_reason(FRIEND)
    assert _friend_lease(lib) is None


def test_a_friend_giving_a_unit_back_may_take_it_again(friend_library):
    lib = friend_library
    order = _friend_lease(lib)
    assert work.release(lib["manager"], FRIEND, {"unit": order["id"]}) == {"ok": True}
    assert _friend_lease(lib)["id"] == order["id"]


def test_a_friend_never_gets_the_last_attempt(friend_library):
    """Un bail que l'ami laisse expirer compte un essai : l'unité ne lui revient pas, le
    dernier essai se fait ici (ou chez une de mes machines). Elle ne finit donc pas en
    échec à cause de lui."""
    lib = friend_library
    order = _friend_lease(lib)
    leases.expire(db.now() + leases.LEASE_S + 1)
    unit = db.query_one("SELECT status, attempts FROM units WHERE id=?", (order["id"],))
    assert (unit["status"], unit["attempts"]) == ("queued", 1)
    given = set()
    for _ in range(4):                    # les autres unités lui sont données (et rendues)
        other = _friend_lease(lib)
        given.add(other["id"])
        work.release(lib["manager"], FRIEND, {"unit": other["id"]})
    assert order["id"] not in given
    db.execute("UPDATE units SET status='done' WHERE id<>?", (order["id"],))
    assert _friend_lease(lib) is None     # restée seule en file : elle ne part pas chez lui
    peers.save(MINE, "PC fixe", KEY, "127.0.0.1", 1)          # une de mes machines, elle, oui
    mine = work.lease(lib["manager"], MINE, {"resources": ["embed"], "caps": {
        "work_api": work.WORK_API, "models": {"nomic-embed-text:latest": "sha256:nomic"}}})
    assert mine["unit"] is not None


def test_a_friend_asking_too_often_waits(friend_library, monkeypatch):
    monkeypatch.setattr(work, "FRIEND_LEASES_PER_MINUTE", 3)
    lib = friend_library
    for _ in range(3):
        order = _friend_lease(lib)
        work.release(lib["manager"], FRIEND, {"unit": order["id"]})
    assert _friend_lease(lib) is None


def test_a_friend_does_not_learn_why_a_file_failed_here(lending):
    a = lending["assignment"]
    a_friend = Assignment(FRIEND, "PC d'un ami", a.order)
    full = OSError(28, "No space left on device")
    assert "space" not in Worker._reason(a_friend, full)
    peers.save(FRIEND, "PC d'un ami", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
    assert "space" not in Worker._reason(a_friend, full)
    assert "space" in Worker._reason(a, full)               # mes machines, elles, le savent


def test_a_friend_getting_work_is_not_slowed_down(friend_library, monkeypatch):
    """Seules comptent les demandes restées sans unité et les unités rendues : un ami rapide
    qui reçoit une unité à chaque demande n'est pas freiné (relecture du 21/10/2026)."""
    monkeypatch.setattr(work, "FRIEND_LEASES_PER_MINUTE", 3)
    lib = friend_library
    orders = [_friend_lease(lib) for _ in range(work.FRIEND_MAX_HELD)]
    assert all(orders)                               # 4 demandes servies, malgré 3 par minute
    for _ in range(3):
        assert _friend_lease(lib) is None            # il détient le plafond : comptées
    for order in orders:                             # il rend un résultat : une place libre…
        db.execute("UPDATE units SET status='done', lease_until=NULL WHERE id=?",
                   (order["id"],))
    assert _friend_lease(lib) is None                # … mais trois demandes vaines : il attend


def test_a_friend_cannot_keep_a_unit_forever(friend_library):
    """Relecture du 21/10/2026 : en renouvelant son bail sans rien rendre, un ami gardait
    ses unités jusqu'au redémarrage, et le travail ne finissait jamais."""
    lib = friend_library
    order = _friend_lease(lib)
    assert work.renew(lib["manager"], FRIEND, {"unit": order["id"]}) == {"ok": True,
                                                                         "state": "running"}
    db.execute("UPDATE units SET started=? WHERE id=?",
               (db.now() - work.FRIEND_LEASE_MAX - 1, order["id"]))
    assert work.renew(lib["manager"], FRIEND, {"unit": order["id"]})["ok"] is False
    unit = db.query_one("SELECT status, attempts, node, error FROM units WHERE id=?",
                        (order["id"],))
    assert (unit["status"], unit["attempts"], unit["node"]) == ("queued", 0, None)
    assert "trop long" in unit["error"]
    given = {(_friend_lease(lib) or {}).get("id") for _ in range(3)}
    assert order["id"] not in given                  # elle ne lui revient pas


@pytest.fixture
def friend_images(tiny_model, monkeypatch):
    """Des images que seuls mes amis savent calculer (cette machine n'a pas le moteur), et
    que j'accepte de leur confier (images_from_friends)."""
    from aiodos.imaging import catalog, engine
    from aiodos.jobs.kinds import images as kind
    from tests.test_images import ready
    monkeypatch.setattr(work.ollama, "list_models", lambda max_age=10: [])
    for node in (FRIEND, FRIEND2):
        peers.save(node, "PC d'un ami", FRIEND_KEY, "127.0.0.1", 2, trust=sharing.FRIEND)
        peers.set_profile(node, {"images": {"engine": "f0aac52", "shares": True}})
    settings.set_value("images_from_friends", True)
    job = queue.create_job("generate_images", kind.normalize({"model": "essai",
                                                              "prompt": "un chat"}))
    caps = {"work_api": work.WORK_API, "images": {"engine": "f0aac52",
                                                  "files": catalog.hashes("essai")}}
    work.forget_friends()
    ready()
    monkeypatch.setattr(engine, "folder", lambda: None)             # pas calculable ici
    manager = _OwnerManager()

    def ask(node):
        return work.lease(manager, node, {"resources": ["gpu"], "caps": caps})["unit"]
    return {"job": job, "ask": ask, "manager": manager}


def test_an_image_only_friends_compute_is_not_stranded_by_an_expired_lease(friend_images):
    """Relecture du 21/10/2026 : « le dernier essai jamais chez un ami » laissait pour
    toujours en file une image que seuls mes amis savent calculer, après un seul bail échu
    (PC de l'ami en veille) ou un redémarrage."""
    ask = friend_images["ask"]
    order = ask(FRIEND)
    leases.expire(db.now() + leases.LEASE_S + 1)
    unit = db.query_one("SELECT status, attempts FROM units WHERE id=?", (order["id"],))
    assert (unit["status"], unit["attempts"]) == ("queued", 1)
    again = ask(FRIEND2)
    assert again is not None and again["id"] == order["id"]
    leases.expire(db.now() + leases.LEASE_S + 1)     # échu une seconde fois : elle échoue
    assert db.query_one("SELECT status FROM units WHERE id=?", (order["id"],))["status"] == \
        "failed"


def test_a_genuine_failure_at_the_only_friend_ends_the_job(friend_images, monkeypatch):
    """Un vrai échec chez le seul ami qui sait la calculer : un autre ami passe d'abord, puis
    lui de nouveau ; après MAX_ATTEMPTS essais, l'unité échoue avec un message qui le dit,
    au lieu d'être retentée après chaque redémarrage."""
    peers.remove(FRIEND2)
    ask, manager = friend_images["ask"], friend_images["manager"]
    order = ask(FRIEND)
    work.fail(manager, FRIEND, {"unit": order["id"], "error": "VRAM insuffisante"})
    unit = db.query_one("SELECT status, attempts, error FROM units WHERE id=?", (order["id"],))
    assert (unit["status"], unit["attempts"]) == ("queued", 1)
    assert "Aucune de mes machines ne sait la calculer" in unit["error"]
    assert ask(FRIEND) is None                       # un autre ami d'abord
    monkeypatch.setattr(work, "FRIEND_RETRY_OTHERS", 0)
    assert ask(FRIEND)["id"] == order["id"]          # personne d'autre : lui de nouveau
    work.fail(manager, FRIEND, {"unit": order["id"], "error": "VRAM insuffisante"})
    unit = db.query_one("SELECT status, error FROM units WHERE id=?", (order["id"],))
    assert unit["status"] == "failed" and "VRAM insuffisante" in unit["error"]
    assert queue.job_summary(friend_images["job"])["failed"] == 1


def test_an_image_one_of_my_machines_computes_keeps_its_last_attempt(friend_images):
    """Une de mes machines en ligne a le modèle : le dernier essai lui revient, comme pour
    une unité que cette machine sait calculer."""
    peers.save(MINE, "PC fixe", KEY, "192.168.1.20", 47701)
    peers.set_profile(MINE, {"images": {"engine": "f0aac52", "models": ["essai"],
                                        "shares": True}})
    peers.touch(MINE)
    ask = friend_images["ask"]
    order = ask(FRIEND)
    leases.expire(db.now() + leases.LEASE_S + 1)
    assert ask(FRIEND) is None and ask(FRIEND2) is None
    work.forget_friends()
    assert db.query_one("SELECT status FROM units WHERE id=?", (order["id"],))["status"] == \
        "queued"
