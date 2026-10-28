"""Le chantier en direct (docs/conception/essaim.md § 13, étape 7) : pour un travail, chaque
fragment, la machine qui le calcule (cette machine, une des miennes ou un ami par son nom, le
réseau public sans nom), depuis quand, ses doublons de fin de partie (« doublée par », puis
« faite par » et « battue ») ; les places qu'annonce chaque machine qui demande du travail,
gardées bornées ; et, pour « Machines connectées », ce que cette machine fait avec le réseau
public : le modèle et la durée, jamais le texte ni une clé (jobs/chantier.py,
network/places_seen.py, network/activity.py)."""
import json
import shutil
import subprocess
import threading
import time
import types
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aiodos import db, paths
from aiodos.api.server import create_app
from aiodos.jobs import chantier, endgame, leases, queue, registry
from aiodos.jobs.models import NORMAL
from aiodos.jobs.registry import UnitSpec
from aiodos.network import (activity, lend_queue, peers, placement, places_seen, public,
                            public_chat, public_lending, sharing, work)
from aiodos.services import ollama
from tests import i18n_js
from tests.test_work_leases import B, _caps, owner  # noqa: F401  (owner : fixture)

NODE = shutil.which("node")
HERE = "noeud-ici"
MINE, OTHER, FRIEND = "machine-fixe", "machine-portable", "machine-ami"
SECRET = "Mon numéro de compte est le 12345"
LENDER = "Kq" * 26                       # la clé d'un prêteur du réseau public


def _split(params):
    return "chantier", [UnitSpec("chantier_task", "cpu", {"source": f"partie-{i}.txt"})
                        for i in range(params["count"])], {}


registry.register_job_kind("chantier_kind", _split)
registry.register_portable("chantier_task", compute=lambda payload, file, options, stop: {},
                           assimilate=lambda payload, output: output)


@pytest.fixture(autouse=True)
def fresh():
    places_seen.forget()
    with endgame._beaten_lock:
        endgame._beaten.clear()
    yield
    places_seen.forget()
    placement.forget()
    public.forget()
    public_lending.forget()


@pytest.fixture
def machines():
    peers.save(MINE, "PC fixe", b"m" * 32, "127.0.0.1", 1)
    peers.save(OTHER, "Vieux portable", b"o" * 32, "127.0.0.1", 2)
    peers.save(FRIEND, "PC de Léa", b"f" * 32, "127.0.0.1", 3, trust=sharing.FRIEND)


@pytest.fixture
def later(monkeypatch):
    """Les unités en calcul ont l'air de tourner depuis plus longtemps (fin de partie)."""
    monkeypatch.setattr(endgame, "time", types.SimpleNamespace(
        time=lambda: time.time() + endgame.ENDGAME_COLD_S + 5))


def _job(count):
    return queue.create_job("chantier_kind", {"count": count}, NORMAL)


def _lend(node, circle="mine"):
    return leases.claim_remote({"cpu"}, node, lambda unit: True, circle=circle)


def _units(site) -> dict:
    return {u["n"]: u for u in site["units"]}


