"""Lire pour un contrôle (docs/conception/reseau-public-calcul.md § 8) : la lecture imposée
par le moteur intégré (llm/reading.py), demandée à une de mes machines (action « read »,
network/lend_reading.py, remote_models.read) ou à une machine du réseau public (POST
/public/v1/read, public_chat.read), et branchée dans les contrôles (public_controls.read,
spot_check : A et B lus sous le même modèle).

Le llama-server est simulé par un vrai serveur HTTP local qui tient le contrat mesuré le
28/10/2026 (grammaire qui impose le texte, logprobs, fin de texte en dernière entrée de
jeton vide, contenu rendu sans ses blancs de tête) ; ses log-probabilités sont tirées du texte
(un mot « PUB » est très improbable). Les lectures d'un autre modèle sont celles mesurées en
vrai sur qwen3.5:9b le 28/10/2026 (question 4 du banc, tools/bench_controle_results.json)."""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aiodos import settings
from aiodos.llm import reading
from aiodos.network import (lend_queue, lend_reading, p2p_app, public_chat, public_controls,
                            public_lending, remote_models, reputation, sharing, spot_check)
from aiodos.network.client import PeerError
from aiodos.services import ollama
from tests.test_collusion import seen as seen_since
from tests.test_public_chat import companion  # noqa: F401 (fixture)
from tests.test_public_lending import HEADERS, MODEL, SECRET, FakeManager, lend
from tests.test_public_lending import engine, node  # noqa: F401 (fixtures)

LENDER = "a" * 52
CONTROL = "c" * 52
ROOT = Path(__file__).resolve().parent.parent
BENCH = json.loads((ROOT / "tools" / "bench_controle_results.json").read_text(
    encoding="utf-8"))[-1]
BENCH_Q = 3                        # « Explique comment fonctionne la photosynthèse. »
# Lues en vrai sous qwen3.5:9b (llama-server b11707, 28/10/2026) : B (même modèle, autre
# carte) et A répondue par qwen2.5:7b à la place du modèle demandé
READ_B = {"n": 402, "mean": -0.2404, "window": -1.0092, "eos": -24.9073}
READ_OTHER_MODEL = {"n": 376, "mean": -1.514, "window": -3.764, "eos": -12.752}
READ_HONEST_A = {"n": 401, "mean": -0.214, "window": -0.931, "eos": -24.975}
# Le banc bornait les réponses à 400 jetons : celles-ci ont été coupées par la limite
LIMIT = 400


@pytest.fixture(autouse=True)
def nothing_pending():
    spot_check.forget()
    yield
    spot_check.forget()


# ------------------------------------------------------- le llama-server simulé
def _log_prob(piece: str) -> float:
    return -9.0 if "PUB" in piece else -0.25


