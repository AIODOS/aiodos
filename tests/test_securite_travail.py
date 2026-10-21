"""Travail confié, et calculé pour les autres (audit de sécurité du 20/10/2026, reprise du
21/10 : groupe « travail »).

Côté propriétaire (network/work.py) :
- rien ne part chez un ami dont le résultat ne se contrôlerait pas ici (image, son, vidéo,
  document que cette machine ne sait pas relire) : crochet générique TaskSpec.checkable,
  registry.friend_checkable ; une tâche contrôlée qui ne dit pas ce qu'elle sait contrôler
  ne part jamais chez un ami ;
- une page web ne part qu'à une machine qui ne lit, pour les autres, que l'Internet public
  (révision du contrat, « work_revision ») ; une machine d'avant l'apprend par un message ;
- l'ordre dit la taille de résultat acceptée (« result_max ») ; un résultat trop gros, rendu
  en échec par un ami, ne compte pas contre lui et l'unité revient ici.
Côté machine qui calcule (network/worker.py) :
- la ressource « net » n'est jamais proposée à une machine qui n'est pas à moi, et une unité
  pour une ressource non proposée est rendue sans être calculée ;
- après une unité qu'elle ne calcule pas pour cette machine, elle ne la redemande pas
  aussitôt : plus d'allers-retours sans fin avec une version d'avant ;
- un modèle Whisper qui manque se télécharge pour mes machines, jamais pour un ami ; s'il
  manque, l'unité est rendue sans compter d'échec ;
- un résultat plus gros que ce qu'accepte son propriétaire n'est pas envoyé ;
- un ami n'apprend rien de cette machine par un échec.
Sans réseau ni Ollama : les appels vont directement au propriétaire, dans la même base."""
import asyncio
import json

import numpy as np
import pytest

from aiodos import db, settings
from aiodos.jobs import kinds, queue, registry  # noqa: F401  (enregistre les tâches)
from aiodos.jobs.models import DEFAULT_SLOTS
from aiodos.jobs.registry import TaskSpec
from aiodos.knowledge import store
from aiodos.network import body_limit, peers, sharing, wire, work, worker
from aiodos.network.worker import Assignment, Worker
from aiodos.services import extract, media, ollama
from tests.test_images import tiny_model  # noqa: F401  (modèle d'images d'essai)
from tests.test_work_leases import B, C, EMBED_DIGEST, _caps, _lease, _unit, owner  # noqa: F401

OWNER, LENDER, LEA = "machine-a", "machine-b", "machine-lea"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture(autouse=True)
def friends_forgotten():
    """Ce qui est retenu des amis en mémoire (demandes, unités rendues en échec) vit le temps
    du processus : chaque essai part de rien, et ne laisse rien aux suivants (un numéro
    d'unité « rendu en échec par B » ici en cacherait un autre ailleurs)."""
    work.forget_friends()
    yield
    work.forget_friends()


# ================================================================ propriétaire
@pytest.fixture
def friend_owner(owner):
    """B est la machine d'un ami, et la bibliothèque est partagée avec les amis."""
    peers.set_trust(B, sharing.FRIEND)
    store.set_scope(owner["collection"], "friends")
    return owner


def _able():
    """Une machine qui sait tout calculer : embeddings, vision, Whisper, tous les formats."""
    return dict(_caps(), whisper=True, models={"nomic-embed-text:latest": EMBED_DIGEST,
                                               "qwen2.5vl:7b": "sha256:vision"})


def _alone(owner, tmp_path, name, data: bytes) -> int:
    """Le propriétaire n'a plus que ce fichier à indexer (ses trois textes mis de côté)."""
    db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (owner["job"],))
    folder = tmp_path / "seul"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data)
    return queue.create_job("index_paths", {"paths": [str(folder)],
                                            "collection_id": owner["collection"]})


