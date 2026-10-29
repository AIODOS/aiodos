"""L'historique du travail fait ici pour d'autres (network/lent_history.py) et sa route
(api/routes/lent.py), pour la vue Tâches, « Pour d'autres » (demandé par l'auteur le
29/10/2026) : une ligne par calcul fini, refusé ou arrêté, écrite par chacun des prêts (les
conversations, lectures de contrôle, images et vidéos du réseau public ; les unités de mes
machines et de mes amis ; les conversations et lectures servies par mes modèles). Jamais le
contenu, ni rien qui identifie un demandeur du réseau public ; 90 jours et 5 000 lignes au
plus ; écrite en fond, sans jamais faire échouer ni attendre le calcul. Sans réseau ni moteur
(moteurs factices des essais de chaque prêt)."""
import logging
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from aiodos import db, settings
from aiodos.api.server import create_app
from aiodos.imaging import engine as images_engine
from aiodos.imaging import generate
from aiodos.jobs import registry
from aiodos.network import (lend_queue, lend_reading, lending, lent_history, p2p_app, peers,
                            public_imaging, public_lending, public_video, sharing, worker)
from aiodos.services import ollama
from tests.test_lend_reading import _read_body, reader  # noqa: F401 (fixture)
from tests.test_public_imaging import FORBIDDEN, lend_images, lender, sd  # noqa: F401 (fixtures)
from tests.test_public_imaging import ask as image_ask
from tests.test_public_lending import (FORWARD, HEADERS, HERE, KEY, MODEL, SECRET, FakeManager,
                                       _long, ask, engine, events, lend, node, post)  # noqa: F401
from tests.test_worker import OWNER, setup  # noqa: F401 (fixture)

TOKEN = "jeton"
AUTH = {"X-Aiodos-Token": TOKEN}


def rows(**where) -> list:
    """Les lignes écrites (après celles en attente), dans l'ordre, filtrées par colonne."""
    assert lent_history.flush()
    found = [dict(r) for r in db.query("SELECT * FROM lent_history ORDER BY id")]
    return [r for r in found if all(r[key] == value for key, value in where.items())]


def nothing_in_the_base(*secrets):
    """Ni le contenu, ni la clé jetable de l'appelant, dans aucune table de la base."""
    assert lent_history.flush()
    for table in db.query("SELECT name FROM sqlite_master WHERE type='table'"):
        for stored in db.query(f'SELECT * FROM "{table["name"]}"'):
            text = repr(tuple(stored))
            for secret in (KEY, *secrets):
                assert secret not in text, (table["name"], secret)


# ========================================================== le réseau public
def test_a_public_conversation_leaves_one_line_without_its_text(node):
    manager, here, lan, engine = node
    lend()
    assert events(here)[-1]["type"] == "done"
    (row,) = rows()
    assert (row["kind"], row["circle"], row["peer"], row["model"]) ==         ("chat", "public", None, MODEL)
    assert (row["outcome"], row["reason"], row["tokens"], row["times"]) == ("done", None, 3, 1)
    assert row["seconds"] is not None and row["seconds"] >= 0
    assert abs(row["at"] - time.time()) < 60
    assert row["width"] is row["height"] is row["frames"] is row["passages"] is None
    nothing_in_the_base("Zébulon", "Hippolyte", "Bonjour", "Sois bref")
    assert lent_history.live() == []


