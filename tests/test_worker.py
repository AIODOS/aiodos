"""La machine qui prête sa puissance : conditions de partage, unité rendue quand
l'utilisateur revient ou qu'Ollama ne répond pas, échec signalé, fichier reçu
vérifié puis effacé, formats lisibles annoncés. Sans réseau : les appels vont
directement au propriétaire."""
import importlib.util

import numpy as np
import pytest
import requests

from aiodos import db, paths, settings
from aiodos.jobs import kinds, queue, registry  # noqa: F401  (enregistre les tâches)
from aiodos.knowledge import store
from aiodos.network import peers, work, worker
from aiodos.network.client import PeerError
from aiodos.network.worker import Assignment, Worker, sharing_state
from aiodos.node import identity, power
from aiodos.services import ollama

OWNER, LENDER = "machine-a", "machine-b"


class OwnerExecutor:
    def settle(self, job_id, finished):
        if finished:
            queue.finalize_job(job_id)

    def publish_job(self, job_id, force=False):
        pass

    def wake(self):
        pass


class OwnerManager:
    executor = OwnerExecutor()

    def changed(self):
        pass


class LenderExecutor:
    interactive = 0

    def __init__(self):
        self.cooldown = {}


class LenderManager:
    node_id = LENDER

    def __init__(self):
        self.executor = LenderExecutor()

    def changed(self):
        pass


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """Propriétaire et machine qui prête partagent ici la même base : leurs
    écritures ne se croisent pas (comptes tenus sous des noms différents)."""
    store.ensure_defaults()
    settings.set_value("embed_model", "nomic-embed-text")    # le modèle « installé » ici
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": "nomic-embed-text:latest", "digest": "sha256:x", "size": 1}])
    peers.save(LENDER, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2)
    note = tmp_path / "note.txt"
    note.write_text("Le crachin breton est une pluie fine.", encoding="utf-8")
    collection = store.list_collections()[0]["id"]
    job_id = queue.create_job("index_paths", {"paths": [str(note)], "collection_id": collection})
    owner = OwnerManager()
    lender = Worker(LenderManager())
    monkeypatch.setattr(lender, "_call", lambda a, action, payload, timeout=30:
                        work.HANDLERS[action](owner, LENDER, payload))
    order = work.lease(owner, LENDER, {"resources": ["embed"],
                                       "caps": {"work_api": work.WORK_API,
                                                "models": {"nomic-embed-text:latest": "sha256:x"}}})
    return {"lender": lender, "job": job_id, "collection": collection,
            "assignment": Assignment(OWNER, "PC portable", order["unit"])}


def _unit(order):
    return dict(db.query_one("SELECT * FROM units WHERE id=?", (order["id"],)))


def _compute_with(monkeypatch, fn):
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", fn)


def test_unit_is_computed_with_the_received_file_then_the_file_is_erased(setup, monkeypatch):
    received = {}

    def compute(payload, file, options, stop):
        received["text"] = open(file, encoding="utf-8").read()
        received["name"] = file.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        return {"pieces": [received["text"]], "vectors": np.ones((1, 4), dtype=np.float32)}
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert setup["lender"]._process(a).startswith("faite")
    assert received == {"text": "Le crachin breton est une pluie fine.", "name": "note.txt"}
    assert _unit(a.order)["status"] == "done"
    assert not any(paths.work_dir().iterdir())                    # fichier effacé
    ledger = {p["node_id"]: p for p in peers.list_peers()}
    assert ledger[OWNER]["given_units"] == 1 and ledger[LENDER]["received_units"] == 1


def test_user_coming_back_stops_the_unit_and_gives_it_back(setup, monkeypatch):
    def compute(payload, file, options, stop):
        stop.set()                         # l'utilisateur revient pendant le calcul…
        return kinds.index._embed(["a", "b"], options, stop)
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert setup["lender"]._process(a) == "interrompue et rendue"
    unit = _unit(a.order)
    assert unit["status"] == "queued" and unit["attempts"] == 0     # aucun essai perdu


def test_engine_down_on_the_lender_gives_the_unit_back_and_pauses_the_resource(setup, monkeypatch):
    def compute(payload, file, options, stop):
        error = requests.ConnectionError("refusé")
        error.request = requests.Request("POST", ollama.base() + "/api/embed").prepare()
        raise error
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert "Ollama" in setup["lender"]._process(a)
    assert _unit(a.order)["status"] == "queued"
    assert "embed" in setup["lender"].executor.cooldown


def test_real_failure_is_reported_to_the_owner_with_the_machine_name(setup, monkeypatch):
    def compute(payload, file, options, stop):
        raise ValueError("document chiffré")
    _compute_with(monkeypatch, compute)
    a = setup["assignment"]
    assert setup["lender"]._process(a) == "échec : document chiffré"
    unit = _unit(a.order)
    assert unit["status"] == "queued" and unit["error"] == "Sur « PC fixe » : document chiffré"


