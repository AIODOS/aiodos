# Projets voisins : ce qu'ils font, ce qu'on peut leur prendre

État au 17/10/2026, à partir de leurs dépôts, de leur documentation et d'articles récents.
Les licences marquées « ? » sont à vérifier avant de reprendre le moindre code.

## 1. Les familles de projets

| Projet | Ce qu'il fait | En commun avec AIODOS | Différent d'AIODOS | Licence |
|---|---|---|---|---|
| **Réunir ses appareils pour un gros modèle** | | | | |
| [exo](https://github.com/exo-explore/exo) | Un seul « gros GPU » fait de ses appareils (Mac, iPhone, Android, NVIDIA, Raspberry Pi) ; API compatible OpenAI et Ollama | Ses machines à la maison, découverte automatique | Découpe **un** modèle entre les appareils ; réseau local rapide ; pas d'amis, pas de bibliothèques ; GPU sous Linux encore en chantier | GPL-3.0 |
| llama.cpp RPC | `rpc-server` sur des machines, qui prêtent leur mémoire à une autre | Le moteur d'Ollama, qu'on utilise | Met la **mémoire** en commun, ne va pas plus vite ; demande un réseau très rapide (2,5 à 10 Gb/s conseillés) | MIT |
| distributed-llama | Découpe d'un modèle entre machines de la maison (processeur, GPU Vulkan) | Machines ordinaires | Un nœud maître, réseau local | ? |
| Parallax (Gradient) | Inférence répartie entre machines de configurations et de lieux différents, en pair à pair | Machines hétérogènes, par Internet | Découpe un modèle ; projet jeune (0.0.1 en octobre 2025) | ? |
| Kalavai | Faire de ses appareils un « nuage » d'IA communautaire | Matériel ordinaire, mise en commun | Vise une grande inférence publique | ? |
| **Calcul ouvert à tous, sans jeton** | | | | |
| BOINC (SETI@home, Folding@home…) | Calcul bénévole en unités indépendantes, crédits, validation par redondance | **Notre modèle** : unités, baux, crédits, économiseur d'écran | Un serveur central par projet ; pas d'IA générative | LGPL-3.0 |
| [AI Horde](https://aihorde.net/mission) | Images et textes générés par les GPU de bénévoles ; « kudos » qui donnent la priorité, ni achetables ni vendables | Bénévoles, crédit de réciprocité | Serveur central ; demandes publiques et anonymes ; pas de données privées | ? |
| [Petals](https://jimmysong.io/ai/petals) | Grands modèles découpés entre bénévoles par Internet (couches en chaîne) | Par Internet, entre inconnus | Découpe un modèle ; activité actuelle incertaine | MIT |
| [LocalAI](https://localai.io/features/distribute/) (mode P2P) | Réseau par jeton partagé (libp2p) : requêtes envoyées à un nœud libre, ou modèle découpé | Pair à pair, sans serveur, découverte locale ou par DHT | Pas de niveaux de confiance ; « aperçu technique » selon sa doc | MIT |
| **Calcul décentralisé avec jeton (pour la phase 3)** | | | | |
| Bittensor, Gensyn, Prime Intellect, Nous Psyche, Golem, Akash | Marchés de calcul ou entraînement entre inconnus, payés en jetons | Calcul entre inconnus, vérification des résultats | Blockchain, paiements, marchés publics | variées |
| **Ateliers d'IA locaux (une machine)** | | | | |
| Ollama | Moteur de modèles locaux | Notre moteur | Pas d'interface d'atelier, pas de réseau | MIT |
| LM Studio | Application de modèles locaux | Modèles locaux | Code fermé | propriétaire |
| GPT4All, AnythingLLM, Jan, Open WebUI | Conversations et documents (RAG) en local | Atelier : conversations, bibliothèques | Une seule machine ; plus mûrs (agents, outils, MCP, voix) | MIT, MIT, Apache-2.0, licence Open WebUI (?) |
| [ODS](https://github.com/Osmantic/ODS) | Faire d'un PC un serveur d'IA privé : conversation, voix, agents, RAG, images | Atelier local tout-en-un | Pas de plusieurs machines annoncées | ? |
| **Briques réseau** | | | | |
| Iroh (n0) | Connexions pair à pair chiffrées, relais, perçage de box | **Notre compagnon aiodos-net** | — | MIT / Apache-2.0 |
| Syncthing | Synchronisation de fichiers en pair à pair | Appareils identifiés par clé, relais, pas de serveur central | Fichiers, pas de calcul | MPL-2.0 |
| Tailscale | Réseau privé virtuel entre ses appareils (WireGuard) | Relier ses machines partout | Serveur de coordination central | BSD-3 (client) |

## 2. Ce qu'AIODOS est seul à réunir

- **Un atelier complet** (conversations, bibliothèques, mémoire) **et** le calcul partagé
  entre mes machines **et celles de mes amis**. Les autres font l'un ou l'autre.
- **Des unités indépendantes, façon BOINC**, plutôt qu'un modèle découpé. Une machine lente,
  un téléphone en 4G ou une machine qui s'éteint en route ne bloquent rien : l'unité repart
  ailleurs.
- **La confiance à deux niveaux et la confidentialité** : bibliothèques gardées sur ce PC,
  noms de fichiers neutres et profil allégé chez un ami, contrôle par échantillon.
- **Sans serveur central de bout en bout** : appairage, invitation à distance, mises à
  jour signées transmises de machine en machine, relais remplaçables par le sien.

## 3. Ce qu'on peut faire en plus, en s'inspirant d'eux

1. **Faire tourner un modèle trop gros pour une seule machine** (exo, llama.cpp RPC).
   Exemple : qwen3.6, 22 Go, ne tient pas dans les 8 Go de la RTX 4070. llama.cpp RPC (MIT)
   pourrait le répartir entre le PC et une autre machine du réseau local. Ce n'est utile
   qu'avec un réseau local rapide ; par Internet, ce serait trop lent.
2. **Choisir seul la machine qui répond** (LocalAI fédéré). Une conversation irait à la
   machine libre du cercle qui a le modèle, sans qu'on la désigne.
3. **Des crédits visibles, non monnayables** (kudos d'AI Horde). La réciprocité devient
   lisible et donne la priorité. C'est une étape possible avant tout jeton, en phase 3.
4. **Chercher dans les bibliothèques partagées d'un ami sans copier ses fichiers.** Seuls
   les passages trouvés reviennent. Aucun voisin ne le fait.
5. **Ouvrir AIODOS aux autres outils.** Une API compatible OpenAI et Ollama laisserait Open
   WebUI, AnythingLLM ou un éditeur de code utiliser les modèles du cercle. Un serveur MCP
   donnerait les bibliothèques aux assistants.
6. **Un « présentateur » façon Syncthing.** Une machine de confiance présente ses machines
   aux autres. Un QR code servirait à l'appairage.
7. **Pour la phase 3** : la redondance de BOINC, et les travaux de Prime Intellect et de
   Gensyn sur la vérification des calculs d'inconnus.
8. **Des applications mobiles natives** (exo tourne sur iPhone et Android) : voir l'étude
   des installeurs.

## 4. Comment se servir du travail des autres

- **Se brancher plutôt que copier.** On utilise déjà llama.cpp (par Ollama) et Iroh. exo
  peut devenir un fournisseur de modèles par son API, sans fusionner son code.
- **Reprendre du code seulement sous une licence compatible.** MIT, BSD et Apache-2.0 vont
  dans une GPL-3.0, et ne gênent pas notre permission pour les boutiques. Le code d'un autre
  projet sous GPL (exo) ne porte pas cette permission : s'en servir comme programme à part,
  pas comme code fusionné. Éviter l'AGPL.
- **Reprendre les idées, qui n'ont pas de licence** : unités et validation de BOINC, kudos
  d'AI Horde, présentateurs et relais de Syncthing, routage en chaîne de Petals.
- **Se mesurer à eux.** Faire des essais comparatifs sur nos machines (exo,
  distributed-llama) avant de choisir une voie pour les gros modèles.
- **Contribuer en retour.** Signaler et corriger en amont ce qu'on trouve (Iroh, llama.cpp) :
  tout le monde en profite, et on a moins de rustines à entretenir.
- **Garder un œil sur eux.** Les projets jeunes (Parallax, Kalavai, ODS) changent vite : à
  revoir tous les trois mois.

## Sources

- exo : [dépôt GitHub](https://github.com/exo-explore/exo), [revue](https://www.promptquorum.com/power-local-llm/exo-review)
- llama.cpp RPC : [guide Seeed Studio](https://wiki.seeedstudio.com/ai_robotics_distributed_llama_cpp_rpc_jetson/), [limites (Discoverer)](https://hpcwithus.discoverer.bg/?p=439), [AMD, grappe Ryzen AI Max+](https://www.amd.com/de/developer/resources/technical-articles/2026/how-to-run-a-one-trillion-parameter-llm-locally-an-amd.html)
- distributed-llama : [gittrend](https://gittrend.io/repo/b4rtaz/distributed-llama)
- Parallax : [SourcePulse](https://www.sourcepulse.org/projects/17735805)
- Kalavai : [blog](https://kalavainet.substack.com/p/setting-a-new-world-record-the-worlds)
- AI Horde : [mission](https://aihorde.net/mission)
- Petals : [présentation](https://jimmysong.io/ai/petals), [article de recherche](https://huggingface.co/papers/2312.08361)
- LocalAI : [P2P et inférence fédérée](https://localai.io/features/distribute/)
- ODS : [AIToolly](https://aitoolly.com/zh/ai-news/article/2026-09-02-ods-transforming-personal-computers-into-comprehensive-local-ai-servers), [dépôt](https://github.com/Osmantic/ODS)
