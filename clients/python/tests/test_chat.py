# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""La conversation (les flux de chat.json, rejoués), les messages d'OpenAI et d'Anthropic, les
en-têtes ; les embeddings (embed.json) ; le catalogue (models.json) ; open_in_aiodos()."""

import base64
import json
import threading

import pytest

from aiodos_client import Keystore, errors
from aiodos_client.chat import image_url, to_openai, to_openai_tools
from aiodos_client.models import ModelList, model_from
from support import interaction, load_contract

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _replay(fake, client, title, **options):
    """Rejoue une interaction de chat.json : la réponse du contrat, la requête du client."""
    item = interaction("chat.json", title)
    fake.override("/v1/chat/completions", item["response"])
    sent = item["request"]["json"]
    arguments = {"model": sent["model"], "tools": sent.get("tools"),
                 "max_tokens": sent.get("max_tokens")}
    if sent.get("reasoning_effort") == "none":
        arguments["think"] = False
    arguments.update(options)
    try:
        return client.chat(sent["messages"], **arguments)
    finally:
        request = fake.requests_to("/v1/chat/completions")[-1]
        for name, value in sent.items():
            if name != "stream":
                assert request["json"][name] == value, name
        assert request["json"]["stream"] is True


def test_stream_with_loading_thinking_and_text(ready, fake):
    client, key = ready
    pieces, thoughts, loading = [], [], []
    result = _replay(fake, client, "conversation en flux : chargement, réflexion, texte",
                     on_text=pieces.append, on_thinking=thoughts.append,
                     on_loading=loading.append)
    assert result.text == "Bonjour !" and pieces == ["Bonjour", " !"]
    assert result.thinking == "L'utilisateur salue." and thoughts == [result.thinking]
    assert loading == [5]
    assert result.usage == {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29}
    assert (result.model, result.machine, result.place) == ("qwen3.5:9b", "PC-BUREAU", "here")
    assert (result.finish_reason, result.stopped_by) == ("stop", "stop")
    assert not result.truncated and result.context_length == 16384
    assert result.tool_calls == []
    request = fake.requests_to("/v1/chat/completions")[-1]
    headers = request["headers"]
    assert headers["Authorization"] == f"Bearer {key}"
    assert headers["Accept"] == "text/event-stream"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Aiodos-Client"] == "python/0.1.0" and headers["Accept-Language"] == "fr"
    assert request["json"]["stream_options"] == {"include_usage": True}


def test_stream_with_a_tool_call(ready, fake):
    client, _ = ready
    result = _replay(fake, client, "conversation en flux : appel d'outil")
    assert result.text == "" and result.finish_reason == "tool_calls"
    assert result.stopped_by == "tool_calls"
    [call] = result.tool_calls
    assert (call.id, call.name) == ("call_8yT2", "set_light")
    assert call.arguments == {"fixture": 3, "level": 80}
    assert call.raw == '{"fixture": 3, "level": 80}'


def test_stream_cut_at_the_end_of_the_context(ready, fake):
    client, _ = ready
    result = _replay(fake, client, "conversation en flux : coupée à la fin du contexte")
    assert result.text == "Il était une fois"
    assert (result.finish_reason, result.stopped_by, result.truncated) == ("length", "context",
                                                                            True)


def test_failure_on_the_way(ready, fake):
    client, _ = ready
    pieces = []
    with pytest.raises(errors.EngineFailed) as caught:
        _replay(fake, client, "conversation en flux : panne en route", on_text=pieces.append)
    assert pieces == ["Bon"]
    assert caught.value.code == "engine_failed" and caught.value.retryable
    assert caught.value.hint == "Réessaie."
    assert caught.value.message == "Le moteur intégré n'a pas pu répondre : connexion perdue."


def test_answer_without_stream_is_understood(ready, fake):
    client, _ = ready
    item = interaction("chat.json", "conversation sans flux")
    fake.override("/v1/chat/completions", item["response"])
    result = client.chat(item["request"]["json"]["messages"], model="qwen3.5:9b")
    assert result.text == "Bonjour !" and result.stopped_by == "stop"
    assert result.usage["total_tokens"] == 12 and result.machine == "PC-BUREAU"


def test_tools_refused_elsewhere(ready, fake):
    client, _ = ready
    with pytest.raises(errors.ToolsUnavailable) as caught:
        _replay(fake, client, "outils demandés, modèle seulement sur une autre machine")
    assert caught.value.param == "tools"
    assert caught.value.hint == "Choisis un modèle de ce PC, ou envoie X-Aiodos-Tools: optional."


def test_aiodos_headers_and_body(ready, fake):
    client, _ = ready
    client.chat([{"role": "user", "content": "Bonjour"}], model="qwen3.5:9b", system="Bref.",
                think=False, max_tokens=64, temperature=0.2, top_p=0.9, stop=["\n\n"], seed=7,
                response_format={"type": "json_object"}, where="here", batch=True,
                require_tools=False, tools=[{"name": "f", "input_schema": {"type": "object"}}])
    request = fake.requests_to("/v1/chat/completions")[-1]
    body, headers = request["json"], request["headers"]
    assert body["messages"][0] == {"role": "system", "content": "Bref."}
    assert body["reasoning_effort"] == "none" and body["max_tokens"] == 64
    assert (body["temperature"], body["top_p"], body["stop"], body["seed"]) == (0.2, 0.9,
                                                                                 ["\n\n"], 7)
    assert body["response_format"] == {"type": "json_object"}
    assert body["tools"][0]["function"]["name"] == "f"
    assert headers["X-Aiodos-Where"] == "here"
    assert headers["X-Aiodos-Priority"] == "background"
    assert headers["X-Aiodos-Tools"] == "optional"
    client.chat([{"role": "user", "content": "Bonjour"}], model="qwen3.5:9b", think="low",
                where=["here", "mine"])
    request = fake.requests_to("/v1/chat/completions")[-1]
    assert request["json"]["reasoning_effort"] == "low"
    assert request["headers"]["X-Aiodos-Where"] == "here,mine"
    for name in ("X-Aiodos-Priority", "X-Aiodos-Tools"):
        assert name not in request["headers"]


def test_english_messages(fake, make_client):
    key = fake.add_key()
    client = make_client(keystore=Keystore.memory(key), lang="en")
    client.chat([{"role": "user", "content": "Hello"}], model="qwen3.5:9b")
    assert fake.requests_to("/v1/chat/completions")[-1]["headers"]["Accept-Language"] == "en"


def test_where_denied_and_model_not_found(ready, fake):
    client, _ = ready
    with pytest.raises(errors.WhereDenied) as caught:
        client.chat([{"role": "user", "content": "Bonjour"}], model="qwen3.5:9b",
                    where=["friends"])
    assert caught.value.allowed == ["here", "mine"]
    with pytest.raises(errors.ModelNotFound):
        client.chat([{"role": "user", "content": "Bonjour"}], model="inexistant:1b")


def test_model_is_chosen_when_absent(ready, fake):
    client, _ = ready
    client.chat([{"role": "user", "content": "Bonjour"}])
    assert fake.requests_to("/v1/chat/completions")[-1]["json"]["model"] == "mistral:7b"
    client.chat([{"role": "user", "content": [{"type": "text", "text": "Que vois-tu ?"},
                                              {"type": "image_url",
                                               "image_url": {"url": PNG}}]}])
    assert fake.requests_to("/v1/chat/completions")[-1]["json"]["model"] == "qwen3.5:9b"
    fake.chat_models = [m for m in fake.chat_models if not m["vision"]]
    with pytest.raises(errors.ModelNotFound) as caught:
        client.chat([{"role": "user", "content": "Vois", "images": [JPEG]}])
    assert caught.value.code == "model_not_found" and "vision" in caught.value.message


def test_who_answered_from_the_headers(ready, fake):
    client, _ = ready
    fake.override("/v1/chat/completions", {"status": 200, "headers": {
        "X-Aiodos-Api": "1", "Content-Type": "text/event-stream",
        "X-Aiodos-Model": "hf.co%2Forg%2Fm:Q4", "X-Aiodos-Machine": "Bureau%20d%27%C3%89lise",
        "X-Aiodos-Place": "mine"}, "sse": [
        'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}',
        "data: [DONE]"]})
    result = client.chat([{"role": "user", "content": "Bonjour"}], model="qwen3.5:9b")
    assert (result.model, result.machine, result.place) == ("hf.co/org/m:Q4", "Bureau d'Élise",
                                                            "mine")


def test_anthropic_blocks():
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "Regarde."},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(JPEG).decode()}}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Il faut l'outil.", "signature": "x"},
            {"type": "text", "text": "Je règle."},
            {"type": "tool_use", "id": "toolu_1", "name": "set_light",
             "input": {"fixture": 3, "level": 80}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "fait"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True,
             "content": [{"type": "text", "text": "projecteur absent"}]},
            {"type": "text", "text": "Merci."}]},
    ]
    converted = to_openai(messages, system=[{"type": "text", "text": "Sois bref."}])
    assert converted[0] == {"role": "system", "content": "Sois bref."}
    image = converted[1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    assert converted[2] == {"role": "assistant", "content": "Je règle.", "tool_calls": [
        {"id": "toolu_1", "type": "function",
         "function": {"name": "set_light", "arguments": '{"fixture": 3, "level": 80}'}}]}
    assert converted[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "fait"}
    assert converted[4] == {"role": "tool", "tool_call_id": "toolu_2",
                            "content": "Error: projecteur absent"}
    assert converted[5] == {"role": "user", "content": "Merci."}
    assert "Il faut l'outil." not in json.dumps(converted)


def test_openai_messages_and_images():
    converted = to_openai([
        {"role": "user", "content": "Bonjour", "images": [PNG, "data:image/gif;base64,R0lG"]},
        {"role": "assistant", "content": "Salut", "reasoning_content": "secret",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": {"a": 1}}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "developer", "content": "règle"},
    ])
    parts = converted[0]["content"]
    assert parts[0] == {"type": "text", "text": "Bonjour"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2]["image_url"]["url"] == "data:image/gif;base64,R0lG"
    assert converted[1]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert "reasoning_content" not in converted[1]
    assert converted[2] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert converted[3] == {"role": "system", "content": "règle"}
    assert image_url(base64.b64encode(JPEG).decode()).startswith("data:image/jpeg;base64,")


def test_images_by_address_are_refused(ready, fake):
    client, _ = ready
    for url in ("https://exemple.fr/chat.png", "file:///C:/chat.png", "pas une image !"):
        with pytest.raises(errors.InvalidRequest):
            client.chat([{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}}]}], model="qwen3.5:9b")
    with pytest.raises(errors.InvalidRequest):
        to_openai([{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://exemple.fr/a.png"}}]}])
    assert fake.requests_to("/v1/chat/completions") == []


def test_tools_conversion():
    anthropic = {"name": "set_light", "description": "Règle.",
                 "input_schema": {"type": "object", "properties": {"level": {"type": "integer"}}}}
    openai = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}
    converted = to_openai_tools([anthropic, openai])
    assert converted[0] == {"type": "function", "function": {
        "name": "set_light", "description": "Règle.", "parameters": anthropic["input_schema"]}}
    assert converted[1] is openai
    with pytest.raises(ValueError):
        to_openai_tools([{"type": "web_search"}])


def test_concurrent_conversations(ready, fake):
    client, _ = ready
    results, failures = [], []

    def talk():
        try:
            results.append(client.chat([{"role": "user", "content": "Bonjour"}],
                                       model="qwen3.5:9b").text)
        except Exception as exc:
            failures.append(exc)

    workers = [threading.Thread(target=talk) for _ in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
    assert failures == [] and results == ["Bonjour !"] * 6


def test_embeddings_contract(ready, fake):
    client, _ = ready
    item = interaction("embed.json", "embeddings")
    fake.override("/v1/embeddings", item["response"])
    vectors = client.embed(item["request"]["json"]["input"],
                           model=item["request"]["json"]["model"])
    assert vectors == [[0.6, 0.8], [0.8, -0.6]]
    assert fake.requests_to("/v1/embeddings")[-1]["json"] == item["request"]["json"]


def test_embeddings_without_the_right(fake, make_client):
    key = fake.add_key(scopes=("chat",))
    client = make_client(keystore=Keystore.memory(key))
    with pytest.raises(errors.ScopeDenied) as caught:
        client.embed("un chat noir", model="embeddinggemma")
    assert caught.value.scope == "embed"
    item = interaction("embed.json", "embeddings sans le droit")
    fake.override("/v1/embeddings", item["response"])
    with pytest.raises(errors.ScopeDenied) as caught:
        client.embed("un chat noir", model="embeddinggemma")
    assert caught.value.message == item["response"]["json"]["error"]["message"]


def test_embeddings_choose_their_model(ready, fake):
    client, _ = ready
    [vector] = client.embed("un chat noir")
    assert len(vector) == 768
    assert fake.requests_to("/v1/embeddings")[-1]["json"]["model"] == "embeddinggemma"
    assert client.embed([]) == []


def test_models_contract(ready, fake):
    client, _ = ready
    for kind in ("chat", "embed"):
        title = ("catalogue des modèles de conversation" if kind == "chat"
                 else "catalogue des modèles d'embedding")
        fake.override("/aiodos/v1/models", interaction("models.json", title)["response"])
        models = client.models(kind)
        assert isinstance(models, ModelList)
        assert fake.requests_to("/aiodos/v1/models")[-1]["path"] == f"/aiodos/v1/models?kind={kind}"
        if kind == "chat":
            qwen = models.by_id["qwen3.5:9b"]
            assert (qwen.context, qwen.vision, qwen.tools, qwen.thinking) == (16384, True, True,
                                                                                True)
            assert not qwen.loaded and qwen.here and qwen.size_bytes == 6600000000
            assert [w.place for w in qwen.where] == ["here", "mine"]
            assert qwen.where[1].id == "peer:9c2e41d0:qwen3.5:9b" and qwen.where[1].loaded
            assert models.best(needs=["vision"]).id == "qwen3.5:9b"
            assert models.best(needs=["tools"]).id == "mistral:7b"      # déjà chargé
            assert models.best(needs=["thinking"]).id == "qwen3.5:9b"
        else:
            [gemma] = models
            assert (gemma.kind, gemma.dimensions, gemma.context) == ("embed", 768, 2048)
            assert models.best().id == "embeddinggemma"
    with pytest.raises(ValueError):
        client.models("image")


def test_catalog_with_unknown_values():
    entry = {"id": "x:1b", "kind": "embed", "label": "x", "installed": False, "context": None,
             "dimensions": None, "license": {"id": "apache-2.0", "restrictive": False},
             "where": [{"id": "local:x:1b", "context": None}]}
    model = model_from(entry)
    assert model.context is None and model.dimensions is None and not model.installed
    assert model.license == {"id": "apache-2.0", "restrictive": False}
    assert hash(model) and ModelList([model]).best() is None    # pas installé
    with pytest.raises(ValueError):
        ModelList([model]).best(needs=["voler"])


def test_open_in_aiodos(ready, fake):
    client, _ = ready
    assert client.open_in_aiodos("models", model="qwen3.5:9b") is True
    assert fake.requests_to("/aiodos/v1/open")[-1]["json"] == {"page": "models",
                                                               "model": "qwen3.5:9b"}
    fake.fail_next("rate_limited", retry_after=10, path="/aiodos/v1/open")
    with pytest.raises(errors.RateLimited) as caught:
        client.open_in_aiodos("applications")
    assert caught.value.retry_after == 10.0
    with pytest.raises(ValueError):
        client.open_in_aiodos("reglages")


def test_contract_error_replies_reach_the_caller(ready, fake):
    """Chaque erreur d'errors.json, servie sur une route du client, lève la bonne classe."""
    client, _ = ready
    for item in load_contract("errors.json")["interactions"]:
        response = item["response"]
        if item["request"]["path"] == "/v1/chat/completions":
            fake.override("/v1/chat/completions", response)
            call = lambda: client.chat([{"role": "user", "content": "Bonjour"}],  # noqa: E731
                                       model="qwen3.5:9b")
        else:
            fake.override("/aiodos/v1/models", response)
            call = client.models
        with pytest.raises(errors.AiodosError) as caught:
            call()
        body = response["json"]
        code = body["error"]["code"] if isinstance(body["error"], dict) else body["code"]
        assert caught.value.code == code, item["name"]
        assert caught.value.status == response["status"]
        retry = response["headers"].get("Retry-After")
        assert caught.value.retry_after == (float(retry) if retry else None)