class Llama:
    """Le contrat de llama-server pour une lecture imposée : le texte tiré de la grammaire,
    découpé en jetons de 4 caractères, chacun avec sa log-probabilité, puis la fin de texte
    (-0.1) ; au plus max_tokens entrées (finish « length » : pas de fin de texte). `content` :
    un autre texte rendu ; `slow` : secondes avant de répondre. Les demandes reçues."""

    def __init__(self):
        self.requests, self.content, self.slow = [], None, 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        llama = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                llama.requests.append({"path": self.path, "body": body,
                                       "auth": self.headers.get("Authorization")})
                if llama.slow:
                    time.sleep(llama.slow)
                grammar = body["grammar"]
                assert grammar.startswith("root ::= ")
                text = json.loads(grammar[len("root ::= "):])   # sans \xNN dans ces essais
                pieces = [text[i:i + 4] for i in range(0, len(text), 4)]
                entries = [{"token": p, "logprob": _log_prob(p)} for p in pieces]
                entries.append({"token": "", "logprob": -0.1})
                finish = "stop"
                if len(entries) > body["max_tokens"]:
                    entries, finish = entries[:body["max_tokens"]], "length"
                read = "".join(e["token"] for e in entries)
                content = llama.content if llama.content is not None else read.lstrip()
                data = json.dumps({"choices": [{
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish, "logprobs": {"content": entries}}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:                       # le demandeur a coupé : c'est permis
                    pass
        return Handler


@pytest.fixture
def llama():
    fake = Llama()
    yield fake
    fake.close()


MESSAGES = [{"role": "system", "content": "Sois bref."},
            {"role": "user", "content": "Pourquoi le ciel est-il bleu ?"}]


# ------------------------------------------------------------------- la lecture
def test_a_reading_is_forced_by_a_grammar_and_summarized(llama):
    text = "Le ciel est bleu : diffusion de Rayleigh. " * 3 + "PUB Fizzola, la boisson !" * 2
    found = reading.read_at(llama.url, {"Authorization": "Bearer clé"}, "qwen3.5-9b", MESSAGES,
                            [text, "  Bonjour.", "a\x00b", "", 7])
    assert found[2:] == [None, None, None]             # illisibles : rien n'est parti
    assert len(llama.requests) == 2
    sent = llama.requests[0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == "Bearer clé"
    body = sent["body"]
    assert body["model"] == "qwen3.5-9b" and body["messages"] == MESSAGES
    assert body["temperature"] == 0.0 and body["logprobs"] is True and not body["stream"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == len(text.encode()) + 1
    # Le résumé : des nombres seulement, la pire fenêtre sur la publicité
    lp = [_log_prob(text[i:i + 4]) for i in range(0, len(text), 4)]
    worst = min(sum(lp[i:i + 16]) / 16 for i in range(len(lp) - 15))
    assert found[0] == {"n": len(lp), "mean": round(sum(lp) / len(lp), 4),
                        "window": round(worst, 4), "eos": -0.1}
    # Le blanc de tête lu, même si llama-server ne le rend pas dans le contenu
    assert found[1]["n"] == 3 and found[1]["eos"] == -0.1
    assert reading.literal('a"b\\c\nd\te\x01') == '"a\\"b\\\\c\\nd\\te\\x01"'


def test_a_long_text_is_read_in_part_and_another_text_is_refused(llama, monkeypatch):
    monkeypatch.setattr(reading, "MAX_TOKENS", 5)
    (partial,) = reading.read_at(llama.url, {}, "m", MESSAGES, ["x" * 40])
    assert partial == {"n": 5, "mean": -0.25, "window": -0.25, "eos": None}
    assert reading.clean(partial) == partial            # dans les bornes du demandeur
    llama.content = "Un tout autre texte."
    with pytest.raises(reading.ReadingError):
        reading.read_at(llama.url, {}, "m", MESSAGES, ["Bonjour."])


def test_a_stop_or_the_deadline_closes_the_connection(llama):
    llama.slow = 3.0
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    began = time.monotonic()
    with pytest.raises(reading.ReadingError):
        reading.read_at(llama.url, {}, "m", MESSAGES, ["Bonjour."], stop=stop)
    with pytest.raises(reading.ReadingError):
        reading.read_at(llama.url, {}, "m", MESSAGES, ["Bonjour."], timeout=0.3)
    assert time.monotonic() - began < 2.5


def test_only_the_integrated_engine_reads(monkeypatch):
    monkeypatch.setattr(ollama, "use_integrated", lambda: False)
    with pytest.raises(reading.Unavailable):
        reading.read(MODEL, MESSAGES, ["Bonjour."])


def test_a_summary_received_is_reduced_to_its_shape():
    good = {"n": 3, "mean": -0.5, "window": -1.0, "eos": None, "autre": "texte"}
    assert reading.clean(good) == {"n": 3, "mean": -0.5, "window": -1.0, "eos": None}
    for bad in (None, [], {"n": True, "mean": -1, "window": -1, "eos": None},
                {"n": 0, "mean": -1, "window": -1, "eos": None},
                {"n": 3, "mean": 0.5, "window": -1, "eos": None},
                {"n": 3, "mean": -11.0, "window": -1, "eos": None},
                {"n": 3, "mean": float("nan"), "window": -1, "eos": None},
                {"n": 3, "mean": -1, "window": -1, "eos": 2.0},
                {"n": 3, "mean": "-1", "window": -1, "eos": None},
                {"n": reading.MAX_TOKENS + 1, "mean": -1, "window": -1, "eos": None}):
        assert reading.clean(bad) is None, bad
    assert reading.summarize([]) is None
    assert reading.summarize([-30.0, float("inf"), -1.0])["mean"] == -5.5   # bornée à CLIP


# ------------------------------------------------- le verdict, sur des résumés
def test_the_verdict_reads_a_summary_as_it_reads_log_probabilities():
    verdict = spot_check.reading_verdict
    lp = [-0.4, -0.1, -1.2, -0.05] * 40
    whole = {"lp": lp, "eos": -0.2, "max_tokens": 400}
    summary = {**reading.summarize(lp, -0.2), "max_tokens": 400}
    assert verdict(whole).measures == verdict(summary).measures
    assert verdict({**summary, "eos": -15.0}).reason == "truncated"
    assert verdict({**READ_OTHER_MODEL, "max_tokens": LIMIT}, READ_B).reason == "unlikely"
    assert verdict({**READ_HONEST_A, "max_tokens": LIMIT}, READ_B).ok
    assert verdict({**summary, "window": -6.5}).reason == "inserted"
    assert verdict({"n": "beaucoup", "mean": -9}).ok                # hors de sa forme : ignorée
    assert verdict(summary, {"mean": "x"}).measures.get("ll_gap") is None


def _bench(label: str) -> str:
    return BENCH["answers"][label][BENCH_Q]["text"]


def _question() -> str:
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import bench_controle
    return bench_controle.QUESTIONS[BENCH_Q]


def test_reading_a_and_b_catches_another_model_the_texts_alone_miss():
    """La mesure du 28/10 : la réponse de qwen2.5:7b à la place de qwen3.5:9b passe la
    comparaison des textes ; lue avec B sous le même modèle, elle est prise. Une machine de
    contrôle qui ne sait pas lire (version d'avant : 404) : les textes seuls, comme avant."""
    question, a, b = _question(), _bench("qwen25"), _bench("autre_carte")
    assert spot_check.compare(question, a, b).ok                   # les textes seuls : rien
    readings = {a: READ_OTHER_MODEL, b: READ_B}
    request = {"model": MODEL, "messages": [{"role": "user", "content": question}],
               "options": {"seed": 7, "num_predict": LIMIT}, "conversation": "c" * 32}
    asked, read_calls = [], []

    def ask(machine, sent):
        asked.append(sent)
        return b

    def read(machine, sent, texts):
        read_calls.append((machine, sent, list(texts)))
        return [readings[text] for text in texts]

    done = []
    spot_check.run_check(LENDER, request, a, ask, [("peer:fixe:" + MODEL, None)], embed=None,
                         read=read, on_done=done.append).join(10)
    (verdict,) = done
    assert not verdict.ok and verdict.reason == "unlikely"
    assert verdict.measures["ll_gap"] == pytest.approx(-1.274, abs=1e-3)
    assert reputation.is_set_aside(LENDER)
    ((machine, sent, texts),) = read_calls
    assert machine == "peer:fixe:" + MODEL and sorted(texts) == sorted([a, b])
    assert sent is asked[0] and sent["conversation"] != request["conversation"]
    # Une honnête, lue de même : réussi
    spot_check.forget()
    readings[a] = READ_HONEST_A
    done.clear()
    spot_check.run_check(CONTROL, request, a, ask, [("peer:fixe:" + MODEL, None)], embed=None,
                         read=read, on_done=done.append).join(10)
    assert done[0].ok and done[0].measures["ll_gap"] == pytest.approx(0.026, abs=1e-3)


def test_a_control_machine_that_cannot_read_leaves_the_texts_alone():
    question, a, b = _question(), _bench("qwen25"), _bench("autre_carte")
    request = {"model": MODEL, "messages": [{"role": "user", "content": question}],
               "options": {"seed": 7, "num_predict": LIMIT}, "conversation": "c" * 32}

    def read(machine, sent, texts):
        raise public_chat.PublicError("no_model")                 # le 404 d'une version d'avant

    done = []
    spot_check.run_check(LENDER, request, a, lambda machine, sent: b,
                         [("peer:fixe:" + MODEL, None)], embed=None, read=read,
                         on_done=done.append).join(10)
    assert done[0].ok and "ll_mean" not in done[0].measures
    assert not reputation.is_set_aside(LENDER)


def test_control_machines_in_disagreement_a_third_reads_every_answer():
    """Deux machines publiques en désaccord (la première ment : A lui paraît d'un autre
    modèle) : sans troisième, sans conclusion, personne n'est accusé (contre la collusion,
    29/10/2026 : la seconde, qui tranchait seule, pouvait être un autre nœud du prêteur). Une
    troisième lit A, sa réponse et celles des deux premières ; la menteuse, seule contre deux,
    est prise, le prêteur blanchi."""
    question, a, b = _question(), _bench("autre_carte"), _bench("ref")
    liar = _bench("qwen25")
    second_key, third_key = "d" * 52, "e" * 52
    first, second, third = (f"public:{key}:{MODEL}" for key in (CONTROL, second_key, third_key))
    for key, hours in ((CONTROL, 72), (second_key, 100), (third_key, 130)):  # indépendantes
        seen_since(key, hours)
    answers = {first: liar, second: b, third: b}
    readings = {a: READ_HONEST_A, b: READ_B, liar: READ_OTHER_MODEL}
    read_by = {}

    def read(machine, sent, texts):
        read_by[machine] = list(texts)
        if machine == first:                       # elle ment : A très improbable, et la sienne
            return [READ_OTHER_MODEL if text == a else READ_B for text in texts]  # probable
        return [readings[text] for text in texts]

    request = {"model": MODEL, "messages": [{"role": "user", "content": question}],
               "options": {"num_predict": LIMIT}, "conversation": "c" * 32}

    def check(controls):
        done = []
        spot_check.run_check(LENDER, request, a, lambda machine, sent: answers[machine],
                             controls, embed=None, read=read, on_done=done.append).join(10)
        return done[0]
    assert check([(first, CONTROL), (second, second_key)]) is None
    assert not reputation.is_set_aside(CONTROL) and not reputation.is_set_aside(LENDER)
    assert sorted(read_by[second]) == sorted([a, b])      # pas encore de désaccord avant elle
    spot_check.forget()
    verdict = check([(first, CONTROL), (second, second_key), (third, third_key)])
    assert verdict.ok and not reputation.is_set_aside(LENDER)
    assert sorted(read_by[third]) == sorted([a, b, liar, b])
    assert reputation.is_set_aside(CONTROL)


# ------------------------------------------------------------ chez le prêteur public
def _read_body(texts=("Réponse A : " + SECRET, "Réponse B"), **extra) -> dict:
    return {"v": 1, "model": MODEL, "messages": [
        {"role": "system", "content": "Sois bref."}, {"role": "user", "content": SECRET}],
        "texts": list(texts) if isinstance(texts, tuple) else texts, **extra}


@pytest.fixture
def reader(monkeypatch):
    """Le moteur intégré, et sa lecture factice : les demandes reçues."""
    calls = []

    def fake(model, messages, texts, stop=None, timeout=reading.READ_S):
        calls.append({"model": model, "messages": messages, "texts": texts})
        return [{"n": 4, "mean": -0.3, "window": -0.3, "eos": -0.1}, None][:len(texts)]
    monkeypatch.setattr(ollama, "use_integrated", lambda: True)
    monkeypatch.setattr(reading, "read", fake)
    return calls


def _events(http, body, headers=HEADERS):
    with http.stream("POST", "/public/v1/read", json=body, headers=headers) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.iter_lines() if line]


def test_a_public_reading_is_admitted_like_a_conversation(node, reader, caplog):  # noqa: F811
    manager, here, lan, engine = node
    lend()
    caplog.set_level(logging.DEBUG)
    events = _events(here, _read_body())
    assert [e["type"] for e in events] == ["meta", "reading"]
    assert events[1]["readings"] == [{"n": 4, "mean": -0.3, "window": -0.3, "eos": -0.1}, None]
    (call,) = reader
    assert call["model"] == MODEL and call["texts"][0].endswith(SECRET)
    assert call["messages"][-1] == {"role": "user", "content": SECRET}
    assert engine.calls == []                                 # aucune génération
    assert SECRET not in caplog.text and "Réponse" not in caplog.text
    assert lend_queue.idle() and not public_lending.serving()
    # Sans clé jetable crue (réseau local, ou sans l'en-tête) : 404, rien de lu
    assert lan.post("/public/v1/read", json=_read_body(), headers=HEADERS).status_code == 404
    assert here.post("/public/v1/read", json=_read_body()).status_code == 404
    assert len(reader) == 1


def test_a_public_reading_is_refused_before_any_work(node, reader, monkeypatch):  # noqa: F811
    manager, here, lan, engine = node

    def refused(body):
        response = here.post("/public/v1/read", json=body, headers=HEADERS)
        return response.status_code, response.json().get("error")
    assert refused(_read_body()) == (503, "not_lending")       # prêt aux inconnus éteint
    lend()
    assert refused(_read_body(model="autre:1b")) == (404, "no_model")
    assert refused(_read_body(texts=["x " * (4 * public_lending.MAX_TOKENS)])) \
        == (413, "too_big")
    assert refused(_read_body(texts=["a", "b", "c", "d"])) == (400, "bad_request")
    assert refused(_read_body(texts="a")) == (400, "bad_request")
    assert refused(_read_body(texts=[])) == (400, "bad_request")
    assert refused({**_read_body(), "v": 2}) == (400, "bad_request")
    monkeypatch.setattr(ollama, "use_integrated", lambda: False)     # Ollama ne lit pas
    assert refused(_read_body()) == (404, "no_reading")
    assert reader == [] and lend_queue.idle()


def test_a_reading_failure_is_a_code_in_the_stream(node, reader, monkeypatch):  # noqa: F811
    manager, here, lan, engine = node
    lend()

    def broken(*args, **kw):
        raise reading.ReadingError("HTTP 500")
    monkeypatch.setattr(reading, "read", broken)
    events = _events(here, _read_body())
    assert events[-1] == {"type": "error", "error": "engine"}
    assert lend_queue.idle()


# ------------------------------------------------------------ chez mes machines
def test_the_reading_between_my_machines_is_for_my_machines_only(reader, monkeypatch):
    manager = FakeManager()
    settings.set_value("lend_models", True)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": MODEL}])
    payload = {"model": MODEL, "messages": MESSAGES, "texts": ["A", "B"]}
    assert p2p_app.STREAM_HANDLERS["read"] is lend_reading.read
    monkeypatch.setattr(sharing, "level", lambda node_id: sharing.FRIEND)
    with pytest.raises(PeerError):
        lend_reading.read(manager, "ami", payload, threading.Event())
    monkeypatch.setattr(sharing, "level", lambda node_id: sharing.MINE)
    frames = list(lend_reading.read(manager, "fixe", payload, threading.Event()))
    assert frames[-1] == {"readings": [{"n": 4, "mean": -0.3, "window": -0.3, "eos": -0.1},
                                       None]}
    assert reader[0]["texts"] == ["A", "B"] and manager.executor.interactive == 0
    assert lend_queue.idle()
    for bad in ({**payload, "texts": ["a"] * 4}, {**payload, "model": "absent:1b"},
                {**payload, "messages": []}):
        with pytest.raises(PeerError):
            lend_reading.read(manager, "fixe", bad, threading.Event())
    monkeypatch.setattr(ollama, "use_integrated", lambda: False)
    with pytest.raises(PeerError):
        lend_reading.read(manager, "fixe", payload, threading.Event())
    settings.set_value("lend_models", False)


def test_my_machine_is_asked_by_the_read_action(monkeypatch):
    from aiodos.network import client, peers
    peers.save("fixe", "PC fixe", b"k" * 32, "127.0.0.1", 1, trust="mine")
    sent = []

    def stream(my_id, peer, action, payload, stop=None, read_timeout=180, **kw):
        sent.append((action, payload))
        yield {"queued": 1}
        yield {"readings": [{"n": 2, "mean": -0.5, "window": -0.5, "eos": None, "x": "y"},
                            "pas un résumé"]}
    monkeypatch.setattr(client, "stream", stream)
    found = remote_models.read("peer:fixe:" + MODEL, MESSAGES, ["A", "B"])
    assert found == [{"n": 2, "mean": -0.5, "window": -0.5, "eos": None}, None]
    assert sent == [("read", {"model": MODEL, "messages": MESSAGES, "texts": ["A", "B"]})]
    monkeypatch.setattr(client, "stream", lambda *a, **kw: iter([{"queued": 1}]))
    with pytest.raises(PeerError):
        remote_models.read("peer:fixe:" + MODEL, MESSAGES, ["A", "B"])


# --------------------------------------------------------- chez une machine publique
def test_a_public_machine_is_asked_through_the_companion(companion):  # noqa: F811
    key = "9e" * 32
    summary = {"n": 3, "mean": -0.4, "window": -0.6, "eos": -0.2}
    companion.script = {key: ("raw", [(0, {"type": "wait", "stage": "loading"}),
                                      (0, {"type": "reading", "readings": [summary, None]})])}
    found = public_chat.read(public_chat.model_id(key, MODEL), MESSAGES, ["A", "B"],
                             conversation="t" * 32)
    assert found == [summary, None]
    (request,) = companion.requests
    assert request["tail"] == "public/v1/read" and request["tag"] == "t" * 32
    assert request["body"] == {"v": 1, "model": MODEL, "messages": MESSAGES,
                               "texts": ["A", "B"]}
    # Une version d'avant : 404 (comme une route inconnue) ; la lecture est absente
    companion.script = {key: ("refuse", 404, None)}
    with pytest.raises(public_chat.PublicError):
        public_chat.read(public_chat.model_id(key, MODEL), MESSAGES, ["A", "B"])
    # Une conversation à la place d'une lecture : illisible, rien n'est cru
    companion.script = {key: ("answer", ["Bonjour"])}
    with pytest.raises(public_chat.PublicError):
        public_chat.read(public_chat.model_id(key, MODEL), MESSAGES, ["A", "B"])
    assert not reputation.is_set_aside(key)


def test_a_paused_public_machine_is_asked_again(monkeypatch):
    calls = []

    def read(machine, messages, texts, stop=None, conversation=None):
        calls.append(conversation)
        if len(calls) == 1:
            raise public_chat.PublicError("busy")              # en pause : elle vient de répondre
        return [None, None]
    monkeypatch.setattr(public_chat, "read", read)
    monkeypatch.setattr(public_controls, "READ_RETRY_S", 0.05)
    machine = public_chat.model_id("9e" * 32, MODEL)
    request = {"messages": MESSAGES, "conversation": "k" * 32}
    assert public_controls.read(machine, request, ["A", "B"]) == [None, None]
    assert calls == ["k" * 32, "k" * 32]
    monkeypatch.setattr(public_chat, "read", lambda *a, **kw: (_ for _ in ()).throw(
        public_chat.PublicError("no_model")))
    with pytest.raises(public_chat.PublicError):
        public_controls.read(machine, request, ["A", "B"])