def test_public_refusals_keep_their_reason_and_only_a_model_checked_here(node):
    """Le modèle d'un refus n'est écrit qu'une fois vérifié (installé ici) : un nom demandé
    puis refusé pourrait être n'importe quel texte de l'inconnu. Deux refus identiques font
    une ligne."""
    manager, here, lan, engine = node
    assert post(here).json() == {"error": "not_lending"}
    lend()
    assert post(here, {**ask(), "model": "Hippolyte-Zébulon:1b"}).json() == {"error": "no_model"}
    manager.executor.interactive = 1                       # l'utilisateur converse ici
    assert post(here).json() == {"error": "busy"}
    assert post(here).json() == {"error": "busy"}
    manager.executor.interactive = 0
    found = {r["reason"]: r for r in rows(outcome="refused")}
    assert set(found) == {"not_lending", "no_model", "busy"}
    assert found["not_lending"]["model"] is None and found["no_model"]["model"] is None
    assert found["busy"]["model"] == MODEL and found["busy"]["times"] == 2
    assert all(r["kind"] == "chat" and r["circle"] == "public" and r["peer"] is None
               for r in found.values())
    assert engine.calls == []
    nothing_in_the_base("Hippolyte", "Zébulon")


def test_a_public_conversation_stopped_by_the_user_is_noted_stopped(node):
    manager, here, lan, engine = node
    lend()
    _long(engine)
    engine.during[2] = manager.executor.interactive_begin    # l'utilisateur converse ici
    assert events(here)[-1] == {"type": "error", "error": "busy"}
    manager.executor.interactive_end()
    (row,) = rows()
    assert (row["outcome"], row["reason"]) == ("stopped", "busy")
    assert 3 <= row["tokens"] < 300


def test_a_public_reading_for_a_check(node, reader, monkeypatch):  # noqa: F811
    manager, here, lan, engine = node
    lend()
    with here.stream("POST", lend_reading.PATH, json=_read_body(), headers=HEADERS) as response:
        assert response.status_code == 200
        assert [line for line in response.iter_lines() if line]
    monkeypatch.setattr(ollama, "use_integrated", lambda: False)     # Ollama ne lit pas
    assert here.post(lend_reading.PATH, json=_read_body(), headers=HEADERS).json() \
        == {"error": "no_reading"}
    done, refused = rows()
    assert (done["kind"], done["circle"], done["model"], done["outcome"]) == \
        ("reading", "public", MODEL, "done")
    assert done["tokens"] == 4                              # n du seul texte lu
    assert (refused["kind"], refused["outcome"], refused["reason"], refused["model"]) == \
        ("reading", "refused", "no_reading", None)
    nothing_in_the_base("Réponse A", "Zébulon")


def test_a_public_image_and_a_description_refused_by_the_filter(lender):  # noqa: F811
    manager, here, lan, sd = lender
    lend_images()
    with here.stream("POST", public_imaging.PATH, json=image_ask(),
                     headers=HEADERS) as response:
        assert response.status_code == 200
        assert '"done"' in "".join(response.iter_lines())
    response = here.post(public_imaging.PATH, json=image_ask(prompt=FORBIDDEN), headers=HEADERS)
    assert response.json() == {"error": "refused_content"}
    done, refused = rows()
    assert (done["kind"], done["circle"], done["model"], done["outcome"]) == \
        ("image", "public", "z-image-turbo", "done")
    assert (done["width"], done["height"], done["seconds"]) == (256, 256, 0.4)
    assert (refused["kind"], refused["outcome"], refused["reason"], refused["model"]) == \
        ("image", "refused", "refused_content", "z-image-turbo")
    nothing_in_the_base("Zébulon", "Hippolyte", "naked")


def test_a_public_video(monkeypatch):
    videos = pytest.importorskip("tests.test_public_videos")
    fake = videos.Vd()
    monkeypatch.setattr(generate, "video", fake)
    monkeypatch.setattr(images_engine, "loaded", lambda: fake.loaded)
    monkeypatch.setattr(public_video, "_ready", lambda model: model == videos.MODEL)
    here = TestClient(p2p_app.create_p2p_app(FakeManager()), client=HERE, base_url=FORWARD)
    lend_images()
    try:
        with here.stream("POST", public_video.PATH, json=videos.ask(),
                         headers=HEADERS) as response:
            assert response.status_code == 200
            assert '"done"' in "".join(response.iter_lines())
    finally:
        public_lending.forget()
    (row,) = rows()
    assert (row["kind"], row["circle"], row["model"], row["outcome"]) == \
        ("video", "public", videos.MODEL, "done")
    assert (row["width"], row["height"], row["frames"]) == (videos.SIDE, videos.SIDE,
                                                            videos.FRAMES)
    nothing_in_the_base("Zébulon", "Hippolyte")