# ------------------------------------------------------------ fragments, doublons
def test_each_fragment_says_its_machine_and_its_endgame_copies(machines, later):
    job = _job(3)
    first = _lend(MINE)                                    # fragment 1 : une de mes machines
    here = queue.claim_next({"cpu"}, HERE)                 # fragment 2 : cette machine
    site = chantier.view(job, HERE)
    units = _units(site)
    assert units[1]["status"] == "running" and units[1]["label"] == "partie-0.txt"
    assert units[1]["machine"] == {"kind": "mine", "name": "PC fixe", "id": MINE}
    assert units[1]["copies"] == [] and units[1]["started"] > 0
    assert units[2]["machine"] == {"kind": "here"} and here["id"] == units[2]["id"]
    assert units[3]["status"] == "queued" and units[3]["since"] > 0
    assert site["counts"] == {"running": 2, "queued": 1} and site["hidden"] == {}
    assert [e["machine"]["kind"] for e in site["machines"]] == ["here", "mine"]

    _lend(FRIEND, circle="friends")                        # fragment 3 : un ami
    copy = endgame.claim_remote({"cpu"}, OTHER, lambda unit: True, leases.LEASE_S)
    assert copy["id"] == first["id"]                       # le plus ancien traîne : doublé
    summary = queue.job_summary(job)
    assert summary["copies"] == 1 and summary["copies_remote"] == {OTHER: 1}
    assert summary["remote"] == {MINE: 1, FRIEND: 1}       # un doublon n'est pas une unité de plus
    units = _units(chantier.view(job, HERE))
    assert units[3]["machine"] == {"kind": "friend", "name": "PC de Léa", "id": FRIEND}
    assert [c["machine"]["name"] for c in units[1]["copies"]] == ["Vieux portable"]

    # Le doublon gagne : le fragment est fait par lui, le calcul de PC fixe est battu
    assert leases.take_back(first["id"], OTHER) is not None
    queue.finish_unit(first["id"], {}, 2.5)
    site = chantier.view(job, HERE)
    done = _units(site)[1]
    assert done["status"] == "done" and done["compute_s"] == 2.5
    assert done["machine"] == {"kind": "mine", "name": "Vieux portable", "id": OTHER}
    assert done["beaten"] == [{"kind": "mine", "name": "PC fixe", "id": MINE}]
    assert queue.job_summary(job)["copies"] == 0
    assert site["typical"] == 2.5
    assert all(u["typical"] == 2.5 for u in site["units"] if u["status"] == "running")


def test_a_stranger_is_never_named_and_a_machine_gone_is_another_one(machines):
    job = _job(2)
    stranger, gone = _lend(MINE), _lend(OTHER)
    db.execute("UPDATE units SET place='public', node=? WHERE id=?", ("cle-publique", stranger["id"]))
    peers.remove(OTHER)                                     # plus appairée depuis
    site = chantier.view(job, HERE)
    units = {u["id"]: u for u in site["units"]}
    assert units[stranger["id"]]["machine"] == {"kind": "public"}
    assert units[gone["id"]]["machine"] == {"kind": "other"}
    assert "cle-publique" not in json.dumps(site)
    public_entry = next(e for e in site["machines"] if e["machine"]["kind"] == "public")
    assert public_entry["places"] is None                   # ses places ne se disent pas


def test_a_big_job_shows_only_what_is_visible_in_fragment_order(machines):
    job = _job(100)
    for _ in range(20):                                     # 20 faites ici
        unit = queue.claim_next({"cpu"}, HERE)
        queue.finish_unit(unit["id"], {}, 1.0)
    for node in (MINE, OTHER):                              # 2 en calcul ailleurs
        _lend(node)
    site = chantier.view(job, HERE)
    shown = [u["status"] for u in site["units"]]
    assert shown.count("running") == 2
    assert shown.count("done") == chantier.DONE_SHOWN
    assert shown.count("queued") == chantier.QUEUED_SHOWN
    assert site["hidden"] == {"done": 20 - chantier.DONE_SHOWN,
                              "queued": 78 - chantier.QUEUED_SHOWN}
    assert [u["n"] for u in site["units"]] == sorted(u["n"] for u in site["units"])
    assert [u["n"] for u in site["units"] if u["status"] == "done"] == list(range(9, 21))
    assert site["resources"] == ["cpu"] and site["typical"] == 1.0
    assert chantier.view(123456, HERE) is None


# ------------------------------------------------------------------ les places
def test_places_said_by_another_machine_are_kept_bounded(monkeypatch):
    assert places_seen.note(MINE, {"places": {"gpu": 1, "cpu": 4, "embed": "<img>", "net": -1,
                                              "disque": 3, "x": True}, "busy": {"cpu": 9}})
    seen = places_seen.get(MINE)
    assert seen["places"] == {"gpu": 1, "cpu": 4} and seen["busy"] == {"gpu": 0, "cpu": 4}
    assert not places_seen.note(OTHER, {"work_api": 1})            # une version d'avant
    assert not places_seen.note(OTHER, {"places": {"gpu": True, "cpu": 10 ** 6}})
    assert places_seen.get(OTHER) is None
    places_seen.took(MINE, "gpu")
    places_seen.took(MINE, "gpu")                                  # jamais plus que ses places
    assert places_seen.get(MINE)["busy"]["gpu"] == 1
    later = time.time() + places_seen.TTL + 1
    monkeypatch.setattr(places_seen, "time", types.SimpleNamespace(time=lambda: later))
    assert places_seen.get(MINE) is None                          # elle ne demande plus rien