@pytest.mark.parametrize("name, data, resource", [
    ("photo.png", PNG, "gpu"),                       # OCR et description : pas refaits ici
    ("message.mp3", b"ID3" + b"\0" * 64, "cpu"),     # transcription : pas refaite ici
    ("contrat.odt", b"PK\x03\x04" + b"\0" * 64, "embed"),  # format sans lecteur ici
])
def test_what_could_not_be_checked_here_is_never_lent_to_a_friend(friend_owner, tmp_path,
                                                                  monkeypatch, name, data,
                                                                  resource):
    """Relecture du 21/10/2026 : ce qu'un ami tire d'une image, d'un son, d'une vidéo ou
    d'un document que je ne sais pas relire n'est jamais pris. Le lui confier, c'était le
    faire calculer pour rien (vision, Whisper), puis refaire l'unité ici ; et sur un
    téléphone sans python-pptx, un .pptx confié à un ami ne s'indexait plus jamais. Mes
    machines, elles, le reçoivent toujours."""
    monkeypatch.setattr(extract, "readable_documents", lambda: [".pdf"])
    _alone(friend_owner, tmp_path, name, data)
    assert _lease(friend_owner, caps=_able(), resources=(resource,))["unit"] is None
    order = _lease(friend_owner, node=C, caps=_able(), resources=(resource,))["unit"]
    assert order is not None and order["blob"]["name"] == name


def test_a_text_a_friend_returns_is_checked_here_so_it_is_still_lent(friend_owner):
    order = _lease(friend_owner, caps=_able())["unit"]
    assert order is not None and order["blob"]["name"].endswith(".txt")


def test_a_task_checked_by_sample_lends_to_friends_only_what_it_says_it_can_check():
    """Le crochet est générique : une tâche contrôlée par échantillon qui ne dit pas ce
    qu'elle sait contrôler ne part jamais chez un ami ; une tâche sans contrôle seulement
    si l'utilisateur accepte de confier ses unités à ses amis (friends_out : ses images)."""
    def compute(payload, file, options, stop):
        return {}

    def assimilate(payload, output):
        return {}

    def sample(payload, output, options):
        pass
    silent = TaskSpec("essai", compute, assimilate, sample=sample)
    assert not registry.friend_checkable(silent, {"path": "notes.txt"})
    says = TaskSpec("essai", compute, assimilate, sample=sample,
                    checkable=lambda payload: payload["relu"])
    assert registry.friend_checkable(says, {"relu": True})
    assert not registry.friend_checkable(says, {"relu": False})
    assert not registry.friend_checkable(TaskSpec("essai", compute, assimilate), {})
    assert registry.friend_checkable(registry.TASKS["generate_image"], {"model": "x"})
    registry.register_portable("essai_controle", compute, assimilate, sample=sample,
                               checkable=lambda payload: True)
    try:
        assert registry.TASKS["essai_controle"].checkable({}) is True
    finally:
        registry.TASKS.pop("essai_controle", None)


def test_once_declared_the_hook_of_the_task_decides(friend_owner, monkeypatch):
    """Quand jobs/kinds/index.py déclarera son crochet, c'est lui qui vaudra ; un crochet en
    erreur ne confie rien à un ami, sans faire échouer la demande."""
    spec = registry.TASKS["index_file"]
    monkeypatch.setattr(spec, "checkable", lambda payload: False)
    assert _lease(friend_owner)["unit"] is None
    assert _lease(friend_owner, node=C)["unit"] is not None    # mes machines : rien ne change

    def broken(payload):
        raise OSError("disque réseau injoignable")
    monkeypatch.setattr(spec, "checkable", broken)
    assert _lease(friend_owner)["unit"] is None
    monkeypatch.setattr(spec, "checkable", lambda payload: True)
    assert _lease(friend_owner)["unit"] is not None


def test_a_web_page_goes_only_to_a_machine_that_reads_only_the_public_internet(owner):
    """Une de mes machines restée sur une version d'avant suivrait une page publique qui
    redirige vers son réseau local, et ce qu'elle y lirait entrerait dans ma bibliothèque.
    Elle ne reçoit plus de page, et l'apprend : refus dit, pas muet."""
    db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (owner["job"],))
    queue.create_job("index_urls", {"urls": ["https://example.org/"],
                                    "collection_id": owner["collection"]})
    before = _caps()                                       # pas de « work_revision »
    reply = _lease(owner, node=C, caps=before, resources=("net",))
    assert reply["unit"] is None and "mets AIODOS à jour" in reply["reason"]
    assert db.query_one("SELECT COUNT(*) AS n FROM units WHERE status='running'")["n"] == 0
    for lie in (True, "2", None):                          # rien de lisible : révision 1
        assert _lease(owner, node=C, caps=dict(before, work_revision=lie),
                      resources=("net",))["unit"] is None
    order = _lease(owner, node=C, caps=dict(before, work_revision=work.WORK_REVISION),
                   resources=("net",))["unit"]
    assert order["task"] == "index_url" and order["payload"]["url"] == "https://example.org/"