def test_file_changed_during_transfer_is_detected(setup, monkeypatch):
    _compute_with(monkeypatch, lambda *args: pytest.fail("ne doit pas calculer"))
    a = setup["assignment"]
    a.order["blob"]["sha256"] = "0" * 64
    assert "transfert" in setup["lender"]._process(a)


def test_presentation_goes_only_to_a_machine_that_can_read_it(setup, monkeypatch, tmp_path):
    """Sans python-pptx (nœud minimal, téléphone sous Termux), la présentation
    échouerait là-bas à chaque essai, et serait perdue au deuxième : elle ne lui
    est pas confiée. Une machine qui n'annonce pas ses formats (version
    précédente) est supposée tout lire, comme avant."""
    slides = tmp_path / "cours.pptx"
    slides.write_bytes(b"PK\x03\x04")              # transmise telle quelle, jamais lue ici
    queue.create_job("index_paths", {"paths": [str(slides)], "collection_id": setup["collection"]})
    missing = {"pptx"}
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, package=None:
                        None if name in missing else find_spec(name, package))

    def lease(caps):
        return work.lease(OwnerManager(), LENDER, {"resources": ["embed"], "caps": caps})["unit"]
    without_pptx = Worker(LenderManager()).capabilities()
    assert ".pptx" not in without_pptx["readers"] and ".pdf" in without_pptx["readers"]
    assert lease(without_pptx) is None
    missing.clear()                                 # python-pptx installé
    order = lease(Worker(LenderManager()).capabilities())
    assert order["blob"]["name"] == "cours.pptx"
    work.release(OwnerManager(), LENDER, {"unit": order["id"]})
    previous_version = {k: v for k, v in without_pptx.items() if k != "readers"}
    assert lease(previous_version)["id"] == order["id"]


def _supply(monkeypatch, plugged=True, level=None, temperature=None, mobile=False):
    monkeypatch.setattr(power, "read", lambda: {
        "plugged": plugged, "level": level, "temperature": temperature,
        "mobile": mobile, "source": "test"})


def test_sharing_conditions(monkeypatch):
    """« Seulement quand je n'utilise pas ce PC » : la règle de l'économiseur d'écran."""
    _supply(monkeypatch, plugged=True)
    monkeypatch.setattr(identity, "idle_seconds", lambda: 30.0)
    assert sharing_state()["state"] == "off"
    settings.set_many({"share_enabled": True, "share_when_idle": True, "idle_minutes": 10})
    state = sharing_state()
    assert state["state"] == "busy" and "10 min" in state["detail"]
    monkeypatch.setattr(identity, "idle_seconds", lambda: 11 * 60.0)
    assert sharing_state()["active"]
    _supply(monkeypatch, plugged=False)
    assert sharing_state()["state"] == "battery"
    settings.set_value("share_on_ac_only", False)        # inactif, et la batterie permise
    assert sharing_state()["active"]


def test_lending_goes_on_while_the_pc_is_used_on_mains_only(monkeypatch):
    """Décision du 17/10/2026 : par défaut, le prêt continue pendant l'utilisation, en
    priorité basse, mais jamais sur batterie, même quand le partage y est permis."""
    settings.set_many({"share_enabled": True, "idle_minutes": 10})
    monkeypatch.setattr(identity, "idle_seconds", lambda: 5.0)       # quelqu'un s'en sert
    _supply(monkeypatch, plugged=True)
    state = sharing_state()
    assert state["active"] and state["state"] == "in_use" and "priorité basse" in state["detail"]
    _supply(monkeypatch, plugged=None)                  # alimentation inconnue : comme le secteur
    assert sharing_state()["state"] == "in_use"
    _supply(monkeypatch, plugged=False)
    assert sharing_state()["state"] == "battery"
    settings.set_value("share_on_ac_only", False)
    state = sharing_state()
    assert not state["active"] and state["state"] == "battery" and "utilisé" in state["detail"]
    monkeypatch.setattr(identity, "idle_seconds", lambda: None)      # inactivité inconnue
    assert not sharing_state()["active"]
    monkeypatch.setattr(identity, "idle_seconds", lambda: 11 * 60.0)  # personne : comme avant
    assert sharing_state()["state"] == "ready"


