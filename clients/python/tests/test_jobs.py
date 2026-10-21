# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Les travaux ComfyUI de bout en bout contre FakeAiodos : tokenize → submit → watch →
download ; pause, reprise, annulation ; reconnexion du flux ; reprise d'un téléchargement
coupé ; client_ref ; échecs et relance ; une appli ne voit rien d'une autre."""

import hashlib
import json
import logging
import os
import shutil
import threading
import time

import pytest

from aiodos_client import File, Job, Keystore, errors
from aiodos_client.fake import png
from aiodos_client.inputs import MAX_INPUT
from support import COMFY, graph, items, wait_until

PNG = png()
SHA = hashlib.sha256(PNG).hexdigest()


def _tokenized(client):
    """Le graphe du contrat, son LoadImage nourri de la référence par tokenize()."""
    api, inputs = client.comfy.tokenize(graph(), {"10": PNG})
    return api, inputs


def _submit(client, **options):
    api, inputs = _tokenized(client)
    values = {"graph": api, "inputs": inputs, "items": items(), "label": "Moods · Séq. 3"}
    values.update(options)
    return client.comfy.submit(**values)


# --- De bout en bout ------------------------------------------------------------------------
def test_tokenize_submit_watch_download(comfy, tmp_path):
    client, key, fake = comfy
    original = graph()
    api, inputs = client.comfy.tokenize(original, {"10": PNG})
    assert api["10"]["inputs"]["image"] == "@in:node10" and inputs == {"node10": PNG}
    assert original == graph()                             # le graphe de l'appli n'a pas changé
    job = client.comfy.submit(graph=api, inputs=inputs, items=items(), label="Moods · Séq. 3",
                              client_ref="pandora:mood:5f2c9a")
    assert job.status == "queued" and job.progress.total == 2
    assert fake.inputs_of(key) == {SHA: {"size": len(PNG), "type": "image/png"}}
    events = list(job.watch(timeout=10))
    kinds = [e.kind for e in events]
    assert kinds[0] == "job" and kinds[-1] == "end" and kinds.count("file") == 2
    assert events[-1].status == "completed" and job.status == "completed"
    assert job.progress.percent == 100 and job.progress.done == 2
    assert [u.label for u in job.units] == ["Mood 1", "Mood 2"]
    paths = job.download(tmp_path / "moods", stem="Mood · Séq. 3")
    assert [p.name for p in paths] == ["Mood · Séq. 3_1.png", "Mood · Séq. 3_2.png"]
    for path, file in zip(paths, paths.files):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == file.sha256
        assert path.read_bytes().startswith(b"\x89PNG") and file.fetched
    assert not list((tmp_path / "moods").glob("*.part"))
    assert paths.job is job and job.files(unfetched=True) == []      # récupérés pour AIODOS
    assert all(f.fetched and f.expires_at for f in job.files())


def test_run_gives_the_paths(comfy, tmp_path):
    client, _, _ = comfy
    api, inputs = _tokenized(client)
    seen = []
    paths = client.comfy.run(graph=api, inputs=inputs, items=items(), dest_dir=tmp_path,
                             stem="plan", timeout=10, on_file=lambda f, p: seen.append(p.name))
    assert [p.name for p in paths] == ["plan_1.png", "plan_2.png"] == seen
    assert paths.job.status == "completed"
    only = client.comfy.run(graph=api, inputs=inputs, timeout=10)      # une unité, dossier neuf
    try:
        assert len(only) == 1 and only[0].name == f"{only.files[0].id}.png"
        assert only[0].is_file() and only[0].parent.name.startswith("aiodos-")
    finally:
        shutil.rmtree(only[0].parent, ignore_errors=True)


def test_watch_announces_each_file_once_and_the_job_is_found_again(comfy):
    client, _, fake = comfy
    job = _submit(client, client_ref="pandora:mood:1")
    job.wait(timeout=10)
    again = client.job(job.id)
    files = [e.file.id for e in again.watch(timeout=10) if e.kind == "file"]
    assert files == [f.id for f in job.files]             # pas encore récupérés : annoncés
    [found] = client.jobs(active=None, client_ref="pandora:mood:1")
    assert found.id == job.id and found.status == "completed"


