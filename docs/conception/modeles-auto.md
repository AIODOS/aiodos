# Le meilleur modèle disponible, pour chaque genre

*Conception du 29/10/2026, d'après la demande de l'auteur et la recherche du même jour (état de
l'art des modèles à poids ouverts, notes : classements Artificial Analysis lus le 29/10/2026,
pages Hugging Face et GitHub des modèles). À valider avant de coder.*

## 1. La demande

> « Pour les modèles d'image utilisés, il faut utiliser le plus puissant s'il est disponible sur
> le réseau. Si on a un PC avec une machine qui peut faire tourner le plus gros modèle, on le
> fait tourner en priorité, puis on descend dans la liste en fonction des machines disponibles.
> […] Dans le sélecteur d'intelligence artificielle, on peut avoir un mode auto pour les modèles
> de génération de textes comme Qwen mais également pour les images, vidéos et audio. […] Dans
> le choix des moteurs d'IA, il faut qu'on ait plus de possibilités. On retravaille ce menu. »

Constat : Z-Image Turbo, le seul modèle d'images du catalogue, a 936 points Elo au classement
des images d'Artificial Analysis (poids ouverts) ; les trois meilleurs modèles ouverts ont
environ 1 040 points, soit environ deux chances sur trois d'être préférés en duel. Le mode
automatique d'aujourd'hui ne choisit que **la machine** d'un modèle de texte donné ; les images
prennent le premier modèle du catalogue qui peut calculer.

## 2. Le principe : « Automatique » pour chaque genre

Quatre genres : **texte**, **images**, **vidéos**, **audio** (voix, musique, effets sonores).
Chacun a son catalogue, **classé du plus puissant au plus modeste** (classement public, puis nos
mesures), avec pour chaque modèle ses variantes (taille des fichiers, mémoire de la carte
graphique nécessaire), sa licence et ses moteurs.

« Automatique » (le choix par défaut de chaque genre) prend **le premier modèle du classement
qu'une machine disponible a et peut faire tourner en ce moment**, puis descend dans la liste :

1. Pour chaque modèle, du plus puissant au plus modeste : une machine qui l'a, libre ou qui le
   sera bientôt : ce PC, mes machines, mes amis (s'ils le proposent et que je l'accepte), le
   réseau public (avec l'accord demandé une fois, jamais pour une conversation « Sur mes
   machines seulement »).
2. Le premier modèle trouvé gagne, **où qu'il soit** : un inconnu qui a un modèle plus puissant
   passe devant mon PC qui n'a qu'un modèle plus modeste (la demande de l'auteur).
3. À modèle égal, la machine la plus proche de moi, puis la plus rapide.

Ce qui ne change pas : rien ne se télécharge sans un clic ; un modèle à licence restrictive
n'est proposé qu'avec sa licence affichée et acceptée ; la réponse dit toujours quel modèle et
quelle machine (« Image · FLUX 3 Klein · réseau public »).

Ce qui s'ajoute : **la suggestion du meilleur modèle pour cette machine** (« Ce PC peut faire
tourner FLUX 3 Image Klein, bien meilleur que Z-Image Turbo : le télécharger ? »), d'après la
mémoire de sa carte graphique, dans Réglages → Modèles et dans le menu.

## 3. Le catalogue proposé

⚠ : licence à afficher et à accepter avant le téléchargement. Mémoire : celle de la carte
graphique pour la variante la plus légère qui tient (à mesurer chez nous).

### Images (sd.cpp et ComfyUI)

| Rang | Modèle | Licence | Mémoire | Note |
|---|---|---|---|---|
| 1 | FLUX 3 Image Klein (BFL, 15/10/2026), Elo 1043 | ⚠ FLUX Kommunity (gratuite hors commerce, et pour un commerce sous 5 M$) | 8 Go en int8 | le meilleur classé ; 8,9 s une image 1024² sur une RTX 4070 |
| 2 | Qwen-Image-2.1 (Alibaba, 09/2026), Elo 1042 | ⚠ non commerciale | ~17 Go en int8 | Turbo en 8 étapes, retouche, transparence |
| 3 | Ming-Image-0.1-Design (inclusionAI, 09/2026), Elo 999 | MIT | à mesurer | fort pour le texte, les affiches, les interfaces |
| 4 | HiDream-O1-Image (05/2026), Elo 975 | MIT | à mesurer (CUDA) | |
| 5 | Z-Image Turbo (déjà là), Elo 936 | Apache-2.0 | 8 Go | repli |
| 6 | FLUX.2 [klein] 4B, Elo 837 | Apache-2.0 | ~13 Go | très rapide, retouche |

Écartés : Fibo 2.0 (ses consignes passent par Gemini), Ideogram 4 (non commercial, d'accès
conditionné), les modèles de 64 à 80 milliards (huit cartes de serveur).

### Vidéos (sd.cpp et ComfyUI)

| Rang | Modèle | Licence | Mémoire | Note |
|---|---|---|---|---|
| 1 | LTX-2.5 (Lightricks, 07/2026), Elo 949 | ⚠ LTX-2.x Community (gratuite sous 10 M$) | 24 Go | vidéo **et son**, jusqu'à la 4K |
| 2 | Wan 2.2 A14B (Alibaba) | Apache-2.0 | 16 à 24 Go (GGUF) | |
| 3 | Kandinsky 6.0 Pro (09/2026) | MIT | 16 Go et ~58 Go de mémoire vive | vidéo et son ; ComfyUI seulement |
| 4 | Wan 2.2 TI2V 5B | Apache-2.0 | 12 à 24 Go | texte ou image vers vidéo, 720p |
| 5 | Wan 2.1 1.3B (déjà là) | Apache-2.0 | 8 Go | repli |

