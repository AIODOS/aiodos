# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Le transport : hôte non local refusé avant tout envoi, aucune redirection suivie, aucun
proxy, lecture SSE, délais d'inactivité et de chargement, annulation réelle d'un flux."""

import socket
import threading
import time

import pytest

from aiodos_client import Aiodos, Keystore, _transport, errors
from aiodos_client import client as client_module
from aiodos_client.fake import FakeAiodos, text_stream
from support import APP, closed_port

HELLO = [{"role": "user", "content": "Bonjour"}]


class _Listener:
    """Un port qui écoute et compte les connexions reçues (faux proxy, fausse cible)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(1)
        self.sock.close()


@pytest.fixture
def listener():
    server = _Listener()
    yield server
    server.close()


@pytest.mark.parametrize("url", [
    "http://192.168.1.10:11435", "http://exemple.fr:11435", "https://127.0.0.1:11435",
    "http://127.0.0.2:11435", "http://0.0.0.0:11435", "http://[::ffff:127.0.0.1]:11435",
    "http://moi:secret@127.0.0.1:11435", "http://evil.com@127.0.0.1:11435",
    "http://127.0.0.1@evil.com:11435", "http://localhost.:11435", "127.0.0.1:11435",
    "http://127.0.0.1:99999", "ftp://localhost/",
])
def test_non_local_addresses_are_refused_before_sending(url, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("aucune connexion ne doit partir")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(errors.NonLocalUrl) as caught:
        Aiodos(**APP, url=url)
    assert caught.value.code == "non_local_url"
    assert "secret" not in caught.value.message
    monkeypatch.setenv("AIODOS_URL", url)
    with pytest.raises(errors.NonLocalUrl):
        Aiodos(**APP)


@pytest.mark.parametrize("url, expected", [
    ("http://127.0.0.1:11435", "http://127.0.0.1:11435"),
    ("http://localhost:2", "http://localhost:2"),
    ("HTTP://LOCALHOST:3/v1", "http://localhost:3"),
    ("http://[::1]:4/", "http://[::1]:4"),
])
def test_local_addresses(url, expected):
    assert _transport.Endpoint(url).url == expected


def test_localhost_works(ready, fake, make_client):
    _, key = ready
    client = make_client(keystore=Keystore.memory(key), url=f"http://localhost:{fake.port}")
    started = time.monotonic()
    assert client.chat(HELLO, model="qwen3.5:9b").text == "Bonjour !"
    assert time.monotonic() - started < 1.5          # sans détour par ::1 (2 s sous Windows)
    assert fake.log[-1]["headers"]["Host"] == f"localhost:{fake.port}"


def test_redirections_are_not_followed(ready, fake, listener):
    client, key = ready
    target = f"http://127.0.0.1:{listener.port}/aiodos/v1/models"
    fake.override("/aiodos/v1/models", {"status": 302, "headers": {"Location": target,
                                                                    "X-Aiodos-Api": "1"},
                                        "json": None})
    with pytest.raises(errors.ProtocolError) as caught:
        client.models()
    assert caught.value.detail == {"status": 302}
    fake.override("/aiodos/v1/info", {"status": 307, "headers": {"Location": target},
                                      "json": None})
    assert client.status().state == "not_aiodos"
    time.sleep(0.2)
    assert listener.connections == 0


def test_no_proxy_is_used(ready, fake, listener, monkeypatch):
    client, _ = ready
    proxy = f"http://127.0.0.1:{listener.port}"
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy)
    monkeypatch.setenv("NO_PROXY", "")
    assert client.status().state == "ready"
    assert client.chat(HELLO, model="qwen3.5:9b").text == "Bonjour !"
    time.sleep(0.2)
    assert listener.connections == 0


def test_unreachable_says_connection_refused(fake, make_client):
    key = fake.add_key()
    client = make_client(keystore=Keystore.memory(key), url=f"http://127.0.0.1:{closed_port()}")
    with pytest.raises(errors.Unreachable) as caught:
        client.models()
    assert "Connexion refusée" in caught.value.message and caught.value.retryable


