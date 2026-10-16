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
