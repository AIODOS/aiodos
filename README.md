# AIODOS

*English version: [README.en.md](README.en.md).*

> *AI* + *hodos*, « le chemin » en grec, celui de « méthode » et de « synode » (faire
> route ensemble) : **la voie de l'IA, faite ensemble**. Le projet s'appelait Cœur IA
> jusqu'au 16/10/2026 ; ses données sont reprises telles quelles.

Atelier d'IA **100 % local** : chat, code et documents avec des modèles open source
(Mistral, Qwen…), sur ta machine, servis par llama.cpp intégré à AIODOS (ou par [Ollama]). **Plusieurs machines peuvent
travailler ensemble** : chaque gros travail est découpé en petites unités
indépendantes, que tes PC appairés se partagent sur le réseau local.

> **Local par défaut, distribué à la demande.** Rien ne quitte la machine sans
> action explicite : le web et le cloud sont optionnels et validés avant chaque envoi.

AIODOS parle français et anglais : il suit la langue du système (navigateur, Windows,
Android), l'anglais quand celle-ci n'est ni l'une ni l'autre, et se change dans Réglages →
Langue.

## Où en est le projet

| Phase | Contenu | État |
|---|---|---|
| 0 — Fondations | Nœud local, file de tâches, interface, bibliothèques | ✅ en place |
| 1 — Réseau local | Découverte des machines, appairage, répartition, partage de puissance, modèles prêtés pour les conversations | ✅ en place |
| 2 — Cercle de confiance | Machines d'amis via Internet (NAT traversal) | en cours : compagnon Internet [`aiodos-net`](compagnon/README.md) (2.2), invitation à distance (2.4), partage avec les amis (2.5), bibliothèques consultables, mises à jour automatiques approuvées sur un deuxième appareil, gros modèles répartis entre mes machines (prototype), images et vidéos réparties (créées dans la conversation), réciprocité souple, clés protégées sur le disque (2.3), appairage par groupe : un ami appairé avec une de mes machines rencontre les autres, avec son accord ([docs/appairage-groupe.md](docs/appairage-groupe.md)) |
| 3 — Réseau public | Vérification des résultats, incitation, gouvernance | en conception : [docs/phase3-reseau-public.md](docs/phase3-reseau-public.md), [docs/phase3-incitation.md](docs/phase3-incitation.md) |

## Ce que fait la phase 0

- **Conversations** avec les modèles locaux (et Mistral/Qwen cloud en option),
  réponses en streaming, rendu Markdown et coloration du code, dossiers, renommage,
  recherche dans leur contenu ; « Régénérer » la dernière réponse (avec le modèle choisi
  à ce moment) et « Modifier » le dernier message, la suite de la conversation et sa
  trace en mémoire s'effaçant.
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
   **Sous un VPN** (NordVPN…) : AIODOS annonce sur chaque vrai réseau local (Wi-Fi,
   Ethernet) et montre l'adresse de la vraie carte, pas celle du VPN
   (`network/interfaces.py`). Il faut seulement que le VPN laisse passer le réseau local :
   chez NordVPN, « Invisibilité sur le réseau local » désactivée. Sinon, les machines déjà
   appairées passent toutes seules par Internet ; une nouvelle s'appaire par invitation.
3. **Prêter sa puissance** (vue « Cette machine ») : par défaut sur secteur seulement.
   Pendant que tu te sers du PC, le calcul prêté continue en priorité basse, jamais sur
   batterie, la carte graphique la moitié du temps au plus ; une conversation passe
   devant : le GPU lui revient. « Seulement quand je n'utilise pas ce PC » le réserve aux
   moments d'inactivité (10 minutes par défaut), comme un économiseur d'écran : dès
   que l'utilisateur revient, les unités en cours sont rendues.
4. **Voir qui est là** (vue Réseau, « Machines connectées » ; nombre de machines en
   ligne à droite de la barre du bas) : comme la liste des pairs d'un client BitTorrent,
   chaque machine appairée avec son chemin (réseau local, Internet en direct ou par un
   relais) et son temps de réponse, ce qu'elle prête (sa puissance, en pause ou non,
   ses modèles, sa carte graphique), ce qu'elle calcule pour toi et toi pour elle en ce
   moment, et le temps de calcul échangé. Un ami voit seulement si ton partage est
   activé, pas ton activité.

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
défaut) : une machine sans GPU, ou dont le modèle ne tient pas en mémoire, fait répondre
ses conversations par le modèle d'une autre machine. La réponse est calculée là-bas et
arrive en flux chiffré, morceau par morceau ; le bouton Stop coupe la génération là-bas.
Le texte de la conversation passe par la machine qui prête le temps de la réponse, sans y
être enregistré, et son travail de fond se met en pause pendant ce temps.

