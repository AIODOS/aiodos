"""Réseau public, phase 3.0 (network/public.py) : désactivé par défaut ; activé, le compagnon
reçoit un profil sans rien de personnel, et ne le reçoit de nouveau que s'il change ou
si le compagnon a été relancé. Un compagnon trop ancien est dit clairement. Le prêt aux
inconnus dans le profil (lends, free, tps) : tests/test_public_lending.py."""
import asyncio

import pytest

from aiodos import __version__, settings
from aiodos.network import public as public_mod
from aiodos.network.companion import CompanionError


class Companion:
    running = True

    def __init__(self, fail=None):
        self.generation = 1
        self.calls = []
        self.fail = fail

    def api(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        if self.fail:
            raise CompanionError(self.fail)
        return {"enabled": bool(body and body.get("enabled")) or method == "GET",
                "key": "ab" * 32, "neighbors": 2,
                "nodes": [{"key": "cd" * 32, "version": "2.1.0", "models": ["mistral:7b"],
                           "lends": False, "seen_s": 3}],
                "rendezvous": {"announced_at": 1, "found": 1, "error": None}, "error": None}


class Internet:
    def __init__(self, companion):
        self.companion = companion
        self.enabled = True
        self.launches = 1


class Manager:
    def __init__(self, companion):
        self.internet = Internet(companion)


@pytest.fixture
def models(monkeypatch):
    monkeypatch.setattr(public_mod.ollama, "list_models",
                        lambda: [{"name": "qwen3.5:9b"}, {"name": "embeddinggemma:latest"}])


def test_off_by_default_and_nothing_personal_when_on(models):
    companion = Companion()
    pub = public_mod.Public(Manager(companion))
    assert settings.get("public_network") is False
    asyncio.run(pub.sync())
    assert companion.calls == [("PUT", "/net/v1/public", {"enabled": False})]
    asyncio.run(pub.sync())                       # rien de nouveau à dire
    assert len(companion.calls) == 1
    settings.set_value("public_network", True)
    asyncio.run(pub.sync())
    assert companion.calls[-1] == ("PUT", "/net/v1/public", {
        "enabled": True, "version": __version__, "lends": False, "free": 0,
        "models": ["embeddinggemma:latest", "qwen3.5:9b"]})
    asyncio.run(pub.sync())                       # le même profil : l'état seulement
    assert companion.calls[-1][:2] == ("GET", "/net/v1/public")
    snap = pub.snapshot()
    assert snap["joined"] and snap["count"] == 1 and snap["neighbors"] == 2
    assert snap["nodes"][0]["models"] == ["mistral:7b"]
    companion.generation += 1                     # compagnon relancé : on lui redit tout
    asyncio.run(pub.sync())
    assert companion.calls[-1][0] == "PUT"


def test_a_new_companion_is_told_again(models):
    """Réseau local ou Internet relancés : un nouveau compagnon repart de zéro, et son
    compteur de lancements vaut celui de l'ancien. Il faut lui redire de rejoindre le réseau
    public (essai du 18/10 : après un arrêt du réseau, le PC n'y revenait jamais)."""
    settings.set_value("public_network", True)
    first = Companion()
    manager = Manager(first)
    pub = public_mod.Public(manager)
    asyncio.run(pub.sync())
    assert first.calls[-1][0] == "PUT"
    second = Companion()                          # même generation que le premier
    manager.internet.companion = second
    manager.internet.launches += 1
    asyncio.run(pub.sync())
    assert second.calls == [("PUT", "/net/v1/public", first.calls[-1][2])]


def test_a_companion_that_left_is_asked_again(models):
    """Le compagnon dit ne plus être dans le réseau public alors qu'on le lui a demandé :
    au tour suivant, on le lui redemande."""
    settings.set_value("public_network", True)
    companion = Companion()
    pub = public_mod.Public(Manager(companion))
    asyncio.run(pub.sync())
    companion.api = lambda method, path, body=None, timeout=None: (
        companion.calls.append((method, path, body)) or {"enabled": method == "PUT", "nodes": []})
    asyncio.run(pub.sync())                       # GET : il n'y est plus
    asyncio.run(pub.sync())
    assert [c[0] for c in companion.calls] == ["PUT", "GET", "PUT"]


def test_an_older_companion_is_said_plainly(models):
    settings.set_value("public_network", True)
    pub = public_mod.Public(Manager(Companion(fail="404 : Chemin inconnu.")))
    asyncio.run(pub.sync())
    assert "prochaine mise à jour" in pub.snapshot()["error"]


def test_without_internet_nothing_is_said():
    pub = public_mod.Public(Manager(None))
    asyncio.run(pub.sync())
    assert pub.snapshot()["joined"] is False and pub.snapshot()["key"] is None


def test_the_api_asks_for_internet_first():
    from fastapi.testclient import TestClient
    from aiodos.api.server import create_app
    with TestClient(create_app("jeton", {"testserver"}, slots={"embed": 0})) as client:
        headers = {"X-Aiodos-Token": "jeton"}
        response = client.post("/api/network/public", json={"enabled": True}, headers=headers)
        assert response.status_code == 400 and "Internet" in response.json()["detail"]
        assert settings.get("public_network") is False
        snap = client.get("/api/network", headers=headers).json()["public"]
        assert snap["wanted"] is False and snap["joined"] is False


def test_the_choice_of_machines_reads_a_fresh_directory():
    """Le choix des machines (placement.py) relit l'annuaire au compagnon s'il date de plus de
    FRESH_S secondes : un prêteur redevenu libre est vu sans attendre la synchronisation
    suivante (15 s). Essai réel du 28/10/2026 : « free » vu avec jusqu'à 90 s de retard, et
    un téléphone sans modèle refusait de converser alors que la machine qui prête était libre.
    Pas rejoint, compagnon arrêté ou qui ne répond pas : l'état gardé, sans erreur."""
    from aiodos.network import route

    def older(seconds):                         # l'état gardé, lu il y a `seconds` de plus
        public_mod._latest["at"] -= seconds
    lender = {"key": "cd" * 32, "version": "1.0.0", "models": ["qwen3.5:9b"], "lends": True,
              "seen_s": 1}
    companion = Companion()
    companion.api = lambda method, path, body=None, timeout=None: (
        companion.calls.append((method, path, timeout)) or
        {"enabled": True, "key": "ab" * 32, "nodes": [dict(lender, free=companion.free)]})
    companion.free = 1
    route.attach(companion)
    try:
        # Pas encore rejoint : rien n'est demandé au compagnon
        assert public_mod.seen(fresh=True) == (None, []) and companion.calls == []
        public_mod._remember(public_mod._typed(
            {"enabled": True, "key": "ab" * 32, "nodes": [dict(lender, free=0)]}))
        # Lu il y a moins de FRESH_S secondes : l'état gardé
        older(public_mod.FRESH_S - 1)
        assert public_mod.seen(fresh=True)[1][0]["free"] == 0 and companion.calls == []
        assert public_mod.seen()[1][0]["free"] == 0
        # Plus vieux : relu au compagnon, une fois, avec un délai court
        older(2)
        assert public_mod.seen(fresh=True)[1][0]["free"] == 1
        assert companion.calls == [("GET", "/net/v1/public", public_mod.FRESH_TIMEOUT)]
        assert public_mod.seen(fresh=True)[1][0]["free"] == 1 and len(companion.calls) == 1
        # Le compagnon ne répond pas : l'état gardé, sans erreur
        older(public_mod.FRESH_S + 1)
        companion.api = lambda *a, **k: (_ for _ in ()).throw(CompanionError("injoignable"))
        assert public_mod.seen(fresh=True)[1][0]["free"] == 1
        # Compagnon arrêté : rien n'est demandé
        companion.running = False
        companion.api = lambda *a, **k: pytest.fail("compagnon arrêté interrogé")
        assert public_mod.seen(fresh=True)[1][0]["free"] == 1
    finally:
        route.detach(companion)