# ===================================================== mes machines, mes amis
def test_a_unit_for_one_of_my_machines(setup, monkeypatch):  # noqa: F811
    """Son nom, le genre, le modèle, les passages ; jamais le fichier ni son nom."""
    def compute(payload, file, options, stop):
        return {"pieces": ["un", "deux"], "vectors": np.ones((2, 4), dtype=np.float32)}
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", compute)
    assert setup["lender"]._process(setup["assignment"]).startswith("faite")
    (row,) = rows()
    assert (row["kind"], row["circle"], row["peer"], row["model"]) == \
        ("index", "mine", "PC portable", "nomic-embed-text")
    assert (row["outcome"], row["passages"]) == ("done", 2) and row["seconds"] >= 0
    for stored in db.query("SELECT * FROM lent_history"):
        assert "note.txt" not in repr(tuple(stored)) and "crachin" not in repr(tuple(stored))
    assert lent_history.live() == []


def test_a_unit_given_back_on_the_way_is_stopped(setup, monkeypatch):  # noqa: F811
    def interrupted(payload, file, options, stop):
        raise registry.Interrupted()                 # l'utilisateur revient pendant le calcul
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", interrupted)
    assert setup["lender"]._process(setup["assignment"]) == "interrompue et rendue"
    (row,) = rows()
    assert (row["circle"], row["outcome"], row["reason"]) == ("mine", "stopped", "interrupted")


def test_a_unit_of_a_friend_refused_here_is_noted_refused(setup, monkeypatch):  # noqa: F811
    peers.set_trust(OWNER, sharing.FRIEND)
    monkeypatch.setattr(worker, "_media_to_index", lambda order: True)   # une image à indexer
    assert setup["lender"]._process(setup["assignment"]) == "réservée à mes machines : rendue"
    (row,) = rows()
    assert (row["circle"], row["peer"], row["outcome"], row["reason"]) == \
        ("friend", "PC portable", "refused", "mine_only")


def test_reading_an_order_never_fails_the_unit(monkeypatch):
    from aiodos.imaging import catalog

    def broken(model):
        raise KeyError(model)
    monkeypatch.setattr(catalog, "is_video", broken)
    order = {"task": "generate_image", "payload": {"model": "z-image-turbo"}}
    assert lent_history.unit_fields(order) == {"kind": "unit", "model": None}
    assert lent_history.unit_sizes({"task": "index_file"}, {"pieces": 3}) == {}
    monkeypatch.setattr(sharing, "level", broken)
    assert lent_history.circle_of("fixe") == "friend"            # le plus étroit


def test_the_kind_and_model_of_a_unit_come_only_from_what_is_checked_here():
    image = {"task": "generate_image", "payload": {"model": "z-image-turbo", "width": 512,
                                                   "height": 768, "prompt": SECRET}}
    assert lent_history.unit_fields(image) == {"kind": "image", "model": "z-image-turbo"}
    assert lent_history.unit_sizes(image) == {"width": 512, "height": 768, "frames": None}
    video = {"task": "generate_image", "payload": {"model": "wan2.1-t2v-1.3b", "frames": 33}}
    assert lent_history.unit_fields(video) == {"kind": "video", "model": "wan2.1-t2v-1.3b"}
    unknown = {"task": "generate_image", "payload": {"model": SECRET}}
    assert lent_history.unit_fields(unknown) == {"kind": "image", "model": None}
    assert lent_history.unit_fields({"task": "index_url", "options": {"embed_model": "bge-m3"},
                                     "payload": {"url": "https://exemple.fr/"}}) == \
        {"kind": "page", "model": "bge-m3"}
    assert lent_history.unit_fields({"task": "engine_unit"})["kind"] == "engine"
    assert lent_history.unit_fields({"task": "autre"})["kind"] == "unit"
    assert lent_history.unit_fields("pas un ordre") == {"kind": "unit", "model": None}
    assert lent_history.unit_sizes({"task": "index_file"}, {"pieces": ["a", "b", "c"]}) == \
        {"passages": 3}


