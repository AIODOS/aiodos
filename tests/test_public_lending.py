"""Le côté prêteur du réseau public qui calcule (network/public_lending.py) : des inconnus
font calculer leurs conversations par cette machine, relayées par le compagnon sur
/public/v1/chat avec leur clé jetable (X-Aiodos-Public).

L'en-tête n'est cru que de cette machine ; une demande publique n'atteint jamais /p2p/… ;
chaque refus (not_lending, busy, no_model, too_big) tombe avant tout calcul ; un téléphone
ne prête qu'en charge et en Wi-Fi ; la réponse part en NDJSON ; rien n'est gardé, ni en base
ni au journal ; le profil public dit lends, free et tps ; une conversation de l'utilisateur
(ou de mes machines, de mes amis) arrête le prêt. Sans réseau ni moteur (moteur factice)."""
import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aiodos import __version__, db, settings
from aiodos.gateway import limits
from aiodos.network import body_limit, connections, lend_queue, p2p_app, public_lending
from aiodos.network import public as public_mod
from aiodos.network.envelope import ReplayGuard
from aiodos.node import android, power, speed
from aiodos.services import ollama

KEY = "ab" * 32                                # clé jetable de l'appelant (64 signes hexa)
HEADERS = {"X-Aiodos-Public": KEY}
HERE = ("127.0.0.1", 5000)                     # le compagnon : depuis cette machine
SECRET = "Mon voisin s'appelle Hippolyte Zébulon"
MODEL = "qwen3.5:9b"


def ask(content=SECRET, **extra) -> dict:
    return {"v": 1, "model": MODEL, "messages": [
        {"role": "system", "content": "Sois bref."}, {"role": "user", "content": content}],
        "options": {"temperature": 0.2, "max_tokens": 100000, "seed": 7}, **extra}


class FakeExecutor:
    def __init__(self):
        self.interactive = 0
        self.busy = {"gpu": 0, "embed": 0, "cpu": 0, "net": 0}

    def interactive_begin(self):
        self.interactive += 1

    def interactive_end(self):
        self.interactive -= 1


class FakeManager:
    node_id = "moi"
    p2p_port = 1
    worker = None

    def __init__(self):
        self.guard = ReplayGuard()
        self.executor = FakeExecutor()

    def name(self):
        return "PC fixe"

    def changed(self):
        pass


class Engine:
    """Le moteur factice : ce qu'il a reçu, et une réponse (lente si `slow`, après `load`
    secondes de chargement du modèle). `during` : {rang d'un mot : ce qui arrive juste après
    lui}, pendant la réponse (TestClient ne rend la réponse qu'une fois finie : c'est d'ici
    qu'on agit en cours de route)."""

    def __init__(self, words=("Bon", "jour", " !"), slow=0.0, load=0.0):
        self.calls = []
        self.words, self.slow, self.load = words, slow, load
        self.during = {}

    def __call__(self, model, messages, think=None, stop=None, options=None, stats=False):
        self.calls.append({"model": model, "messages": messages, "think": think,
                           "options": options, "stats": stats})
        if self.load:
            time.sleep(self.load)
        for rank, word in enumerate(self.words):
            if stop is not None and stop.is_set():
                return
            yield "content", word
            if rank in self.during:
                self.during[rank]()
            if self.slow:
                time.sleep(self.slow)
        if stats:
            yield "stats", {"prompt_tokens": 12, "completion_tokens": len(self.words),
                            "done_reason": "stop"}


@pytest.fixture
def engine(monkeypatch):
    found = Engine()
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": MODEL}, {"name": "embeddinggemma:latest"}])
    monkeypatch.setattr(ollama, "capabilities", lambda model: (
        ["embedding"] if "embed" in model else ["completion", "thinking"]))
    monkeypatch.setattr(ollama, "chat_stream", found)
    return found


@pytest.fixture
def node(engine):
    manager = FakeManager()
    app = p2p_app.create_p2p_app(manager)
    yield manager, TestClient(app, client=HERE), TestClient(app), engine
    public_lending.forget()
    assert lend_queue.idle()                          # aucune place oubliée


