# AIODOS

> *AI* + *hodos*, « le chemin » en grec, celui de « méthode » et de « synode » (faire
> route ensemble) : **la voie de l'IA, faite ensemble**. Le projet s'appelait Cœur IA
> jusqu'au 16/10/2026 ; ses données sont reprises telles quelles.

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
| 1 — Réseau local | Découverte des machines, appairage, répartition, partage de puissance, modèles prêtés pour les conversations | ✅ en place |
| 2 — Cercle de confiance | Machines d'amis via Internet (NAT traversal) | en cours : compagnon Internet [`aiodos-net`](compagnon/README.md) (2.2), invitation à distance (2.4), partage avec les amis (2.5), mises à jour automatiques |
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
2. **Appairer** : les machines AIODOS du réseau apparaissent d'elles-mêmes (sinon,
   les ajouter par adresse IP). Un code à 6 chiffres s'affiche sur les deux écrans :
   l'appairage n'aboutit que s'il est confirmé **sur chacune des deux machines**.
3. **Prêter sa puissance** (vue « Cette machine ») : par défaut sur secteur seulement.
   Pendant que tu te sers du PC, le calcul prêté continue en priorité basse, jamais sur
   batterie, la carte graphique la moitié du temps au plus ; une conversation passe
   devant : le GPU lui revient. « Seulement quand je n'utilise pas ce PC » le réserve aux
   moments d'inactivité (10 minutes par défaut), comme l'économiseur d'écran de
   SETI@home : dès que l'utilisateur revient, les unités en cours sont rendues.

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
  seraient pas comparables. Une machine ne reçoit aussi que les documents qu'elle
  sait lire (paquets de lecture installés chez elle). La mémoire des conversations
  ne quitte jamais sa machine, et une bibliothèque peut être **gardée sur ce PC
  uniquement** (menu ⋯, cadenas).
- **Réciprocité** : chaque machine tient le compte du temps de calcul donné et reçu ;
  celles qui ont le plus calculé pour toi sont servies en premier.

**Prêter ses modèles pour les conversations** (vue « Cette machine », désactivé par
défaut) : une machine sans GPU, ou dont le modèle ne tient pas en mémoire, choisit dans
la conversation un modèle d'une autre machine (« Sur mes machines » dans le choix du
modèle). La réponse est calculée là-bas et arrive en flux chiffré, morceau par morceau ;
le bouton Stop coupe la génération là-bas. Le texte de la conversation passe par la
machine qui prête le temps de la réponse, sans y être enregistré, et son travail de fond
se met en pause pendant ce temps.

Une machine dédiée au calcul peut tourner sans fenêtre : `python -m aiodos --headless`.
`--slots embed=1,gpu=0` limite ce qu'elle exécute en parallèle, et
`requirements-node.txt` suffit à l'installer (ni fenêtre, ni Whisper).

**Le travail de fond passe après l'utilisateur** : indexations et calcul pour les autres
machines tournent en priorité basse et en mode efficacité du processeur ; pendant une
conversation, le calcul sur la carte graphique attend la fin de la réponse ; sur batterie,
il est ménagé (une tâche à la fois, des pauses, moins de fils de calcul), ou attend le
secteur, au choix. Mesures et détails : [docs/arriere-plan.md](docs/arriere-plan.md).

**Un téléphone Android** peut aussi calculer, sous Termux : il ne prête sa puissance
qu'en charge, batterie assez chargée et pas trop chaude, comme BOINC sous Android.
Pas à pas : [docs/android.md](docs/android.md).

**Avec des amis (phase 2)** : en confirmant un appairage, chaque machine dit si l'autre est
une des siennes ou celle d'un ami. Un ami ne reçoit que les bibliothèques « partagées avec mes
amis » (menu ⋯ de la bibliothèque), sous des noms de fichiers neutres, sans chemin ni nom de
bibliothèque, et ne voit de ta machine qu'un profil allégé. Ce qu'il calcule est vérifié par
échantillon : en cas d'écart, sa machine est mise de côté. La vue Réseau montre ce qui lui a été
confié. Un ami qui n'est pas sur ton réseau s'appaire sur invitation (vue Réseau, carte
« Internet ») : tu lui envoies un code, il te renvoie sa réponse, et vous confirmez chacun le
même code à 6 chiffres. Détails : [docs/phase2-internet.md](docs/phase2-internet.md), étapes 2.4
et 2.5.

**Par Internet (phase 2, en cours)** : un petit programme compagnon,
[`aiodos-net`](compagnon/README.md) (Rust, Iroh), relie les machines appairées quand
elles ne sont plus sur le même réseau (téléphone en 4G…) : une connexion chiffrée par
machine, en direct ou par un relais, qui n'accepte que les machines appairées. Il est
désactivé par défaut (réglage `internet_enabled`) ; le réseau local reste le premier
chemin essayé. Étude et plan : [docs/phase2-internet.md](docs/phase2-internet.md).

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

