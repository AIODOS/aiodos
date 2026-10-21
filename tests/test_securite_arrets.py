"""Arrêts (audit de sécurité du 20/10/2026, suites des relectures du 21/10) : ce qui
s'arrête quand on le demande, et ce qu'on ne croit plus quand sd-server n'est plus là.

- Une unité calculée ici reçoit son arrêt (jobs/executor.py) : annuler ou supprimer son
  travail, ou arrêter AIODOS, interrompt la lecture d'une page (une page du réseau local
  qui distille ses octets ne garde plus sa place « net » jusqu'au délai total), une
  transcription, une image ; l'unité retourne en file sans compter d'essai, et rien
  n'entre dans les bibliothèques d'un travail annulé. Une pause du travail laisse finir
  l'unité en cours.
- La recherche web d'une réponse reçoit son arrêt (chat/pipeline.py, depuis la route de la
  conversation) : Stop interrompt la recherche ou la page en cours de lecture, et les
  suivantes ne sont pas lues. La recherche elle-même (DuckDuckGo) est bornée en taille et
  en durée, et n'en garde que des adresses http(s).
- Une image n'est demandée à sd-server, et son « terminé » n'est cru, que si c'est bien le
  sd-server lancé par AIODOS, seul sur son port (imaging/generate.py, engine.serving) :
  tombé en route, qui prend son port ne fait pas passer sa propre image, et l'image de
  départ d'une retouche ne part pas chez un programme qui s'est ajouté sur ce port.
Sans Internet : des serveurs d'essai sur 127.0.0.1.
"""
import asyncio
import os
import socketserver
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from fastapi.testclient import TestClient

from aiodos import db
from aiodos.api.server import create_app
from aiodos.chat import pipeline
from aiodos.imaging import engine, generate
from aiodos.jobs import executor as executor_module
from aiodos.jobs import queue, registry
from aiodos.jobs.executor import Executor
from aiodos.jobs.registry import Interrupted, JobKind, TaskSpec, UnitSpec
from aiodos.knowledge import store
from aiodos.services import web
from tests.test_images import FakeSD, _payload, png, tiny_model, wait_for  # noqa: F401

TOKEN = "jeton-arrets"
AUTH = {"X-Aiodos-Token": TOKEN}
ME, SOMEONE_ELSE = os.getpid(), 4242       # ce programme ; un autre programme de la machine


