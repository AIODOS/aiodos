"""Chaque test travaille dans une base neuve, sans Ollama ni réseau."""
import hashlib

import numpy as np
import pytest

from coeur import db
from coeur.knowledge import search


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COEUR_DATA_DIR", str(tmp_path))
    search.invalidate()
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
    from coeur.services import ollama
    monkeypatch.setattr(ollama, "embed", _bag_of_words)
    return _bag_of_words