def test_a_conversation_lent_to_a_friend(engine):  # noqa: F811
    peers.save("ami-1", "PC de Pierre", b"k" * 32, "127.0.0.1", 3, trust=sharing.FRIEND)
    settings.set_value("lend_models", True)
    manager = FakeManager()
    payload = {"model": MODEL, "messages": [{"role": "user", "content": SECRET}]}
    chunks = list(lending.chat(manager, "ami-1", payload, threading.Event()))
    assert {"content": "Bon"} in chunks
    (row,) = rows()
    assert (row["kind"], row["circle"], row["peer"], row["model"]) == \
        ("chat", "friend", "PC de Pierre", MODEL)
    assert (row["outcome"], row["tokens"]) == ("done", 3)
    # Arrêtée en route (bouton Stop chez elle) : arrêtée
    stop = threading.Event()
    for chunk in lending.chat(manager, "ami-1", payload, stop):
        stop.set()
    assert (rows()[-1]["outcome"], rows()[-1]["reason"]) == ("stopped", "left")
    nothing_in_the_base("Zébulon", "Hippolyte", "Bonjour")


def test_a_reading_for_one_of_my_machines(reader, monkeypatch):  # noqa: F811
    peers.save("fixe", "PC fixe", b"k" * 32, "127.0.0.1", 4)
    settings.set_value("lend_models", True)
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [{"name": MODEL}])
    payload = {"model": MODEL, "messages": [{"role": "user", "content": SECRET}],
               "texts": ["A", "B"]}
    assert list(lend_reading.read(FakeManager(), "fixe", payload, threading.Event()))
    (row,) = rows()
    assert (row["kind"], row["circle"], row["peer"], row["outcome"], row["tokens"]) == \
        ("reading", "mine", "PC fixe", "done", 4)


# ============================================================ les garde-fous
def test_never_a_machine_nor_a_name_for_the_public_network():
    lent_history.record("chat", "public", "done", peer_id="cle-du-noeud", peer="Hippolyte",
                        model="qwen3.5:9b", seconds=2.0, tokens=12)
    lent_history.record("chat", "voisins", "done")                  # cercle inconnu : rien
    lent_history.record("chat", "public", "done", reason="Le texte de l'inconnu")
    lent_history.record("chat", "public", "done", model="nom\nsur deux lignes")
    first, second, third = rows()
    assert first["peer"] is None and first["model"] == "qwen3.5:9b" and first["tokens"] == 12
    assert second["reason"] is None and third["model"] is None       # pas un code, pas un nom
    nothing_in_the_base("cle-du-noeud", "Hippolyte")


def test_the_history_keeps_90_days_and_5000_lines_at_most():
    assert (lent_history.MAX_DAYS, lent_history.MAX_ROWS) == (90, 5000)
    now = time.time()
    lent_history.record("image", "public", "done", at=now - 91 * 86400, seconds=1.0)
    lent_history.record("image", "public", "done", at=now - 89 * 86400, seconds=1.0)
    assert [round((now - r["at"]) / 86400) for r in rows()] == [89]
    columns = "at, kind, circle, model, seconds, outcome"
    with db.transaction() as conn:
        conn.executemany(f"INSERT INTO lent_history({columns}) VALUES (?, ?, ?, ?, ?, ?)",
                         [(now - 3600 + i * 0.1, "chat", "public", MODEL, 1.0, "done")
                          for i in range(5100)])
    first = db.query_one("SELECT MIN(id) AS id FROM lent_history")["id"]
    lent_history.record("video", "friend", "done", peer="PC de Pierre")
    assert lent_history.flush()
    assert db.query_one("SELECT COUNT(*) AS n FROM lent_history")["n"] == 5000
    assert db.query_one("SELECT MIN(id) AS id FROM lent_history")["id"] > first + 100
    newest = db.query_one("SELECT * FROM lent_history ORDER BY id DESC LIMIT 1")
    assert (newest["kind"], newest["peer"]) == ("video", "PC de Pierre")