**La machine qui répond se choisit seule** : dans le choix du modèle, chaque modèle
n'apparaît qu'une fois, en mode « automatique ». La conversation part vers cette machine
si elle a le modèle et qu'elle est libre (sauf s'il n'y tient pas en mémoire vidéo alors
qu'une autre machine libre l'y fait tenir), sinon vers la moins occupée de tes machines
qui le prêtent ; chaque réponse dit quelle machine l'a calculée, et une machine qui ne
répond pas cède la place à la suivante. On peut aussi désigner une machine précise, à côté
du modèle. Les machines d'un ami ne répondent d'office que si on l'accepte (Réglages →
Conversation). Quelle que soit la machine, une bibliothèque gardée sur ce PC et la mémoire
des conversations ne la suivent jamais, ni les réponses qui s'en sont servies, ni celles qui
ont pu les reprendre ensuite dans la même conversation (l'historique envoyé les remplace par
une mention) ; chez un ami, seules les bibliothèques partagées avec tes amis donnent leurs
extraits. Un modèle du cloud, après validation, ne reçoit ni les bibliothèques gardées sur ce
PC, ni la mémoire des conversations.

**Ouvrir AIODOS aux applications** (PANDORA, ARKLIGHT PC…) : chaque appli demande l'accès
d'elle-même ; une carte s'affiche dans AIODOS avec un code à 6 chiffres à comparer, ce qu'elle
pourra faire et où ses calculs pourront se faire (ce PC, mes machines, mes amis, les inconnus
du réseau public), à décocher au besoin. Le premier accord ouvre la passerelle ; chaque appli a
sa clé, retirable d'un clic. Bibliothèques clientes sans dépendance, Python et JavaScript,
sous Apache-2.0 (`clients/`) : [docs/integrer-aiodos.md](docs/integrer-aiodos.md), référence
[docs/api-applications.md](docs/api-applications.md).

**Ouvrir AIODOS aux autres outils** (Réglages → Applications et autres outils, désactivé par
défaut) : une API compatible OpenAI et Ollama, et un serveur MCP, sur cette machine seulement
(`127.0.0.1`, port fixe 11435, clé générale ou clé « à la main » par outil). Open WebUI, AnythingLLM, Continue, un
éditeur de code ou un assistant (Claude Desktop, Claude Code…) y trouvent les modèles de
cette machine et ceux que te prêtent tes machines, avec le même choix automatique, et
cherchent dans tes bibliothèques. **Sans Ollama** : le moteur intégré (llama.cpp) sert ces
API lui-même, outils (*tool calling*), images, format de réponse imposé, complétion de code
et embeddings compris ; ce qu'il ne sait pas faire est refusé clairement. Pas à pas :
[docs/autres-outils.md](docs/autres-outils.md).

Une machine dédiée au calcul peut tourner sans fenêtre : `python -m aiodos --headless`.
`--slots embed=1,gpu=0` limite ce qu'elle exécute en parallèle, et
`requirements-node.txt` suffit à l'installer (ni fenêtre, ni Whisper).

**Le travail de fond passe après l'utilisateur** : indexations et calcul pour les autres
machines tournent en priorité basse (et sous Windows, en mode efficacité du processeur) ;
pendant une conversation, le calcul sur la carte graphique attend la fin de la réponse ;
sur batterie, il est ménagé (une unité à la fois par ressource, des pauses, moins de fils
de calcul), ou attend le secteur, au choix. Mesures et détails : [docs/arriere-plan.md](docs/arriere-plan.md).

