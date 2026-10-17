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

## 3. Recommandation : llama.cpp RPC, piloté par AIODOS

llama.cpp est le moteur qu'Ollama utilise déjà : Ollama lance `lib/ollama/llama-server.exe`.
Il est sous licence MIT. Son mode RPC (`ggml-rpc-server`, protocole 3.0.0) prête les cartes
graphiques d'une machine à une autre. Chaque machine garde une partie des poids et du cache,
au prorata de sa mémoire libre. Il marche sous Windows, Linux, macOS et Android, avec CUDA,
Vulkan, Metal ou le processeur seul. Les fichiers d'Ollama sont au format GGUF (qwen3.6 :
architecture `qwen35moe`) : llama.cpp devrait les lire tels quels, M1 le vérifiera. Le
`llama-server` fourni avec Ollama connaît l'option `--rpc`, mais il n'a pas le module RPC
(`ggml-rpc`). Il faut donc la version officielle de llama.cpp.

Ce que llama.cpp ne fait pas, et qu'AIODOS apporterait :

- **La sécurité.** Le serveur RPC n'a ni mot de passe ni chiffrement. Sa documentation
  prévient : jamais sur un réseau ouvert. AIODOS le lancerait seulement sur 127.0.0.1, et le
  joindrait par un tunnel chiffré du compagnon (clé de la machine, liste blanche), en local
  comme par Internet. Au début, ce serait seulement entre **mes machines** : un ami
  malveillant pourrait exploiter une faille de ce serveur, encore au stade du prototype.
- **Le choix des machines et du découpage**, selon la mémoire libre, la vitesse et la
  latence mesurée.
- **La même version partout.** llama.cpp serait livré avec les mises à jour signées, comme
  le compagnon. La version Vulkan marche sur toutes les cartes et pèse 30 Mo. La version
  CUDA est plus rapide sur NVIDIA, pour environ 600 Mo.
- **Les poids.** Ils sont envoyés une fois, puis gardés en cache par chaque machine (`-c`).
  AIODOS pourrait remplir ce cache à l'avance, par le réseau local.
- **Les pannes.** Si une machine part, la conversation repart sur un modèle qui tient sur
  une seule machine.

Le reste ne change pas : la file de travail, les unités, la confiance à deux niveaux. Le
modèle réparti devient un modèle de plus dans le choix de la conversation, comme les
modèles « Sur mes machines ».

## 4. Mesures prévues

- **M0** : le PC seul, par Ollama. Fait (§ 1).
- **M1** : llama.cpp seul sur le PC, même fichier. Il s'agit de le comparer à Ollama et de
  vérifier que le fichier d'Ollama se lit.
- **M2** : llama.cpp et `ggml-rpc-server` sur le même PC, une deuxième machine simulée. On
  mesure le coût du protocole.
- **M3** : M2 à travers le relais de `bench_repartition.py`, avec 1, 5, 20 et 50 ms
  d'aller-retour et un débit plafonné à 40 Mbit/s. On voit jusqu'où la chaîne reste utile :
  Wi-Fi, Internet proche, Internet lointain. Le relais est vérifié : 5 ms demandées en
  donnent 6, 20 ms en donnent 21.
- **M4** : M2 à travers le compagnon (Iroh), pour mesurer le coût du chiffrement.
- **M5** : une vraie deuxième machine, d'abord un téléphone en Wi-Fi, puis le PC d'un ami
  (essai 2.8).

## Sources

- llama.cpp : [RPC](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md),
  [versions publiées](https://github.com/ggml-org/llama.cpp/releases) (b11707 du
  17/10/2026, construite avec `-DGGML_RPC=ON`)
- [distributed-llama](https://github.com/b4rtaz/distributed-llama),
  [exo](https://github.com/exo-explore/exo), [Petals](https://github.com/bigscience-workshop/petals)
- Projets voisins et licences : [projets-voisins.md](projets-voisins.md)