def lend(power_share=100):
    """Prêt aux inconnus permis (éteint dans les essais, AIODOS_QUIET), partage actif ;
    puissance prêtée à 100 % : pas de pause entre deux conversations, sauf si demandée."""
    settings.set_many({"lend_public": True, "share_enabled": True, "lend_power": power_share})
    from aiodos.node import places
    places.forget()


def post(http, body=None, headers=HEADERS, **kw):
    return http.post("/public/v1/chat", json=ask() if body is None else body,
                     headers=headers, **kw)


def events(http, body=None, headers=HEADERS):
    with http.stream("POST", "/public/v1/chat", json=ask() if body is None else body,
                     headers=headers) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.iter_lines() if line]


# ------------------------------------------------------- l'en-tête, et lui seul
def test_the_public_key_is_believed_only_from_this_machine():
    def scope(host, key=KEY, server="0.0.0.0"):
        return {"client": (host, 5000), "server": (server, 47701),
                "headers": [(b"x-aiodos-public", key.encode())]}
    assert connections.public_key(scope("127.0.0.1")) == KEY
    assert connections.public_key(scope("::1", key=KEY.upper())) == KEY
    assert connections.public_key(scope("192.168.1.20", server="192.168.1.20")) == KEY
    for host in ("192.168.1.66", "10.0.0.2", "testclient", None):
        assert connections.public_key(scope(host)) == ""
    for key in ("", "pas une clé", "zz" * 32, "ab" * 100, "ab"):
        assert connections.public_key(scope("127.0.0.1", key=key)) == ""
    # L'autre en-tête du compagnon ne vaut pas celui-ci
    assert connections.public_key({"client": ("127.0.0.1", 1), "server": ("0.0.0.0", 1),
                                   "headers": [(b"x-aiodos-peer", KEY.encode())]}) == ""


def test_without_a_believable_key_every_public_route_is_unknown(node):
    """Du réseau local, même avec l'en-tête ; de cette machine, sans lui, ou avec seulement
    X-Aiodos-Peer (la clé d'une machine appairée, jointe par Internet) : 404, comme une route
    qui n'existe pas, et rien n'est calculé."""
    manager, here, lan, engine = node
    lend()
    assert post(lan).status_code == 404                       # en-tête venu d'ailleurs
    assert post(lan, headers={**HEADERS, "X-Aiodos-Peer": KEY}).status_code == 404
    assert post(here, headers={}).status_code == 404
    assert post(here, headers={"X-Aiodos-Peer": KEY}).status_code == 404
    assert post(here, headers={"X-Aiodos-Public": "pas-une-cle"}).status_code == 404
    response = here.get("/public/v1/chat")
    assert response.status_code == 404 and response.json() == {"detail": "Not Found"}
    assert lan.post("/public/v1/autre", json={}, headers=HEADERS).status_code == 404
    assert engine.calls == []
    # Avec la clé jetable, un X-Aiodos-Peer en plus ne change rien : un inconnu, servi comme tel
    assert post(here, headers={**HEADERS, "X-Aiodos-Peer": "cd" * 32}).status_code == 200
    assert len(engine.calls) == 1


def test_a_public_request_never_reaches_the_routes_between_my_machines(node):
    """Avec la clé jetable d'un inconnu, aucune route /p2p/… (pas même « hello », ouverte à
    tous) : une demande publique ne passe jamais pour celle d'une machine appairée."""
    manager, here, lan, engine = node
    assert here.post("/p2p/v1/hello").status_code == 200
    for path in ("/p2p/v1/hello", "/p2p/v1/ping", "/p2p/v1/stream/chat", "/p2p/v1/pair/start"):
        response = here.post(path, json={}, headers=HEADERS)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}
    # X-Aiodos-Peer en plus n'y change rien
    assert here.post("/p2p/v1/hello", headers={**HEADERS, "X-Aiodos-Peer": KEY}).status_code == 404