**Un téléphone Android** peut aussi calculer, avec l'appli AIODOS, qui fait tourner le
nœud elle-même (rien d'autre à installer) : il ne prête sa puissance qu'en charge,
batterie assez chargée et pas trop chaude, comme BOINC sous Android. Pas à pas :
[docs/android.md](docs/android.md).

**Avec des amis (phase 2)** : en confirmant un appairage, chaque machine dit si l'autre est
une des siennes ou celle d'un ami. Un ami ne reçoit que les bibliothèques « partagées avec mes
amis » (menu ⋯ de la bibliothèque), sous des noms de fichiers neutres, sans chemin ni nom de
bibliothèque, et ne voit de ta machine qu'un profil allégé. Ce qu'il calcule est vérifié par
échantillon : en cas d'écart, sa machine est mise de côté. La vue Réseau montre ce qui lui a été
confié. Un ami qui n'est pas sur ton réseau s'appaire sur invitation (vue Réseau, carte
« Internet ») : tu lui envoies un code, il te renvoie sa réponse, et vous confirmez chacun le
même code à 6 chiffres. Détails : [docs/phase2-internet.md](docs/phase2-internet.md), étapes 2.4
et 2.5.

**Chercher dans les bibliothèques des autres, sans copier leurs fichiers** : une bibliothèque
peut être « consultable par » mes machines, ou mes machines et mes amis (menu ⋯ ; personne
par défaut). Dans une bibliothèque ouverte, on ferme un dossier ou un document précis (œil
barré), et une bibliothèque gardée sur ce PC n'est jamais consultable. Côté demandeur,
l'option « Chez les autres » de la conversation, ou la recherche de la vue Bibliothèques,
envoie la question aux machines qui lui ouvrent quelque chose (avec son accord pour un ami) :
chacune cherche chez elle, avec son propre modèle, et ne rend que quelques passages, cités
avec le nom de la machine. Au plus 8 passages par recherche et 10 recherches par minute ; chaque
recherche reçue est notée dans la vue Réseau, sans la question. Détails :
[docs/phase2-internet.md](docs/phase2-internet.md), « Bibliothèques consultables ».

**Par Internet (phase 2, en cours)** : un petit programme compagnon,
[`aiodos-net`](compagnon/README.md) (Rust, Iroh), relie les machines appairées quand
elles ne sont plus sur le même réseau (téléphone en 4G…) : une connexion chiffrée par
machine, en direct ou par un relais, qui n'accepte que les machines appairées. Il est
désactivé par défaut (réglage `internet_enabled`) ; le réseau local reste le premier
chemin essayé. Étude et plan : [docs/phase2-internet.md](docs/phase2-internet.md).

**Un modèle trop gros pour une seule machine (prototype)** se découpe entre mes machines
avec [llama.cpp] : chacune en garde une tranche de couches sur sa carte graphique le temps
de la conversation, la machine qui pose la question gardant la fin du modèle. Il apparaît
alors en « réparti » dans le choix du modèle. Il faut, sur les machines qui prêtent,
« Prêter ma carte graphique pour les gros modèles » (vue « Cette machine », activé par
défaut), et partout le compagnon : les tranches passent par ses tunnels chiffrés, entre
mes machines seulement, jamais chez un ami. llama.cpp est livré avec les mises à jour
signées, la même version partout. Dès qu'on le choisit, il se prépare pendant qu'on écrit
la question (cartes demandées, chargement), et la conversation montre où il en est ;
ensuite, chaque machine garde les poids en cache.
Mesures, choix et limites : [docs/gros-modeles.md](docs/gros-modeles.md) ; images et
vidéo : [docs/generation-repartie.md](docs/generation-repartie.md).

**Créer des images et des vidéos, dans la conversation, à plusieurs machines** : on les
demande comme à un autre assistant (« Crée une image d'un phare sous l'orage »), ou avec
le bouton Image à côté du message (modèle, format, durée, nombre), et on les retouche de
la même façon (« Fais-la de nuit » : la nouvelle image part de la précédente ; « Encore
une » : une variante). Chacune devient une
unité de la file, calculée ici ou par mes machines qui ont le même modèle et prêtent leur
puissance ; elle s'affiche dans la conversation et n'est gardée qu'ici (dans
`Images\AIODOS` pour AIODOS installé, ou le dossier choisi dans les Réglages). La vue
Images et vidéos réunit la galerie et les modèles. Le moteur est
[stable-diffusion.cpp], livré avec les mises à jour, et le modèle Z-Image Turbo
(Apache-2.0, 6,7 Go, chaque fichier vérifié par son empreinte) : téléchargé depuis la vue,
ou copié d'une de mes machines qui l'a déjà, sans Internet. Sur une
RTX 4070 de portable : 22 s par image en 1024 × 1024. Pour la vidéo, Wan 2.1 (Apache-2.0,
7,2 Go) : 2 s de vidéo en 480p en moins de 4 minutes ; MiniMax H3 (vidéo et son) est aussi
proposé, sa licence affichée (elle exclut notamment l'Union européenne : à chacun d'en
juger). Un ami ne calcule mes images que si nous l'acceptons tous les deux. Détails et
mesures :
[docs/generation-repartie.md](docs/generation-repartie.md).

## Installation

