# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.2.0
"""Le client comprend chaque réponse de clients/contract/jobs.json : le faux AIODOS la sert
telle quelle (override), le client l'appelle comme une appli, et la requête qu'il envoie est
celle du contrat."""

import copy
import hashlib

import pytest

from aiodos_client import Job, errors
from aiodos_client.fake import png
from aiodos_client.files import File
from support import fill, load_contract

JOBS = {item["name"]: item for item in load_contract("jobs.json")["interactions"]}
JOB_ID = "job_EUi5rlF-uTASX7Pm"
FILE_ID = "file_juJXvOesHQXzSYVLCmYBXw"
PNG = png()
SHA = hashlib.sha256(PNG).hexdigest()


def _serve(fake, name, path=None, **changes):
    """La réponse du contrat pour cette interaction, servie au prochain appel du client."""
    item = JOBS[name]
    response = copy.deepcopy(item["response"])
    if response.get("body") == "{png}":
        response["raw"] = PNG
    elif response.get("body") == "{png:0-9}":
        response["raw"] = PNG[:10]
    response.pop("body", None)
    if isinstance(response.get("json"), dict):
        response["json"].update(changes)
    request = item["request"]
    target = path or fill(request["path"], sha256=SHA, job_id=JOB_ID,
                          file_id=FILE_ID).split("?", 1)[0]
    fake.override(target, response, method=request["method"])
    return item


def _sent(fake, item, path=None):
    """La requête reçue pour cette interaction : même méthode, même corps que le contrat."""
    request = item["request"]
    target = path or fill(request["path"], sha256=SHA, job_id=JOB_ID,
                          file_id=FILE_ID).split("?", 1)[0]
    found = [r for r in fake.requests_to(target) if r["method"] == request["method"]]
    assert found, f"{item['name']} : rien reçu sur {target}"
    last = found[-1]
    if request.get("json") is not None:
        assert last["json"] == fill(request["json"], sha256=SHA), item["name"]
    return last


def _submit_args(request):
    body = fill(request["json"], sha256=SHA)
    return {"graph": body["graph"], "label": body.get("label"),
            "client_ref": body.get("client_ref"), "items": body.get("items"),
            "inputs": {name: {"sha256": value["sha256"]}
                       for name, value in body.get("inputs", {}).items()}}


def test_engines(comfy):
    client, _, fake = comfy
    item = _serve(fake, "moteurs")
    engines = client.engines()
    _sent(fake, item)
    engine = engines.by_id["comfyui"]
    assert engine.label == "ComfyUI" and engine.enabled
    assert engine.here["version"] == "0.35.1" and engine.machines["here"] == 1
    assert engine.has_model("sd_xl_base_1.0.safetensors", "checkpoints")
    assert not engine.has_model("sd_xl_base_1.0.safetensors", "loras")
    assert engine.object_info["machine"] == "PC-BUREAU" and engine.recipes == 0


def test_input_sent(comfy):
    client, _, fake = comfy
    _serve(fake, "entrée inconnue", path=f"/aiodos/v1/inputs/{SHA}")
    item = _serve(fake, "entrée envoyée", sha256=SHA)    # l'exemple porte une autre empreinte
    assert client.inputs.put(PNG) == SHA
    sent = _sent(fake, item)
    assert sent["headers"]["Content-Type"] == "image/png" and sent["bytes"] == len(PNG)


def test_input_already_there(comfy):
    client, _, fake = comfy
    item = _serve(fake, "entrée déjà là")
    assert client.inputs.put(PNG) == SHA
    _sent(fake, item)
    assert [r["method"] for r in fake.requests_to(f"/aiodos/v1/inputs/{SHA}")] == ["HEAD"]


def test_input_unknown_is_sent(comfy):
    client, _, fake = comfy
    _serve(fake, "entrée inconnue", path=f"/aiodos/v1/inputs/{SHA}")
    assert client.inputs.put(PNG) == SHA
    assert [r["method"] for r in fake.requests_to(f"/aiodos/v1/inputs/{SHA}")] == ["HEAD",
                                                                                   "PUT"]


