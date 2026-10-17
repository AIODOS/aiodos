"""Chaque test travaille dans une base neuve, sans Ollama ni réseau, et sur secteur
(simulé : sur batterie, le travail de fond ralentirait les tests, cf. jobs/pacing.py)."""
import hashlib

import numpy as np
import pytest

from aiodos import db
from aiodos.jobs import pacing
from aiodos.knowledge import search
from aiodos.services import ollama


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AIODOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "ac")
    search.invalidate()
    pacing.start()                     # ni conversation en cours, ni arrêt d'un test précédent
    ollama.set_thread_limit(None)
    yield tmp_path
    db.close()


def _bag_of_words(texts, model=None):
    """Embedding factice et déterministe : un sac de mots haché sur 64 dimensions."""
    vectors = []
    for text in texts:
        v = np.zeros(64, dtype=np.float32)
        for word in text.lower().split():
            v[int(hashlib.md5(word.strip(".,:;!?").encode()).hexdigest(), 16) % 64] += 1
        vectors.append(v.tolist())
    return vectors


@pytest.fixture
def fake_embed(monkeypatch):
    from aiodos.services import ollama
    monkeypatch.setattr(ollama, "embed", _bag_of_words)
    return _bag_of_words
