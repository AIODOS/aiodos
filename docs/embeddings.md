# Choix du modèle d'embedding (octobre 2026)

## Le problème

Mesure du 15/10/2026 sur les données réelles, avec `nomic-embed-text` (le modèle par
défaut jusque-là) : en français, les passages **sans rapport** obtenaient 0,55 à 0,68
de similarité cosinus, contre ≈ 0,82 pour le bon passage. Le seuil
`rag_min_score = 0,70` tenait donc à peu de chose, et les préfixes
`search_query:` / `search_document:` de nomic n'aidaient pas en français.

## Méthode

- **Jeu de test** : [`tools/embedding_bench_fr.json`](../tools/embedding_bench_fr.json) —
  32 passages du genre de ceux d'une bibliothèque (documents, notes, devis, mémoire des
  conversations), 36 questions avec leur(s) bon(s) passage(s) (37 paires pertinentes),
  12 questions hors sujet qui ne devraient rien remonter. Soit 1 476 paires « sans
  rapport » et 23 « pièges » (même thème, mais ne répond pas), étiquetés **avant** les
  mesures.
- **Outil** : [`tools/embedding_bench.py`](../tools/embedding_bench.py), résultats bruts
  dans [`tools/embedding_bench_results.json`](../tools/embedding_bench_results.json).
  Un seul modèle chargé à la fois, déchargé après usage.

  ```
  python tools/embedding_bench.py --prefixes --details --json tools/embedding_bench_results.json
  ```
- **Conditions** : 16/10/2026, RTX 4070 Laptop 8 Go, Ollama 0.40.5, textes envoyés sans
  préfixe (comme le fait Aiodos), par lots de 16.
- **Indicateurs** : scores des bons passages et du bruit, AUC (probabilité qu'un bon
  passage score plus haut qu'un passage sans rapport : c'est ce qu'exige un seuil unique
  pour toutes les questions), bon passage en tête (top 1), et surtout ce que donne chaque
  seuil : questions restées sans bon passage, passages sans rapport retenus.

## Résultats

| Modèle | Dim. | Disque | VRAM | Bons passages min / méd. | Bruit méd. / p99 / max | AUC | Top 1 |
|---|---|---|---|---|---|---|---|
| nomic-embed-text | 768 | 262 Mo | 320 Mo | 0,548 / 0,743 | 0,561 / 0,663 / 0,750 | 0,9750 | 86 % |
| bge-m3 | 1024 | 1 104 Mo | 657 Mo | 0,490 / 0,643 | 0,311 / 0,466 / 0,556 | 0,9997 | 97 % |
| snowflake-arctic-embed2 | 1024 | 1 107 Mo | 657 Mo | 0,336 / 0,539 | 0,072 / 0,266 / 0,436 | 0,9998 | 97 % |
| **embeddinggemma** | 768 | 593 Mo | 654 Mo | 0,449 / 0,578 | 0,119 / 0,314 / 0,444 | **1,0000** | **100 %** |
| qwen3-embedding:0.6b | 1024 | 610 Mo | 1 172 Mo | 0,466 / 0,636 | 0,231 / 0,439 / 0,582 | 0,9995 | 100 % |

Ce que donne le seuil (sur 36 questions, 1 476 paires sans rapport, 23 pièges) :

| Modèle | Seuil | Questions sans bon passage | Passages sans rapport retenus | Pièges retenus |
|---|---|---|---|---|
| nomic-embed-text | 0,70 (ancien réglage) | **11** | 2 | 2 |
| nomic-embed-text | 0,64 | 4 | **51** | 6 |
| bge-m3 | 0,45 | 0 | 24 | 8 |
| snowflake-arctic-embed2 | 0,29 | 0 | 8 | 4 |
| **embeddinggemma** | **0,40** | **0** | **2** | 3 |
| qwen3-embedding:0.6b | 0,42 | 0 | 25 | 5 |

- **nomic-embed-text** ne sépare pas le bon passage du bruit : au seuil de 0,70, près
  d'une question sur trois perd son bon passage ; plus bas, le bruit déferle. Pire cas :
  « Comment l'API locale est-elle protégée ? » → un échange sur *nano banana* à 0,750.