def test_while_the_pc_is_used_the_gpu_is_lent_half_the_time_at_most(setup):
    lender = setup["lender"]
    lender.state = {"active": True, "state": "in_use", "detail": ""}
    lender._rest_gpu(setup["assignment"], compute_s=2.0)          # unité « embed », 100 %
    remaining = lender._gpu_rest_until - worker.time.monotonic()
    assert 1.5 < remaining <= 2.0                                  # 2 s × 50 / 50
    settings.set_value("share_max_gpu", 25)                        # plus strict : gardé
    lender._gpu_rest_until = 0.0
    lender._rest_gpu(setup["assignment"], compute_s=2.0)
    assert 5.5 < lender._gpu_rest_until - worker.time.monotonic() <= 6.0
    lender.state = {"active": True, "state": "ready", "detail": ""}
    settings.set_value("share_max_gpu", 100)
    lender._gpu_rest_until = 0.0
    lender._rest_gpu(setup["assignment"], compute_s=2.0)           # personne : pas de pause
    assert lender._gpu_rest_until == 0.0


def test_a_pc_keeps_its_rules_whatever_its_battery(monkeypatch):
    """Les critères du téléphone ne s'appliquent pas à un PC portable sur secteur,
    ni à une machine dont l'alimentation est inconnue (comme avant)."""
    monkeypatch.setattr(identity, "idle_seconds", lambda: None)
    settings.set_many({"share_enabled": True})
    _supply(monkeypatch, plugged=True, level=30, temperature=55.0)
    assert sharing_state()["active"]
    _supply(monkeypatch, plugged=None)
    assert sharing_state()["active"]


def test_phone_computes_only_charging_charged_enough_and_cool(monkeypatch):
    monkeypatch.setattr(identity, "idle_seconds", lambda: None)   # pas mesurable sur Android
    settings.set_many({"share_enabled": True, "share_min_battery": 80, "share_max_temp": 40})
    _supply(monkeypatch, plugged=True, level=85, temperature=31.5, mobile=True)
    assert sharing_state()["active"]
    _supply(monkeypatch, plugged=False, level=85, temperature=31.5, mobile=True)
    state = sharing_state()
    assert state["state"] == "battery" and "ce téléphone" in state["detail"]
    _supply(monkeypatch, plugged=None, level=None, mobile=True)      # ni /sys ni Termux:API
    assert sharing_state()["state"] == "unknown"
    _supply(monkeypatch, plugged=True, level=62, temperature=31.5, mobile=True)
    state = sharing_state()
    assert state["state"] == "low_battery" and "62 %" in state["detail"]
    assert "80 %" in state["detail"]
    _supply(monkeypatch, plugged=True, level=85, temperature=None, mobile=True)
    assert sharing_state()["active"]                                  # température inconnue


def test_hot_phone_waits_to_cool_down_before_resuming(monkeypatch):
    monkeypatch.setattr(identity, "idle_seconds", lambda: None)
    settings.set_many({"share_enabled": True, "share_max_temp": 40})

    def at(degrees, previous=None):
        _supply(monkeypatch, plugged=True, level=90, temperature=degrees, mobile=True)
        return sharing_state(previous)
    assert at(39.9)["active"]
    hot = at(40.4)
    assert hot["state"] == "hot" and hot["detail"] == (
        "En pause : batterie à 40,4 °C (reprise à 37 °C).")
    assert at(39.0, previous=hot)["state"] == "hot"       # pas de reprise à 39,9 °C
    assert at(37.0, previous=at(37.5, previous=hot))["active"]


def test_phone_charging_slowly_does_not_stop_and_start_over_and_over(monkeypatch):
    """Sur le port USB d'un PC, un téléphone peut se décharger en calculant : une
    fois lancé, il ne s'arrête que 5 % sous la charge minimale."""
    monkeypatch.setattr(identity, "idle_seconds", lambda: None)
    settings.set_many({"share_enabled": True, "share_min_battery": 80})

    def at(level, previous=None):
        _supply(monkeypatch, plugged=True, level=level, temperature=30.0, mobile=True)
        return sharing_state(previous)
    assert at(79)["state"] == "low_battery"            # pour commencer : 80 %
    ready = at(80)
    assert ready["active"]
    assert at(76, previous=ready)["active"]            # en cours : jusqu'à 75 %
    low = at(74, previous=ready)
    assert low["detail"] == "En pause : batterie à 74 % (reprise à 80 %)."
    assert at(79, previous=low)["state"] == "low_battery"
    assert at(80, previous=low)["active"]


def test_phone_rules_follow_the_settings(monkeypatch):
    monkeypatch.setattr(identity, "idle_seconds", lambda: None)
    settings.set_many({"share_enabled": True, "share_on_ac_only": False,
                       "share_min_battery": 50, "share_max_temp": 0})
    _supply(monkeypatch, plugged=False, level=55, temperature=44.0, mobile=True)
    assert sharing_state()["active"]                # sur batterie, si l'utilisateur l'a voulu
    _supply(monkeypatch, plugged=False, level=45, temperature=44.0, mobile=True)
    assert sharing_state()["state"] == "low_battery"