def test_the_same_client_ref_gives_the_same_job(comfy):
    client, _, fake = comfy
    first = _submit(client, client_ref="pandora:mood:5f2c9a")
    second = _submit(client, client_ref="pandora:mood:5f2c9a")
    assert second.id == first.id
    assert len(client.jobs(active=None)) == 1
    other = _submit(client, client_ref="pandora:mood:autre")
    assert other.id != first.id


def test_jobs_are_listed_and_filtered(comfy, manual):
    client, _, fake = comfy
    done = _submit(client)
    fake.advance(done.id, steps=3)
    waiting = _submit(client, client_ref="pandora:b")
    assert [j.id for j in client.jobs()] == [waiting.id]                       # actifs
    assert [j.id for j in client.jobs(active=False)] == [done.id]
    assert [j.id for j in client.jobs(active=None)] == [waiting.id, done.id]  # récents d'abord
    assert [j.id for j in client.jobs(active=None, status="completed")] == [done.id]
    assert [j.id for j in client.jobs(active=None, status=["queued", "failed"])] == [waiting.id]
    with pytest.raises(ValueError):
        client.jobs(status="fini")
    with pytest.raises(ValueError):
        client.jobs(active="oui")


# --- Les commandes ---------------------------------------------------------------------------
def test_pause_after_the_current_unit_then_resume(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    fake.advance(job.id)                                  # Mood 1 en cours
    job.pause()
    assert job.status == "pausing" and job.pause_info == {"when": "after_current", "pending": 1}
    fake.advance(job.id)                                  # Mood 1 fini, rien ne repart
    assert job.refresh().status == "paused" and job.progress.done == 1
    assert job.progress.message == "1 sur 2 · en pause"
    assert job.resume().status == "in_progress" and job.pause_info is None
    fake.advance(job.id, steps=2)
    assert job.refresh().status == "completed"
    with pytest.raises(errors.JobNotPausable):
        job.pause()


def test_pause_now_gives_the_unit_back(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    fake.advance(job.id)
    job.pause(when="now")
    assert job.status == "paused" and [u.status for u in job.units] == ["queued", "queued"]
    with pytest.raises(ValueError):
        job.pause(when="bientôt")


def test_cancel_keeps_the_files_already_made(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    fake.advance(job.id, steps=2)                         # Mood 1 fini, Mood 2 en cours
    job.cancel()
    assert job.status == "cancelling" and job.cancelled_by == "app"
    wait_until(lambda: job.refresh().status == "cancelled")
    assert [u.status for u in job.units] == ["completed", "cancelled"] and len(job.files) == 1
    with pytest.raises(errors.JobNotCancellable):
        job.cancel()


def test_cancel_from_watch_cancels_in_aiodos(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    stop = threading.Event()
    seen = []
    with pytest.raises(errors.Cancelled) as caught:
        for event in job.watch(cancel=stop, timeout=10):
            seen.append(event.kind)
            stop.set()                                    # l'utilisateur arrête
    assert caught.value.message == "Arrêté" and seen == ["job"]
    assert [r["method"] for r in fake.requests_to(f"/aiodos/v1/jobs/{job.id}/cancel")] == ["POST"]
    assert fake.job_record(job.id)["cancelled_by"] == "app"


def test_watch_timeout_leaves_the_job_running(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    started = time.monotonic()
    with pytest.raises(errors.Timeout):
        job.wait(timeout=0.4)
    assert time.monotonic() - started < 3
    assert fake.requests_to(f"/aiodos/v1/jobs/{job.id}/cancel") == []
    assert fake.job_record(job.id)["status"] == "queued"


def test_failed_unit_then_retry(comfy, manual):
    client, _, fake = comfy
    fake.fail_unit(1, message="KSampler : CUDA out of memory (nœud 3)")
    job = _submit(client)
    fake.advance(job.id, steps=3)
    job.refresh()
    assert job.status == "completed" and job.progress.failed == 1
    assert job.units[1].error == {"code": "engine_error",
                                  "message": "KSampler : CUDA out of memory (nœud 3)"}
    job.retry_failed()
    assert job.status == "in_progress" and job.units[1].status == "queued"
    fake.advance(job.id, steps=2)
    assert job.refresh().status == "completed" and job.progress.done == 2


def test_run_raises_when_every_unit_failed(comfy):
    client, _, fake = comfy
    fake.fail_unit(0, code="engine_error", message="KSampler : CUDA out of memory (nœud 3)")
    api, inputs = _tokenized(client)
    with pytest.raises(errors.EngineFailed) as caught:
        client.comfy.run(graph=api, inputs=inputs, timeout=10)
    assert "CUDA out of memory" in caught.value.message and not caught.value.retryable
    assert caught.value.detail["errors"][0]["code"] == "engine_error"


def test_delete(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    with pytest.raises(errors.JobNotCancellable):
        job.delete()                                      # pas encore fini
    fake.advance(job.id, steps=3)
    file = job.refresh().files[0]
    assert job.delete() is True
    with pytest.raises(errors.JobNotFound):
        job.refresh()
    assert file.refresh().fetched is False                # ses fichiers suivent leur garde
    other = _submit(client)
    fake.advance(other.id, steps=3)
    gone = other.refresh().files[0]
    assert other.delete(files=True) is True
    with pytest.raises(errors.FileNotFound):
        gone.refresh()
    kept = _submit(client)
    fake.advance(kept.id, steps=3)
    assert kept.refresh().files[0].delete() is True


# --- Le flux d'événements --------------------------------------------------------------------
def test_events_reconnect_after_a_cut(comfy):
    client, _, fake = comfy
    fake.job_delay = 0.2
    fake.cut_events(after=1, times=2)                     # deux flux coupés après un événement
    job = _submit(client)
    events = list(job.watch(timeout=15))
    assert events[-1].kind == "end" and job.status == "completed"
    assert len(fake.requests_to(f"/aiodos/v1/jobs/{job.id}/events")) == 3
    files = [e.file.id for e in events if e.kind == "file"]
    assert sorted(files) == sorted(f.id for f in job.files) and len(files) == 2
    assert "unreachable" not in [e.kind for e in events]  # repris aussitôt, sans panne


def test_events_say_unreachable_then_resume(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    events = job.watch(timeout=20)
    assert next(events).kind == "job"
    fake.close_gateway(discovery=False)                   # AIODOS ne répond plus
    lost = next(events)
    assert lost.kind == "unreachable" and isinstance(lost.error, errors.Unreachable)
    fake.open_gateway(discovery=False)
    back = next(events)
    assert back.kind == "job" and back.job.status == "queued"
    fake.advance(job.id, steps=3)
    rest = [e.kind for e in events]
    assert rest[-1] == "end" and rest.count("file") == 2 and "unreachable" not in rest


def test_events_skip_pings_and_keep_unknown_kinds(comfy, manual):
    client, _, fake = comfy
    job = _submit(client)
    path = f"/aiodos/v1/jobs/{job.id}/events"
    state = fake.job_record(job.id)
    fake.override(path, {"status": 200, "headers": {"X-Aiodos-Api": "1"}, "sse": [
        ": ping", "event: unit", 'data: {"index": 0, "status": "in_progress"}',
        "event: job", "data: " + json.dumps(state),
        "event: end", f'data: {{"id": "{job.id}", "status": "deleted"}}']})
    kinds = [(e.kind, e.status) for e in job.watch(timeout=10)]
    assert kinds == [("unit", None), ("job", None), ("end", "deleted")]


def test_watch_fails_on_a_job_that_is_gone(comfy):
    client, _, _ = comfy
    with pytest.raises(errors.JobNotFound):
        list(Job(client, "job_inconnu").watch(timeout=5))


# --- Les fichiers --------------------------------------------------------------------------
def test_download_resumes_a_cut_transfer(comfy, tmp_path):
    client, _, fake = comfy
    job = _submit(client, items=None)
    job.wait(timeout=10)
    [file] = job.files
    fake.cut_content(after=30)                            # le transfert se coupe à 30 octets
    path = file.save(tmp_path / "mood.png")
    content = fake.requests_to(f"/aiodos/v1/files/{file.id}/content")
    assert [r["headers"].get("Range") for r in content] == [None, "bytes=30-"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == file.sha256 and file.fetched
    assert not (tmp_path / "mood.png.part").exists()


def test_a_partial_file_is_resumed_by_the_next_call(comfy, tmp_path):
    client, _, fake = comfy
    job = _submit(client, items=None)
    job.wait(timeout=10)
    [file] = job.files
    fake.cut_content(after=10, times=3)                   # plus de coupures que de reprises
    with pytest.raises(errors.Unreachable) as caught:
        file.save(tmp_path)
    assert caught.value.retryable
    part = tmp_path / f"{file.id}.png.part"
    assert part.stat().st_size == 30
    path = file.save(tmp_path)
    content = fake.requests_to(f"/aiodos/v1/files/{file.id}/content")
    assert content[-1]["headers"]["Range"] == "bytes=30-" and not part.exists()
    assert path.read_bytes() == file.content()


def test_a_foreign_partial_file_is_replaced(comfy, tmp_path):
    client, _, fake = comfy
    job = _submit(client, items=None)
    job.wait(timeout=10)
    [file] = job.files
    (tmp_path / "mood.png.part").write_bytes(b"\x89PNG pas le bon fichier")
    path = file.save(tmp_path / "mood.png")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == file.sha256
    ranges = [r["headers"].get("Range")
              for r in fake.requests_to(f"/aiodos/v1/files/{file.id}/content")]
    assert ranges == ["bytes=23-", None]                  # repris, faux, puis tout depuis le début


def test_a_corrupted_file_is_refused(comfy, tmp_path):
    client, _, fake = comfy
    job = _submit(client, items=None)
    job.wait(timeout=10)
    [file] = job.files
    fake.override(f"/aiodos/v1/files/{file.id}/content",
                  {"status": 200, "headers": {"X-Aiodos-Api": "1", "Content-Type": "image/png"},
                   "raw": b"\x89PNG" + b"\x00" * (file.bytes - 4)}, times=2)
    with pytest.raises(errors.ProtocolError) as caught:
        file.save(tmp_path / "mood.png")
    assert "empreinte" in caught.value.message and caught.value.detail["file"] == file.id
    assert not (tmp_path / "mood.png").exists() and not (tmp_path / "mood.png.part").exists()
    with pytest.raises(errors.ProtocolError):
        file.content()


def test_download_skips_what_is_already_there(comfy, tmp_path):
    client, _, fake = comfy
    job = _submit(client)
    job.wait(timeout=10)
    first = job.download(tmp_path)
    count = len(fake.log)
    again = job.download(tmp_path)
    assert list(again) == list(first)
    assert not any("/content" in r["path"] for r in fake.log[count:])


def test_file_content_and_range(comfy):
    client, _, _ = comfy
    job = _submit(client, items=None)
    job.wait(timeout=10)
    [file] = job.files
    data = file.content()
    assert hashlib.sha256(data).hexdigest() == file.sha256 and len(data) == file.size
    assert file.content(0, 7) == b"\x89PNG\r\n\x1a\n" and file.content(70) == data[70:]
    with pytest.raises(ValueError):
        file.content(5, 2)


# --- Les entrées ---------------------------------------------------------------------------
def test_inputs_are_sent_once(comfy, tmp_path):
    client, key, fake = comfy
    assert client.inputs.put(PNG) == SHA
    path = tmp_path / "ref.png"
    path.write_bytes(PNG)
    assert client.inputs.put(path) == SHA == client.inputs.put(str(path))
    methods = [r["method"] for r in fake.requests_to(f"/aiodos/v1/inputs/{SHA}")]
    assert methods == ["HEAD", "PUT", "HEAD", "HEAD"]
    job = client.comfy.submit(graph=graph(), inputs={"ref0": {"sha256": SHA}})
    assert job.status == "queued"


def test_an_input_too_large_is_refused_before_sending(comfy, tmp_path):
    client, _, fake = comfy
    big = tmp_path / "clip.mp4"
    with open(big, "wb") as handle:
        handle.truncate(MAX_INPUT + 1)
    with pytest.raises(errors.InputTooLarge) as caught:
        client.inputs.put(big)
    assert isinstance(caught.value, errors.FileTooLarge) and "100 Mo" in caught.value.message
    assert caught.value.detail == {"bytes": MAX_INPUT + 1, "max_bytes": MAX_INPUT}
    with pytest.raises(errors.InputTooLarge):
        client.comfy.submit(graph=graph(), inputs={"ref0": big, "ref1": PNG})
    assert not any(r["path"].startswith("/aiodos/v1/inputs/") for r in fake.log)
    assert fake.requests_to("/aiodos/v1/jobs") == []


def test_an_unknown_kind_of_input_is_refused(comfy):
    client, _, _ = comfy
    with pytest.raises(errors.UnsupportedMediaType):
        client.inputs.put(b"pas une image")


def test_the_right_is_said_even_through_head(fake, make_client):
    key = fake.add_key(scopes=("chat",))
    client = make_client(keystore=Keystore.memory(key))
    with pytest.raises(errors.ScopeDenied) as caught:
        client.inputs.put(PNG)
    assert caught.value.scope == "engines:comfyui"
    with pytest.raises(errors.ScopeDenied):
        client.comfy.submit(graph=graph(), inputs={"ref0": PNG})


# --- Le corps d'un travail -----------------------------------------------------------------
def test_submit_body_priority_and_models(comfy):
    client, _, fake = comfy
    _submit(client, priority="low", bounds={"unit_seconds": 60}, strict=True,
            models={"ckpt": {"folder": "checkpoints", "name": "sd_xl_base_1.0.safetensors"}},
            graph={**_tokenized(client)[0], "4": {"class_type": "CheckpointLoaderSimple",
                                                  "inputs": {"ckpt_name": "@model:ckpt"}}})
    sent = fake.requests_to("/aiodos/v1/jobs")[-1]
    body = sent["json"]
    assert body["priority"] == "low" and "where" not in body
    assert body["bounds"] == {"unit_seconds": 60} and body["strict"] is True
    assert body["models"]["ckpt"]["folder"] == "checkpoints"
    assert body["inputs"] == {"node10": {"sha256": SHA}} and body["engine"] == "comfyui"
    assert "X-Aiodos-Where" not in sent["headers"]
    plain = _submit(client)
    body = fake.requests_to("/aiodos/v1/jobs")[-1]["json"]
    assert "priority" not in body and "models" not in body and "bounds" not in body
    assert plain.status == "queued"
    with pytest.raises(ValueError):
        _submit(client, priority="urgent")


def test_where_narrows_the_agreement(comfy):
    """Le lieu part en X-Aiodos-Where : il resserre l'accord (plus large : WhereDenied), et
    AIODOS n'en garde que ce qu'il sert aux travaux ComfyUI (ce PC et mes machines)."""
    client, _, fake = comfy
    job = _submit(client, where=["here", "mine"])
    assert fake.requests_to("/aiodos/v1/jobs")[-1]["headers"]["X-Aiodos-Where"] == "here,mine"
    assert job.where == ("here", "mine") == job.where_effective
    with pytest.raises(errors.WhereDenied) as caught:
        _submit(client, where=["here", "friends"])        # plus large que l'accord
    assert caught.value.allowed == ["here", "mine"]
    with pytest.raises(errors.EngineUnavailable):
        _submit(client, where="mine")                     # aucune de mes machines n'a ComfyUI


def test_my_other_machines_compute_too(comfy):
    client, _, fake = comfy
    fake.comfy_peers = [{"name": "Portable"}]
    engine = client.engines().by_id["comfyui"]
    assert engine.machines["mine"] == 1
    assert engine.models[0].where == {"here": True, "mine": ["Portable"], "friends": None,
                                      "public": None}
    job = _submit(client, where="mine").wait(timeout=10)
    assert job.status == "completed" and job.where_effective == ("mine",)
    assert {(u.machine, u.place) for u in job.units} == {("Portable", "mine")}
    assert {(f.machine, f.place) for f in job.files} == {("Portable", "mine")}
    assert job.progress.machines[0].name == "Portable"
    api, inputs = _tokenized(client)
    absent = {**api, "4": {"class_type": "CheckpointLoaderSimple",
                           "inputs": {"ckpt_name": "absent.safetensors"}}}
    plan = client.comfy.plan(graph=absent, inputs=inputs)
    assert not plan.ok and plan.where_reason == "il manque des modèles"
    assert [m["machine"] for m in plan.missing] == ["PC-BUREAU", "Portable"]
    with pytest.raises(errors.ModelUnavailable) as caught:
        client.comfy.submit(graph=absent, inputs=inputs)
    assert caught.value.machine == "PC-BUREAU" and "PC-BUREAU" in caught.value.message
    fake.comfy_peers = [{"name": "Portable", "models": [
        {"folder": "checkpoints", "name": "absent.safetensors"}]}]
    plan = client.comfy.plan(graph=absent, inputs=inputs)
    assert plan.ok and plan.where_effective == ("mine",) and plan.machines["mine"] == 1
    assert client.comfy.submit(graph=absent, inputs=inputs).wait(timeout=10).units[0].machine \
        == "Portable"


def test_no_machine_in_time_is_worth_a_retry(comfy):
    client, _, fake = comfy
    fake.fail_unit(0, code="no_machine", message="Aucune machine ne l'a prise à temps.")
    api, inputs = _tokenized(client)
    with pytest.raises(errors.EngineFailed) as caught:
        client.comfy.run(graph=api, inputs=inputs, timeout=10)
    assert caught.value.retryable and caught.value.detail["errors"][0]["code"] == "no_machine"


def test_sensitive_stays_on_my_machines_within_the_agreement(fake, make_client):
    granted_all = make_client(keystore=Keystore.memory(fake.add_key(
        scopes=COMFY, where=("here", "mine", "friends", "public"))))
    _submit(granted_all, sensitive=True)
    assert fake.requests_to("/aiodos/v1/jobs")[-1]["headers"]["X-Aiodos-Where"] == "here,mine"
    _submit(granted_all, sensitive=True, where="here")
    assert fake.requests_to("/aiodos/v1/jobs")[-1]["headers"]["X-Aiodos-Where"] == "here"
    with pytest.raises(ValueError):
        _submit(granted_all, sensitive=True, where=["friends"])
    here_only = make_client(keystore=Keystore.memory(fake.add_key(scopes=COMFY,
                                                                  where=("here",))))
    job = _submit(here_only, sensitive=True)              # « mine » décoché à l'accord
    assert fake.requests_to("/aiodos/v1/jobs")[-1]["headers"]["X-Aiodos-Where"] == "here"
    assert job.status == "queued"
    friends_only = make_client(keystore=Keystore.memory(fake.add_key(scopes=COMFY,
                                                                     where=("friends",))))
    count = len(fake.requests_to("/aiodos/v1/jobs"))
    with pytest.raises(errors.WhereDenied) as caught:
        _submit(friends_only, sensitive=True)
    assert "sensibles" in caught.value.message and caught.value.allowed == ["friends"]
    assert len(fake.requests_to("/aiodos/v1/jobs")) == count    # rien n'est parti


def test_submit_arguments_are_checked_before_anything(comfy):
    client, _, fake = comfy
    count = len(fake.log)
    with pytest.raises(ValueError):
        client.comfy.submit()
    with pytest.raises(ValueError):
        client.comfy.submit(graph=graph(), graphs=[graph()])
    with pytest.raises(ValueError):
        client.comfy.submit(graph=graph(), items=items(), repeat={"count": 2})
    with pytest.raises(ValueError):
        client.comfy.submit(graph=graph(), inputs={"réf 0": PNG})
    with pytest.raises(TypeError):
        client.comfy.submit(graph=graph(), inputs={"ref0": 12})
    with pytest.raises(FileNotFoundError):
        client.comfy.submit(graph=graph(), inputs={"ref0": "C:/pas/de/fichier.png"})
    assert len(fake.log) == count


def test_repeat_and_several_graphs(comfy):
    client, _, fake = comfy
    api, inputs = _tokenized(client)
    job = client.comfy.submit(graph=api, inputs=inputs,
                              repeat={"count": 3, "vary": [{"node": "3", "input": "steps",
                                                            "step": 5}]})
    assert job.progress.total == 3
    job = client.comfy.submit(graphs=[api, api], inputs=inputs)
    assert job.progress.total == 2 and job.wait(timeout=10).status == "completed"


def test_errors_of_the_gateway_become_classes(comfy):
    client, _, fake = comfy
    api, inputs = _tokenized(client)
    with pytest.raises(errors.InputMissing) as caught:
        client.comfy.submit(graph=api)                    # @in:node10 sans entrée
    assert caught.value.input == "node10"
    bad = {**api, "6": {"class_type": "CLIPTextEncode",
                        "inputs": {"text": "C:\\Windows\\win.ini", "clip": ["4", 1]}}}
    with pytest.raises(errors.ForbiddenValue) as caught:
        client.comfy.submit(graph=bad, inputs=inputs)
    assert (caught.value.node, caught.value.input, caught.value.reason) == ("6", "text",
                                                                            "absolute_path")
    with pytest.raises(errors.GraphInvalid) as caught:
        client.comfy.submit(graph=api, inputs=inputs, items=[{"set": [["3", "seed", 1]]}])
    assert caught.value.code == "patch_invalid"
    no_output = {k: v for k, v in api.items() if k != "9"}
    with pytest.raises(errors.GraphInvalid) as caught:
        client.comfy.submit(graph=no_output, inputs=inputs)
    assert caught.value.code == "graph_invalid"
    with pytest.raises(errors.ModelUnresolved):
        client.comfy.submit(graph={**api, "4": {"class_type": "CheckpointLoaderSimple",
                                                "inputs": {"ckpt_name": "@model:x"}}},
                            inputs=inputs)
    fake.comfyui = False
    with pytest.raises(errors.EngineUnavailable) as caught:
        client.comfy.submit(graph=api, inputs=inputs)
    assert caught.value.status == 409
    plan = client.comfy.plan(graph=api, inputs=inputs)
    assert not plan.ok and plan.where_effective == () and plan.where_reason
    assert client.engines()[0].enabled is False


def test_plan_says_what_is_missing(comfy):
    client, _, fake = comfy
    api, inputs = _tokenized(client)
    absent = {**api, "4": {"class_type": "CheckpointLoaderSimple",
                           "inputs": {"ckpt_name": "absent.safetensors"}}}
    plan = client.comfy.plan(graph=absent, inputs=inputs, items=items())
    assert not plan.ok and plan.units == 2
    assert plan.missing == ({"folder": None, "name": "absent.safetensors",
                             "machine": "PC-BUREAU"},)
    assert fake.requests_to("/aiodos/v1/jobs") == []      # rien n'est mis en file


# --- Une appli ne voit rien d'une autre ----------------------------------------------------
def test_another_app_sees_nothing(comfy, make_client):
    client, _, fake = comfy
    job = _submit(client)
    job.wait(timeout=10)
    file = job.files[0]
    other = make_client(keystore=Keystore.memory(fake.add_key(scopes=COMFY, name="ARKLIGHT")))
    with pytest.raises(errors.JobNotFound):
        other.job(job.id)
    with pytest.raises(errors.FileNotFound):
        File(other, file.id).refresh()
    with pytest.raises(errors.FileNotFound):
        File(other, file.id, file.data).content()
    with pytest.raises(errors.InputMissing):              # l'entrée de PANDORA, pas la sienne
        other.comfy.submit(graph=graph(), inputs={"ref0": {"sha256": SHA}})
    assert other.jobs(active=None) == []
    resp, _ = other._request("HEAD", f"/aiodos/v1/inputs/{SHA}")
    assert resp.status == 404


# --- La clé ne se montre jamais ------------------------------------------------------------
def test_the_key_never_shows_in_the_jobs(comfy, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    client, key, fake = comfy
    job = _submit(client)
    api, inputs = _tokenized(client)
    shown = [repr(job), str(job), repr(client.engines()),
             repr(client.comfy.plan(graph=api, inputs=inputs)), repr(client.comfy)]
    for event in job.watch(timeout=10):
        shown += [repr(event), repr(event.file)]
    shown += [repr(p) for p in job.download(tmp_path)]
    fake.fail_next("key_revoked", path=f"/aiodos/v1/jobs/{job.id}")
    with pytest.raises(errors.Revoked) as caught:
        job.refresh()
    shown += [repr(caught.value), str(caught.value), repr(caught.value.detail)]
    secret = key[20:]
    for text in shown + [caplog.text] + [r.getMessage() for r in caplog.records]:
        assert secret not in text
    assert os.environ.get("AIODOS_KEY") is None