- **embeddinggemma** est le seul modèle dont les bons passages (≥ 0,449) passent tous
  au-dessus du bruit (≤ 0,444) : il existe un seuil sans perte, et le classement est
  parfait. Les deux seuls passages « sans rapport » au-dessus de 0,40 sont en fait des
  voisins de thème (« Comment installer Aiodos ? » → la charte du logo de Aiodos ;
  « rendre un script exécutable sous Linux » → l'activation d'un venv).
- **bge-m3** et **snowflake-arctic-embed2** (même architecture, XLM-RoBERTa) sont très
  proches l'un de l'autre, nettement meilleurs que nomic, un cran en dessous
  d'embeddinggemma : leurs bons passages les plus faibles (les deux questions sur le
  budget de l'association) tombent dans le haut du bruit.
- **qwen3-embedding:0.6b** classe bien mais garde un bruit de fond plus haut, et prend
  deux fois plus de VRAM.

Préfixes conseillés par les auteurs (`--prefixes`) : ils aident un peu snowflake
(« query: », AUC 0,9999) et qwen3 (consigne en anglais), pas embeddinggemma (le meilleur
résultat est sans préfixe) ni nomic. Aiodos n'en met donc aucun.

### Place en mémoire vidéo

Les trois modèles en tête prennent ≈ 655 Mo de VRAM (contre 320 Mo pour nomic), se
chargent en 2,5 à 3,5 s à froid et calculent 20 à 25 ms par passage. Modèles de
conversation mesurés seuls sur la même machine (un seul modèle chargé à la fois) :
`qwen2.5-coder:7b` (modèle par défaut) occupe 4 535 Mo et laisse 3,2 Go libres ;
`qwen3.5:9b` en occupe 5 206 Mo (le reste en mémoire vive) et laisse 1,7 Go. D'après ces
chiffres, un modèle d'embedding de 650 Mo devrait tenir à côté du modèle de conversation
sans qu'Ollama doive décharger l'un pour charger l'autre à chaque question (non vérifié
avec les deux chargés ensemble) ; qwen3-embedding (1,2 Go) serait juste avec qwen3.5.

## Décision

- **Modèle par défaut : `embeddinggemma`** — meilleure séparation et meilleur classement
  du jeu de test, même VRAM que bge-m3 et snowflake, deux fois plus léger à télécharger,
  vecteurs de 768 dimensions (un quart de stockage en moins que 1024). Licence : Gemma
  (poids ouverts, avec une politique d'usage) ; si l'on tient à une licence libre au
  sens strict, `bge-m3` (MIT) ou `snowflake-arctic-embed2` (Apache 2.0) sont les
  meilleures solutions de repli.
- **Seuil `rag_min_score` = 0,40** : le bon passage le plus faible du jeu de test
  (0,449), arrondi au centième inférieur, moins 0,04 de marge pour des questions moins
  bien formulées que celles du jeu. Même règle pour les autres modèles ; nomic garde son
  ancien 0,70 (aucun seuil ne lui convient).
- **Le seuil suit le modèle** : tant que l'utilisateur ne le fixe pas lui-même,
  `rag_min_score` prend la valeur mesurée pour le modèle d'embedding choisi
  (`RAG_THRESHOLDS` dans `aiodos/settings.py`), et revient à cette valeur quand on change
  de modèle. Une installation restée sur nomic garde donc 0,70.
- **Fin de l'écart relatif** (`RELEVANCE_MARGIN` = 0,25 dans `aiodos/chat/pipeline.py`) :
  ce garde-fou, réglé pour nomic, retirait les extraits à plus de 0,25 du meilleur. Avec
  la même question déjà posée en mémoire (« Utilisateur : Quel est le budget 2024 ? »,
  0,888 avec embeddinggemma), il aurait écarté le vrai budget (0,524) et la réponse
  mémorisée (0,564) pour ne garder que la question. Le seuil absolu suffit désormais.
- **Changer de modèle relance la réindexation** (type de travail `reindex`) : les
  vecteurs d'un modèle ne se comparent pas à ceux d'un autre.

## Refaire la mesure

Pour essayer un autre modèle : `ollama pull <modèle>`, puis
`python tools/embedding_bench.py <modèle> --details --json tools/embedding_bench_results.json`,
et ajouter son seuil à `RAG_THRESHOLDS` selon la même règle.