- **Double-clic sur `lancer.bat`** : ouvre AIODOS dans sa fenêtre.
- En ligne de commande : `python -m aiodos`
- Nœud sans fenêtre (machine de calcul) : `python -m aiodos --headless`
  (affiche l'adresse locale à ouvrir dans un navigateur).

Sous Windows, **fermer la fenêtre la cache seulement** : AIODOS reste dans la zone de
notification, près de l'horloge, et le nœud continue de tourner. Clic sur l'icône (ou
relancer AIODOS) : la fenêtre revient ; clic droit → « Quitter AIODOS » pour l'arrêter.
Réglable dans « Cette machine » → « Travail de fond ».

Au premier lancement, les données de la v1 (`legacy/data`) sont importées
automatiquement : conversations, dossiers, bibliothèques et réglages.

## Mises à jour automatiques

Une machine installée (chez un ami, un téléphone…) se met à jour seule, sans serveur :
une version publiée est **signée** par la clé de publication de l'auteur, passe de
machine appairée en machine appairée, s'installe **à côté** de la version actuelle, et
AIODOS redémarre dessus quand la machine est libre. Si elle ne démarre pas, la
précédente reprend la main aussitôt. Réglage : Réglages → Mises à jour (activé par
défaut). Une copie de développement (dépôt git) n'est jamais remplacée.

Publier : `python tools/publish.py release --notes "…"` après avoir écrit le numéro dans
`aiodos/__init__.py`. Détails, sécurité et limites : [docs/mises-a-jour.md](docs/mises-a-jour.md).

## Architecture

```
aiodos/
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
│   ├── pacing.py      cadence du travail de fond : conversations d'abord, batterie ménagée
│   ├── leases.py      baux : unités confiées pour un temps limité à une autre machine
│   ├── registry.py    tâches portables : calcul (n'importe où) / intégration (chez soi)
│   └── kinds/         types de travaux (indexation, réindexation…)
├── network/           réseau local (protocole « aiodos/1 »)
│   ├── discovery.py   annonces UDP sur le réseau local
│   ├── pairing.py     appairage par code à 6 chiffres (X25519 → HKDF)
│   ├── envelope.py    messages scellés, horodatés, anti-rejeu (ChaCha20-Poly1305)
│   ├── p2p_app.py     serveur entre nœuds, séparé de l'API locale
│   ├── work.py        travail confié : bail, fichier par morceaux, résultats
│   ├── worker.py      partage de puissance : calculer pour ses autres machines
│   ├── lending.py     prêter ses modèles pour les conversations des autres machines
│   ├── remote_models.py  utiliser dans ses conversations les modèles prêtés
│   ├── route.py       chemin vers une machine : réseau local s'il répond, Internet sinon
│   ├── companion.py   lancer et surveiller le compagnon Internet aiodos-net
│   ├── internet.py    le compagnon vu du réseau : liste blanche, état par machine
│   └── netinfo.py     clés Internet échangées entre machines appairées (net/info)
├── node/              la machine : profil (identity.py), alimentation (power.py),
│                      priorité du travail de fond (priority.py), Android (android.py)
├── updates/           mises à jour signées : transmission entre machines, installation
│                      à côté, lanceur avec retour en arrière (docs/mises-a-jour.md)
├── api/               API locale + flux d'événements (SSE)
└── ui/                fenêtre native, icône de la zone de notification (tray.py),
                       interface web (HTML/CSS/JS, sans compilation)
compagnon/             aiodos-net : compagnon Internet en Rust (Iroh), cf. son README
tests/                 tests automatisés (pytest)
tools/                 publication des versions, outils de mesure (modèle d'embedding,
                       coût du travail de fond : bench_background.py)
docs/                  notes de mesures et de choix techniques, téléphone Android
legacy/                v1 (juin 2026, PyQt6), conservée et toujours lançable
```

**Sécurité de l'API locale** : elle n'écoute que sur `127.0.0.1`, vérifie l'en-tête
`Host` (contre le *DNS rebinding*) et exige un jeton tiré au hasard à chaque lancement.

## Données

Tout est dans `data/` (base `aiodos.db`, journaux) : ce dossier n'est jamais versionné.

## Tests

```
python -m pytest tests
```

## Licence

Apache-2.0 — voir [LICENSE](LICENSE) et [NOTICE](NOTICE). Chacun peut utiliser, modifier et
redistribuer AIODOS, y compris dans un autre produit ou par une boutique d'applications, à
condition de garder la licence et les mentions d'auteur, et de signaler les fichiers
modifiés. Chaque contributeur accorde aussi ses brevets éventuels sur le code qu'il apporte.
Les contributions sont acceptées sous cette même licence.

[Ollama]: https://ollama.com
