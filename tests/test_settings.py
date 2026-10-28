"""Réglages : seuil de pertinence propre à chaque modèle d'embedding."""
from aiodos import settings


def test_default_model_and_its_threshold():
    assert settings.get("embed_model") == "embeddinggemma"
    assert settings.get("rag_min_score") == 0.40
    assert settings.get_all()["rag_min_score"] == 0.40


def test_threshold_follows_the_embedding_model():
    # Base importée de la v1 : nomic-embed-text enregistré, seuil jamais fixé
    settings.set_value("embed_model", "nomic-embed-text")
    assert settings.get("rag_min_score") == 0.70
    assert settings.get_all()["rag_min_score"] == 0.70
    settings.set_value("rag_min_score", 0.66)                 # fixé par l'utilisateur
    assert settings.get("rag_min_score") == 0.66
    settings.set_value("embed_model", "bge-m3:latest")         # nouveau modèle : valeur mesurée
    assert settings.get("rag_min_score") == 0.45
    assert settings.get_all()["rag_min_score"] == 0.45


def test_threshold_set_with_the_model_is_kept():
    settings.set_many({"embed_model": "bge-m3", "rag_min_score": 0.5})
    assert settings.get("rag_min_score") == 0.5
    settings.set_many({"embed_model": "bge-m3", "web_results": 4})   # même modèle
    assert settings.get("rag_min_score") == 0.5


def test_unknown_or_empty_model():
    settings.set_value("embed_model", "  mxbai-embed-large  ")
    assert settings.get("embed_model") == "mxbai-embed-large"
    assert settings.get("rag_min_score") == settings.DEFAULTS["rag_min_score"]
    settings.set_value("embed_model", "")                      # vide : modèle par défaut
    assert settings.get("embed_model") == "embeddinggemma"


def test_changing_a_companion_option_relaunches_it(data_dir, monkeypatch):
    """Étape 2.7 : relais, annuaires, ouverture de port : le compagnon repart avec."""
    from fastapi.testclient import TestClient

    from aiodos.api.server import create_app
    from aiodos.network.internet import Internet
    relaunched = []
    monkeypatch.setattr(Internet, "relaunch_from_thread", lambda self: relaunched.append(True))
    with TestClient(create_app("jeton", {"testserver"})) as client:
        headers = {"X-Aiodos-Token": "jeton"}
        client.put("/api/settings", json={"think": False}, headers=headers)
        assert relaunched == []
        values = client.put("/api/settings", json={
            "internet_relays": ["n0", "https://relais.example.org/"]}, headers=headers).json()
        assert relaunched == [True]
        assert values["internet_relays"] == ["n0", "https://relais.example.org/"]
        client.put("/api/settings", json={"internet_portmap": False}, headers=headers)
        assert relaunched == [True, True]


def test_my_machines_compute_together_from_the_first_launch(monkeypatch):
    """Décision de l'auteur (20/10) : réseau local, Internet, calcul et prêt entre mes
    machines activés dès la première ouverture ; le 27/10, le réseau public, le prêt d'images
    aux amis et l'accès des applications de ce PC aussi ; le 28/10, le prêt aux conversations
    d'inconnus. Faire calculer les miennes par mes amis reste sur autorisation. Les essais
    automatiques (AIODOS_QUIET=1) les gardent éteints."""
    monkeypatch.delenv("AIODOS_QUIET", raising=False)
    for key in settings.FOR_MY_MACHINES + settings.OPEN_FROM_THE_START:
        assert settings.get(key) is True and settings.get_all()[key] is True
    assert set(settings.OPEN_FROM_THE_START) == {"public_network", "lend_images_friends",
                                                 "gateway_apps", "lend_public"}
    for key in ("images_from_friends", "auto_route_friends"):
        assert settings.get(key) is False
    settings.set_value("internet_enabled", False)          # coupé ensuite : reste coupé
    assert settings.get("internet_enabled") is False
    monkeypatch.setenv("AIODOS_QUIET", "1")
    assert settings.get("network_enabled") is False
    assert settings.get("public_network") is False and settings.get("gateway_apps") is False
    assert settings.get("lend_public") is False
