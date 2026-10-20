# Le moteur intégré : les modèles sans Ollama

Décision de l'auteur, le 20/10/2026 : « Remplacer dès maintenant » Ollama. AIODOS ne doit
plus dépendre d'un logiciel à installer et à lancer à part (sur le PC, il disait « aucun
modèle » dès qu'Ollama était arrêté ; sur le téléphone, il fallait Ollama dans Termux).

## Ce qui est fait

- **llama.cpp en mode routeur** (`aiodos/llm/engine.py`) : un seul `llama-server` sert tous
  les modèles de la machine par leur nom, les charge à la demande, décharge le plus ancien
  au-delà de deux (un modèle de conversation et un d'embedding), et place lui-même les
  couches dans la mémoire de la carte graphique (`--fit on`). Sur 127.0.0.1, avec une clé
  tirée à chaque lancement, exigée par le routeur **et par les llama-server qu'il lance pour
  chaque modèle** (sans elle : 401, sauf `/health`). Un modèle inutilisé 5 minutes est
  déchargé, comme chez Ollama.
- **Les modèles** (`aiodos/llm/models.py`) : ceux qu'AIODOS a téléchargés (`data/models/`),
  puis ceux d'Ollama, lus tels quels dans son dossier, **sans lancer Ollama**. Mêmes noms
  qu'Ollama (« qwen3.5:9b ») : réglages et conversations ne changent pas.
- **Les mêmes fonctions qu'avant** (`aiodos/llm/integrated.py`) : lister, converser en flux
  (réflexion à part), embeddings (normés), lire les images, capacités, décharger.
  `services/ollama.py` reste la seule porte d'entrée : moteur intégré dès que llama.cpp est
  là (réglage `llm_engine` : auto, integrated, ollama), Ollama sinon.
- **Télécharger sans Ollama** (`aiodos/llm/catalog.py`, `download.py`) : des fichiers GGUF
  de Hugging Face, épinglés (révision du dépôt, taille, SHA-256), repris après une coupure,
  vérifiés avant d'être mis en place. Seulement sur demande (« Installer un modèle »).

## Mesures du 20/10/2026 (llama.cpp b11707, Vulkan, RTX 4070 Laptop)

Modèles d'Ollama lus tels quels :

| Se chargent | Ne se chargent pas (format propre à Ollama) |
|---|---|
| qwen3.5:9b (avec son projecteur d'images), mistral:7b, qwen2.5:7b, qwen2.5-coder:7b, bge-m3, nomic-embed-text, qwen3-embedding:0.6b, embeddinggemma-2:270m | embeddinggemma (« wrong number of tensors »), qwen2.5vl (architecture « qwen25vl »), snowflake-arctic-embed2 (vocabulaire) |

Un modèle qui ne se charge pas est écarté pour de bon (par fichier : sa version standard,
téléchargée sous le même nom, passe), avec un message qui dit de l'installer depuis
« Installer un modèle ».

- Conversation, qwen2.5-coder:7b : 10 s chargement compris ; qwen3.5:9b : premier mot à
  14 s chargement compris, réflexion séparée de la réponse.
- Images : **qwen3.5:9b voit les images** (projecteur) et remplace qwen2.5vl : le logo
  d'AIODOS décrit en 22 s. Si le modèle de vision réglé ne voit pas les images, le plus
  capable des modèles installés qui les voient (et tiennent dans 8 Go) le remplace.
- Embeddings, nomic-embed-text : 768 dimensions, normés, 3,5 s chargement compris.

## Clé des llama-server de chaque modèle et textes longs (20/10/2026, b11707)

- **Clé** : le routeur exigeait sa clé, mais les llama-server qu'il lance pour chaque modèle
  (127.0.0.1, port au hasard) répondaient sans clé, CORS ouvert (`/slots`, `/v1/models`,
  `/v1/embeddings` : 200 avec « Origin: https://exemple.invalid ») : une page web aurait pu
  balayer les ports et se servir des modèles. `api-key` et `api-key-file` dans les
  préréglages ne servent à rien (le routeur les retire des arguments de ses enfants, vérifié) ;
  il leur donne en revanche son environnement et leur transmet l'en-tête `Authorization` de
  chaque demande. La clé passe donc par `LLAMA_API_KEY` (plus par `--api-key`, ni dans la
  ligne de commande). Vérifié : un enfant répond 401 sans clé (`/slots`, `/v1/models`,
  `/props`, `/v1/embeddings`), 200 avec ; `/health` reste ouvert ; le routeur répond comme
  avant. CORS reste ouvert : sans la clé, une page n'obtient rien. **À revérifier à chaque
  nouvelle version de llama.cpp** (si le routeur cessait de transmettre son environnement,
  les enfants seraient de nouveau ouverts).
- **Textes longs** : un modèle d'embedding refusait tout texte de plus de 512 jetons
  (llama-server ramène ses lots à 512 quand `ubatch-size` n'est pas donné). Ses préréglages
  ont maintenant `batch-size` et `ubatch-size` à la taille de son contexte (2048). Mesuré
  avec nomic-embed-text : 700 et 1470 jetons passent (16 textes de 1470 jetons en 0,5 s) ;
  2048 jetons au plus, au-delà le refus garde le même message (« current batch size:
  2048 »), que la passerelle rend clair. Coût : un tampon de calcul plus grand tant que le
  modèle est chargé (carte graphique du PC : nomic-embed-text 25 → 104 Mio, bge-m3 19 → 80,
  qwen3-embedding:0.6b 24 → 96, embeddinggemma-2:270m 82 → 340), à mesurer sur le téléphone.

## Catalogue (Hugging Face, relevé le 20/10/2026)

| Nom | Dépôt | Licence | Taille |
|---|---|---|---|
| qwen3.5:0.8b, 2b, 4b, 9b | unsloth/Qwen3.5-…-GGUF (Q4_K_M + projecteur F16) | Apache-2.0 | 0,7 à 6,6 Go |
| mistral:7b | bartowski/Mistral-7B-Instruct-v0.3-GGUF (Q4_K_M) | Apache-2.0 | 4,4 Go |
| phi4-mini | unsloth/Phi-4-mini-instruct-GGUF (Q4_K_M) | MIT | 2,5 Go |
| embeddinggemma | ggml-org/embeddinggemma-300M-GGUF (Q8_0) | conditions de Gemma | 334 Mo |

embeddinggemma est le modèle des bibliothèques (docs/embeddings.md) : sa version
standard est le même modèle que celui d'Ollama. **Mesuré le 20/10** sur les passages
enregistrés de l'AIODOS installé : similarité 0,9996 en moyenne (0,9995 au moins) entre
les vecteurs d'Ollama et ceux du moteur intégré, mêmes passages en tête de recherche :
pas de réindexation à prévoir. Téléchargé et vérifié (SHA-256) en une dizaine de secondes.

## Reste à faire

1. Livrer llama.cpp avec AIODOS (l'installeur le joint ; les versions signées le livrent
   déjà pour les gros modèles) : sans lui, AIODOS revient à Ollama.
2. ~~La passerelle vers les autres outils~~ : fait le 20/10 (`gateway/engine.py`,
   [autres-outils.md](autres-outils.md)). Sans Ollama, les outils gardent les mêmes API :
   catalogue d'après le moteur intégré (`/api/tags`, `/v1/models`, `/api/show` avec le
   contexte du routeur, `/api/ps`) ; `/v1/chat/completions` transmis tel quel au routeur
   (outils, images, format imposé, `logprobs`, `n`), une conversation en texte mise en forme
   pour les gabarits stricts ; l'API d'Ollama au format d'Ollama (le texte par la façade ;
   images, outils et `format` traduits pour le routeur ; complétion de code par `/infill`) ;
   embeddings par la façade. Ce que le routeur ne prend pas est refusé clairement (400).
   **Essai réel le 20/10** (llama.cpp b11707, dossier de données à part, `ollama_url` vers un
   port fermé) : 32 vérifications sur 32 — qwen3.5:9b réfléchit à part, appelle un outil en
   flux puis lit son résultat, voit une image rouge ou bleue ; qwen2.5-coder:7b complète du
   code au milieu d'un texte, donne deux réponses avec probabilités, suit un schéma JSON ;
   mistral:7b reçoit deux messages de suite (mis en forme) ; nomic-embed-text, 768
   dimensions (256 sur demande) ; clients officiels openai et MCP (recherche dans une
   bibliothèque indexée par le moteur intégré). Refus clairs : image pour un modèle qui ne la
   voit pas, complétion de code pour mistral, texte de 1802 jetons pour l'embedding (limite passée depuis de 512 à 2048 jetons) ;
   qwen2.5vl et embeddinggemma d'Ollama écartés (404) dès leur premier essai. Un outil qui
   part en route : le routeur arrête la génération (« cancel task », 66 jetons).
3. ~~Le téléphone~~ : fait le 20/10 ([android.md](android.md)). L'appli contient l'archive
   officielle pour Android de llama.cpp b11707 et le même moteur intégré qu'ici. Sur un
   Galaxy S23, embeddinggemma calcule environ 400 passages par minute, avec les mêmes
   vecteurs qu'ici (similarité 0,9997 en moyenne, 0,9995 au moins).