def test_a_public_request_is_bounded_before_being_read(node):
    """256 Kio au plus (comme le compagnon) ; au-delà, un code, pas une phrase."""
    manager, here, lan, engine = node
    lend()
    big = ask(content="a" * (body_limit.PUBLIC_BODY + 10))
    response = post(here, big)
    assert response.status_code == 413 and response.json() == {"error": "too_big"}
    assert post(lan, big).status_code == 404                  # sans la clé : rien n'est lu
    assert engine.calls == []


# ---------------------------------------------------- refus, avant tout calcul
def test_not_lending_until_allowed_and_sharing(node, monkeypatch):
    manager, here, lan, engine = node
    assert settings.get("lend_public") is False                # éteint dans les essais
    assert post(here).status_code == 503 and post(here).json() == {"error": "not_lending"}
    settings.set_value("lend_public", True)                    # permis, partage coupé
    assert post(here).json() == {"error": "not_lending"}
    lend()
    assert post(here).status_code == 200
    settings.set_value("lend_public", False)                   # coupé ensuite
    assert post(here).json() == {"error": "not_lending"}
    assert len(engine.calls) == 1


def test_never_on_battery(node, monkeypatch):
    """Même quand le partage y est permis (« seulement sur secteur » décoché) : jamais sur
    batterie pour un inconnu."""
    manager, here, lan, engine = node
    lend()
    settings.set_value("share_on_ac_only", False)
    monkeypatch.setenv(power.SIMULATE_ENV, "battery")
    response = post(here)
    assert response.status_code == 503 and response.json() == {"error": "not_lending"}
    assert engine.calls == []


def test_a_phone_lends_to_strangers_only_charging_and_on_wifi(node, monkeypatch):
    manager, here, lan, engine = node
    lend()
    supply = {"plugged": True, "level": 100, "temperature": 30.0, "mobile": True,
              "source": "android"}
    wifi = {"on": False}
    monkeypatch.setattr(android, "is_android", lambda: True)
    monkeypatch.setattr(android, "on_wifi", lambda: wifi["on"])
    monkeypatch.setattr(power, "read", lambda: dict(supply))
    assert post(here).json() == {"error": "not_lending"}       # en charge, données mobiles
    wifi["on"] = True
    assert events(here)[-1]["type"] == "done"                  # en charge et en Wi-Fi
    supply["plugged"] = False
    assert post(here).json() == {"error": "not_lending"}       # débranché
    supply["plugged"] = None
    assert post(here).json() == {"error": "not_lending"}       # alimentation inconnue
    assert len(engine.calls) == 1


def test_busy_while_someone_else_wants_this_machine(node):
    """L'utilisateur (une conversation ici), mes machines ou mes amis (une conversation
    servie ou en attente), le travail de fond sur la carte graphique : occupée."""
    manager, here, lan, engine = node
    lend()
    manager.executor.interactive = 1
    response = post(here)
    assert response.status_code == 503 and response.json() == {"error": "busy"}
    manager.executor.interactive = 0
    for resource in ("gpu", "embed"):
        manager.executor.busy[resource] = 1
        assert post(here).json() == {"error": "busy"}
        manager.executor.busy[resource] = 0
    with lend_queue.turn("une-de-mes-machines"):               # conversation prêtée en cours
        assert post(here).json() == {"error": "busy"}
    assert engine.calls == []
    assert post(here).status_code == 200


def test_one_public_conversation_at_a_time(node):
    manager, here, lan, engine = node
    lend()
    engine.words = [f"mot{i} " for i in range(5)]
    seen = {}
    # Pendant la première réponse, une seconde demande (une autre connexion du compagnon)
    engine.during[1] = lambda: seen.update(second=post(here), free=public_lending.free(manager))
    found = events(here)
    assert seen["second"].status_code == 503 and seen["second"].json() == {"error": "busy"}
    assert seen["free"] is False
    assert found[-1]["type"] == "done" and len(engine.calls) == 1