def test_refusals_never_push_out_the_work_done():
    """Des refus à la chaîne (MAX_REFUSED au plus) ne chassent pas les calculs faits."""
    now = time.time()
    with db.transaction() as conn:
        conn.executemany("INSERT INTO lent_history(at, kind, circle, outcome, reason) "
                         "VALUES (?, 'chat', 'public', ?, ?)",
                         [(now - 7200, "done", None)] * 10
                         + [(now - 7200, "refused", "busy")] * lent_history.MAX_REFUSED)
    lent_history.refused("image", "public", "refused_content", model="z-image-turbo")
    assert lent_history.flush()
    count = {r["outcome"]: r["n"] for r in db.query(
        "SELECT outcome, COUNT(*) AS n FROM lent_history GROUP BY outcome")}
    assert count == {"done": 10, "refused": lent_history.MAX_REFUSED}
    assert rows()[-1]["reason"] == "refused_content"


def test_identical_refusals_or_stops_make_one_line_and_work_done_never_does():
    now = time.time()
    for _ in range(3):
        lent_history.refused("image", "public", "refused_content", model="z-image-turbo")
    lent_history.refused("image", "public", "busy", model="z-image-turbo")
    lent_history.record("chat", "public", "stopped", "left", model=MODEL, seconds=1.5)
    lent_history.record("chat", "public", "stopped", "left", model=MODEL, seconds=2.0)
    lent_history.record("chat", "public", "done", model=MODEL, seconds=1.0)
    lent_history.record("chat", "public", "done", model=MODEL, seconds=1.0)
    # Le même refus, plus d'une heure après le précédent : une autre ligne
    lent_history.record("video", "public", "refused", "busy", at=now - 2 * lent_history.MERGE_S)
    lent_history.refused("video", "public", "busy")
    found = rows()
    assert [(r["kind"], r["outcome"], r["reason"], r["times"]) for r in found] == [
        ("image", "refused", "refused_content", 3), ("image", "refused", "busy", 1),
        ("chat", "stopped", "left", 2), ("chat", "done", None, 1), ("chat", "done", None, 1),
        ("video", "refused", "busy", 1), ("video", "refused", "busy", 1)]
    assert found[2]["seconds"] == 3.5


def test_a_failed_or_slow_write_never_stops_nor_waits_the_computation(node, monkeypatch,
                                                                       caplog):
    manager, here, lan, engine = node
    lend()
    held, released = threading.Event(), threading.Event()
    real = lent_history._write

    def slow(*args):
        held.set()
        released.wait(10)
        return real(*args)
    monkeypatch.setattr(lent_history, "_write", slow)
    assert events(here)[-1]["type"] == "done"                 # l'écriture attend encore
    assert held.wait(5) and not released.is_set()
    released.set()
    assert len(rows()) == 1

    def broken(*args):
        raise OSError("disque plein")
    monkeypatch.setattr(lent_history, "_write", broken)
    with caplog.at_level(logging.DEBUG, logger="aiodos.net"):
        assert events(here)[-1]["type"] == "done"
        assert lent_history.flush()
    assert "Historique du travail pour d'autres : écriture impossible" in caplog.text
    assert len(rows()) == 1
    assert lend_queue.idle()