def test_the_machine_that_computes_says_its_revision(monkeypatch):
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [])
    lender = Worker(_LenderManager())
    caps = lender.capabilities()
    assert caps["work_revision"] == work.WORK_REVISION >= work.REVISION_FOR["net"] >= 2
    assert caps["work_api"] == work.WORK_API == 1          # les versions d'avant : servies


def test_the_order_says_how_big_a_result_may_be(owner):
    """La taille que network/body_limit.py acceptera : l'index d'un long document, d'une de
    mes machines ; un message ordinaire, d'un ami."""
    assert _lease(owner, node=C)["unit"]["result_max"] == work.MINE_RESULT
    peers.set_trust(B, sharing.FRIEND)
    store.set_scope(owner["collection"], "friends")
    assert _lease(owner)["unit"]["result_max"] == body_limit.SEALED_BODY
    assert max(body_limit.SEALED_BODY, work.result_limit(B)) == body_limit.SEALED_BODY


def test_a_result_too_big_for_me_does_not_count_against_a_friend(friend_owner, monkeypatch):
    """Un dossier de longs journaux : chaque index dépasse ce que j'accepte d'un ami. Ce
    n'est pas sa faute : ni échec compté, ni mise de côté. L'unité revient ici, et ne lui
    est plus confiée (un autre échec, lui, compte)."""
    monkeypatch.setattr(work, "FRIEND_FAILURES_ASIDE", 1)
    order = _lease(friend_owner)["unit"]
    assert work.fail(friend_owner["manager"], B, {
        "unit": order["id"], "too_big": True,
        "error": "résultat trop volumineux pour être rendu (40,0 Mio, 16,0 Mio au plus)"}) \
        == {"ok": True}
    assert sharing.set_aside_reason(B) is None
    unit = _unit(order["id"])
    assert (unit["status"], unit["attempts"]) == ("queued", 0) and "volumineux" in unit["error"]
    assert {p["node_id"]: p for p in peers.list_peers()}[B]["failed_units"] == 0
    others = [_lease(friend_owner)["unit"] for _ in range(2)]
    assert order["id"] not in [o["id"] for o in others]
    assert work.fail(friend_owner["manager"], B, {"unit": others[0]["id"],
                                                  "error": "PDF illisible"}) == {"ok": True}
    assert sharing.set_aside_reason(B) is not None         # un vrai échec compte toujours


def test_too_big_is_no_excuse_for_an_image(tiny_model, monkeypatch):
    """La limite d'une image est taillée pour ce qu'elle produit : « trop gros » n'y est pas
    crédible, c'est un échec comme un autre. Sinon un ami ferait échouer mes images (que
    seuls mes amis calculent) sans jamais être mis de côté."""
    from aiodos.imaging import catalog, engine
    from aiodos.jobs.kinds import images as kind
    from tests.test_images import ready
    monkeypatch.setattr(work.ollama, "list_models", lambda max_age=10: [])
    peers.save(LEA, "PC de Léa", b"l" * 32, "127.0.0.1", 3, sharing.FRIEND)
    peers.set_profile(LEA, {"images": {"engine": "f0aac52", "shares": True}})
    settings.set_value("images_from_friends", True)
    queue.create_job("generate_images", kind.normalize({"model": "essai", "prompt": "un chat"}))
    ready()
    monkeypatch.setattr(engine, "folder", lambda: None)             # pas calculable ici
    monkeypatch.setattr(work, "FRIEND_FAILURES_ASIDE", 1)
    manager = _OwnerManager()
    order = work.lease(manager, LEA, {"resources": ["gpu"], "caps": {
        "work_api": work.WORK_API,
        "images": {"engine": "f0aac52", "files": catalog.hashes("essai")}}})["unit"]
    assert order["result_max"] > body_limit.SEALED_BODY            # taillée pour une image
    work.fail(manager, LEA, {"unit": order["id"], "too_big": True, "error": "trop gros"})
    assert sharing.set_aside_reason(LEA) is not None


# ======================================================= machine qui calcule
class _OwnerExecutor:
    def settle(self, job_id, finished):
        if finished:
            queue.finalize_job(job_id)

    def publish_job(self, job_id, force=False):
        pass

    def wake(self):
        pass


class _OwnerManager:
    executor = _OwnerExecutor()

    def changed(self):
        pass

    def name(self):
        return "PC portable"