**Windows, avec l'installeur** (`AIODOS-Setup-<version>.exe`) : il installe AIODOS pour
toi seul, sans droits administrateur, avec son propre Python, le compagnon Internet,
llama.cpp et stable-diffusion.cpp. Plus besoin d'Ollama : le moteur intégré (llama.cpp)
télécharge ses modèles lui-même, et lit tels quels ceux qu'Ollama a déjà téléchargés
([docs/moteur-integre.md](docs/moteur-integre.md)). Tes
données vont dans `%LOCALAPPDATA%\AIODOS`, et les versions suivantes arrivent par les
mises à jour signées. L'installeur n'est pas encore signé : Windows demande de confirmer
(« Informations complémentaires », puis « Exécuter quand même »). Pour le construire :
`python tools/build_installer.py` (Inno Setup 6, `dist/`). Étude complète :
[docs/installeurs.md](docs/installeurs.md).

**Téléphone Android** : l'appli AIODOS (`python tools/build_android.py --install`) fait
tourner le nœud dans son propre processus (Python par Chaquopy, llama.cpp et le compagnon
Internet rangés dans l'appli), en arrière-plan avec sa notification, et affiche son
interface ([docs/android.md](docs/android.md)). Ni Termux ni Ollama.

**À la main**, depuis le code :

1. Le moteur intégré a besoin de llama.cpp (réglage `llamacpp_dir`, ou celui livré avec
   les versions) ; sinon, installer [Ollama] et quelques modèles, par exemple :
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

**Démarrer avec l'appareil, en arrière-plan** (« Cette machine » → « Travail de fond ») :
sous Windows, un raccourci dans le dossier Démarrage lance AIODOS sans ouvrir sa fenêtre
(`python -m aiodos --background`, l'icône près de l'horloge la rouvre) ; sur un téléphone,
l'appli AIODOS démarre son nœud quand le téléphone s'allume (activé dès la première
ouverture).

Au premier lancement, les données de la v1 (`legacy/data`) sont importées
automatiquement : conversations, dossiers, bibliothèques et réglages.

## Mises à jour automatiques

Une machine installée (chez un ami, un téléphone…) se met à jour seule, sans serveur :
une version publiée est **signée** par la clé de publication de l'auteur, passe de
machine appairée en machine appairée, s'installe **à côté** de la version actuelle, et
AIODOS redémarre dessus quand la machine est libre. Si elle ne démarre pas, la
précédente reprend la main aussitôt. Réglage : Réglages → Mises à jour (activé par
défaut). Une copie de développement (dépôt git) n'est jamais remplacée. Une version
emporte aussi le compagnon Internet et les moteurs llama.cpp et stable-diffusion.cpp,
vérifiés par leur empreinte.

**Approbation sur un deuxième appareil** : une fois les appareils déclarés (téléphone de
l'auteur et appareil de secours), une version publiée attend d'être approuvée sur l'un
d'eux (Réglages → Mises à jour → « Versions en attente ») avant de partir. Une clé de
publication volée ne suffit plus à pousser une version chez les amis. Tant qu'aucun
appareil n'est déclaré, rien ne change.

Publier : `python tools/publish.py release --notes "…"` après avoir écrit le numéro dans
`aiodos/__init__.py`. Détails, sécurité et limites : [docs/mises-a-jour.md](docs/mises-a-jour.md).

## Architecture

```
aiodos/
├── __main__.py        lancement : nœud (FastAPI) + fenêtre native (pywebview / WebView2)
├── db.py              base SQLite unique (WAL, transactions)
├── settings.py        réglages (les clés API ne sortent jamais en clair)
├── i18n/              textes en français et en anglais : un fichier par fonctionnalité et
│                      par langue (fr/*.json, en/*.json), langue de chaque requête
├── render.py          Markdown → HTML sûr, coloration Pygments
├── llm/               moteur intégré : llama.cpp en mode routeur (engine.py), modèles
│                      d'AIODOS et d'Ollama (models.py), catalogue et téléchargement
│                      (catalog.py, download.py), mêmes fonctions qu'Ollama (integrated.py)
├── services/          modèles (ollama.py : moteur intégré ou Ollama), extraction de
│                      fichiers, médias, web, cloud
├── knowledge/         bibliothèques : stockage, découpage, recherche vectorielle,
│                      ce qui en est consultable par les autres machines (visibility.py)
├── chat/              conversations et préparation des réponses
├── imaging/           images réparties : modèles épinglés (catalog.py), téléchargement,
│                      sd-server (engine.py), une image (generate.py), galerie
├── cluster/           gros modèles répartis entre mes machines (llama.cpp RPC) :
│                      découpage (plan.py), carte prêtée (helper.py), groupe (group.py),
│                      moteur livré avec les versions (delivery.py), docs/gros-modeles.md
├── gateway/           AIODOS ouvert aux applications et aux autres outils
│   │                  (docs/integrer-aiodos.md, docs/api-applications.md, docs/autres-outils.md)
│   ├── server.py      serveur sur 127.0.0.1, port fixe ; Guard : hôte, pages web, clé, appelant,
│   │                  limites, droits, journal
│   ├── apps.py        une clé par appli (seul le haché est gardé), clés à la main
│   ├── access_flow.py demande d'accès (RFC 8628), carte d'accord, sourdine ; access_api.py : routes
│   ├── app_connect.py se connecter passerelle fermée (python -m aiodos --app-request)
│   ├── caller.py      programme et compte Windows de l'appelant (table TCP, jeton)
│   ├── scopes.py      droits, cercles (here, mine, friends, public), priorité
│   ├── web_guard.py   aucune page web (Origin, Fetch Metadata, JSON exigé)
│   ├── info.py        /aiodos/v1/info et la preuve d'identité ; discovery.py : gateway.json
│   ├── errors.py      codes d'erreur stables, en français et en anglais
│   ├── limits.py      débit et simultanéité par appli ; usage.py : journal sans contenu
│   ├── model_card.py  fiches des modèles ; models_api.py : /aiodos/v1/models ; open_page.py
│   ├── access.py      clé générale des autres outils (DPAPI)
│   ├── openai_api.py  API compatible OpenAI (/v1/…)
│   ├── ollama_api.py  API compatible Ollama (/api/…)
│   ├── mcp_server.py  serveur MCP : bibliothèques et conversation pour les assistants
│   ├── mcp_stdio.py   pont stdio, pour les assistants qui lancent un programme
│   ├── catalog.py     modèles proposés et leurs identifiants
│   ├── answer.py      réponse par cette machine ou par une machine appairée, selon le lieu
│   └── engine.py      réponse par le moteur intégré, sans Ollama (routeur de llama.cpp)
├── jobs/              file de tâches : travaux découpés en unités (modèle BOINC)
│                      qui se comporte comme un torrent : pauses, reprise sans perte
│                      (docs/file-de-travaux.md)
│   ├── queue.py       persistance, priorités, réessais, reprise
│   ├── executor.py    exécution selon les ressources (GPU, embeddings, CPU, réseau)
│   ├── pacing.py      cadence du travail de fond : conversations d'abord, batterie ménagée
│   ├── leases.py      baux : unités confiées pour un temps limité à une autre machine
│   ├── registry.py    tâches portables : calcul (n'importe où) / intégration (chez soi)
│   └── kinds/         types de travaux (indexation, réindexation, images…)
├── network/           réseau local (protocole « aiodos/1 »)
│   ├── discovery.py   annonces UDP sur le réseau local
│   ├── interfaces.py  vraies cartes réseau (Wi-Fi, Ethernet), sans celles d'un VPN
│   ├── pairing.py     appairage par code à 6 chiffres (X25519 → HKDF)
│   ├── group.py       appairage par groupe : une machine en présente une autre, sans code
│   ├── envelope.py    messages scellés, horodatés, anti-rejeu (ChaCha20-Poly1305)
│   ├── p2p_app.py     serveur entre nœuds, séparé de l'API locale
│   ├── work.py        travail confié : bail, fichier par morceaux, résultats
│   ├── worker.py      partage de puissance : calculer pour ses autres machines
│   ├── lending.py     prêter ses modèles pour les conversations des autres machines
│   ├── remote_models.py  utiliser dans ses conversations les modèles prêtés
│   ├── placement.py   choisir seul la machine qui répond (mode automatique)
│   ├── consult.py     être consulté : recherche dans mes bibliothèques ouvertes, limites, journal
│   ├── remote_search.py  chercher dans les bibliothèques que mes machines et mes amis m'ouvrent
│   ├── route.py       chemin vers une machine : réseau local s'il répond, Internet sinon
│   ├── companion.py   lancer et surveiller le compagnon Internet aiodos-net
│   ├── internet.py    le compagnon vu du réseau : liste blanche, état par machine
│   └── netinfo.py     clés Internet échangées entre machines appairées (net/info)
├── node/              la machine : profil (identity.py), alimentation (power.py),
│                      priorité du travail de fond (priority.py), Android (android.py),
│                      le nœud dans l'appli Android (android_app.py), programmes lancés
│                      arrêtés avec AIODOS (children.py), démarrage avec l'appareil
│                      (autostart.py)
├── updates/           mises à jour signées : transmission entre machines, installation
│                      à côté, lanceur avec retour en arrière, approbation sur un deuxième
│                      appareil (approval.py), moteurs livrés (engines.py) — docs/mises-a-jour.md
├── api/               API locale + flux d'événements (SSE)
└── ui/                fenêtre native, icône de la zone de notification (tray.py),
                       interface web (HTML/CSS/JS, sans compilation ; js/i18n.js : ses textes)
compagnon/             aiodos-net : compagnon Internet en Rust (Iroh), cf. son README
android/               l'appli Android : projet Gradle (Chaquopy), Java, paquets Python ;
                       ses textes dans res/values (anglais) et res/values-fr
tests/                 tests automatisés (pytest)
tools/                 publication des versions (moteurs joints : engine_release.py),
                       appli Android (build_android.py, android_wheels.py, android_deps.py),
                       outils de mesure (modèle d'embedding, coût du travail de fond :
                       bench_background.py, modèles répartis : bench_repartition.py)
docs/                  notes de mesures et de choix techniques, téléphone Android
legacy/                v1 (juin 2026, PyQt6), conservée et toujours lançable
```

## Traductions

Chaque texte visible est dans `aiodos/i18n/<langue>/<fonctionnalité>.json`, sous les mêmes
clés en français (`fr`, la langue de départ) et en anglais (`en`) : l'interface les reçoit
de son nœud (`GET /api/i18n`), le nœud répond à chaque requête dans la langue de
l'interface (`Accept-Language`), et l'appli Android a les siens dans `android/res/values*`.
Les tests (`tests/test_i18n*.py`) vérifient que chaque texte existe dans les deux langues et
qu'aucun ne reste écrit en dur dans le code. Les documents de `docs/` restent en français.

## Sécurité

**API locale** : elle n'écoute que sur `127.0.0.1`, vérifie l'en-tête `Host` (contre le
*DNS rebinding*) et exige un jeton tiré au hasard à chaque lancement. L'interface est
servie avec une politique de contenu stricte : seuls les scripts du nœud s'exécutent, et
rien ne part vers un autre hôte. Une réponse de modèle ne charge jamais d'image toute
seule, et ses liens ne s'ouvrent que vers le web ou un courriel. La passerelle des autres
outils, fermée par défaut, n'écoute elle aussi que sur `127.0.0.1`, vérifie `Host` et
`Origin` (une page web ne s'en sert pas) et exige sa clé d'accès, stable et renouvelable.

**Audit du 20/10/2026** : 37 failles confirmées, corrigées le 21/10 pour la version 2.0.2,
sauf une (le pont MCP de la passerelle, réécrite à part). Modèle de menace, failles,
bornes, compatibilité et ce qui reste ouvert : [docs/securite.md](docs/securite.md).
**L'appairage par code passe au protocole 2** : mettre à jour toutes les machines,
téléphone compris, avant d'en appairer de nouvelles ; les paires existantes continuent de
se parler.

## Données

Tout est dans `data/` (base `aiodos.db`, journaux) : ce dossier n'est jamais versionné.

## Tests

```
python -m pytest tests
```

Les essais de la passerelle avec les clients officiels d'OpenAI et de MCP demandent en plus
`pip install -r requirements-dev.txt` (de préférence dans un environnement à part) ; sans eux,
ils sont ignorés. L'essai réel des gros modèles répartis (`tests/test_cluster_e2e.py`) demande
llama.cpp (variable `AIODOS_LLAMACPP_DIR` : son dossier), le compagnon compilé et
`mistral:7b` ; sinon, il est ignoré.

## Licence

Apache-2.0 — voir [LICENSE](LICENSE) et [NOTICE](NOTICE). Chacun peut utiliser, modifier et
redistribuer AIODOS, y compris dans un autre produit ou par une boutique d'applications, à
condition de garder la licence et les mentions d'auteur, et de signaler les fichiers
modifiés. Chaque contributeur accorde aussi ses brevets éventuels sur le code qu'il apporte.
Les contributions sont acceptées sous cette même licence.

[Ollama]: https://ollama.com
[llama.cpp]: https://github.com/ggml-org/llama.cpp
[stable-diffusion.cpp]: https://github.com/leejet/stable-diffusion.cpp
