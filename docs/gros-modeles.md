# Gros modèles répartis entre plusieurs machines

Étude commencée le 17/10/2026. Le but : « faire tourner les plus gros moteurs en les
répartissant », pour Qwen comme pour les autres moteurs de génération. C'est la direction
principale d'AIODOS.

## 1. Ce que fait le PC aujourd'hui

Le PC a une RTX 4070 Laptop (8 Go), 32 Go de mémoire vive et un i9-14900HX. Le moteur est
Ollama 0.40.5. Les mesures sont faites avec `tools/bench_repartition.py` : un texte lu de
601 jetons, 128 jetons écrits, médiane de 3 essais. Les résultats bruts sont dans
`docs/gros-modeles-mesures.json`.

| Modèle et placement | Lecture (jetons/s) | Écriture (jetons/s) | Premier mot |
|---|---|---|---|
| qwen3.6 (35 milliards de paramètres, mélange d'experts) : 5,2 Go sur le GPU, le reste en mémoire vive | 227 | 38,1 | 2,7 s |
| qwen3.5:9b (dense), tout sur le GPU | 1 766 | 33,0 | 0,3 s |
| qwen3.5:9b, la moitié des couches sur le GPU | 586 | 8,7 | 1,0 s |
| qwen3.5:9b, tout en mémoire vive | 36 | 6,8 | 16,7 s |

Ce qu'on en tire :

- **qwen3.6 est un « mélange d'experts ».** Il a 35,5 milliards de paramètres, mais pour
  chaque mot, seuls 8 experts sur 256 travaillent, soit environ 3 milliards de paramètres.
  Le PC seul l'écrit déjà vite, même aux trois quarts en mémoire vive. Le répartir
  n'accélérerait pas l'écriture. Une autre carte graphique accélérerait surtout la lecture
  des longs textes.
- **Un modèle dense qui déborde de la carte graphique s'effondre.** Avec la moitié de ses
  couches en mémoire vive, il écrit 4 fois moins vite. Tout en mémoire vive, il lit 50 fois
  moins vite. C'est là qu'une deuxième carte graphique change tout, chez soi ou chez un ami.
- **Certains modèles ne tiennent dans aucune machine du cercle.** Ici, la limite est
  d'environ 35 Go. Un modèle de 70 milliards de paramètres en Q4 pèse environ 40 Go : il ne
  peut tourner qu'en réunissant plusieurs machines.

## 2. Les façons de répartir un modèle

| Façon | Ce qui voyage | Réseau qu'il faut | Exemples |
|---|---|---|---|
| **Couches en chaîne** : chaque machine garde une tranche des couches | L'état intermédiaire, à chaque mot : 8 Kio pour qwen3.6 (2 048 nombres) | Peu de débit, mais un aller-retour par machine et par mot : la latence compte | llama.cpp RPC (MIT), Petals (MIT, sans mise à jour depuis 2024) |
| **Chaque couche découpée** entre toutes les machines | Des échanges à chaque couche | Très rapide (réseau local filaire, Thunderbolt) | distributed-llama (MIT), exo (Apache-2.0, surtout Mac) |
| **Experts répartis** (mélanges d'experts) | Des échanges à chaque couche | Très rapide | possible avec llama.cpp (`--override-tensor`) |
| **Par composants** (images, vidéo) : encodeur de texte, cœur du modèle et décodeur sur des machines différentes | Quelques mégaoctets par image | Ordinaire, Internet compris | à construire |
| **Unités indépendantes** : plusieurs réponses ou images à la fois, chacune sur une machine qui a tout le modèle | La demande et le résultat | Ordinaire | AIODOS aujourd'hui, AI Horde |
| **Brouillon et vérification** : un petit modèle propose, le grand vérifie | Quelques jetons | Ordinaire | décodage spéculatif de llama.cpp |

Les couches en chaîne conviennent le mieux à des machines ordinaires et différentes les unes
des autres. Découper chaque couche demande un réseau que des amis n'ont pas entre eux. Pour
les images et la vidéo, les composants et les unités indépendantes se complètent.

## 3. Les mesures de la répartition (17/10/2026)

Le moteur est llama.cpp b11707 (version Vulkan) ; Ollama le lance déjà, sous licence MIT.
Son mode RPC (`ggml-rpc-server`, protocole 3.0.0) prête les cartes graphiques d'une machine
à une autre. Le `llama-server` fourni avec Ollama connaît l'option `--rpc`, mais il n'a pas
le module RPC : il faut la version officielle. Elle lit les fichiers GGUF d'Ollama tels
quels (qwen3.5:9b, qwen3.6). La deuxième machine est simulée sur le même PC : un
`ggml-rpc-server` prête la carte (la même RTX 4070), et le relais de
`tools/bench_repartition.py` ajoute le délai et plafonne le débit. Le relais est vérifié :
5 ms demandées en donnent 6, 20 ms en donnent 21. Les mesures sont faites avec `llama-bench`
(512 jetons lus, 128 écrits, 3 essais), sur qwen3.5:9b (dense, 5,2 Go). Les résultats bruts
sont dans `docs/gros-modeles-mesures.json`.

**M1, le PC seul avec llama.cpp :**

| Placement | Lecture (jetons/s) | Écriture (jetons/s) |
|---|---|---|
| Tout sur la carte | 2 174 | 42,7 |
| Moitié sur la carte, moitié en mémoire vive | 638 | 7,4 |
| Tout en mémoire vive | 38 | 7,0 |

llama.cpp écrit plus vite qu'Ollama sur la carte (42,7 jetons/s contre 33,0).

**M2 et M3, la moitié du modèle sur la carte d'un deuxième PC :**

| Aller-retour entre les deux PC | Lecture (jetons/s) | Écriture (jetons/s) |
|---|---|---|
| 0 ms (même PC) | 1 648 | 37,9 |
| 1 ms (réseau local filaire) | 2 013 | 35,3 |
| 5 ms (bon Wi-Fi) | 1 996 | 31,4 |
| 20 ms (Internet proche) | 1 939 | 21,4 |
| 50 ms (Internet lointain) | 1 674 | 12,0 |
| Tout le modèle sur la carte du deuxième PC, 0 ms | 1 795 | 38,2 |
| Tout le modèle sur la carte du deuxième PC, 20 ms | 1 331 | 15,4 |

Ce qu'on en tire :

- **Le protocole coûte peu** : 11 % sur le même PC.
- **Chaque machine de plus coûte environ un aller-retour par mot écrit.** À 20 ms, il faut
  47 ms par mot au lieu de 26. La lecture d'un texte, faite par paquets, ne souffre presque
  pas du délai.
- **Même par Internet, la carte d'un ami vaut mieux que la mémoire vive.** À 50 ms, le
  modèle coupé en deux écrit encore 12 jetons/s ; le PC seul, avec la moitié du modèle en
  mémoire vive, en écrit 7,4.

**M3 bis, l'ordre des machines et le débit.** Le dernier étage du modèle note les 248 000
mots du vocabulaire de Qwen : environ 1 Mo par mot écrit. Si c'est la deuxième machine qui
le calcule, ce mégaoctet traverse le réseau à chaque mot. Mesures à 20 ms, poids déjà en
cache chez la deuxième machine :

| Ordre | Sans plafond | 40 Mbit/s | 10 Mbit/s |
|---|---|---|---|
| Le 2e PC calcule la fin | 21,5 | 3,3 | 1,1 |
| La fin reste sur la machine qui pose la question | 22,1 | 15,3 | 8,6 |

(jetons écrits par seconde)

Garder la fin ici rend l'écriture 5 à 8 fois plus rapide quand le débit est plafonné. En
contrepartie, la lecture d'un long texte est plus lente (69 jetons/s contre 214 à 40 Mbit/s) :
le paquet de mots lus traverse alors deux fois le réseau.

**Mélange d'experts.** qwen3.6 avec llama.cpp, experts en mémoire vive et le reste sur la
carte : 161 jetons/s en lecture et 16,8 en écriture. Ollama place mieux ce modèle sur ce PC
(227 et 38,1). Avec la moitié des couches confiée au processeur d'un deuxième PC sans carte
graphique, on tombe à 34 et 5,2 : **prêter un processeur et sa mémoire vive ne sert à
rien, seules les cartes graphiques valent la peine d'être prêtées.**

**M4, le tunnel chiffré du compagnon.** Deux compagnons sur ce PC, relais coupés, en direct
(chemin « direct », 0,1 à 1,1 ms) ; le serveur RPC n'est joint que par le tunnel :

| Placement | TCP sans chiffrement | Tunnel du compagnon |
|---|---|---|
| Moitié ici, moitié sur la carte du 2e PC | 38,4 jetons/s écrits, 1 959 lus | 33,1 écrits, 1 824 lus |
| Tout sur la carte du 2e PC | 38,2 écrits, 1 880 lus | 32,2 écrits, 1 286 lus |

Le chiffrement et les deux compagnons coûtent environ 4 ms par mot écrit (14 à 16 %). Un
premier essai avec dumbpipe donnait bien plus (19 jetons/s) : il passait sans doute par un
relais de n0, pas en direct.

## 4. Ce qu'AIODOS fait maintenant (prototype du 17/10/2026)

- **Le compagnon ouvre des tunnels** (`compagnon/src/tunnel.rs`). Une machine expose un
  service TCP local à des machines nommées (`PUT /net/v1/exposed`). L'autre machine ouvre
  un port local qui la relie à ce service (`POST /net/v1/tunnels`), à travers la connexion
  QUIC déjà gardée entre elles : chiffrée, authentifiée par les clés, directe quand c'est
  possible. Le serveur RPC n'écoute que sur 127.0.0.1.
- **La machine qui prête sa carte** (`aiodos/cluster/helper.py`) répond aux actions
  `cluster/lend`, `cluster/keep` et `cluster/release`. Il faut :
  - que la demande vienne de **mes machines**, jamais d'un ami ;
  - que le réglage « Prêter ma carte graphique pour les gros modèles » soit activé (vue
    Nœud, coupé par défaut) ;
  - la même version de llama.cpp des deux côtés.

  Elle lance alors `ggml-rpc-server` avec sa meilleure carte, ou celle du réglage « Carte
  prêtée », et garde les poids reçus en cache (`data/cluster/cache`). Elle reprend sa
  carte quand la machine qui l'emprunte la rend, se tait depuis 90 s, ou quand le réglage
  est coupé. Pendant le prêt, ses unités de fond laissent la place.
- **La machine qui pose la question** (`aiodos/cluster/group.py`) propose « réparti » dans le
  choix du modèle, pour les modèles d'Ollama trop gros pour sa carte quand mes machines en
  ligne prêtent la leur. `plan.py` choisit le moins de machines possible, les plus grandes
  cartes d'abord ; elles gardent les premières couches, et la carte d'ici garde les
  dernières et le dernier étage. Au premier message, AIODOS demande les cartes, ouvre les
  tunnels et lance `llama-server` sur le fichier d'Ollama. Le groupe reste prêt 10 minutes
  après la dernière réponse.
- **Le moteur** (`aiodos/cluster/engine.py`) cherche llama.cpp dans le réglage « Dossier de
  llama.cpp », sinon celui livré avec les versions, sinon un llama.cpp posé à la main dans
  `data/engines/llama.cpp`. Sous Windows, llama-server et le serveur
  RPC sont rangés dans un objet de travail (`aiodos/node/children.py`) : ils s'arrêtent avec
  AIODOS, même s'il est tué, au lieu de garder la mémoire de la carte graphique.
- **Les conversations** passent par l'API de llama-server. Les consignes données au milieu
  de la conversation (résultats web, rappel juste avant la question) rejoignent le message
  de l'utilisateur qui les suit : certains modèles (Mistral) refusent tout ce qui n'alterne
  pas utilisateur et assistant, ce qu'Ollama cachait.
- **Livré avec les versions** (`tools/engine_release.py`, `aiodos/updates/engines.py`) :
  l'outil de publication joint l'archive officielle de llama.cpp, d'une version épinglée
  (b11707, Vulkan, 30 Mo pour Windows), empreinte vérifiée et signée dans le manifeste. Le
  service de mise à jour la reçoit comme le compagnon ; identique d'une version à l'autre,
  elle est copiée au lieu d'être retéléchargée (`store.reuse`). Elle se déplie une fois dans
  `data/engines/llama.cpp/<empreinte>/`. Toutes les machines d'une même version d'AIODOS
  ont donc la même version de llama.cpp, condition pour se prêter leur carte.
- **Essai de bout en bout** (`tests/test_cluster_e2e.py`, variable `AIODOS_LLAMACPP_DIR`) :
  deux nœuds sur ce PC, appairés sur invitation. La carte graphique de A est cachée à
  llama.cpp ; A converse avec mistral:7b, calculé par la carte de B à travers le tunnel.

## 5. Suite

1. **llama.cpp pour Android** : il n'y a pas d'archive officielle ; à construire avec le
   NDK, comme le compagnon. Windows est livré depuis le 17/10/2026.
2. **Remplir le cache d'une machine par le réseau local**, avant le premier usage :
   aujourd'hui, les poids passent par le tunnel à la première conversation.
3. **Essai réel à deux PC** (M5), puis avec un ami, quand le serveur RPC de llama.cpp sera
   sorti du stade du prototype.
4. **Images et vidéo** : étude dans [generation-repartie.md](generation-repartie.md). Couper
   le cœur ne sert qu'à la mémoire ; d'abord des unités indépendantes avec
   stable-diffusion.cpp, puis l'encodeur de texte sur une autre machine.

## Sources

- llama.cpp : [RPC](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md),
  [versions publiées](https://github.com/ggml-org/llama.cpp/releases) (b11707 du
  17/10/2026, construite avec `-DGGML_RPC=ON`)
- [distributed-llama](https://github.com/b4rtaz/distributed-llama),
  [exo](https://github.com/exo-explore/exo), [Petals](https://github.com/bigscience-workshop/petals)
- Projets voisins et licences : [projets-voisins.md](projets-voisins.md)