def test_what_is_computed_right_now_and_the_interface_is_told():
    heard = []
    lent_history.listen(heard.append)
    try:
        lent = lent_history.begin("image", "friend", peer="PC de Pierre", model="z-image-turbo")
        public = lent_history.begin("chat", "public", peer_id="cle", peer="Hippolyte", model=MODEL)
        assert [(item["kind"], item["circle"], item["peer"], item["model"])
                for item in lent_history.live()] == [
            ("image", "friend", "PC de Pierre", "z-image-turbo"),
            ("chat", "public", None, MODEL)]
        assert heard == [{"type": "lent"}] * 2
        lent.end("done", seconds=3.0, width=512, height=512)
        lent.end("failed")                                     # une seule fois
        public.end("stopped", "busy")
        assert lent_history.flush()
        assert lent_history.live() == []
        assert len(rows()) == 2 and rows()[0]["outcome"] == "done"
        assert len(heard) >= 3                                 # et après l'écriture
    finally:
        lent_history.listen(None)


# ================================================================ la route
def test_the_route_pages_the_history_and_totals_it_by_circle():
    now = time.time()
    midnight = lent_history._midnight(now)
    today = max(midnight + 1, now - 30)
    for _ in range(3):
        lent_history.record("chat", "public", "done", model=MODEL, seconds=10.0, at=today)
    lent_history.record("image", "friend", "done", peer="PC de Pierre", seconds=20.0, at=today)
    lent_history.record("index", "mine", "stopped", "interrupted", peer="PC portable",
                        seconds=5.0, at=today)
    lent_history.record("image", "public", "refused", "refused_content", at=today)
    lent_history.record("video", "public", "done", seconds=100.0,
                        at=lent_history._midnight(now, 3) + 3600)       # il y a 3 jours
    lent_history.record("video", "public", "done", seconds=1000.0,
                        at=lent_history._midnight(now, 8) + 3600)       # il y a 8 jours
    assert lent_history.flush()
    with TestClient(create_app(TOKEN, {"testserver"})) as client:
        assert client.get("/api/lent").status_code == 401
        body = client.get("/api/lent", headers=AUTH).json()
        totals = body["totals"]
        assert totals["today"]["public"] == {"n": 3, "seconds": 30.0}
        assert totals["today"]["friend"] == {"n": 1, "seconds": 20.0}
        assert totals["today"]["mine"] == {"n": 0, "seconds": 5.0}
        assert totals["today"]["all"] == {"n": 4, "seconds": 55.0}
        assert totals["week"]["public"] == {"n": 4, "seconds": 130.0}
        assert totals["week"]["all"] == {"n": 5, "seconds": 155.0}
        assert totals["since"]["today"] == midnight
        assert body["live"] == [] and body["more"] is False
        # Les plus récents d'abord, par leur fin (la vidéo d'il y a 8 jours, notée la
        # dernière, vient en dernier), puis par leur identifiant
        assert [r["kind"] for r in body["items"]] == \
            ["image", "index", "image", "chat", "chat", "chat", "video", "video"]
        assert set(body["items"][0]) == {
            "id", "at", "kind", "circle", "peer", "model", "seconds", "tokens", "width",
            "height", "frames", "passages", "outcome", "reason", "times"}
        # Par pages, les plus récents d'abord ; les totaux et le direct avec la première
        first = client.get("/api/lent?limit=3", headers=AUTH).json()
        assert len(first["items"]) == 3 and first["more"] is True
        after = client.get(f"/api/lent?limit=3&before={first['items'][-1]['id']}",
                           headers=AUTH).json()
        assert [r["id"] for r in after["items"]] == [r["id"] for r in body["items"][3:6]]
        assert "totals" not in after and "live" not in after
        # Une ligne effacée (ou réunie) entre-temps : la page part de son identifiant
        gone = client.get("/api/lent?before=999999", headers=AUTH).json()
        assert [r["id"] for r in gone["items"]] == [r["id"] for r in body["items"]]
        for bad in ("limit=0", "limit=201", "before=0", "before=x"):
            assert client.get(f"/api/lent?{bad}", headers=AUTH).status_code == 422
        going = lent_history.begin("video", "public", model="wan2.1-t2v-1.3b")
        live = client.get("/api/lent", headers=AUTH).json()["live"]
        assert [(x["kind"], x["circle"], x["peer"]) for x in live] == [("video", "public", None)]
        going.end("done")