class _LenderManager:
    node_id = LENDER

    def __init__(self):
        self.executor = type("Executor", (), {"interactive": 0, "cooldown": {}})()

    def changed(self):
        pass


@pytest.fixture
def lending(monkeypatch, tmp_path):
    """Propriétaire et machine qui calcule dans la même base (comme tests/test_worker.py).
    Deux notes à indexer ; la première est confiée à la machine qui calcule."""
    store.ensure_defaults()
    settings.set_value("embed_model", "nomic-embed-text")
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [
        {"name": "nomic-embed-text:latest", "digest": "sha256:x", "size": 1}])
    peers.save(LENDER, "PC fixe", b"k" * 32, "127.0.0.1", 1)
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2)
    folder = tmp_path / "docs"
    folder.mkdir()
    for name, text in (("note.txt", "Le crachin breton est une pluie fine."),
                       ("suite.txt", "Le vent se lève sur la lande.")):
        (folder / name).write_text(text, encoding="utf-8")
    collection = store.list_collections()[0]["id"]
    queue.create_job("index_paths", {"paths": [str(folder)], "collection_id": collection})
    owner = _OwnerManager()
    lender = Worker(_LenderManager())
    asked = []

    def call(a, action, payload, timeout=30):
        asked.append((action, payload))
        return work.HANDLERS[action](owner, LENDER, payload)
    monkeypatch.setattr(lender, "_call", call)

    def lease():
        order = work.lease(owner, LENDER, {"resources": ["embed"], "caps": {
            "work_api": work.WORK_API, "models": {"nomic-embed-text:latest": "sha256:x"}}})
        return Assignment(OWNER, "PC portable", order["unit"])
    return {"lender": lender, "asked": asked, "lease": lease, "owner": owner,
            "collection": collection, "assignment": lease()}


def _computing(monkeypatch, compute):
    monkeypatch.setattr(registry.TASKS["index_file"], "compute", compute)


def _indexed(text="Le crachin breton est une pluie fine.", rows=1, width=4):
    return {"pieces": [text] * rows, "vectors": np.ones((rows, width), dtype=np.float32)}


def _active(monkeypatch, lender, offered):
    """La machine qui calcule prête ces ressources ; les machines appairées sont candidates."""
    monkeypatch.setattr(worker, "sharing_state",
                        lambda previous=None: {"active": True, "state": "ready", "detail": ""})
    monkeypatch.setattr(lender, "_offered", lambda: list(offered))
    monkeypatch.setattr(lender, "_candidates", peers.list_peers)


def test_reading_the_web_is_never_offered_to_a_machine_that_is_not_mine(monkeypatch):
    """Je ne lis pas le Web pour un ami (index_url, réservée à mes machines) : la ressource
    « net » ne lui est pas proposée. Un ami resté sur une version d'avant me confiait ses
    pages ; je les lui rendais, il me les redonnait (des centaines de fois par seconde)."""
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2, sharing.MINE)
    peers.save(LEA, "PC de Léa", b"l" * 32, "127.0.0.1", 3, sharing.FRIEND)
    lender = Worker(_LenderManager())
    _active(monkeypatch, lender, ["embed", "net"])
    asked = []
    monkeypatch.setattr(lender, "_ask", lambda peer, offer: asked.append(
        (peer["node_id"], list(offer))) or {"unit": None, "retry_after": 60})
    asyncio.run(lender._tick())
    assert dict(asked) == {OWNER: ["embed", "net"], LEA: ["embed"]}
    asked.clear()
    lender._next_ask.clear()
    _active(monkeypatch, lender, ["net"])                  # rien d'autre de libre ici
    asyncio.run(lender._tick())
    assert asked == [(OWNER, ["net"])]                     # Léa n'est même pas demandée


def test_my_machines_are_asked_for_work_before_friends():
    """Les demandes de travail partent l'une après l'autre : un ami qui fait attendre la
    sienne jusqu'à son échéance ne retarde plus celles de mes machines, même s'il m'a
    beaucoup donné (relecture finale du 21/10/2026). Entre amis, la réciprocité."""
    peers.save(LEA, "PC de Léa", b"l" * 32, "127.0.0.1", 3, sharing.FRIEND)
    peers.save("machine-max", "PC de Max", b"m" * 32, "127.0.0.1", 4, sharing.FRIEND)
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2, sharing.MINE)
    peers.account(LEA, received_s=5000.0)              # Léa m'a beaucoup donné
    for node_id in (LEA, "machine-max", OWNER):
        peers.touch(node_id)
    lender = Worker(_LenderManager())
    assert [p["node_id"] for p in lender._candidates()] == [OWNER, LEA, "machine-max"]


