# Le moteur intégré : les modèles sans Ollama

Décision de l'auteur, le 20/10/2026 : « Remplacer dès maintenant » Ollama. AIODOS ne doit
plus dépendre d'un logiciel à installer et à lancer à part (sur le PC, il disait « aucun
modèle » dès qu'Ollama était arrêté ; sur le téléphone, il fallait Ollama dans Termux).

## Ce qui est fait

- **llama.cpp en mode routeur** (`aiodos/llm/engine.py`) : un seul `llama-server` sert tous
  les modèles de la machine par leur nom, les charge à la demande, décharge le plus ancien
  au-delà de deux (un modèle de conversation et un d'embedding), et place lui-même les
  couches dans la mémoire de la carte graphique (`--fit on`). Sur 127.0.0.1, avec une clé
  tirée à chaque lancement (sans elle : 401). Un modèle inutilisé 5 minutes est déchargé,
  comme chez Ollama.
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

## Catalogue (Hugging Face, relevé le 20/10/2026)

| Nom | Dépôt | Licence | Taille |
|---|---|---|---|
| qwen3.5:0.8b, 2b, 4b, 9b | unsloth/Qwen3.5-…-GGUF (Q4_K_M + projecteur F16) | Apache-2.0 | 0,7 à 6,6 Go |
| mistral:7b | bartowski/Mistral-7B-Instruct-v0.3-GGUF (Q4_K_M) | Apache-2.0 | 4,4 Go |
| phi4-mini | unsloth/Phi-4-mini-instruct-GGUF (Q4_K_M) | MIT | 2,5 Go |
| embeddinggemma | ggml-org/embeddinggemma-300M-GGUF (Q8_0) | conditions de Gemma | 334 Mo |

embeddinggemma est le modèle des bibliothèques (docs/embeddings.md) : sa version
standard est le même modèle que celui d'Ollama ; à vérifier sur les vecteurs déjà
enregistrés avant de décider s'il faut réindexer.

## Reste à faire

1. Livrer llama.cpp avec AIODOS (l'installeur le joint ; les versions signées le livrent
   déjà pour les gros modèles) : sans lui, AIODOS revient à Ollama.
2. La passerelle vers les autres outils (`gateway/`) parle encore à Ollama : la brancher sur
   le routeur (son API OpenAI est déjà celle de llama-server).
3. Le téléphone : llama.cpp pour Android dans l'appli (nœud intégré à l'appli, décision du
   20/10), à la place d'Ollama dans Termux.