def test_input_with_the_wrong_hash(comfy):
    client, _, fake = comfy
    _serve(fake, "entrée inconnue", path=f"/aiodos/v1/inputs/{SHA}")
    _serve(fake, "entrée à la mauvaise empreinte", path=f"/aiodos/v1/inputs/{SHA}")
    with pytest.raises(errors.InputHashMismatch) as caught:
        client.inputs.put(PNG)
    assert isinstance(caught.value, errors.InvalidRequest) and caught.value.status == 400
    assert caught.value.sha256 == JOBS["entrée à la mauvaise empreinte"]["response"]["json"][
        "error"]["aiodos"]["detail"]["sha256"]
    assert caught.value.hint == "Calcule le SHA-256 des octets envoyés."


def test_plan(comfy):
    client, _, fake = comfy
    item = _serve(fake, "pré-vol")
    plan = client.comfy.plan(**_submit_args(item["request"]))
    _sent(fake, item)
    assert plan.ok and plan.units == 2 and plan.where_effective == ("here",)
    assert plan.missing == () and plan.where_reason is None
    assert plan.machines["here"] is True


def test_job_submitted_then_the_same(comfy):
    client, _, fake = comfy
    item = _serve(fake, "travail déposé")
    job = client.comfy.submit(**_submit_args(item["request"]))
    _sent(fake, item)
    assert (job.id, job.status, job.label, job.client_ref) == (JOB_ID, "queued",
                                                               "Moods · Séq. 3",
                                                               "pandora:mood:5f2c9a")
    assert job.progress.total == 2 and job.progress.percent == 0
    assert job.progress.message == "0 sur 2" and not job.finished
    assert [u.stage for u in job.units] == ["waiting_machine", "waiting_machine"]
    assert job.where == ("here",) and job.files == [] and job.error is None
    again = _serve(fake, "même client_ref : le même travail")
    same = client.comfy.submit(**_submit_args(again["request"]))
    _sent(fake, again)
    assert same.id == job.id


def test_job_in_progress(comfy):
    client, _, fake = comfy
    item = _serve(fake, "travail en cours")
    job = Job(client, JOB_ID).refresh()
    _sent(fake, item)
    assert job.status == "in_progress" and job.progress.percent == 50
    assert job.progress.message == "1 sur 2 · 1 en cours sur 1 machine"
    [machine] = job.progress.machines
    assert (machine.name, machine.place, machine.running, machine.done) == ("PC-BUREAU", "here",
                                                                            1, 1)
    first, second = job.units
    assert (first.status, first.files, first.seconds) == ("completed", (FILE_ID,), 10.6)
    assert (second.status, second.stage) == ("in_progress", "running")
    [file] = job.files
    assert (file.id, file.type, file.bytes, file.width, file.unit) == (FILE_ID, "image/png",
                                                                       1014522, 768, 0)
    assert not file.fetched and file.expires_at is None and file.machine == "PC-BUREAU"


def test_job_finished(comfy):
    client, _, fake = comfy
    _serve(fake, "travail fini")
    job = Job(client, JOB_ID).refresh()
    assert job.status == "completed" and job.finished and job.progress.done == 2
    assert job.completed_at == 1760950017 and len(job.files) == 2


def test_my_jobs(comfy):
    client, _, fake = comfy
    item = _serve(fake, "ses travaux", path="/aiodos/v1/jobs")
    jobs = client.jobs(active=None, client_ref="pandora:mood:5f2c9a")
    sent = _sent(fake, item, path="/aiodos/v1/jobs")
    assert "client_ref=pandora%3Amood%3A5f2c9a" in sent["path"] and "active" not in sent["path"]
    [job] = jobs
    assert job.id == JOB_ID and job.status == "completed"


def test_events_until_the_end(comfy):
    client, _, fake = comfy
    item = _serve(fake, "événements jusqu'à la fin")
    events = list(Job(client, JOB_ID).watch(timeout=10))
    sent = _sent(fake, item)
    assert sent["headers"]["Accept"] == "text/event-stream"
    assert [e.kind for e in events] == ["job", "job", "file", "file", "job", "end"]
    assert [e.file.id for e in events if e.kind == "file"] == [FILE_ID,
                                                               "file_bvuRVoags4c-28D6pPV-yw"]
    assert events[0].data["status"] == "queued" and events[-1].status == "completed"
    assert events[-1].job.status == "completed" and events[-1].job.progress.percent == 100


