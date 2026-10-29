"""Chaque test travaille dans une base neuve, sans Ollama ni réseau, sur secteur et sur
une machine inactive depuis un jour, la mémoire vive assez libre (simulés : sur batterie,
pendant que quelqu'un se sert du PC, ou mémoire pleine, le travail de fond et le prêt
ralentiraient les tests, cf. jobs/pacing.py, network/worker.py et node/places.py ; les
nœuds lancés par les tests en héritent)."""
import hashlib
import os

# Réseau, Internet et prêt sont activés par défaut depuis le 20/10 (réseau public, applis et
# images des amis depuis le 27/10) : les essais les gardent éteints tant qu'ils ne les
# demandent pas (settings.FOR_MY_MACHINES, OPEN_FROM_THE_START), nœuds lancés compris
os.environ["AIODOS_QUIET"] = "1"
# Les essais lisent les textes en français, quelle que soit la langue de la machine qui les
# lance (nœuds lancés compris) ; test_i18n.py passe à l'anglais quand il le faut
os.environ["AIODOS_LANGUAGE"] = "fr"
# Le fichier de découverte des applications (gateway/discovery.py) : jamais le vrai
os.environ["AIODOS_DISCOVERY_FILE"] = "-"

import numpy as np
import pytest

from aiodos import db
from aiodos.i18n import language
from aiodos.jobs import pacing
from aiodos.knowledge import search
from aiodos.network import client, work
from aiodos.node import places
from aiodos.services import extract, ollama


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AIODOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIODOS_SIMULATE_POWER", "ac")
    monkeypatch.setenv("AIODOS_SIMULATE_IDLE", "86400")
    # Mémoire vive assez libre : les places (node/places.py) ne dépendent pas de ce qui tourne
    # sur la machine des essais
    monkeypatch.setenv(places.SIMULATE_MEMORY_ENV, "80")
    places.forget()
    search.invalidate()
    language.forget()                  # la langue choisie dans la base d'un essai précédent
    pacing.start()                     # ni conversation en cours, ni arrêt d'un test précédent
    ollama.set_thread_limit(None)
    # Ce qu'un essai précédent a retenu en mémoire : paquets de lecture « installés » (il a
    # pu en cacher un), échecs et demandes des amis (work.py), places des conversations et
    # appels abandonnés (client.py), tous par identifiant de machine
    extract._installed.cache_clear()
    work.forget_friends()
    client._turns.clear()
    client._lingering.clear()
    # Vitesse mesurée des réponses (profil public) et pause après une conversation publique
    from aiodos.network import public, public_chat, public_lending
    from aiodos.node import speed
    speed.forget()
    public_lending.forget()
    # Côté demandeur : nœuds publics vus, identifiants des conversations (public_chat.tag),
    # contrôles au hasard en cours (spot_check), prêteurs écartés après un échec (reputation),
    # machines proposées et leurs refus suspects (public_refusals)
    from aiodos.network import public_refusals, reputation, spot_check
    public.forget()
    public_chat.forget()
    spot_check.forget()
    reputation.forget()
    public_refusals.forget()
    # Applications (gateway/) : clés en cache, demandes d'accès, limites
    from aiodos.gateway import access_flow, apps, limits, open_page
    apps.forget_all()
    access_flow.forget()
    limits.forget()
    open_page.forget()
    yield tmp_path
    # L'historique du travail pour d'autres : ses lignes écrites dans la base de cet essai,
    # pas dans celle du suivant ; plus rien en cours
    from aiodos.network import lent_history
    lent_history.forget()
    db.close()


def _bag_of_words(texts, model=None, timeout=None):
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