def test_a_machine_asking_for_work_tells_its_places_seen_in_the_site(owner):
    caps = {**_caps(), "places": {"gpu": 1, "embed": 2, "cpu": 4, "net": 3},
            "busy": {"embed": 1}}
    order = work.lease(owner["manager"], B, {"resources": ["embed"], "caps": caps})["unit"]
    assert order
    seen = places_seen.get(B)
    assert seen["places"] == {"gpu": 1, "embed": 2, "cpu": 4, "net": 3}
    assert seen["busy"] == {"gpu": 0, "embed": 2, "cpu": 0, "net": 0}   # + l'unité confiée
    site = chantier.view(owner["job"], HERE, {"slots": {"embed": 2}, "busy": {"embed": 0}})
    entry = next(e for e in site["machines"] if e["machine"].get("id") == B)
    assert entry["units"] == 1 and entry["places"]["busy"]["embed"] == 2
    assert site["resources"] == ["embed"]
    paired = peers.list_peers()
    activity.annotate(paired, None)
    live = next(p["live"] for p in paired if p["node_id"] == B)
    assert live["places"]["places"]["embed"] == 2 and live["copies_for_me"] == []
    activity.forget(B)                                    # machine retirée : ses places aussi
    assert places_seen.get(B) is None


def test_copies_computed_for_me_are_seen_live(machines, later):
    job = _job(1)
    _lend(MINE)
    assert endgame.claim_remote({"cpu"}, OTHER, lambda unit: True, leases.LEASE_S)
    paired = peers.list_peers()
    activity.annotate(paired, None)
    live = {p["node_id"]: p["live"] for p in paired}
    assert live[OTHER]["copies_for_me"] == [{"title": "chantier", "units": 1}]
    assert live[OTHER]["for_me"] == [] and live[MINE]["for_me"] == [{"title": "chantier", "units": 1}]
    assert queue.job_summary(job)["copies_remote"] == {OTHER: 1}


# ------------------------------------------------------------- le réseau public
def test_a_conversation_served_to_the_public_shows_its_model_and_duration_only(monkeypatch):
    monkeypatch.setattr(ollama, "chat_stream",
                        lambda *a, **k: iter([("content", "Réponse confidentielle")]))
    ticket = lend_queue.take_now(lend_queue.PUBLIC)
    asked = {"model": "qwen3.5:9b", "messages": [{"role": "user", "content": SECRET}],
             "options": {}, "place": public_lending._Place(ticket)}
    events = public_lending.answer(SimpleNamespace(), asked, threading.Event())
    assert next(events)["type"] == "meta"
    live = activity.public_live()
    assert [s["model"] for s in live["serving"]] == ["qwen3.5:9b"]
    assert abs(live["serving"][0]["since"] - time.time()) < 30
    assert set(live["serving"][0]) == {"model", "since"}
    assert SECRET not in json.dumps(live, ensure_ascii=False)
    assert list(events)[-1]["type"] == "done"
    assert activity.public_live()["serving"] == []          # finie : plus rien
    assert lend_queue.idle()


def test_my_conversation_answered_by_the_public_shows_the_model_never_the_key():
    public._remember({"enabled": True, "key": "ma-cle", "nodes": [
        {"key": LENDER, "models": ["qwen3.5:9b", "nomic-embed-text"], "seen_s": 0, "lends": True}]})
    assert activity.public_live()["asking"] == []
    with placement._occupy(public_chat.model_id(LENDER, "qwen3.5:9b"), None):
        live = activity.public_live()
        assert live["asking"] == [{"model": "qwen3.5:9b", "n": 1}]
        assert LENDER not in json.dumps(live)
        # Deux modèles de conversation chez ce prêteur : on ne dit pas lequel
        public._remember({"enabled": True, "key": "ma-cle", "nodes": [
            {"key": LENDER, "models": ["qwen3.5:9b", "gemma3:4b"], "seen_s": 0, "lends": True}]})
        assert activity.public_live()["asking"] == [{"model": None, "n": 1}]
    assert activity.public_live()["asking"] == []