def test_the_power_lent_bounds_strangers_too(node):
    """Puissance prêtée à 60 % : après une conversation publique de t s, pas d'autre avant
    t × 40 / 60 s (comme pour une unité prêtée)."""
    manager, here, lan, engine = node
    lend(power_share=60)
    engine.slow = 0.3
    assert events(here)[-1]["type"] == "done"
    assert post(here).json() == {"error": "busy"}
    assert public_lending.free(manager) is False
    public_lending.forget()
    assert public_lending.free(manager) is True


def test_the_pause_counts_only_the_generation(node, monkeypatch):
    """Essai réel du 28/10 : la pause comptait le chargement du modèle (48 s sur 52,5 s). Elle
    ne compte plus que la génération, du premier mot au dernier."""
    manager, here, lan, engine = node
    lend(power_share=60)
    counted = []
    monkeypatch.setattr(public_lending, "_rest_after", lambda m, seconds: counted.append(seconds))
    engine.load, engine.slow = 1.5, 0.1            # 1,5 s de chargement, 0,3 s de génération
    assert events(here)[-1]["type"] == "done"
    assert len(counted) == 1 and 0.25 <= counted[0] < 1.2
    engine.load, engine.words = 0.3, []            # rien de généré : pas de pause
    events(here)
    assert counted[1] == 0.0


# ------------------------------------------- avant le premier mot : des signes de vie
def test_signs_of_life_while_the_model_loads(node, monkeypatch):
    """Tant que rien n'est parti, un signe de vie toutes les WAIT_S s : « loading » tant que
    le modèle n'est pas chargé, « reading » ensuite ; plus rien après le premier morceau."""
    manager, here, lan, engine = node
    lend()
    monkeypatch.setattr(public_lending, "WAIT_S", 0.2)
    loaded = []
    monkeypatch.setattr(ollama, "loaded_models", lambda: [{"name": m} for m in loaded])
    engine.load, engine.slow = 0.9, 0.5            # puis 0,5 s entre deux mots
    found = events(here)
    kinds = [e["type"] for e in found]
    first = kinds.index("delta")
    assert kinds[0] == "meta" and kinds[-1] == "done" and first >= 3
    assert all(e == {"type": "wait", "stage": "loading"} for e in found[1:first])
    assert "wait" not in kinds[first:]
    loaded.append(MODEL)                           # déjà chargé : il lit la demande
    engine.slow = 0.0
    found = events(here)
    assert {e["stage"] for e in found if e["type"] == "wait"} == {"reading"}
    engine.load = 0.0                              # le premier mot tout de suite : aucun
    assert all(e["type"] != "wait" for e in events(here))


def test_one_reading_of_the_engine_at_a_time(node, monkeypatch):
    """Le moteur intégré peut tenir son verrou tout un chargement : les lectures de l'étape
    ne s'empilent pas (une à la fois), et les signes de vie partent quand même."""
    manager, here, lan, engine = node
    lend()
    monkeypatch.setattr(public_lending, "WAIT_S", 0.1)
    monkeypatch.setattr(public_lending, "STAGE_TIMEOUT", 0.05)
    running, most, guard = [0], [0], threading.Lock()

    def slow():
        with guard:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.5)
        with guard:
            running[0] -= 1
        return []
    monkeypatch.setattr(ollama, "loaded_models", slow)
    engine.load = 1.2
    waits = [e for e in events(here) if e["type"] == "wait"]
    assert len(waits) >= 5 and {w["stage"] for w in waits} == {"loading"}
    assert most[0] == 1


class Pushes:
    """Ce que public_lending fait redire au compagnon (Public.push) : « free » à chaque fois."""

    def __init__(self, manager):
        self.manager, self.seen = manager, []

    def push(self):
        self.seen.append(public_lending.profile_fields(self.manager)["free"])
        return True