def _in_thread(fn):
    outcome = {}

    def run():
        try:
            outcome["value"] = fn()
        except Exception as exc:      # noqa: BLE001  (l'essai regarde l'exception)
            outcome["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _unit(job_id):
    return db.query_one("SELECT status, error, attempts FROM units WHERE job_id=?", (job_id,))


# ------------------------------------------------------------- unités d'ici
class _Wiki:
    """Le wiki du réseau local (127.0.0.1) : une page qui distille un octet tous les
    dixièmes de seconde, une minute durant ; note quand le lecteur arrive et quand il
    coupe."""

    def __init__(self):
        self.connected, self.left = threading.Event(), threading.Event()
        wiki = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.close_connection = True          # une page par connexion
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                wiki.connected.set()
                try:
                    for _ in range(600):
                        self.wfile.write(b"1\r\na\r\n")
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    wiki.left.set()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/wiki"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def wiki(monkeypatch):
    """Délai total d'une lecture porté à 15 s : sans arrêt, la lecture irait jusque-là."""
    monkeypatch.setattr(web, "TOTAL_TIMEOUT", 15.0)
    page = _Wiki()
    yield page
    page.close()


def _app():
    """AIODOS, une seule place « net » et rien d'autre : la page occupe sa place."""
    return TestClient(create_app(TOKEN, {"testserver"},
                                 slots={"gpu": 0, "embed": 0, "cpu": 0, "net": 1}))


def _index(client, url) -> tuple:
    store.ensure_defaults()
    collection = store.list_collections()[0]["id"]
    reply = client.post("/api/index_urls", headers=AUTH,
                        json={"urls": [url], "collection_id": collection})
    assert reply.status_code == 200
    return reply.json()["job_id"], collection


def test_cancelling_in_the_tasks_view_stops_reading_a_page_of_my_network(wiki):
    """Une page du réseau local (unité index_url_here, lue ici) qui distille ses octets :
    annuler le travail coupe la lecture aussitôt, au lieu de 15 s (30 s d'ordinaire) de
    place « net » prise ; l'unité est rendue sans compter d'essai, et rien n'entre dans la
    bibliothèque."""
    with _app() as client:
        job_id, collection = _index(client, wiki.url)
        assert wiki.connected.wait(5)
        assert client.post(f"/api/jobs/{job_id}/cancel", headers=AUTH).json() == {"ok": True}
        assert wiki.left.wait(4), "la lecture aurait dû s'arrêter avec le travail"
        wait_for(lambda: _unit(job_id)["status"] == "cancelled")     # file en torrent : annulée
    job = queue.job_summary(job_id)
    assert job["status"] == "cancelled" and job["done"] == 0 and job["failed"] == 0
    assert _unit(job_id)["attempts"] == 0 and _unit(job_id)["error"] is None
    assert store.list_entries(collection) == []


def test_stopping_aiodos_stops_reading_a_page(wiki):
    """Fenêtre fermée, AIODOS arrêté : la page en cours de lecture est coupée, pas lue
    jusqu'à son délai total."""
    with _app() as client:
        _index(client, wiki.url)
        assert wiki.connected.wait(5)
        began = time.monotonic()
    assert wiki.left.wait(4), "la lecture aurait dû s'arrêter avec AIODOS"
    assert time.monotonic() - began < 10                  # 15 s sans arrêt


@pytest.fixture
def running():
    """Un exécuteur qui tourne, sa boucle asyncio dans un fil à part, comme dans le nœud."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="essai-executeur")
    thread.start()

    def run(coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(10)
    executor = Executor("noeud-essai", lambda event: None)
    run(executor.start())
    yield executor
    run(executor.stop())
    wait_for(lambda: not executor._running)
    run(asyncio.sleep(0.05))
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


@pytest.fixture
def slow_task(monkeypatch):
    """Une tâche d'essai (processeur) qui dure tant que son arrêt n'est pas levé, ou que
    l'essai ne la libère pas (gate) ; huit secondes au plus. Elle note l'arrêt reçu ; levé,
    elle s'interrompt (Interrupted), ou échoue comme une connexion coupée (« then » :
    « error »)."""
    seen = types.SimpleNamespace(stops=[], started=threading.Event(), gate=threading.Event(),
                                 finished=[])

    def compute(payload, file, options, stop):
        seen.stops.append(stop)
        seen.started.set()
        deadline = time.monotonic() + 8
        while not seen.gate.is_set() and time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                if payload.get("then") == "error":
                    raise RuntimeError("connexion coupée")
                raise Interrupted()
            time.sleep(0.02)
        seen.finished.append(payload)
        return {"fini": True}
    monkeypatch.setitem(registry.TASKS, "essai_arret", TaskSpec("essai_arret", compute))
    monkeypatch.setitem(registry.JOB_KINDS, "essai_arret", JobKind(
        "essai_arret", lambda params: ("Essai", [UnitSpec("essai_arret", "cpu", dict(params))],
                                       {})))
    return seen


def test_cancelling_a_job_stops_its_unit_running_here(running, slow_task):
    job_id = queue.create_job("essai_arret", {})
    running.wake()
    assert slow_task.started.wait(5)
    queue.cancel_job(job_id)                      # comme la vue Tâches, une création retirée…
    running.wake()
    wait_for(lambda: _unit(job_id)["status"] != "running", 4)
    assert slow_task.stops[0] is not None and slow_task.stops[0].is_set()
    assert slow_task.finished == []               # pas allée au bout
    assert _unit(job_id)["status"] == "cancelled" and _unit(job_id)["attempts"] == 0
    assert queue.job_summary(job_id)["status"] == "cancelled"


def test_a_job_deleted_meanwhile_stops_its_unit(running, slow_task):
    job_id = queue.create_job("essai_arret", {})
    running.wake()
    assert slow_task.started.wait(5)
    queue.pause_job(job_id)              # seul un travail ni en file ni en cours se supprime
    queue.delete_job(job_id)
    running.wake()
    wait_for(lambda: slow_task.stops[0] is not None and slow_task.stops[0].is_set(), 4)
    wait_for(lambda: running.busy["cpu"] == 0 and not running._halts)
    assert slow_task.finished == []
    assert db.query_one("SELECT COUNT(*) AS n FROM units")["n"] == 0


def test_pausing_a_job_lets_its_running_unit_finish(running, slow_task):
    """Une pause empêche de commencer d'autres unités ; celle en cours va au bout."""
    job_id = queue.create_job("essai_arret", {})
    running.wake()
    assert slow_task.started.wait(5)
    queue.pause_job(job_id)
    running.wake()
    time.sleep(0.5)                               # quelques tours de la boucle
    stop = slow_task.stops[0]
    assert stop is not None and not stop.is_set()
    slow_task.gate.set()
    wait_for(lambda: _unit(job_id)["status"] == "done")
    assert slow_task.finished == [{}]