# -------------------------------------------------------------------- l'API
AUTH = {"X-Aiodos-Token": "jeton"}


def test_the_site_and_the_public_activity_through_the_interface_api():
    with TestClient(create_app("jeton", {"testserver"})) as client:
        job = _job(2)
        site = client.get(f"/api/jobs/{job}/chantier", headers=AUTH)
        assert site.status_code == 200
        body = site.json()
        assert body["job"] == job and body["total"] == 2 and len(body["units"]) == 2
        assert {"machines", "resources", "hidden", "now", "counts"} <= set(body)
        assert client.get("/api/jobs/987654/chantier", headers=AUTH).status_code == 404
        network = client.get("/api/network", headers=AUTH).json()
        assert network["public_live"] == {"serving": [], "asking": []}
        listed = client.get("/api/jobs", headers=AUTH).json()["jobs"]
        assert all("copies" in j and "copies_remote" in j for j in listed)


# ------------------------------------------------------------ la vue (Node.js)
HOSTILE = "<img src=x onerror=alert(1)>"


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_site_view_escapes_what_comes_from_other_machines(tmp_path):
    """Un nom de machine, un nom de fichier ou une erreur viennent d'ailleurs : échappés."""
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    static = paths.STATIC_DIR / "js"
    for name in ("dom.js", "icons.js", "state.js", "views/chantier.js"):
        (js / name).write_bytes((static / name).read_bytes())
    (js / "api.js").write_text("export const get = async () => ({});\n", encoding="utf-8")
    prelude = i18n_js.install(js, module="./js/i18n.js")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    now = 1_000_000.0
    hostile = {"kind": "mine", "name": HOSTILE, "id": "x"}
    data = {
        "units": [
            {"n": 1, "status": "running", "label": HOSTILE, "machine": hostile, "started": now - 75,
             "typical": 100, "stage": "receiving",
             "copies": [{"machine": {"kind": "here"}, "started": now - 20}]},
            {"n": 2, "status": "done", "label": "", "machine": {"kind": "public"},
             "compute_s": 42, "beaten": [hostile]},
            {"n": 3, "status": "failed", "machine": {"kind": "other"}, "error": HOSTILE},
            {"n": 4, "status": "<b>", "label": "x"},
        ],
        "machines": [{"machine": hostile, "units": 1, "copies": 0,
                      "places": {"places": {"cpu": 4, "gpu": "<i>"}, "busy": {"cpu": 2}, "at": now}}],
        "resources": ["cpu"], "hidden": {"queued": 5, "<x>": 3},
    }
    (tmp_path / "run.js").write_text(
        prelude + "import { chantierHtml } from './js/views/chantier.js';\n"
        "const [data, now] = JSON.parse(process.argv[2]);\n"
        "console.log(JSON.stringify(chantierHtml(data, now)));\n", encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "run.js"), json.dumps([data, now])],
                          capture_output=True, text=True, encoding="utf-8", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    html = json.loads(done.stdout)
    assert "<img" not in html and "&lt;img" in html
    assert "cu-<b>" not in html and 'cu cu-cancelled' in html       # un état inconnu : annulée
    assert "Sur « &lt;img" in html and "depuis 1 min 15" in html and "environ 75 %" in html
    assert "réception du résultat" in html and "Doublée par cette machine" in html
    assert "Faite par une machine du réseau public" in html and "en 42 s" in html
    assert "battue : « &lt;img" in html and "Fragment 2" in html
    assert "En échec chez une autre machine" in html
    assert html.count('class="busy"') == 2 and "Processeur 2/4" in html
    assert "Non montrés : 5 en file" in html and "&lt;x&gt;" not in html
