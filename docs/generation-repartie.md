# Images et vidéo réparties entre plusieurs machines

Étude du 17/10/2026, suite de [gros-modeles.md](gros-modeles.md). Le but reste celui de
l'utilisateur : faire tourner les plus gros moteurs en les répartissant, pour le texte
comme pour « tous les autres moteurs de génération ». Les chiffres de trafic sont des
calculs, pas des mesures.

## 1. Ce qui change par rapport aux modèles de texte

Un modèle de texte qui écrit est limité par la lecture de la mémoire, un mot à la fois. Le
couper en tranches ne fait voyager que l'état d'un mot (16 Kio) : c'est ce qui rend la
répartition de llama.cpp possible, même par Internet.

Un modèle d'images ou de vidéo est limité par le calcul. Chaque pas de débruitage traite
des milliers de jetons à la fois. Le couper en tranches fait voyager tout leur état à
chaque pas, 3 à 4 ordres de grandeur de plus qu'un modèle de texte :

| Ce qui traverse le réseau | Flux.1, 1024² | Wan-14B, 480p × 81 images |
|---|---|---|
| Texte encodé, une fois par demande | 8 Mo | 8 Mo |
| Latent, une fois par passage de main | 1 Mo | 8,4 Mo |
| État du cœur, par coupure et par pas | 57 Mo | 671 Mo (le double avec la CFG) |
| Résultat | PNG de 1 à 2 Mo | MP4 de quelques Mo |

Pour comparer : 1 Gbit/s fait environ 110 Mo/s, un envoi Internet de 10 à 50 Mbit/s
environ 1,2 à 6 Mo/s.

D'où trois règles :

- **Couper le cœur du modèle n'accélère jamais une image.** Cela n'apporte que de la
  mémoire, et seulement sur un réseau local filaire.
- **Sur ce PC, la mémoire vive suffit déjà.** Ces modèles calculent tant que garder leurs
  poids en mémoire vive coûte peu (stable-diffusion.cpp : `--offload-to-cpu`, « sans
  ralentir la génération »). Avec 8 Go de carte et 32 Go de mémoire vive, Flux, Qwen-Image
  ou Wan-14B tournent ici.
- **Ce qui vaut la peine de partir ailleurs** : des images entières (unités
  indépendantes), ou des morceaux qui n'échangent que le texte encodé ou un latent.

## 2. Les façons de répartir

| Façon | Ce qui voyage | Réseau | Gain |
|---|---|---|---|
| **Unités indépendantes** : graines, lots, tuiles d'agrandissement, tranches d'images pour l'agrandissement ou l'interpolation | La demande, puis le PNG ou le MP4 | Internet compris | Débit multiplié par le nombre de machines ; chacune a tout le modèle |
| **Encodeur de texte ailleurs** (T5-XXL, UMT5, Mistral-24B de Flux.2, Qwen3-VL de Qwen-Image-2.1) | 4 à 16 Mo par demande | Internet compris | Libère la carte d'ici : les encodeurs pèsent de 4 à 17 Go |
| **Décodeur (VAE) ailleurs** | Le latent | Internet compris | Peu de chose |
| **Wan 2.2 A14B, un expert par machine** (bruit fort, puis bruit faible) | Un latent de 8 Mo par vidéo | Internet compris | Débit doublé sur une file de vidéos |
| **CFG partagée** : passes avec et sans consigne sur deux machines | 8 à 17 Mo par pas | Réseau local | ×1,89 mesuré sur Wan-14B (deux cartes, une machine) ; rien pour les modèles distillés sans CFG (Flux dev, Turbo, Lightning) |
| **Cœur coupé en tranches** (ggml RPC, `diffusion=cuda0&RPC0`) | 57 à 1 300 Mo par pas | Local filaire seulement | De la mémoire, pas de vitesse |
| **Parallélisme à l'intérieur du cœur** (xDiT, PipeFusion, DistriFusion…) | Encore plus, à chaque couche | NVLink ou 100 Gbit/s | PyTorch et NCCL : Linux seulement ; écarté |

## 3. Les outils

