# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""La clé n'apparaît jamais : ni dans un repr ou un str, ni dans un message d'erreur, ni dans
le journal (même à DEBUG), sur tout un parcours ; le device_code non plus."""

import logging
import threading

import pytest

from aiodos_client import Keystore, errors
from aiodos_client.fake import FakeAiodos


def _texts_of(records):
    out = []
    for record in records:
        out.append(record.getMessage())
        out.append(repr(record.args))
        if record.exc_info and record.exc_info[1] is not None:
            out.append(repr(record.exc_info[1]))
    return out


def test_the_key_never_shows(fake, make_client, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    store = Keystore.memory()
    client = make_client(keystore=store)
    shown = []

    worker_errors = []

    def connect():
        try:
            access = client.connect(["chat", "embed"], purpose="Essai")
            shown.extend([repr(access), str(access)])
        except Exception as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=connect)
    worker.start()
    request = fake.wait_request()
    device_code = request["device_code"]
    key = fake.approve()
    worker.join(5)
    assert not worker_errors and store.get() == key
    status = client.status()
    shown += [repr(client), str(client), repr(store), repr(status), str(status)]
    result = client.chat([{"role": "user", "content": "Bonjour"}], model="qwen3.5:9b")
    shown += [repr(result)]
    shown += [repr(client.models()), repr(client.embed("x"))]
    caught = []
    for code in ("key_revoked", "scope_denied", "engine_unavailable"):
        fake.fail_next(code, path="/aiodos/v1/models")
        try:
            client.models()
        except errors.AiodosError as exc:
            caught.append(exc)
    fake.override("/v1/chat/completions", {"status": 400, "headers": {"X-Aiodos-Api": "1"},
                                           "json": {"error": {"message": f"Clé {key} refusée",
                                                              "code": "invalid_request"}}})
    try:
        client.chat([{"role": "user", "content": "Bonjour"}], model="qwen3.5:9b")
    except errors.AiodosError as exc:
        caught.append(exc)
    for exc in caught:
        shown += [repr(exc), str(exc), exc.message, exc.hint or "", repr(exc.detail)]
    with FakeAiodos(impostor=True) as impostor:
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", impostor.discovery_file)
        with pytest.raises(errors.Untrusted) as untrusted:
            client.models()
        shown += [repr(untrusted.value), str(untrusted.value), repr(client.status())]
        assert impostor.seen_keys == []
    monkeypatch.setenv("AIODOS_DISCOVERY_FILE", fake.discovery_file)
    client.disconnect()
    secret = key[20:]
    for text in shown + _texts_of(caplog.records) + [caplog.text]:
        assert secret not in text
        assert device_code not in text
    assert any("aiodos_client" in record.name for record in caplog.records)


def test_no_handler_but_a_null_one():
    logger = logging.getLogger("aiodos_client")
    assert any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)