def test_the_profile_is_pushed_at_the_start_and_the_end(node):
    """Essai réel du 28/10 : « free » se voyait toutes les 15 s seulement. Il est redit au
    début (occupée) et à la fin (libre, ou en pause : alors encore à la fin de la pause)."""
    manager, here, lan, engine = node
    lend()
    manager.public = Pushes(manager)
    assert events(here)[-1]["type"] == "done"
    assert manager.public.seen == [0, 1]
    lend(power_share=60)
    manager.public.seen.clear()
    engine.slow = 0.2
    events(here)
    deadline = time.monotonic() + 5
    while len(manager.public.seen) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert manager.public.seen == [0, 0, 1]        # occupée, en pause, libre


def test_public_push_says_the_profile_at_once(node):
    """Public.push : la synchronisation planifiée sur la boucle du gestionnaire, sans attendre
    la suivante ; rien sans boucle."""
    import asyncio
    manager, here, lan, engine = node

    class Internet:
        companion = Companion()
        enabled = True
        launches = 1
    manager.internet = Internet()
    settings.set_value("public_network", True)
    lend()
    pub = public_mod.Public(manager)
    assert pub.push() is False                     # pas de boucle : rien
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    manager._loop = loop
    calls = manager.internet.companion.calls

    def put_free():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            puts = [body for method, _path, body in calls if method == "PUT"]
            if puts and (not seen or len(puts) > len(seen)):
                return puts[-1]["free"]
            time.sleep(0.02)
        raise AssertionError("aucun profil redit")
    try:
        seen = []
        assert pub.push()
        seen.append(put_free())
        manager.executor.interactive = 1           # l'utilisateur converse ici : occupée
        assert pub.push()
        seen.append(put_free())
        assert seen == [1, 0]
    finally:
        manager.executor.interactive = 0
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()


def test_at_most_so_many_public_requests_per_minute(node, monkeypatch):
    manager, here, lan, engine = node
    lend()
    monkeypatch.setattr(limits, "PER_MINUTE", 2)
    assert post(here).status_code == 200
    assert post(here).status_code == 200
    assert post(here).json() == {"error": "busy"}
    assert len(engine.calls) == 2


def test_only_a_conversation_model_installed_here_under_its_exact_name(node):
    manager, here, lan, engine = node
    lend()
    for model in ("llama-inconnu", "qwen3.5", "QWEN3.5:9B", "embeddinggemma:latest"):
        response = post(here, dict(ask(), model=model))
        assert response.status_code == 404 and response.json() == {"error": "no_model"}, model
    assert engine.calls == [] and lend_queue.idle()


def test_text_only_and_a_bounded_context(node):
    manager, here, lan, engine = node
    lend()
    image = {"role": "user", "content": [{"type": "text", "text": "Quoi ?"},
                                         {"type": "image_url", "image_url": {"url": "data:"}}]}
    attached = {"role": "user", "content": "Regarde", "images": ["aGVsbG8="]}
    digits = "1234567890" * 900                     # un jeton par chiffre : 9 000 jetons
    too_many = [{"role": "user", "content": "a"}] * (public_lending.MAX_MESSAGES + 1)
    for messages in ([image], [attached], [{"role": "user", "content": digits}], too_many):
        response = post(here, dict(ask(), messages=messages))
        assert response.status_code == 413 and response.json() == {"error": "too_big"}
    assert engine.calls == [] and lend_queue.idle()
    # 20 000 lettres et espaces : moins de 8 192 jetons estimés, la demande passe
    assert post(here, ask(content="mot " * 5000)).status_code == 200


def test_a_malformed_request_is_refused_plainly(node):
    manager, here, lan, engine = node
    lend()
    for body in (dict(ask(), v=2), dict(ask(), messages=[]), dict(ask(), messages="Salut"),
                 dict(ask(), messages=[{"role": "tool", "content": "x"}]),
                 dict(ask(), messages=[{"role": ["user"], "content": "x"}]),
                 dict(ask(), model=None), dict(ask(), options="chaud"),
                 dict(ask(), options={"temperature": "chaud"}),
                 dict(ask(), options={"seed": 2 ** 40}), dict(ask(), options={"seed": True})):
        response = post(here, body)
        assert response.status_code == 400 and response.json() == {"error": "bad_request"}, body
    response = here.post("/public/v1/chat", content=b"[[[[", headers=HEADERS)
    assert response.json() == {"error": "bad_request"}
    assert engine.calls == [] and lend_queue.idle()