- **stable-diffusion.cpp** (MIT, quasi quotidien, master-973 du 17/10/2026) est l'équivalent
  de llama.cpp pour les images et la vidéo, sur ggml lui aussi.
  - Versions Windows CPU, CUDA, Vulkan (cartes AMD et Intel) et ROCm.
  - Images : SD 1.x à 3.5, Flux.1, Flux.2, FLUX.3 Klein, Chroma, Qwen-Image, Z-Image.
  - Vidéo : Wan 2.1 et 2.2, LTX-2.x, HunyuanVideo 1.5.
  - Il sait placer chaque composant sur un appareil (`--backend te=…,diffusion=…,vae=…`),
    et utiliser des appareils distants par ggml RPC depuis juin 2026 (PR #1629).
  - **Réserve** : il lui faut un `rpc-server` construit à partir de son propre commit de ggml
    (`GGML_MAX_NAME=160`), pas celui de llama.cpp, et sans doute une construction à nous :
    les versions publiées n'ont peut-être pas le RPC.
- **LocalAI** (MIT) fait déjà ce chemin : son module stable-diffusion.cpp emploie les mêmes
  serveurs RPC que llama.cpp. C'est le précédent le plus proche d'AIODOS.
- **ComfyUI** est sous GPL-3.0 : seulement comme programme à part, jamais fusionné.
  ComfyUI-Distributed (Apache-2.0) répartit graines, lots et agrandissements, sur des
  cartes NVIDIA.
- **SwarmUI** (MIT) envoie des générations entières à des machines distantes.
- **AI Horde** est sous AGPL-3.0, avec un serveur central : écarté.

## 4. Les modèles

Taille du cœur seul, en Go :

| Modèle | 16 bits | Q8 | Q4 | Tient en 8 Go ? |
|---|---|---|---|---|
| SD 3.5 Large (8,1 milliards) | 16,3 | 8,8 | 4,8 | en Q4 |
| Flux.1 dev / schnell (12) | 23,8 | 12,7 | 6,8 | en Q4, juste |
| Flux.2-dev (32) | ≈64 | ≈34 | 18,7 | non, plus un encodeur Mistral-24B |
| FLUX.3 Klein (5,6) | ≈11 | — | — | oui, en 8 bits |
| Z-Image Turbo (6) | ≈12 | 7,2 | ≈4 | oui |
| Qwen-Image (20) | 40,9 | 21,8 | 13,1 | non |
| Wan 2.1 1.3B | ≈2,8 | — | — | oui |
| Wan-14B (chaque expert de Wan 2.2 A14B) | 29,1 | 15,9 | 10,1 | non |
| LTX-2.5 (22) | ≈44 | 22,7 | 11,5 | non, plus un encodeur Gemma-12B |

L'encodeur UMT5-XXL de Wan pèse 11,4 Go en 16 bits, 6,0 en Q8, 3,7 en Q4.

**Licences des modèles**, à vérifier avant d'en livrer un :

- sous Apache-2.0 : Wan, Qwen-Image, Z-Image, Flux.1-schnell, Flux.2-klein-4B ;
- non commerciaux : Flux.1-dev, Flux.2-dev ;
- HunyuanVideo exclut l'Union européenne.

## 5. Recommandation

1. **Livrer stable-diffusion.cpp avec les versions signées**, comme llama.cpp : la version
   Vulkan pour toutes les cartes, la version CUDA pour NVIDIA. AIODOS le pilote par son
   serveur HTTP (`sd-server`).
2. **D'abord, des unités indépendantes dans la file de travail actuelle.** Une unité, c'est
   un modèle (empreinte), une consigne, une graine et des réglages ; elle rend un PNG ou un
   MP4. Cela couvre les graines, les lots, les tuiles d'agrandissement, et marche par
   Internet comme sur les cartes AMD et Intel. Les modèles se partagent entre machines,
   empreinte vérifiée.
3. **Ensuite, l'encodeur de texte sur une autre machine**, par les tunnels du compagnon
   (`--rpc-servers 127.0.0.1:<tunnel> --backend te=RPC0,diffusion=cuda0,vae=cuda0`, cache
   `-c` pour n'envoyer l'encodeur qu'une fois). Le PC portable pourrait alors faire tourner
   Flux.2-dev ou LTX-2.5 avec l'encodeur chez une autre machine.
4. **Plus tard, les partages par latent** : un expert de Wan 2.2 par machine, la CFG sur
   deux machines. stable-diffusion.cpp ne sait pas encore s'arrêter et reprendre à un pas
   donné ; il faudra du code à nous.
5. **Écarté** : le parallélisme à l'intérieur du cœur (Linux, NVLink), et fusionner du code
   GPL ou AGPL.

## 6. Ce qu'AIODOS fait maintenant (17/10/2026)

L'étape 2 de la recommandation : des images entières, unités indépendantes de la file.

- **Vue Images** (`aiodos/ui/static/js/views/images.js`). On décrit l'image, on choisit le
  format (carré 1024², portrait, paysage, rapide 512²), le nombre (1 à 16) et, si l'on
  veut, la graine. La galerie montre chaque image avec la machine qui l'a calculée et le
  temps pris. « Reprendre » remet la description et la graine dans le formulaire.
- **Le modèle** : Z-Image Turbo (Apache-2.0), en trois fichiers épinglés dans
  `aiodos/imaging/catalog.py` (taille, empreinte SHA-256, adresse Hugging Face à un commit
  précis) : le cœur en Q4_K (3,9 Go), l'encodeur de texte Qwen3-4B en Q4_K_M (2,5 Go) et
  le décodeur de Flux (0,3 Go), en tout 6,7 Go. Un bouton les télécharge
  (`imaging/download.py`) : reprise après une coupure, chaque fichier vérifié avant d'être
  gardé dans `data/images/models/`. Quand une de mes machines a déjà le modèle, un autre
  bouton le copie depuis elle (`imaging/share.py`, action `images/file`, réservée à mes
  machines) : par morceaux de 8 Mo sur le réseau local, 512 Ko par Internet, même
  vérification. Mesure du 17/10/2026 entre deux nœuds de ce PC, chiffrement compris :
  6,7 Go en 3 min 15 (34 Mo/s ; 27 Mo/s avec des morceaux de 2 Mo).
- **Le moteur** (`imaging/engine.py`) : `sd-server` de stable-diffusion.cpp (master-973,
  Vulkan, 29 Mo), livré avec les versions signées comme llama.cpp
  (`tools/engine_release.py`, `updates/engines.py`). Un dossier choisi dans le réglage
  « Dossier de stable-diffusion.cpp » passe avant, un dossier posé à la main dans
  `data/engines/stable-diffusion.cpp` après. Il reste chargé entre deux images et
  s'arrête 1 min 30 après la dernière. Il calcule sur la meilleure carte : jamais celle intégrée au
  processeur quand il y en a une autre (sur ce PC, Vulkan0 est la carte Intel).
- **Une image = une unité** (`jobs/kinds/images.py`) : modèle, description, graine,
  taille et pas. Les graines d'une demande se suivent. Une de mes machines qui a le
  même modèle, aux mêmes empreintes, et qui prête sa puissance, en calcule sa part
  (`network/work.py`). Le PNG revient, il est contrôlé (signature, taille demandée), puis
  gardé ici seulement (`imaging/gallery.py`, table `images`). Une unité ne part jamais
  chez un ami : la description peut être personnelle. L'image n'emporte pas sa
  description dans ses métadonnées.
- **Une machine sans le modèle** laisse ses images en file pour celles qui l'ont
  (`TaskSpec.here`), sans retenir le reste de sa file (indexation…) ni l'empêcher de
  prêter sa carte. Sans aucune machine capable, la demande est refusée.
- **Une image à la fois par machine** : sd-server les calcule l'une après l'autre, et une
  image d'ici ne coupe pas celle d'une autre machine. Une image arrêtée pendant le
  chargement du modèle est rendue à la file, pas comptée en échec. Une machine qui
  calcule revérifie les réglages de l'ordre reçu, et refuse un ordre d'image d'un ami.
- **Essai réel de bout en bout** (`tests/test_images_e2e.py`, variables
  `AIODOS_SDCPP_DIR` et `AIODOS_IMAGE_MODELS`) : deux nœuds sur ce PC, A sans
  stable-diffusion.cpp, B avec, qui prête sa puissance. A demande deux images en 512² :
  B les calcule, A les garde, B n'en garde aucune. 51 s en tout le 17/10/2026, empreintes
  des 6,7 Go et chargement compris.
- **La carte graphique partagée avec Ollama.** Essai du 17/10/2026 : Ollama gardait deux
  modèles chargés (7,7 Go sur 8 avec sd-server), et la première image a échoué faute de
  mémoire. Désormais :
  - avant de charger le modèle d'images, AIODOS décharge de la carte les modèles de
    conversation d'Ollama (les modèles d'embedding restent) ;
  - si sd-server manque quand même de mémoire, il est relancé (nouveau plan de
    placement) et l'image refaite une fois ;
  - une conversation qui commence ici reprend la carte : sd-server inoccupé s'arrête
    aussitôt, et une image en cours finit d'abord.

**Mesures** (RTX 4070 Laptop 8 Go, Vulkan, Z-Image Turbo Q4_K, 8 pas) :

| | 1024 × 1024 | 512 × 512 |
|---|---|---|
| Chargement du modèle | 1,5 à 7 s | |
| Toute première image | 2 min 16 (compilation des shaders Vulkan : 70 s au premier pas, 26 s au décodage ; le pilote les garde ensuite) | |
| Image, PC au repos | 22 s (2 s par pas, encodeur 1,6 s, décodage 2,6 s) | |
| Image, suite de tests en parallèle | 35 s (3,7 s par pas) | 12 s |
| Carte partagée avec Ollama | | 47 s |

Le placement automatique de sd-server garde le cœur sur la carte (3,7 Go) et met
l'encodeur de texte et le décodeur en mémoire vive : 2,8 Go de carte occupés entre deux
images. Deux machines comme celle-ci font donc deux images en 22 s au lieu d'une.

Vérifié le 17/10/2026 : mistral:7b chargé dans Ollama (4,7 Go de carte), une image
demandée : AIODOS l'a déchargé, sd-server s'est chargé en 1,5 s, l'image a pris 22 s.

**Vulkan ou CUDA ?** Mesure du 17/10/2026, même PC, même modèle, PC au repos (version
CUDA 12 de master-973, 343 Mo, plus 563 Mo de bibliothèques CUDA) :

| | Vulkan (livrée, 29 Mo) | CUDA (900 Mo) |
|---|---|---|
| 1024 × 1024, 8 pas | 20,2 à 20,6 s | 19,1 à 19,5 s (−5 %) |
| 512 × 512 | 6,2 s | 4,6 s |

La version Vulkan reste celle livrée avec les mises à jour : elle sert toutes les cartes,
et CUDA ne gagne presque rien en 1024². Sur une carte NVIDIA, on peut choisir CUDA à la
main (réglage « Dossier de stable-diffusion.cpp »).

## 7. La vidéo (17/10/2026)

Même chemin que les images : une vidéo demandée est une unité de la file, calculée ici ou
par une de mes machines qui a le modèle, et gardée ici seulement. Le moteur est le même
`sd-server` (API `vid_gen`) ; la vidéo sort en WebM, que l'interface lit telle quelle.

- **Wan 2.1 T2V 1,3B** (Apache-2.0) : le cœur en fp16 (2,8 Go), l'encodeur UMT5-XXL en
  Q5_K_M (4,1 Go), le décodeur (0,25 Go), en tout 7,2 Go. Réglages de l'exemple de
  stable-diffusion.cpp : 20 pas, guidage 6, `flow_shift` 3, euler, la consigne négative
  habituelle de Wan quand l'utilisateur n'en donne pas. 16 images par seconde, des durées
  en 4n + 1 images (1, 2, 3 ou 5 s), en 832 × 480, 480 × 832 ou 624 × 624.
- **MiniMax H3** (vidéo et son, 24 images par seconde) : proposé à la demande de
  l'utilisateur, comme dans PANDORA (`core/h3_local.py`). Sa licence exclut l'Union
  européenne, le Royaume-Uni, la Corée du Sud et les États-Unis : la fiche du modèle
  l'affiche, et le téléchargement demande de l'avoir lue. Chacun en prend la
  responsabilité ; AIODOS ne distribue ni les poids ni les sorties. Fichiers allégés pour
  une machine à 32 Go de mémoire vive : cœur réduit en Q4_K_M (11,4 Go), encodeur
  Qwen3-VL-32B en Q2_K_M (13,1 Go), décodeurs vidéo (5,2 Go) et son (0,6 Go). Réglages de
  stable-diffusion.cpp (`docs/minimax_h3.md`) : guidage 1, bruit tiré sur le processeur,
  17k + 5 images, côtés en multiples de 32. **Pas encore essayé ici** (30 Go à
  télécharger).
- **Les règles de chaque modèle** (formats, durées, cadence, bornes) sont dans le
  catalogue (`imaging/catalog.py`) ; la demande est vérifiée d'après elles, ici et sur la
  machine qui calcule.
- **PANDORA** (du même auteur) parle déjà à ce serveur pour son moteur H3 local : même
  protocole (`vid_gen`, `jobs`), même résultat WebM, qu'il convertit en MP4 avec ffmpeg.
  AIODOS reprend ce protocole et ces contraintes, sans copier de code (PANDORA est sous
  GPL-3.0, AIODOS sous Apache-2.0). Pour l'intégration future, PANDORA pourra confier
  ses vidéos à AIODOS, qui les répartira entre les machines.

**Mesure** (RTX 4070 Laptop, Vulkan, Wan 2.1 1,3B, 832 × 480, 33 images = 2 s, 20 pas) :
3 min 42 la première fois, dont l'encodage du texte (13 s, sur le processeur), 20 pas de
6 à 7 s (2 min 12), le décodage et la compilation des shaders (1 min 17). Le WebM pèse
2 Mo. Le placement automatique garde le cœur sur la carte (2,7 Go) et met l'encodeur et
le décodeur en mémoire vive. Quatre vidéos se calculent donc quatre fois plus vite sur
quatre machines : c'est là que la répartition paie le plus.

La version CUDA n'aide pas non plus pour la vidéo : 3 min 34 au lieu de 3 min 42, avec des
pas plus lents (8,5 s) et un décodage à court de mémoire, refait par tuiles.

**Suite** :

1. L'encodeur de texte sur une autre machine : il faut un sd-server construit avec le RPC
   de ggml (les versions publiées ne l'ont pas : pas de `ggml-rpc.dll` dans l'archive).
2. Android : pas d'archive officielle pour Android ; à construire avec le NDK.

## 8. Dans la conversation (18/10/2026)

Demande de l'auteur : la création d'images ne doit pas être à part, puisque dans tous les
assistants elle se fait dans la conversation ; pareil pour la vidéo. Les images et les
vidéos se demandent donc là, et la vue « Images et vidéos » garde la galerie, les modèles
et les créations en cours.

- **Une demande écrite** est reconnue avant l'envoi (`chat/creations.py`, `intent`, appelé
  par le preflight) : un verbe de création (crée, génère, dessine, fais, je voudrais…),
  un genre (image, photo, logo, affiche, vidéo…) et ce qu'il faut montrer. « Crée-moi une
  image d'un phare sous l'orage » donne la description « un phare sous l'orage ». La
  phrase peut aussi dire le nombre (« 4 images »), le format (portrait, paysage, carré,
  pour un téléphone…) et, pour une vidéo, la durée. Ne sont **pas** des demandes : une
  question (« comment créer une image Docker ? »), un sujet de code ou de système
  (Docker, ISO, Python, image disque…), une tournure figurée (« donne-moi une image
  claire de la situation »), une phrase sans sujet (« tu sais créer des images ? »).
  « Dessine-moi un mouton » en est une, sans le mot « image ».