def test_gpu_share_below_100_percent_imposes_a_rest(setup):
    settings.set_value("share_max_gpu", 25)
    lender = setup["lender"]
    lender._rest_gpu(setup["assignment"], compute_s=2.0)          # unité « embed »
    remaining = lender._gpu_rest_until - worker.time.monotonic()
    assert 5.5 < remaining <= 6.0                                  # 2 s × 75 / 25


def test_failed_work_requests_are_logged_once_per_cause_with_a_slow_link_timeout(
        setup, monkeypatch, caplog):
    lender = setup["lender"]
    peer = {"node_id": OWNER, "name": "PC portable"}
    timeouts, replies = [], [PeerError("Machine injoignable (ReadTimeout)."),
                             PeerError("Machine injoignable (ReadTimeout)."),
                             {"unit": None, "retry_after": 600, "reason": "Versions différentes"}]

    def fake_call(my_id, peer_row, action, payload, key=None, timeout=10):
        timeouts.append(timeout)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(worker.client, "call", fake_call)
    with caplog.at_level("INFO", logger="aiodos.worker"):
        for _ in range(3):
            lender._ask(peer, ["embed"])
    notes = [r.getMessage() for r in caplog.records if r.name == "aiodos.worker"]
    assert notes == ["« PC portable » : demande de travail impossible (Machine injoignable (ReadTimeout).)",
                     "« PC portable » : Versions différentes"]
    assert timeouts == [worker.LEASE_TIMEOUT] * 3 and worker.LEASE_TIMEOUT >= 20


def _reading(received):
    def compute(payload, file, options, stop):
        received["text"] = open(file, encoding="utf-8").read()
        return {"pieces": [received["text"]], "vectors": np.ones((1, 4), dtype=np.float32)}
    return compute


def test_a_transfer_cut_in_the_middle_resumes_where_it_stopped(setup, monkeypatch):
    """Étape 2.6 : coupure en plein transfert (4G, relais) : le morceau est redemandé au
    même endroit, sans tout reprendre depuis le début."""
    monkeypatch.setattr(work, "BLOB_CHUNK", 8)
    monkeypatch.setattr(worker, "CHUNK_RETRY_DELAYS", (0.01, 0.01))
    lender, a = setup["lender"], setup["assignment"]
    owner_call, asked, cuts = lender._call, [], [True]

    def flaky(assignment, action, payload, timeout=30):
        if action == "work/blob":
            asked.append(payload["offset"])
            if payload["offset"] == 16 and cuts:
                cuts.pop()
                raise PeerError("Machine injoignable par Internet (ReadTimeout).")
        return owner_call(assignment, action, payload, timeout)
    monkeypatch.setattr(lender, "_call", flaky)
    received = {}
    _compute_with(monkeypatch, _reading(received))
    assert lender._process(a).startswith("faite")
    assert received["text"] == "Le crachin breton est une pluie fine."
    assert asked == [0, 8, 16, 16, 24, 32]


def test_a_transfer_cut_for_too_long_gives_up(setup, monkeypatch):
    monkeypatch.setattr(worker, "CHUNK_RETRY_DELAYS", (0.01, 0.01))
    lender, a = setup["lender"], setup["assignment"]

    def down(assignment, action, payload, timeout=30):
        raise PeerError("Machine injoignable.")
    monkeypatch.setattr(lender, "_call", down)
    _compute_with(monkeypatch, lambda *args: pytest.fail("rien à calculer"))
    assert "injoignable" in lender._process(a)


def test_an_owner_reached_only_by_internet_is_asked_for_work(setup):
    """Machine appairée sur invitation (étape 2.4) : pas d'adresse sur le réseau local,
    seulement sa clé Internet. On lui demande du travail comme aux autres (essai de 2.7) ;
    une machine qu'on ne sait joindre d'aucune façon, non."""
    peers.save(OWNER, "PC portable", b"k" * 32, None, None)
    peers.set_net(OWNER, "ab" * 32, {"relays": ["http://127.0.0.1:3340/"], "addrs": []})
    peers.save("machine-c", "PC sans adresse", b"k" * 32, None, None)
    asked = [p["node_id"] for p in setup["lender"]._candidates()]
    assert OWNER in asked and "machine-c" not in asked


def test_an_owner_reached_only_by_internet_sends_smaller_pieces(setup, monkeypatch):
    peers.save(OWNER, "PC portable", b"k" * 32, None, None)     # plus de réseau local
    monkeypatch.setattr(work, "INTERNET_CHUNK", 10)
    lender, a = setup["lender"], setup["assignment"]
    owner_call, sizes = lender._call, []

    def spy(assignment, action, payload, timeout=30):
        if action == "work/blob":
            sizes.append(payload["size"])
        return owner_call(assignment, action, payload, timeout)
    monkeypatch.setattr(lender, "_call", spy)
    _compute_with(monkeypatch, _reading({}))
    assert lender._process(a).startswith("faite")
    assert sizes and set(sizes) == {10}