@pytest.mark.parametrize("name", ["photo.png", "message.mp3", "film.mp4"])
def test_a_friend_never_gets_its_media_decoded_here(monkeypatch, name):
    """Un propriétaire à jour ne confie plus d'image, de son ni de vidéo à un ami (son
    résultat ne se revérifierait pas chez lui) ; seul un ami malveillant, ou resté sur une
    version d'avant, en envoie encore : rendue sans être téléchargée ni décodée, et cette
    machine au repos. Pour une de mes machines, rien ne change (relecture finale du
    21/10/2026)."""
    peers.save(LEA, "PC de Léa", b"l" * 32, "127.0.0.1", 3, sharing.FRIEND)
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2, sharing.MINE)
    lender = Worker(_LenderManager())
    order = {"id": 7, "task": "index_file", "resource": "gpu", "lease_s": 90,
             "payload": {"source": name}, "options": {},
             "blob": {"name": name, "size": 10, "sha256": "0" * 64}}
    calls = []
    monkeypatch.setattr(lender, "_call", lambda a, action, payload, timeout=30:
                        calls.append(action) or {"ok": True})

    def download(a, folder):
        raise RuntimeError("fichier téléchargé")
    monkeypatch.setattr(lender, "_download", download)
    outcome = lender._process(Assignment(LEA, "PC de Léa", dict(order)))
    assert outcome == "réservée à mes machines : rendue" and calls == ["work/release"]
    assert lender._resting[LEA]
    calls.clear()
    outcome = lender._process(Assignment(OWNER, "PC portable", dict(order)))
    assert "fichier téléchargé" in outcome and calls == ["work/fail"]    # calculée pour moi


def test_a_machine_handing_what_i_do_not_compute_for_it_is_left_alone(monkeypatch):
    """Une machine qui me confie, encore et encore, une unité que je ne calcule pas pour
    elle (une image d'ami sans mon accord, une page web d'une version d'avant) : rendue une
    fois, puis plus rien ne lui est demandé pendant un moment, au lieu d'allers-retours."""
    peers.save(LEA, "PC de Léa", b"l" * 32, "127.0.0.1", 3, sharing.FRIEND)
    lender = Worker(_LenderManager())
    _active(monkeypatch, lender, ["gpu", "embed"])
    image = {"id": 5, "task": "generate_image", "resource": "gpu", "lease_s": 90,
             "payload": {"model": "essai", "prompt": "un chat"}, "options": {}, "blob": None}
    asked, outcomes, calls = [], [], []
    monkeypatch.setattr(lender, "_ask", lambda peer, offer: asked.append(list(offer))
                        or {"unit": dict(image)})
    monkeypatch.setattr(lender, "_call", lambda a, action, payload, timeout=30:
                        calls.append(action) or {"ok": True})
    monkeypatch.setattr(lender, "_launch", lambda peer, order: outcomes.append(
        lender._process(Assignment(peer["node_id"], peer["name"], order))))
    for _ in range(5):
        asyncio.run(lender._tick())
    assert outcomes == ["réservée à mes machines : rendue"] and calls == ["work/release"]
    assert len(asked) == 1
    resting = lender._resting[LEA]
    assert set(resting) >= {"gpu", "embed"}                # rien ne lui est demandé...
    left = min(resting.values()) - worker.time.monotonic()
    assert worker.REFUSED_RETRY - 60 < left <= worker.REFUSED_RETRY   # ... pendant 10 min
    lender._resting[LEA] = {resource: 0.0 for resource in resting}    # le repos fini :
    asyncio.run(lender._tick())                            # elle est redemandée
    assert len(asked) == 2


