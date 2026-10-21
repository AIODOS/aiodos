# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""La surface de la livraison 2 (moteurs, travaux, entrées, fichiers) : présente, et
NotSupported sans rien essayer tant que `features` ne l'annonce pas."""

import pytest

from aiodos_client import Keystore, errors
from aiodos_client.fake import FakeAiodos
from aiodos_client.jobs import File, Job

CALLS = {
    "engines.comfyui": [
        lambda ai: ai.comfy.plan(graph={}), lambda ai: ai.comfy.object_info(),
        lambda ai: ai.comfy.template("image_sdxl_t2i"), lambda ai: ai.comfy.recipes(),
        lambda ai: ai.comfy.recipe_for("image_sdxl_t2i"),
        lambda ai: ai.comfy.tokenize({}, {"12": b"png"}),
        lambda ai: ai.comfy.submit(graph={}, label="Mood"), lambda ai: ai.comfy.run(graph={})],
    "jobs": [
        lambda ai: ai.jobs(), lambda ai: ai.job("job_1"), lambda ai: Job(ai, "job_1").watch(),
        lambda ai: Job(ai, "job_1").wait(), lambda ai: Job(ai, "job_1").cancel(),
        lambda ai: Job(ai, "job_1").pause(), lambda ai: Job(ai, "job_1").download(".")],
    "files": [lambda ai: File(ai, "file_1").link(), lambda ai: File(ai, "file_1").content()],
    "inputs": [lambda ai: ai.inputs.put(b"\x89PNG")],
    "engines": [lambda ai: ai.engines()],
}


@pytest.mark.parametrize("feature", sorted(CALLS))
def test_not_supported_while_not_announced(ready, fake, feature):
    client, _ = ready
    for call in CALLS[feature]:
        with pytest.raises(errors.NotSupported) as caught:
            call(client)
        assert caught.value.code == "not_supported"
        assert caught.value.detail["feature"] == feature
        assert feature in caught.value.message
    routes = {entry["path"].split("?", 1)[0] for entry in fake.log}
    assert routes <= {"/aiodos/v1/info"}                 # rien d'autre n'a été essayé


def test_not_supported_by_this_client_when_announced(make_client, monkeypatch):
    with FakeAiodos(features=("access", "models", "chat", "embed", "open", "engines",
                              "engines.comfyui", "jobs", "inputs", "files")) as server:
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", server.discovery_file)
        client = make_client()
        for feature, calls in CALLS.items():
            for call in calls:
                with pytest.raises(errors.NotSupported) as caught:
                    call(client)
                assert caught.value.detail == {"feature": feature, "client": "0.1.0"}
                assert "aiodos-client 0.1.0" in caught.value.message
        routes = {entry["path"].split("?", 1)[0] for entry in server.log}
        assert routes <= {"/aiodos/v1/info"}


def test_not_supported_when_aiodos_is_away(make_client):
    with pytest.raises(errors.NotSupported):
        make_client().comfy.submit(graph={})


def test_l1_features_are_checked_too(make_client, monkeypatch):
    with FakeAiodos(features=("access", "models")) as server:
        monkeypatch.setenv("AIODOS_DISCOVERY_FILE", server.discovery_file)
        client = make_client(keystore=Keystore.memory(server.add_key()))
        with pytest.raises(errors.NotSupported) as caught:
            client.chat([{"role": "user", "content": "Bonjour"}], model="qwen3.5:9b")
        assert caught.value.detail == {"feature": "chat"}
        with pytest.raises(errors.NotSupported):
            client.embed("x", model="embeddinggemma")
        assert server.requests_to("/v1/chat/completions") == []
        assert server.requests_to("/v1/embeddings") == []