def test_tokens_are_estimated_prudently():
    estimate = public_lending.estimate_tokens
    assert estimate("") == 0
    assert estimate("abc") == 1 and estimate("abcd") == 2
    assert estimate("2026") == 4                                # un jeton par chiffre
    assert estimate("été") == 3                                 # « é » : un jeton chacun
    assert estimate("The quick brown fox") >= 19 / 4            # plus qu'un vrai décompte


# ---------------------------------------------------------- la réponse en flux
def test_a_conversation_is_answered_in_ndjson(node):
    manager, here, lan, engine = node
    lend()
    found = events(here)
    assert found[0] == {"type": "meta", "model": MODEL, "version": __version__}
    assert found[1:4] == [{"type": "delta", "text": w} for w in ("Bon", "jour", " !")]
    assert found[4]["type"] == "done" and found[4]["tokens"] == 3
    assert isinstance(found[4]["seconds"], float) and len(found) == 5
    call = engine.calls[0]
    assert call["model"] == MODEL and call["think"] is False and call["stats"] is True
    assert call["messages"] == ask()["messages"]
    # max_tokens ramené à 2 048 ; température et graine passées telles quelles
    assert call["options"] == {"num_predict": 2048, "temperature": 0.2, "seed": 7}
    # Mon travail de fond n'est pas suspendu pour un inconnu : c'est lui qui passe devant
    assert manager.executor.interactive == 0 and lend_queue.idle()


def test_an_engine_failure_is_a_code_in_the_stream(node, monkeypatch):
    manager, here, lan, engine = node
    lend()

    def failing(model, messages, **kw):
        yield "content", "Bon"
        raise RuntimeError(f"le moteur a lu « {SECRET} » puis s'est arrêté")
    monkeypatch.setattr(ollama, "chat_stream", failing)
    found = events(here)
    assert found[-2:] == [{"type": "delta", "text": "Bon"}, {"type": "error", "error": "engine"}]
    assert lend_queue.idle()


def test_nothing_is_kept_neither_in_the_base_nor_in_the_log(node, caplog):
    manager, here, lan, engine = node
    lend()
    with caplog.at_level(logging.DEBUG):
        assert events(here)[-1]["type"] == "done"
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"Conversation pour le réseau public : {MODEL}, 3 jetons, ")
               and m.endswith(" s") for m in said)
    for message in said:
        assert SECRET not in message and "Sois bref" not in message and "Bonjour" not in message
        assert KEY not in message
    for row in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        for stored in db.query(f'SELECT * FROM "{row["name"]}"'):
            text = repr(tuple(stored))
            assert "Zébulon" not in text and KEY not in text and "Bonjour" not in text, row["name"]


# ----------------------------------------- l'utilisateur et les miens d'abord
def _long(engine):
    engine.slow = 0.02
    engine.words = [f"mot{i} " for i in range(300)]       # 6 s si rien ne l'arrête


def test_a_conversation_of_the_user_stops_the_public_loan(node):
    manager, here, lan, engine = node
    lend()
    _long(engine)
    engine.during[2] = manager.executor.interactive_begin    # l'utilisateur converse ici
    found = events(here)
    assert found[-1] == {"type": "error", "error": "busy"}
    deltas = [e for e in found if e["type"] == "delta"]
    assert 3 <= len(deltas) < 100
    assert lend_queue.idle()
    manager.executor.interactive_end()


def test_a_conversation_of_my_machines_or_friends_stops_the_public_loan(node):
    manager, here, lan, engine = node
    lend()
    _long(engine)
    served = threading.Event()

    def friend():
        with lend_queue.turn("un-ami", max_wait=10):          # elle attend son tour…
            served.set()
    waiting = threading.Thread(target=friend)
    engine.during[2] = waiting.start
    found = events(here)
    waiting.join(timeout=10)
    assert found[-1] == {"type": "error", "error": "busy"}
    assert len(found) < 100 and served.is_set()               # … et l'a eu
    assert lend_queue.idle()