def test_a_unit_for_a_resource_i_did_not_offer_is_handed_back_unopened(monkeypatch):
    """Elle occuperait ici une ressource au-delà de ses places, ou que je garde à mes
    machines : rendue sans être calculée, et la machine au repos."""
    peers.save(LEA, "PC de Léa", b"l" * 32, "127.0.0.1", 3, sharing.FRIEND)
    lender = Worker(_LenderManager())
    _active(monkeypatch, lender, ["embed"])
    page = {"id": 9, "task": "index_url", "resource": "net", "lease_s": 90,
            "payload": {"url": "http://10.0.0.1/"}, "options": {}, "blob": None}
    launched, sent = [], []
    monkeypatch.setattr(lender, "_ask", lambda peer, offer: {"unit": dict(page)})
    monkeypatch.setattr(lender, "_launch", lambda peer, order: launched.append(order))
    monkeypatch.setattr(worker.client, "call", lambda my_id, peer, action, payload, key=None,
                        timeout=10, limit=None: sent.append((action, payload)) or {"ok": True})
    asyncio.run(lender._tick())
    asyncio.run(lender._tick())
    assert launched == [] and sent == [("work/release", {"unit": 9})]


def test_an_unreadable_answer_to_a_work_request_breaks_nothing(monkeypatch):
    """Une réponse qui n'est pas un objet, ou un délai illisible (NaN, texte) : la machine
    est redemandée plus tard, et les autres sont servies."""
    monkeypatch.setattr(ollama, "list_models", lambda max_age=10: [])
    peers.save(LEA, "PC de Léa", b"l" * 32, "127.0.0.1", 3, sharing.FRIEND)
    peers.save(OWNER, "PC portable", b"k" * 32, "127.0.0.1", 2, sharing.MINE)
    lender = Worker(_LenderManager())
    _active(monkeypatch, lender, ["embed"])
    replies = iter([[1, 2, 3], {"unit": None, "retry_after": "bientôt"},
                    {"unit": None, "retry_after": float("nan")},
                    {"unit": None, "retry_after": -5}])
    monkeypatch.setattr(worker.client, "call", lambda *args, **kwargs: next(replies))
    asyncio.run(lender._tick())
    lender._next_ask.clear()
    asyncio.run(lender._tick())
    for until, _ in lender._next_ask.values():
        assert until - worker.time.monotonic() > worker.ASK_AGAIN - 1


def test_the_whisper_model_is_downloaded_for_my_machines_never_for_a_friend(lending,
                                                                          monkeypatch):
    """Régression de la correction Whisper (relecture du 21/10/2026) : un modèle qui manque
    ne se téléchargeait plus du tout, même pour mes machines ; leurs sons échouaient ici. La
    machine qui calcule en décide d'après le niveau de confiance, jamais d'après l'ordre."""
    seen = []

    def compute(payload, file, options, stop):
        seen.append(media._download.get())
        return _indexed()
    _computing(monkeypatch, compute)
    lender = lending["lender"]
    assert lender._process(lending["assignment"]).startswith("faite")      # une des miennes
    peers.set_trust(OWNER, sharing.FRIEND)
    assert lender._process(lending["lease"]()).startswith("faite")         # un ami
    assert seen == [True, False]
    assert media._download.get() is None                   # hors du calcul : la règle


def test_a_missing_whisper_model_gives_the_unit_back_without_a_failure(lending, monkeypatch):
    """Ni le fichier ni l'unité n'y sont pour rien : rendue sans compter d'échec (un ami me
    mettait de côté au bout de dix ; mes fichiers finissaient « en échec »). Ses autres sons
    demanderaient le même modèle : plus de cette ressource pour lui pendant un moment."""
    def compute(payload, file, options, stop):
        raise media.WhisperMissing("Modèle Whisper « medium » absent de cette machine.")
    _computing(monkeypatch, compute)
    lender, a = lending["lender"], lending["assignment"]
    outcome = lender._process(a)
    assert "Whisper" in outcome and "rendue" in outcome
    unit = _unit(a.order["id"])
    assert (unit["status"], unit["attempts"], unit["error"]) == ("queued", 0, None)
    actions = [action for action, _ in lending["asked"]]
    assert "work/release" in actions and "work/fail" not in actions
    peer = {"node_id": OWNER, "trust": sharing.MINE}
    assert lender._offer_for(peer, sorted(DEFAULT_SLOTS)) == sorted(
        r for r in DEFAULT_SLOTS if r != a.resource)