def test_sse_details(ready, fake):
    client, _ = ready
    lines = text_stream(loading=0)
    first = lines[1]
    split = first.replace('"delta":', '"delta":\ndata: ', 1)     # un événement sur 2 lignes
    script = [": un commentaire quelconque", "event: message\r\nid: 7\r\n" + lines[0] + "\r",
              split] + lines[2:]
    fake.chat_script(script)
    result = client.chat(HELLO, model="qwen3.5:9b")
    assert result.text == "Bonjour !" and result.thinking == "L'utilisateur salue."


def test_stream_ended_without_end(ready, fake):
    client, _ = ready
    fake.chat_script(text_stream(loading=0)[:3])                # ni fin, ni [DONE]
    with pytest.raises(errors.ProtocolError) as caught:
        client.chat(HELLO, model="qwen3.5:9b")
    assert caught.value.message == "La réponse d'AIODOS s'est arrêtée avant la fin."


def test_stream_cut_on_the_way(ready, fake):
    client, _ = ready
    fake.override("/v1/chat/completions", {"status": 200, "headers": {
        "X-Aiodos-Api": "1", "Content-Type": "text/event-stream"},
        "sse": text_stream(loading=0)[:3], "cut": True})      # connexion fermée en route
    with pytest.raises(errors.Unreachable) as caught:
        client.chat(HELLO, model="qwen3.5:9b")
    assert caught.value.message.startswith("Connexion refusée ou coupée (127.0.0.1:")


def test_loading_comments_reset_the_inactivity_delay(fake, make_client):
    key = fake.add_key()
    client = make_client(keystore=Keystore.memory(key), timeout=0.6, load_timeout=0.6)
    script = []
    for second in range(1, 7):
        script += [f": aiodos loading {second}", 0.3]
    fake.chat_script(script + text_stream(loading=0))
    seen = []
    started = time.monotonic()
    result = client.chat(HELLO, model="qwen3.5:9b", on_loading=seen.append)
    assert time.monotonic() - started > 1.5                  # bien plus que le délai
    assert result.text == "Bonjour !" and seen == [1, 2, 3, 4, 5, 6]


def test_inactivity_timeout(fake, make_client):
    key = fake.add_key()
    client = make_client(keystore=Keystore.memory(key), timeout=0.4, load_timeout=0.4)
    lines = text_stream(loading=0)
    fake.chat_script(lines[:2] + [1.2] + lines[2:])
    started = time.monotonic()
    with pytest.raises(errors.Timeout) as caught:
        client.chat(HELLO, model="qwen3.5:9b")
    assert time.monotonic() - started < 1.1
    assert caught.value.detail == {"seconds": 0.4} and caught.value.retryable


def test_load_timeout_before_the_first_chunk(fake, make_client):
    key = fake.add_key()
    client = make_client(keystore=Keystore.memory(key), timeout=0.3, load_timeout=2)
    fake.chat_script([0.8] + text_stream(loading=0))
    assert client.chat(HELLO, model="qwen3.5:9b").text == "Bonjour !"    # pas chargé : 2 s
    client.models()                                         # mistral:7b est chargé : 0,3 s
    with pytest.raises(errors.Timeout):
        client.chat(HELLO, model="mistral:7b")


def test_real_cancellation(fake, make_client):
    key = fake.add_key()
    client = make_client(keystore=Keystore.memory(key), timeout=30, load_timeout=30)
    fake.chat_script(text_stream(loading=0)[:2], hold=True)
    stop = threading.Event()
    pieces = []

    def on_text(piece):
        pieces.append(piece)
        if len(pieces) >= 3:
            stop.set()

    started = time.monotonic()
    with pytest.raises(errors.Cancelled) as caught:
        client.chat(HELLO, model="qwen3.5:9b", on_text=on_text, cancel=stop)
    assert time.monotonic() - started < 3
    assert caught.value.message == "Arrêté" and not caught.value.retryable
    assert fake.stream_closed.wait(5)                       # AIODOS voit l'appli partir