def test_my_background_work_on_the_graphics_card_stops_the_public_loan(node):
    manager, here, lan, engine = node
    lend()
    _long(engine)
    engine.during[2] = lambda: manager.executor.busy.update(gpu=1)
    assert events(here)[-1] == {"type": "error", "error": "busy"}


def test_unplugged_during_the_answer_the_loan_stops(node, monkeypatch):
    manager, here, lan, engine = node
    lend()
    monkeypatch.setattr(public_lending, "CHECK_S", 0.1)
    _long(engine)
    engine.during[2] = lambda: settings.set_value("lend_public", False)
    found = events(here)
    assert found[-1] == {"type": "error", "error": "not_lending"} and len(found) < 100


# ------------------------------------------------------------ le profil public
def test_the_public_profile_says_lends_free_and_speed(node):
    manager, here, lan, engine = node
    fields = public_lending.profile_fields
    assert fields(manager) == {"lends": False, "free": 0}     # éteint dans les essais
    lend()
    assert fields(manager) == {"lends": True, "free": 1}
    manager.executor.interactive = 1
    assert fields(manager) == {"lends": True, "free": 0}
    manager.executor.interactive = 0
    speed.note(100, 4.0)
    speed.note(50, 1.0)
    speed.note(20, 2.0)
    assert fields(manager) == {"lends": True, "free": 1, "tps": 25.0}     # la médiane
    settings.set_value("share_enabled", False)              # partage coupé : ne prête pas
    assert fields(manager) == {"lends": False, "free": 0, "tps": 25.0}


class Companion:
    running = True
    generation = 1

    def __init__(self):
        self.calls = []

    def api(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        return {"enabled": True, "key": "ef" * 32, "neighbors": 1, "error": None, "nodes": [
            {"key": "cd" * 32, "version": "1.2.0", "models": [MODEL], "lends": True,
             "seen_s": 4, "free": 1, "tps": 42.5},
            {"key": "12" * 32, "version": 3, "models": "tous", "lends": "oui",
             "seen_s": float("nan"), "free": "oui", "tps": float("inf")},
            {"key": "34" * 32, "lends": True, "free": 999, "tps": -3},
            "pas un nœud", {"key": 5}, {"models": []}]}


def test_the_profile_goes_to_the_companion_and_seen_nodes_keep_free_and_speed(node):
    import asyncio
    manager, here, lan, engine = node

    class Internet:
        companion = Companion()
        enabled = True
        launches = 1
    manager.internet = Internet()
    settings.set_value("public_network", True)
    lend()
    speed.note(40, 2.0)
    pub = public_mod.Public(manager)
    asyncio.run(pub.sync())
    sent = manager.internet.companion.calls[-1][2]
    assert {k: sent[k] for k in ("lends", "free", "tps")} == {"lends": True, "free": 1,
                                                               "tps": 20.0}
    nodes = pub.snapshot()["nodes"]
    assert nodes == [
        {"key": "cd" * 32, "version": "1.2.0", "models": [MODEL], "lends": True, "seen_s": 4,
         "free": 1, "tps": 42.5},
        {"key": "12" * 32, "version": "", "models": [], "lends": False, "seen_s": 0},
        {"key": "34" * 32, "version": "", "models": [], "lends": True, "seen_s": 0,
         "free": 255}]


def test_the_speed_of_every_answer_here_is_measured(monkeypatch):
    def answer(n):
        def chunks(*args):
            for i in range(n):
                time.sleep(0.001)
                yield "content", f"m{i}"
        return chunks
    monkeypatch.setattr(ollama, "_chat_stream", answer(5))
    assert len(list(ollama.chat_stream(MODEL, []))) == 5
    assert speed.median() is None                            # trop courte pour dire
    monkeypatch.setattr(ollama, "_chat_stream", answer(40))
    assert len(list(ollama.chat_stream(MODEL, []))) == 40
    assert speed.median() > 0
