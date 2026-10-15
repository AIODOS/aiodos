# Cœur IA

Atelier d'IA **100 % local** : chat, code et documents avec des modèles open source
(Mistral, Qwen…) via [Ollama], sur ta machine. Conçu pour qu'à terme **plusieurs
machines travaillent ensemble**, à la façon de SETI@home : chaque gros travail est
découpé en unités indépendantes qui pourront être réparties entre les nœuds.

> **Local par défaut, distribué à la demande.** Rien ne quitte la machine sans
> action explicite : le web et le cloud sont optionnels et validés avant chaque envoi.

## Où en est le projet

| Phase | Contenu | État |
|---|---|---|
| 0 — Fondations | Nœud local, file de tâches, interface, bibliothèques | ✅ en place |
| 1 — Réseau local | Découverte des machines, appairage, répartition, partage de puissance | à venir |
| 2 — Cercle de confiance | Machines d'amis via Internet (NAT traversal) | plus tard |
| 3 — Réseau public | Vérification des résultats, incitation, gouvernance | plus tard |

## Ce que fait la phase 0

- **Conversations** avec les modèles locaux (et Mistral/Qwen cloud en option),
  réponses en streaming, rendu Markdown et coloration du code, dossiers, renommage.
- **Réflexion des modèles** (Qwen3…) affichée en direct, désactivable pour aller vite.
- **Bibliothèques de connaissance** : fichiers, dossiers entiers, pages web ; tous les
  formats (PDF, Word, Excel, PowerPoint, code…), images (OCR + description par un
  modèle de vision) et audio/vidéo (transcription Whisper). L'IA cite ses sources.
- **Mémoire entre conversations** (optionnelle).
- **File de tâches** persistante : priorités (urgent / normal / de fond) puis ordre
  d'arrivée, pause, annulation, réessais, reprise automatique après une coupure.
  Une conversation passe toujours devant le travail de fond.
- **Profil du nœud** : GPU, VRAM, modèles installés (et ceux qui ne tiennent pas en
  VRAM), secteur/batterie, inactivité — la base du partage de puissance de la phase 1.

## Installation

1. Installer [Ollama] et quelques modèles, par exemple :
   ```
   ollama pull qwen2.5-coder:7b
   ollama pull mistral:7b
   ollama pull nomic-embed-text      # requis pour les bibliothèques
   ollama pull qwen2.5vl:7b          # optionnel : lecture des images
   ```
2. Python 3.12 ou plus récent, puis :
   ```
   pip install -r requirements.txt
   ```

## Lancer

- **Double-clic sur `lancer.bat`** : ouvre Cœur IA dans sa fenêtre.
- En ligne de commande : `python -m coeur`
- Nœud sans fenêtre (futures machines de calcul) : `python -m coeur --headless`
  (affiche l'adresse locale à ouvrir dans un navigateur).

Au premier lancement, les données de la v1 (`legacy/data`) sont importées
automatiquement : conversations, dossiers, bibliothèques et réglages.

## Architecture

```
coeur/
├── __main__.py        lancement : nœud (FastAPI) + fenêtre native (pywebview / WebView2)
├── db.py              base SQLite unique (WAL, transactions)
├── settings.py        réglages (les clés API ne sortent jamais en clair)
├── render.py          Markdown → HTML sûr, coloration Pygments
├── services/          Ollama, extraction de fichiers, médias, web, cloud
├── knowledge/         bibliothèques : stockage, découpage, recherche vectorielle
├── chat/              conversations et préparation des réponses
├── jobs/              file de tâches : travaux découpés en unités (modèle BOINC)
│   ├── queue.py       persistance, priorités, réessais, reprise
│   ├── executor.py    exécution selon les ressources (GPU, embeddings, CPU, réseau)
│   └── kinds/         types de travaux (indexation…)
├── node/identity.py   profil de la machine
├── api/               API locale + flux d'événements (SSE)
└── ui/                fenêtre native et interface web (HTML/CSS/JS, sans compilation)
tests/                 tests automatisés (pytest)
legacy/                v1 (juin 2026, PyQt6), conservée et toujours lançable
```

**Sécurité de l'API locale** : elle n'écoute que sur `127.0.0.1`, vérifie l'en-tête
`Host` (contre le *DNS rebinding*) et exige un jeton tiré au hasard à chaque lancement.

## Données

Tout est dans `data/` (base `coeur.db`, journaux) : ce dossier n'est jamais versionné.

## Tests

```
python -m pytest tests
```

## Licence

GPL-3.0 — voir [LICENSE](LICENSE).

[Ollama]: https://ollama.com