- **Le bouton Image**, à gauche du message, ouvre le mode « Créer » : modèle, format,
  durée, nombre ; le message entier est alors la description. « Reprendre » (sur une
  création, ou dans la galerie) y remet la description, les réglages et la graine.
- **Retoucher la dernière création** (`chat/retouches.py`) : après une image, « Fais-la
  de nuit », « Ajoute un chat », « Peux-tu la rendre plus lumineuse ? » se rapportent à
  elle, sans redire « image ». Le modèle de conversation réécrit la description (l'ancienne
  et la demande ; « Je retouche la description… » le temps de l'écrire), et la nouvelle
  création garde le modèle, le format et la graine : la composition reste. « Encore une »,
  « Une autre », « Pareil » : une variante, même description, autre graine. Sans modèle de
  conversation en 45 s, la demande s'ajoute à la description. Une question (« Pourquoi
  est-elle floue ? ») n'est pas une retouche. Essai réel : « Fais-la de nuit, avec la pleine
  lune » après la vidéo du renard a donné « Un renard roux qui trotte dans la neige fraîche
  sous la lumière de la pleine lune de nuit », même graine, même format.
- **Une image retouchée part de la précédente** (image vers image de sd-server : son API
  native prend `init_image`, en data URL, et `strength`) : la composition reste. L'image de
  départ voyage comme le fichier d'une unité (`file_key`), à mes machines seulement : chez
  un ami, la retouche part sans elle, et `work/blob` la lui refuse même s'il la demande
  ([securite.md](securite.md), faille n° 18). La machine qui calcule revérifie la force. Mesuré sur
  Z-Image Turbo (8 pas) : la force avance par huitièmes ; 0,75 (6/8) garde la scène et sa
  lumière, de quoi ajouter, retirer ou changer un détail (« des lumières aux fenêtres » :
  même cabane, fenêtres éclairées) ; 0,875 (7/8) change l'ambiance (nuit, saison, style)
  en recomposant une partie de la scène. AIODOS prend 0,875 quand la demande parle
  d'ambiance, 0,75 sinon. Une vidéo, elle, repart de sa description (même graine).