**MiniMax H3**, déjà au catalogue : sa licence **exclut l'Union européenne** (et les États-Unis,
le Royaume-Uni, la Corée du Sud) et interdit le service à des tiers. À retirer (cf. § 7).
HunyuanVideo 1.5 : même exclusion de l'UE, écarté.

### Audio (moteurs nouveaux)

| Genre | Modèle | Licence | Moteur | Note |
|---|---|---|---|---|
| Voix | Qwen3-TTS 1.7B (01/2026) | Apache-2.0 | qwentts.cpp (C++) | français, clonage d'une voix en 3 s |
| Voix | Chatterbox Multilingual | MIT | ONNX | 23 langues dont le français |
| Voix (téléphone) | Kokoro-82M | Apache-2.0 | sherpa-onnx (Android) | |
| Musique | ACE-Step 1.5 turbo 2B (01/2026) | MIT | acestep.cpp (C++) ou ComfyUI | moins de 4 Go, chansons avec paroles |
| Musique | ACE-Step 1.5 XL 4B (04/2026) | MIT | idem | 12 à 20 Go |
| Musique | MiniMax Music 3 (08/2026) | ⚠ communautaire (nom à afficher pour un usage commercial) | ComfyUI | le meilleur ouvert ; 8 à 24 Go, CUDA |
| Effets | Stable Audio 3 Small SFX (05/2026) | ⚠ Stability Community (gratuite sous 1 M$) | ComfyUI | 0,6B, pensé pour les portables |

## 4. Le menu

Un seul bouton sous la saisie, « **Modèle** », qui dit le choix du genre en cours (« Texte ·
Automatique », ou dans le mode Créer, « Image · Automatique »). Il ouvre un panneau :

- quatre onglets : **Texte · Images · Vidéos · Audio** ;
- en tête de chaque onglet, **Automatique** (recommandé), avec ce qu'il prendrait en ce
  moment : « → FLUX 3 Image Klein, PC fixe » ;
- puis les modèles, du plus puissant au plus modeste, chacun avec où il peut tourner (ici,
  mes machines, amis, réseau public), sa licence (⚠) et, s'il n'est nulle part, « à
  télécharger » (la mémoire qu'il demande, et si ce PC peut le faire tourner) ;
- pour le texte, la machine se choisit comme aujourd'hui (« auto:<modèle> »).

Le même panneau sert dans Réglages → Modèles pour les choix par défaut de chaque genre.

## 5. Les moteurs

Aujourd'hui : llama.cpp (texte, intégré), stable-diffusion.cpp (images, vidéos), ComfyUI
(images, vidéos ; recettes d'AIODOS et workflows de l'utilisateur), Whisper (transcription,
pas encore livré par l'installeur). À ajouter pour l'audio : **qwentts.cpp** (voix),
**acestep.cpp** (musique), **sherpa-onnx** (voix sur téléphone), et les recettes ComfyUI de
musique et d'effets (ACE-Step, Stable Audio, MiniMax Music 3 sont dans le cœur de ComfyUI).
Réglages → Moteurs montre chacun : présent ou non, version, ce qu'il sait faire.

## 6. Les étapes

1. **Le classement et « Automatique » pour les images et les vidéos** : catalogue classé
   (puissance, variantes, mémoire, licence), choix du meilleur modèle disponible sur le réseau
   (§ 2), le profil public annonce déjà les modèles d'images et de vidéos prêtés ; mise à jour
   de stable-diffusion.cpp (FLUX 3 Klein y est depuis le 16/10/2026).
2. **Le menu** (§ 4), texte compris.
3. **Les nouveaux modèles** d'images et de vidéos (§ 3), téléchargés sur clic, mesurés chez nous
   (mémoire, temps) : le classement se corrige avec nos mesures.
4. **L'audio** : moteurs, catalogue, création dans la conversation (« Crée une musique… »,
   « Lis ce texte avec une voix… »).

## 7. Décisions de l'auteur (29/10/2026, soir)

- **Licences** : « Étant donné qu'il n'y a pas de chiffre d'affaires sous AIODOS, qui est
  gratuit, on peut utiliser les meilleurs modèles. […] Cette décision est à prendre par les
  utilisateurs eux-mêmes. Chacun est responsable de ses actes. » Le catalogue prend les
  meilleurs modèles, non commerciaux compris (Qwen-Image-2.1…) ; chaque licence restrictive est
  affichée et acceptée par l'utilisateur avant le téléchargement (comme MiniMax H3 aujourd'hui).
  Restent écartés les modèles qui ne tournent pas sur une machine d'utilisateur (64 à 80
  milliards de paramètres) ou qui dépendent d'un service en ligne (Fibo 2.0 : Gemini).
- **MiniMax H3** : gardé, avec un avertissement (pays exclus par sa licence, service à des tiers
  interdit) ; il peut être prêté à des tiers, avec un avertissement à celui qui prête.
- **Audio** : voix, musique et effets sonores.
- **Menu** : un seul bouton, quatre onglets (§ 4).

## 8. Ce qui reste incertain

Les classements n'affichent pas leur date ; le classement des vidéos sans son n'a pas pu être lu
(l'ordre après LTX-2.5 est une estimation) ; la mémoire de Ming-Image et HiDream-O1 quantifiés
n'est pas publiée ; aucun temps mesuré sur une RTX 4070 portable de 8 Go (le PC de l'auteur) ;
les licences de MiniMax H3 et Krea 2 lues dans des résumés, pas dans leur texte. Chaque modèle
ajouté est mesuré chez nous avant d'être classé.