def test_an_error_after_the_stop_is_not_counted_as_a_failure(running, slow_task):
    """Une lecture coupée par l'arrêt finit souvent en erreur (connexion coupée) : l'unité
    est rendue, sans essai compté ni erreur notée."""
    job_id = queue.create_job("essai_arret", {"then": "error"})
    running.wake()
    assert slow_task.started.wait(5)
    queue.cancel_job(job_id)
    running.wake()
    wait_for(lambda: _unit(job_id)["status"] != "running", 4)
    unit = _unit(job_id)
    assert unit["status"] == "cancelled" and unit["error"] is None and unit["attempts"] == 0


def test_a_unit_whose_stop_is_already_raised_does_not_start(monkeypatch):
    calls = []
    monkeypatch.setitem(registry.TASKS, "essai_vite", TaskSpec(
        "essai_vite", lambda payload, file, options, stop: calls.append(stop) or {"ok": True}))
    stop = threading.Event()
    assert executor_module._run_unit("essai_vite", {}, stop)[0] == {"ok": True}
    assert calls == [stop]                        # l'arrêt va au calcul
    stop.set()
    with pytest.raises(Interrupted):
        executor_module._run_unit("essai_vite", {}, stop)
    assert calls == [stop]                        # déjà levé : elle ne commence pas


# ---------------------------------------------------------- recherche web du chat
def test_stop_interrupts_the_web_search_of_an_answer(monkeypatch):
    """Stop pendant la préparation : la page en cours de lecture s'interrompt (elle aurait
    pu distiller ses octets jusqu'au délai total), les suivantes ne sont pas lues."""
    results = [{"title": f"Page {i}", "url": f"https://exemple{i}.org/"} for i in range(3)]
    monkeypatch.setattr(web, "web_search", lambda question, count, stop=None: results)
    asked = []

    def fetch(url, max_chars=20000, *, public_only=True, stop=None):
        asked.append((url, stop, public_only))
        if (stop or threading.Event()).wait(5):
            raise web.FetchStopped()
        return "Titre", "texte"
    monkeypatch.setattr(web, "fetch_url_text", fetch)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    began = time.monotonic()
    blocks, sources = pipeline.web_context("cherche la météo", stop)
    assert time.monotonic() - began < 2
    assert [url for url, _stop, _public in asked] == ["https://exemple0.org/"]
    assert asked[0][1] is stop and asked[0][2] is True      # adresses publiques seulement
    assert blocks == [] and sources == []


def test_a_search_stopped_before_it_starts_asks_nothing(monkeypatch):
    searched = []
    monkeypatch.setattr(web, "web_search",
                        lambda question, count, stop=None: searched.append(question) or [])
    stop = threading.Event()
    stop.set()
    assert pipeline.web_context("cherche la météo", stop) == ([], [])
    _messages, sources = pipeline.build_messages(
        [], "cherche la météo", libraries=False, conversations=False, web_search=True,
        stop=stop)
    assert searched == [] and sources == []


def test_stop_in_the_chat_route_interrupts_the_page_being_read(monkeypatch):
    """Par la route, comme le fait l'interface (relecture du 21/10/2026) : le client s'en va
    (Stop) pendant la lecture d'une page lente. Elle s'interrompt aussitôt (la route lançait
    la recherche sans l'arrêt : jusqu'à 30 s par page, et chaque page), les suivantes ne
    sont pas lues, et rien ne part au modèle."""
    import uvicorn

    from tests.nodes import free_port
    results = [{"title": f"Page {i}", "url": f"https://exemple{i}.org/"} for i in range(3)]
    monkeypatch.setattr(web, "web_search", lambda question, count, stop=None: results)
    asked, interrupted = [], threading.Event()

    def fetch(url, max_chars=20000, *, public_only=True, stop=None):
        asked.append(url)
        (stop or threading.Event()).wait(8)          # une page lente, sauf arrêt
        if stop is not None and stop.is_set():
            interrupted.set()
            raise web.FetchStopped()
        return "Titre", "texte"
    monkeypatch.setattr(web, "fetch_url_text", fetch)
    sent = []
    monkeypatch.setattr(pipeline, "generate", lambda model, messages, *a, **k:
                        sent.append(model) or iter([("content", "Réponse.")]))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(TOKEN, {f"127.0.0.1:{port}"}),
                                           host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started, 15)
        response = requests.post(f"http://127.0.0.1:{port}/api/chat", headers=AUTH,
                                 stream=True, timeout=10,
                                 json={"text": "Quelle météo demain à Lyon ?",
                                       "model": "local-test", "web": True,
                                       "libraries": False, "conversations": False})
        assert response.status_code == 200
        wait_for(lambda: asked, 5)                   # la première page est en cours
        response.close()                             # Stop : le client s'en va
        assert interrupted.wait(5), "la page en cours de lecture aurait dû s'interrompre"
        time.sleep(0.5)
        assert asked == ["https://exemple0.org/"] and sent == []
    finally:
        server.should_exit = True
        thread.join(15)