def test_unfetched_files(comfy):
    client, _, fake = comfy
    item = _serve(fake, "fichiers pas encore récupérés")
    files = Job(client, JOB_ID).files(unfetched=True)
    sent = _sent(fake, item)
    assert sent["path"].endswith("/files?unfetched=1")
    assert [f.id for f in files] == [FILE_ID, "file_bvuRVoags4c-28D6pPV-yw"]
    assert all(f.job_id == JOB_ID and not f.fetched for f in files)


def test_one_file(comfy):
    client, _, fake = comfy
    item = _serve(fake, "un fichier")
    file = File(client, FILE_ID).refresh()
    _sent(fake, item)
    assert (file.type, file.verified, file.sha256) == (
        "image/png", "none", "9b71d224bd62f3785d96d46ad3ea3d73319bfbc2890caadae2dff72519673ca7")
    assert file.extension == ".png" and file.default_name == f"{FILE_ID}.png"


def test_file_content_by_range(comfy):
    client, _, fake = comfy
    item = _serve(fake, "le contenu d'un fichier, par plage")
    data = File(client, FILE_ID, {"bytes": len(PNG)}).content(0, 9)
    sent = _sent(fake, item)
    assert sent["headers"]["Range"] == "bytes=0-9"
    assert data == PNG[:10] and data.startswith(b"\x89PNG")


def test_pause_after_the_current_unit(comfy):
    client, _, fake = comfy
    item = _serve(fake, "pause après l'unité en cours")
    job = Job(client, JOB_ID).pause()
    _sent(fake, item)
    assert job.status == "pausing" and job.pause_info == {"when": "after_current", "pending": 1}
    assert job.progress.message == "0 sur 2 · en pause"


def test_cancel(comfy):
    client, _, fake = comfy
    _serve(fake, "annulation")
    job = Job(client, JOB_ID).cancel()
    assert job.status == "cancelling" and job.cancelled_by == "app"
    assert [u.status for u in job.units] == ["in_progress", "cancelled"]


def test_cancel_a_finished_job(comfy):
    client, _, fake = comfy
    _serve(fake, "annulation d'un travail fini")
    with pytest.raises(errors.JobNotCancellable) as caught:
        Job(client, JOB_ID).cancel()
    assert caught.value.status == 409 and caught.value.message == "Ce travail est déjà fini."


def test_job_of_another_app(comfy):
    client, _, fake = comfy
    _serve(fake, "travail d'une autre appli")
    with pytest.raises(errors.JobNotFound) as caught:
        client.job("job_autre")
    assert isinstance(caught.value, errors.NotFound) and caught.value.status == 404


def test_editor_format(comfy):
    client, _, fake = comfy
    _serve(fake, "graphe au format de l'éditeur")
    with pytest.raises(errors.GraphInvalid) as caught:
        client.comfy.submit(graph={"nodes": [], "links": []})
    assert caught.value.code == "graph_not_api" and caught.value.graph == 0
    assert caught.value.param == "graphs"


def test_missing_model(comfy):
    client, _, fake = comfy
    item = _serve(fake, "modèle absent")
    args = _submit_args(item["request"])
    args["client_ref"] = None
    with pytest.raises(errors.ModelUnavailable) as caught:
        client.comfy.submit(**args)
    assert caught.value.models == [{"folder": None, "name": "absent.safetensors"}]
    assert caught.value.retryable and caught.value.status == 409


def test_without_the_right(comfy):
    client, _, fake = comfy
    item = _serve(fake, "sans le droit engines:comfyui")
    with pytest.raises(errors.ScopeDenied) as caught:
        client.comfy.submit(**_submit_args(item["request"]))
    assert caught.value.scope == "engines:comfyui"


def test_every_interaction_is_replayed():
    tested = {"moteurs", "entrée envoyée", "entrée déjà là", "entrée inconnue",
              "entrée à la mauvaise empreinte", "pré-vol", "travail déposé",
              "même client_ref : le même travail", "travail en cours", "travail fini",
              "ses travaux", "événements jusqu'à la fin", "fichiers pas encore récupérés",
              "un fichier", "le contenu d'un fichier, par plage", "pause après l'unité en cours",
              "annulation", "annulation d'un travail fini", "travail d'une autre appli",
              "graphe au format de l'éditeur", "modèle absent", "sans le droit engines:comfyui"}
    assert set(JOBS) == tested and len(JOBS) == 22