def test_a_friend_learns_nothing_of_this_machine_from_a_failure(lending, monkeypatch):
    """Une erreur du calcul (lecteur de documents, transcription) peut citer des chemins de
    ce PC, donc le nom de son compte : un ami n'en apprend que la sorte. Un refus de son
    ordre (fichier modifié pendant le transfert…) lui reste dit tel quel."""
    peers.set_trust(OWNER, sharing.FRIEND)        # pour qui calcule, le propriétaire est un ami
    secret = r"C:\Users\Camille\AppData\Local\huggingface\hub\models--Systran"

    def compute(payload, file, options, stop):
        raise media.TranscriptionError(f"Transcription impossible : RuntimeError : {secret}")
    _computing(monkeypatch, compute)
    lender, a = lending["lender"], lending["assignment"]
    assert "Camille" in lender._process(a)                 # son propre journal garde tout
    error = _unit(a.order["id"])["error"]
    assert "Camille" not in error and "TranscriptionError" in error
    b = lending["lease"]()
    b.order["blob"]["sha256"] = "0" * 64
    lender._process(b)
    assert "modifié pendant le transfert" in _unit(b.order["id"])["error"]


def test_my_own_machines_still_learn_everything_from_a_failure(lending, monkeypatch):
    def compute(payload, file, options, stop):
        raise ValueError(r"document chiffré (C:\Partage\contrat.pdf)")
    _computing(monkeypatch, compute)
    lending["lender"]._process(lending["assignment"])
    assert _unit(lending["assignment"].order["id"])["error"] == \
        r"Sur « PC fixe » : document chiffré (C:\Partage\contrat.pdf)"


def test_a_result_too_big_for_its_owner_is_not_sent(lending, monkeypatch):
    """Un ami n'accepte d'un ami qu'un message ordinaire (network/body_limit.py). Avant, un
    long index partait quand même, était refusé, trois fois, puis le bail expirait. Il est
    maintenant rendu en échec sans être envoyé, sans reproche, et l'unité revient chez son
    propriétaire, qui ne la confie plus à cette machine."""
    monkeypatch.setattr(body_limit, "SEALED_BODY", 64 * 1024)    # un message ordinaire, réduit
    lender, owner = lending["lender"], lending["owner"]
    released = lending["assignment"]
    lender._quiet(released, "work/release")              # l'ordre de la fixture : rendu
    peers.set_trust(LENDER, sharing.FRIEND)              # pour le propriétaire, un ami
    store.set_scope(lending["collection"], "friends")
    a = lending["lease"]()
    assert a.order["result_max"] == 64 * 1024
    _computing(monkeypatch, lambda *args: _indexed(rows=40, width=1024))   # ~220 Kio scellés
    lending["asked"].clear()
    outcome = lender._process(a)
    assert "trop volumineux" in outcome
    actions = [action for action, _ in lending["asked"]]
    assert "work/result" not in actions and actions[-1] == "work/fail"
    assert lending["asked"][-1][1]["too_big"] is True
    unit = _unit(a.order["id"])
    assert (unit["status"], unit["attempts"]) == ("queued", 0) and "volumineux" in unit["error"]
    assert sharing.set_aside_reason(LENDER) is None
    assert {p["node_id"]: p for p in peers.list_peers()}[LENDER]["failed_units"] == 0
    again = work.lease(owner, LENDER, {"resources": ["embed"], "caps": {
        "work_api": work.WORK_API, "models": {"nomic-embed-text:latest": "sha256:x"}}})["unit"]
    assert again is None or again["id"] != a.order["id"]


def test_an_owner_of_a_previous_version_gets_the_result_as_before(lending, monkeypatch):
    """Une version d'avant ne dit pas « result_max » (et n'a pas de limite) : le résultat
    part comme avant."""
    a = lending["assignment"]
    a.order.pop("result_max", None)
    _computing(monkeypatch, lambda *args: _indexed(rows=40, width=1024))
    assert lending["lender"]._process(a).startswith("faite")
    assert "work/result" in [action for action, _ in lending["asked"]]


def test_the_size_measured_here_is_the_size_of_the_request(lending):
    """La taille estimée avant l'envoi couvre la requête scellée réelle (network/envelope.py),
    sans la dépasser de beaucoup : un résultat qui passe n'est pas refusé ici."""
    from aiodos.network import envelope
    body = {"unit": 1, "compute_s": 1.5, "output": wire.pack(_indexed(
        "Électricité : « été » à 30 °C.\n\tFin", rows=200, width=384))}
    sealed = envelope.wrap(b"k" * 32, "work/result", "a" * 32, "b" * 32, body)
    real = len(json.dumps(sealed).encode())
    estimated = worker._sealed_size(body)
    assert real <= estimated <= real + 8192
