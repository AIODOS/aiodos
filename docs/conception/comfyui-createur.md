# ComfyUI pour les créations d'AIODOS lui-même

*Décisions de l'auteur du 29/10/2026. Suite de
[partage-puissance.md](partage-puissance.md) § 3 (ComfyUI pour les applis : L2b, L2c, et
les recettes, § 3.9, conçues mais pas codées) et de
[reseau-public-calcul.md](reseau-public-calcul.md) § 11 (images et vidéos par le réseau
public).*

## 1. Le besoin

> « Pour ce qui est des vidéos avec PANDORA, j'aimerais que AIODOS permette de créer des
> vidéos ou des images via Comfy. Intégrons-le également directement dans AIODOS. »

Aujourd'hui, AIODOS rejoue dans ComfyUI les travaux que lui confient les applis (PANDORA),
sur ce PC et sur mes machines ; mais **ses propres créations** (mode « Créer » de la
conversation, vue « Images et vidéos ») passent par sd.cpp seulement.

## 2. Ce qui est décidé

1. **Quel ComfyUI** : celui déjà installé sur la machine (Desktop ou portable : trouvé
   comme pour les applis, `engines/comfy_find.py`, lancé au besoin sans fenêtre,
   `engines/comfy_instance.py`) ; s'il n'y en a pas, **un bouton propose de l'installer**, sur
   le clic de l'utilisateur seulement (plusieurs Go ; jamais d'office).
2. **Quels workflows** : **ceux d'AIODOS et les miens**.
   - **Les recettes** (§ 3.9 de partage-puissance.md) : quelques workflows éprouvés, image et
     vidéo, **sans nœuds ajoutés** (nœuds de ComfyUI seulement), livrés avec AIODOS, signés
     avec la clé de publication, le graphe toujours **construit à partir de la recette**,
     jamais reçu ; leurs modèles déclarés par empreinte, téléchargés **sur le clic** de
     l'utilisateur, licence affichée (règle des licences de modèles).
   - **Mes workflows** : importés depuis ComfyUI (« Exporter (API) ») ou depuis PANDORA ; leurs
     paramètres (description, description négative, graine, taille, pas, durée…) repérés
     d'office quand c'est possible (nœuds de texte, échantillonneur, latent vide…), ajustables
     à la main ; proposés dans le mode « Créer » à côté des modèles de sd.cpp.
3. **Le réseau public** : **les recettes seulement**. Un inconnu ne rejoue chez lui qu'une
   recette de son propre catalogue (nœuds de ComfyUI seulement : aucun code arbitraire),
   avec le **même filtre** des descriptions (`imaging/prompt_filter.py`) et le **même accord
   une fois** que les images et les vidéos ; mes workflows importés restent **sur mes
   machines** (un nœud ajouté peut exécuter n'importe quel code chez celui qui calcule).

## 3. Ce qu'il faut construire (dans l'ordre)

1. **Le catalogue des recettes** (`engines/recipes.py`, `engines/catalogue.py`, fichiers de
   recettes, `tools/comfy_recipes.py`) : format du § 3.9 ; un premier jeu modeste (une
   image, une vidéo) à partir des gabarits officiels de ComfyUI (MIT, cité dans `NOTICE`) ;
   signature : préparée mais l'auteur signe lui-même (aucune signature de publication sans
   lui) — d'ici là, recettes **non signées acceptées seulement localement**, jamais pour le
   réseau public.
2. **Créer avec ComfyUI ici** : une recette ou un de mes workflows, ses paramètres, la file
   de travaux (unités, chantier en direct, fin de partie déjà là), le résultat rangé comme
   une création ; dans « Créer », le choix du moteur (sd.cpp ou ComfyUI) et de la recette ou
   du workflow ; dans « Images et vidéos », l'état de ComfyUI (trouvé, lancé, absent →
   « Installer ComfyUI »), les recettes disponibles selon les modèles présents, l'import d'un
   workflow.
3. **Chez mes machines** : comme les travaux ComfyUI des applis (L2b).
4. **Par le réseau public** : recettes seulement, sur le modèle des images publiques
   (`network/public_imaging.py`), recette nommée dans la demande, graphe construit chez le
   prêteur, filtre, accord, rien de gardé.

## 4. Ouvert

- Quelles recettes livrer d'abord (modèles d'image et de vidéo les plus utiles à des
  machines grand public : 8 Go de carte).
- La signature du catalogue par l'auteur, et sa diffusion avec les versions.