def test_cancel_while_waiting_for_the_first_chunk(fake, make_client):
    key = fake.add_key()
    client = make_client(keystore=Keystore.memory(key), timeout=60, load_timeout=60)
    fake.chat_script([20] + text_stream(loading=0))         # un modèle qui se charge, muet
    flag = {"stop": False}
    threading.Timer(0.3, lambda: flag.update(stop=True)).start()
    started = time.monotonic()
    with pytest.raises(errors.Cancelled):
        client.chat(HELLO, model="qwen3.5:9b", cancel=lambda: flag["stop"])
    assert time.monotonic() - started < 2


def test_cancel_before_sending(ready, fake):
    client, _ = ready
    stop = threading.Event()
    stop.set()
    with pytest.raises(errors.Cancelled):
        client.chat(HELLO, model="qwen3.5:9b", cancel=stop)
    assert fake.requests_to("/v1/chat/completions") == []


def test_a_broken_cancel_function_is_ignored(ready, fake):
    client, _ = ready

    def broken():
        raise RuntimeError("cassée")

    assert client.chat(HELLO, model="qwen3.5:9b", cancel=broken).text == "Bonjour !"
    with pytest.raises(TypeError):
        client.chat(HELLO, model="qwen3.5:9b", cancel=42)


def test_proof_is_redone_when_needed(ready, fake, monkeypatch):
    client, _ = ready

    def proofs():
        return [e for e in fake.requests_to("/aiodos/v1/info") if "challenge=" in e["path"]]

    client.models()
    client.models()
    assert len(proofs()) == 1                                 # gardée moins de 60 s
    fake.instance = "autre-instance"
    fake.write_discovery()
    client.models()
    assert len(proofs()) == 2                                 # l'instance a changé
    monkeypatch.setattr(client_module, "PROOF_MAX_AGE", 0.0)
    client.models()
    assert len(proofs()) == 3                                 # trop ancienne
    monkeypatch.setattr(client_module, "PROOF_MAX_AGE", 60.0)
    fake.close_gateway(discovery=False)
    with pytest.raises(errors.Unreachable):
        client.models()
    fake.open_gateway(discovery=False)
    client.models()
    assert len(proofs()) == 4                                 # après une erreur de connexion


def test_gateway_states_stop_calls_early(ready, fake):
    client, _ = ready
    fake.write_discovery(state="closed")
    with pytest.raises(errors.GatewayClosed):
        client.models()
    fake.write_discovery(running=False)
    with pytest.raises(errors.NotRunning):
        client.chat(HELLO, model="qwen3.5:9b")
    assert fake.log == []


def test_too_old_and_not_aiodos_on_calls(make_client):
    with FakeAiodos(mode="old") as old:
        key = old.add_key()
        with pytest.raises(errors.TooOld):
            make_client(keystore=Keystore.memory(key), url=old.url).models()
        assert old.seen_keys == []
    with FakeAiodos(mode="ollama") as ollama:
        key = ollama.add_key()
        with pytest.raises(errors.NotAiodos):
            make_client(keystore=Keystore.memory(key), url=ollama.url).chat(HELLO, model="x")
        assert ollama.seen_keys == []


def test_untrusted_on_calls(make_client, monkeypatch):
    with FakeAiodos(impostor=True) as impostor:
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", impostor.discovery_file)
        key = impostor.add_key()
        client = make_client(keystore=Keystore.memory(key))
        for call in (client.models, lambda: client.chat(HELLO, model="qwen3.5:9b"),
                     lambda: client.embed("x", model="embeddinggemma")):
            with pytest.raises(errors.Untrusted):
                call()
        assert impostor.seen_keys == []
