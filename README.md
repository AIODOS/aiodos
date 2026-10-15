# Cœur IA

Atelier d'IA **100 % local** : chat, code et documents avec des modèles open source
(Mistral, Qwen…) via [Ollama], sur ta machine. **Plusieurs machines peuvent
travailler ensemble**, à la façon de SETI@home : chaque gros travail est découpé en
unités indépendantes, que tes PC appairés se partagent sur le réseau local.

> **Local par défaut, distribué à la demande.** Rien ne quitte la machine sans
> action explicite : le web et le cloud sont optionnels et validés avant chaque envoi.

## Où en est le projet

| Phase | Contenu | État |
|---|---|---|
| 0 — Fondations | Nœud local, file de tâches, interface, bibliothèques | ✅ en place |
| 1 — Réseau local | Découverte des machines, appairage, répartition, partage de puissance | ✅ en place (modèles servis par une autre machine : à venir) |
| 2 — Cercle de confiance | Machines d'amis via Internet (NAT traversal) | plus tard |
| 3 — Réseau public | Vérification des résultats, incitation, gouvernance | plus tard |

## Ce que fait la phase 0

- **Conversations** avec les modèles locaux (et Mistral/Qwen cloud en option),
  réponses en streaming, rendu Markdown et coloration du code, dossiers, renommage.
- **Réflexion des modèles** (Qwen3…) affichée en direct, désactivable pour aller vite.
- **Bibliothèques de connaissance** : fichiers, dossiers entiers, pages web ; tous les
  formats (PDF, Word, Excel, PowerPoint, code…), images (OCR + description par un
  modèle de vision) et audio/vidéo (transcription Whisper). L'IA cite ses sources.
  Changer de modèle d'embedding relance tout seul la réindexation.
- **Mémoire entre conversations** (optionnelle).
- **File de tâches** persistante : priorités (urgent / normal / de fond) puis ordre
  d'arrivée, pause, annulation, réessais, reprise automatique après une coupure.
  Une conversation passe toujours devant le travail de fond.
- **Profil du nœud** : GPU, VRAM, modèles installés (et ceux qui ne tiennent pas en
  VRAM), secteur/batterie, inactivité.

## Faire travailler plusieurs machines (phase 1)

1. **Activer le réseau local** sur chaque PC (vue « Réseau local »). Windows demande
   l'autorisation du pare-feu pour Python : l'accepter pour les **réseaux privés** seulement.
2. **Appairer** : les machines Cœur IA du réseau apparaissent d'elles-mêmes (sinon,
   les ajouter par adresse IP). Un code à 6 chiffres s'affiche sur les deux écrans :
   l'appairage n'aboutit que s'il est confirmé **sur chacune des deux machines**.
3. **Prêter sa puissance** (vue « Cette machine ») : par défaut, seulement quand le PC
   n'est pas utilisé depuis 10 minutes et qu'il est sur secteur, comme l'économiseur
   d'écran de SETI@home. Dès que l'utilisateur revient, les unités en cours sont
   rendues. Une conversation passe aussi devant : le GPU lui revient.

Comment le travail circule :

- **Modèle « pull »** : c'est la machine qui a de la puissance libre qui demande du
  travail. On lui confie une unité pour un temps limité (bail renouvelé tant qu'elle
  calcule) ; sans nouvelles, l'unité revient dans la file. Son propre travail passe
  toujours avant celui des autres.
- **Ce qui part** : le fichier de l'unité confiée, et lui seul, chiffré
  (ChaCha20-Poly1305, clé propre à chaque paire de machines), vérifié par empreinte
  SHA-256 et effacé dès le calcul fini. **Ce qui revient** : les passages et leurs
  vecteurs, contrôlés puis enregistrés **uniquement chez le propriétaire**.
- **Même modèle d'embedding exigé**, à l'empreinte près : sinon les vecteurs ne
  seraient pas comparables. La mémoire des conversations ne quitte jamais sa machine,
  et une bibliothèque peut être **gardée sur ce PC uniquement** (menu ⋯, cadenas).
- **Réciprocité** : chaque machine tient le compte du temps de calcul donné et reçu ;
  celles qui ont le plus calculé pour toi sont servies en premier.

Une machine dédiée au calcul peut tourner sans fenêtre : `python -m coeur --headless`.
`--slots embed=1,gpu=0` limite ce qu'elle exécute en parallèle.

## Installation

1. Installer [Ollama] et quelques modèles, par exemple :
   ```
   ollama pull qwen2.5-coder:7b
   ollama pull mistral:7b
   ollama pull embeddinggemma        # requis pour les bibliothèques (docs/embeddings.md)
   ollama pull qwen2.5vl:7b          # optionnel : lecture des images
   ```
2. Python 3.12 ou plus récent, puis :
   ```
   pip install -r requirements.txt
   ```

## Lancer

- **Double-clic sur `lancer.bat`** : ouvre Cœur IA dans sa fenêtre.
- En ligne de commande : `python -m coeur`
- Nœud sans fenêtre (machine de calcul) : `python -m coeur --headless`
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
│   ├── leases.py      baux : unités confiées pour un temps limité à une autre machine
│   ├── registry.py    tâches portables : calcul (n'importe où) / intégration (chez soi)
│   └── kinds/         types de travaux (indexation, réindexation…)
├── network/           réseau local (protocole « coeur/1 »)
│   ├── discovery.py   annonces UDP sur le réseau local
│   ├── pairing.py     appairage par code à 6 chiffres (X25519 → HKDF)
│   ├── envelope.py    messages scellés, horodatés, anti-rejeu (ChaCha20-Poly1305)
│   ├── p2p_app.py     serveur entre nœuds, séparé de l'API locale
│   ├── work.py        travail confié : bail, fichier par morceaux, résultats
│   └── worker.py      partage de puissance : calculer pour ses autres machines
├── node/identity.py   profil de la machine
├── api/               API locale + flux d'événements (SSE)
└── ui/                fenêtre native et interface web (HTML/CSS/JS, sans compilation)
tests/                 tests automatisés (pytest)
tools/                 outils de mesure (choix du modèle d'embedding)
docs/                  notes de mesures et de choix techniques
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