# La recherche elle-même (DuckDuckGo), imitée sur 127.0.0.2, tenue ici pour une adresse
# d'Internet : la garde des adresses ne laisse joindre que l'Internet public
SEARCH_HOST = "127.0.0.2"
RESULTS = b"""<html><body>
<a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexemple.org%2Fa&amp;rut=1">Exemple <b>A</b></a>
<a class="result__a" href="javascript:alert(document.cookie)">Piege</a>
<a class="result__a" href="search-ms:query=x&amp;crumb=location:\\\\example.org\\x">Piege 2</a>
<a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=pub.example">Pub</a>
<a class="result__a" href="https://exemple.org/b">Exemple B</a>
<a class="result__a" href="file:///C:/Windows/win.ini">Piege 3</a>
<a class="result__a" href="http://exemple.net/c">Exemple
   C</a>
</body></html>"""


class _FakeSearch:
    """Faux DuckDuckGo (POST /html/) : `answer(handler)` répond."""

    def __init__(self, answer):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                answer(self)

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self):
                # Sans getfqdn : la résolution inverse de 127.0.0.2 prend 5 s sous Windows
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]

        self.server = Server((SEARCH_HOST, 0), Handler)
        self.url = f"http://{SEARCH_HOST}:{self.server.server_address[1]}/html/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _results_page(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(RESULTS)))
    handler.end_headers()
    handler.wfile.write(RESULTS)


def _dripping_results(handler):
    """Une réponse qui distille un octet tous les dixièmes de seconde, sans fin."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    try:
        for _ in range(3000):
            handler.wfile.write(b"1\r\n \r\n")
            handler.wfile.flush()
            time.sleep(0.1)
    except OSError:
        pass


@pytest.fixture
def fake_search(monkeypatch):
    from aiodos.services import addresses
    real = addresses.is_public
    monkeypatch.setattr(addresses, "is_public", lambda ip: ip == SEARCH_HOST or real(ip))
    servers = []

    def start(answer):
        servers.append(_FakeSearch(answer))
        monkeypatch.setattr(web, "SEARCH_URL", servers[-1].url)
        return servers[-1]
    yield start
    for server in servers:
        server.close()


def test_only_web_addresses_come_out_of_a_search(fake_search):
    """Une source « web » devient un lien dans la réponse : ni javascript:, ni search-ms:,
    ni file:, ni publicité, ni lien de DuckDuckGo lui-même."""
    fake_search(_results_page)
    assert web.web_search("chats", 10) == [
        {"title": "Exemple A", "url": "https://exemple.org/a"},
        {"title": "Exemple B", "url": "https://exemple.org/b"},
        {"title": "Exemple C", "url": "http://exemple.net/c"}]


def _searching(stop=None):
    outcome = {}

    def run():
        try:
            outcome["value"] = web.web_search("chats", 3, stop=stop)
        except Exception as exc:      # noqa: BLE001  (l'essai regarde l'exception)
            outcome["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_a_search_that_never_ends_is_cut_at_its_total_delay(fake_search, monkeypatch):
    """Le délai de requests ne vaut qu'entre deux lectures : une réponse qui distille ses
    octets retenait la préparation de la réponse indéfiniment."""
    monkeypatch.setattr(web, "SEARCH_TIMEOUT", 1.0)
    fake_search(_dripping_results)
    began = time.monotonic()
    thread, outcome = _searching()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - began < 8
    assert isinstance(outcome.get("error"), web.PageUnreadable)
    assert "trop lente" in str(outcome["error"])


def test_a_search_stops_when_asked(fake_search):
    fake_search(_dripping_results)
    stop = threading.Event()
    thread, outcome = _searching(stop)
    time.sleep(0.4)
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and isinstance(outcome.get("error"), web.FetchStopped)


def test_a_huge_search_answer_is_read_up_to_its_maximum(fake_search, monkeypatch):
    monkeypatch.setattr(web, "SEARCH_MAX_BYTES", 64 * 1024)
    read = []

    def huge(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", str(len(RESULTS) + 8 * 1024 ** 2))
        handler.end_headers()
        try:
            handler.wfile.write(RESULTS)
            for _ in range(128):
                handler.wfile.write(b" " * 64 * 1024)
                read.append(1)
        except OSError:
            pass
    fake_search(huge)
    assert [r["url"] for r in web.web_search("chats", 3)][:1] == ["https://exemple.org/a"]


# --------------------------------------------------------------------- sd-server
@pytest.fixture
def sd_server(tiny_model, monkeypatch):  # noqa: F811  (fixture importée)
    """Le sd-server d'AIODOS, imité : un faux sd-server (tests/test_images.FakeSD), qui
    écoute dans ce programme, tenu pour lancé par AIODOS (engine._state). Qui écoute sur son
    port se règle dans `listening` (la table des ports, engine._listeners), et
    `process.alive` dit s'il tourne encore ; engine.serving est le vrai."""
    servers, listening = [], set()
    monkeypatch.setattr(engine, "_listeners", lambda port: set(listening))
    monkeypatch.setattr(engine, "_sockets_of", lambda pid: {pid})
    monkeypatch.setattr(engine, "release", lambda: None)
    monkeypatch.setattr(generate, "POLL_S", 0.02)

    def make(**options):
        fake = FakeSD(**options)
        servers.append(fake)
        port = fake.server.server_address[1]
        process = types.SimpleNamespace(port=port, alive=True, stop=lambda: None,
                                        process=types.SimpleNamespace(pid=ME))
        monkeypatch.setitem(engine._state, "process", process)
        monkeypatch.setattr(engine, "acquire",
                            lambda model_id, stop=None, waiting=None: f"http://127.0.0.1:{port}")
        listening.add(ME)
        return fake, process, listening
    yield make
    for fake in servers:
        fake.close()