- **Prudence, et « Répondre en texte »** (relecture du 18/10) : après une création, la
  conversation continue aussi en texte ; une retouche sans pronom (« avec un chien »,
  « en noir et blanc ») doit être courte, et « Encore merci », « Change de sujet : … »,
  « Plus précisément, explique… » n'en sont pas. Une question avec « je voudrais » ou « il
  me faut » (« Je veux une vidéo de mon mariage, qui contacter ? ») n'est pas une demande,
  ni « une image de marque ». Si une phrase est quand même prise pour une demande, la
  réponse propose « Répondre en texte » : la création quitte la conversation (son travail
  s'arrête) et le modèle de conversation reçoit le message.
- **Les images gardent leur dossier** (migration 13) : changer de dossier, même d'un dossier
  choisi à un autre, ne perd pas les précédentes ; un dossier sur un disque débranché ne
  gêne que ses propres images.
- **Un ami pourrait calculer** : si aucun modèle n'est prêt ici ni chez mes machines,
  mais qu'un ami propose le sien, la conversation le nomme, avec « Autoriser mes amis »
  (le réglage `images_from_friends`), et la création repart aussitôt.
- **Le modèle** : celui choisi la dernière fois pour ce genre s'il peut calculer, sinon
  le premier prêt, ici ou chez mes machines en ligne ; ceux à licence restrictive en
  dernier. Si aucun ne peut, la conversation le dit, avec un lien vers la vue Images et
  vidéos, et rien n'est gardé.
- **Ce qui est gardé** : le message, puis une réponse de l'assistant dont la colonne
  `media` (migration 11) garde la demande : genre, modèle, description, nombre, taille,
  graine, lot et travail. La réponse montre les images à mesure qu'elles arrivent (lot de
  la galerie, `GET /api/images?batch=`), avec l'état du travail et de quoi l'arrêter. Le
  texte de cette réponse la résume pour le modèle de conversation (« (Création d'une
  image avec Z-Image Turbo, d'après la description : « … ».) ») : il sait ce qui a été
  créé sans voir les images, et la mémoire des conversations aussi.
- **Le modèle de conversation sait qu'AIODOS crée des images** quand un modèle peut les
  calculer : une consigne courte le lui dit, pour qu'il ne réponde pas « je ne peux pas ».
- **Rien ne change pour la répartition** : le travail est le même que celui de la vue
  Images (unités indépendantes, mes machines d'abord, un ami seulement avec l'accord des
  deux côtés), et la description ne part chez une autre machine que le temps du calcul.

## Sources

- stable-diffusion.cpp : [dépôt](https://github.com/leejet/stable-diffusion.cpp),
  [RPC](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/rpc.md),
  [composants](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/backend.md),
  [performances](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/performance.md),
  [Wan](https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/master/docs/wan.md),
  [PR #1629](https://github.com/leejet/stable-diffusion.cpp/pull/1629)
- LocalAI : [images](https://localai.io/docs/features/image-generation/),
  [répartition](https://localai.io/docs/features/distribute/)
- ComfyUI : [dépôt](https://github.com/Comfy-Org/ComfyUI),
  [CFG sur plusieurs cartes](https://github.com/Comfy-Org/ComfyUI/pull/7063) ;
  [ComfyUI-Distributed](https://github.com/robertvoy/ComfyUI-Distributed)
- [SwarmUI](https://github.com/mcmonkeyprojects/SwarmUI), [xDiT](https://github.com/xdit-project/xDiT),
  [article xDiT](https://arxiv.org/abs/2411.01738), [AI Horde](https://github.com/Haidra-Org/horde-worker-reGen)
- Modèles : [Flux.2](https://huggingface.co/blog/flux-2),
  [Wan 2.2](https://github.com/Wan-Video/Wan2.2),
  [Flux.1 GGUF](https://huggingface.co/city96/FLUX.1-dev-gguf),
  [Qwen-Image GGUF](https://huggingface.co/city96/Qwen-Image-gguf),
  [Wan GGUF](https://huggingface.co/city96/Wan2.1-T2V-14B-gguf),
  [UMT5 GGUF](https://huggingface.co/city96/umt5-xxl-encoder-gguf),
  [LTX-2.5](https://comfyui-wiki.com/en/news/2026-08-11-ltx-2-5-open-weights-release),
  [licence HunyuanVideo](https://canirun.ai/license/tencent-hunyuan-community/)
