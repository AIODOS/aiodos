# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Ce qu'AIODOS n'annonce pas (`features`) lève NotSupported sans rien essayer : les travaux
ComfyUI devant une version d'AIODOS qui ne les sert pas, et ce que ce client ne sert pas
encore (recettes, object_info, gabarits, liens signés, priorité, signalements)."""

import pytest

from aiodos_client import Keystore, errors
from aiodos_client.fake import FEATURES, L1_FEATURES, FakeAiodos, png
from aiodos_client.files import File
from aiodos_client.jobs import Job
from support import COMFY, graph

CALLS = {
    "engines.comfyui": [
        lambda ai: ai.comfy.plan(graph={}), lambda ai: ai.comfy.tokenize({}, {"12": b"png"}),
        lambda ai: ai.comfy.submit(graph={}, label="Mood"), lambda ai: ai.comfy.run(graph={})],
    "jobs": [
        lambda ai: ai.jobs(), lambda ai: ai.job("job_1"), lambda ai: Job(ai, "job_1").watch(),
        lambda ai: Job(ai, "job_1").wait(), lambda ai: Job(ai, "job_1").cancel(),
        lambda ai: Job(ai, "job_1").refresh(), lambda ai: Job(ai, "job_1").files(),
        lambda ai: Job(ai, "job_1").retry_failed(), lambda ai: Job(ai, "job_1").delete(),
        lambda ai: Job(ai, "job_1").download(".")],
    "jobs.pause": [lambda ai: Job(ai, "job_1").pause(), lambda ai: Job(ai, "job_1").resume()],
    "files": [lambda ai: File(ai, "file_1", {"bytes": 3, "sha256": "0" * 64}).content(),
              lambda ai: File(ai, "file_1").refresh(), lambda ai: File(ai, "file_1").delete(),
              lambda ai: File(ai, "file_1", {"bytes": 3, "sha256": "0" * 64}).save("x.png")],
    "inputs": [lambda ai: ai.inputs.put(b"\x89PNG")],
    "engines": [lambda ai: ai.engines()],
}
# Ce que ce client ne sert pas encore, quel que soit AIODOS
UNSERVED = {
    "engines.comfyui.object_info": [lambda ai: ai.comfy.object_info()],
    "engines.comfyui.templates": [lambda ai: ai.comfy.template("image_sdxl_t2i")],
    "recipes": [lambda ai: ai.comfy.recipes(), lambda ai: ai.comfy.recipe_for("image_sdxl_t2i"),
                lambda ai: ai.comfy.submit(recipe="comfy-templates/image_sdxl_t2i"),
                lambda ai: ai.comfy.plan(recipe="comfy-templates/image_sdxl_t2i")],
    "files.link": [lambda ai: File(ai, "file_1").link()],
    "jobs.priority": [lambda ai: Job(ai, "job_1").set_priority("high")],
    "jobs.report": [lambda ai: Job(ai, "job_1").report(0, "visage déformé")],
}


def _client(server, make_client, monkeypatch):
    monkeypatch.setenv("AIODOS_DISCOVERY_FILE", server.discovery_file)
    return make_client(keystore=Keystore.memory(server.add_key(scopes=COMFY)))


def _l2_routes(server):
    return {entry["path"].split("?", 1)[0] for entry in server.log} - {"/aiodos/v1/info"}


@pytest.mark.parametrize("feature", sorted(CALLS))
def test_not_supported_while_not_announced(make_client, monkeypatch, feature):
    with FakeAiodos(features=[f for f in FEATURES if f != feature]) as server:
        client = _client(server, make_client, monkeypatch)
        for call in CALLS[feature]:
            with pytest.raises(errors.NotSupported) as caught:
                call(client)
            assert caught.value.code == "not_supported"
            assert caught.value.detail["feature"] == feature
            assert feature in caught.value.message
        assert _l2_routes(server) == set()                # rien d'autre n'a été essayé


def test_an_aiodos_of_l1_serves_none_of_it(make_client, monkeypatch):
    with FakeAiodos(features=L1_FEATURES) as server:
        client = _client(server, make_client, monkeypatch)
        for calls in CALLS.values():
            for call in calls:
                with pytest.raises(errors.NotSupported):
                    call(client)
        assert _l2_routes(server) == set()
        assert client.status().features == L1_FEATURES


@pytest.mark.parametrize("feature", sorted(UNSERVED))
def test_not_yet_served_by_this_client(comfy, feature):
    client, _, fake = comfy
    for call in UNSERVED[feature]:
        with pytest.raises(errors.NotSupported) as caught:
            call(client)
        assert caught.value.detail == {"feature": feature}
        assert "Cette version d'AIODOS ne sert pas encore" in caught.value.message
    assert _l2_routes(fake) == set()


def test_announced_but_unknown_to_this_client(make_client, monkeypatch):
    with FakeAiodos(features=FEATURES + ("recipes", "files.link")) as server:
        client = _client(server, make_client, monkeypatch)
        for feature in ("recipes", "files.link"):
            for call in UNSERVED[feature]:
                with pytest.raises(errors.NotSupported) as caught:
                    call(client)
                assert caught.value.detail == {"feature": feature, "client": "0.2.0"}
                assert "aiodos-client 0.2.0" in caught.value.message
                assert caught.value.hint == "Mets à jour aiodos-client."
        assert _l2_routes(server) == set()


def test_when_aiodos_is_away(make_client):
    with pytest.raises(errors.NeedsAccess):              # pas de clé : rien ne part
        make_client().comfy.submit(graph=graph(), inputs={"ref0": png()})
    away = make_client(keystore=Keystore.memory(FakeAiodos().add_key()))
    with pytest.raises(errors.Unreachable):
        away.comfy.submit(graph=graph(), inputs={"ref0": png()})


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