def test_a_retouch_is_not_sent_to_a_port_another_program_listens_on(sd_server):
    fake, _process, listening = sd_server(polls=1)
    made = generate.image(_payload())                     # seul sur son port : demandée
    assert generate.png_size(made["png"]) == (256, 384) and len(fake.bodies) == 1
    listening.add(SOMEONE_ELSE)                           # un autre programme s'y ajoute
    with pytest.raises(generate.ImageError, match="un autre programme écoute"):
        generate.image(_payload(init_png=png(256, 384), strength=0.5))
    assert len(fake.bodies) == 1                          # rien n'est parti, ni l'image de départ


def test_a_finished_image_from_whoever_took_the_port_is_not_believed(sd_server):
    """sd-server tombe pendant le calcul ; un autre programme s'installe sur son port et
    répond « terminé », avec son image."""
    fake, process, listening = sd_server(polls=None)      # calcule sans fin
    thread, outcome = _in_thread(lambda: generate.image(_payload()))
    wait_for(lambda: fake.jobs.get("job_1", {}).get("seen", 0) >= 1)
    process.alive = False
    listening.clear()
    listening.add(SOMEONE_ELSE)
    fake.polls = 1                                        # « terminé », après la chute
    thread.join(5)
    assert not thread.is_alive() and "value" not in outcome
    assert isinstance(outcome.get("error"), generate.ImageError)


@pytest.mark.parametrize("moment", ["relecture coupée", "autre réponse"])
def test_an_image_stopped_with_aiodos_is_handed_back_not_failed(sd_server, monkeypatch,
                                                                moment):
    """AIODOS qui s'arrête lève l'arrêt puis arrête sd-server, parfois pendant une relecture
    de l'état : l'unité est rendue (Interrupted), pas comptée en échec, et ce qui répond
    alors sur le port n'est pas pris."""
    fake, process, _listening = sd_server(polls=None if moment == "relecture coupée" else 1)
    stop = threading.Event()
    real_get = requests.get

    def get(url, **kwargs):
        answer = real_get(url, **kwargs)
        stop.set()
        process.alive = False
        if moment == "relecture coupée":
            raise requests.ConnectionError("connexion refusée")
        return answer
    monkeypatch.setattr(generate.requests, "get", get)
    with pytest.raises(Interrupted):
        generate.image(_payload(), stop=stop)
