# L'essaim : chaque demande partagée entre un maximum de machines

*Intention, 27 octobre 2026. Document de réflexion écrit avec l'auteur. Codé : l'outil de
mesure `tools/bench_essaim.py` et les places de chaque machine (§ 3).*

## 1. L'intention

> « Que ce ne soit plus une IA qui travaille seule, mais d'autres IA en même temps qui
> partagent le travail entier. Que ce soit les images, les conversations, absolument toute
> demande répartie sur un maximum de machines possible. » — l'auteur, 27/10/2026

Aujourd'hui, une demande à une IA est traitée par **un** modèle sur **une** machine. AIODOS
veut changer cela :

- AIODOS n'est pas un chat où l'on choisit un modèle : c'est **une IA en soi**, qui reçoit
  la demande, trouve comment la découper, distribue les morceaux aux IA présentes sur le
  réseau (les miennes, celles de mes amis, celles d'inconnus), puis **réassemble** le
  résultat, comme on défragmente un disque, et vérifie que tout est bien fait.
- Le partage crée une **parité** : chaque machine a un nombre limité de places ; une machine
  pleine ne reçoit rien. Ce n'est donc jamais toujours la même qui travaille, et chacun a
  intérêt à être là (cf. [la réciprocité](../phase3-incitation.md)).
- C'est le cœur d'AIODOS : relier par Internet les machines de personnes qui ne se
  connaissent pas. Le réseau local n'est qu'un raccourci.

## 2. Vocabulaire

| Mot | Sens |
|---|---|
| **Demande** | Ce que demande l'utilisateur : une question, un code, une image, une vidéo… |
| **Chantier** | La demande découpée : la liste des fragments, leur ordre, leurs dépendances. |
| **Contrat** | Ce que tous les fragments doivent respecter pour former un seul résultat (fil directeur, termes, faits, interfaces d'un code, cadre et palette d'une image…). C'est le « fichier .torrent » de la demande. |
| **Fragment** | Un morceau de travail assez indépendant pour être calculé ailleurs, en même temps. |
| **Chef de chantier** | L'IA qui découpe la demande et écrit le contrat (§ 8). |
| **Ouvrier** | Une machine qui calcule un fragment. |
| **Assembleur** | Qui recolle les fragments ; l'assemblage peut lui-même être partagé (§ 6). |
| **Vérificateur** | Qui contrôle le résultat : un autre modèle, des essais, un recalcul (§ 9). |
| **Places** | Le nombre de fragments qu'une machine peut calculer en même temps (§ 3). |

## 3. Les places de chaque machine

Chaque machine annonce ses **places**, par genre de travail, d'après son matériel et ses
mesures : par exemple 5 places de lecture, 2 d'écriture, 1 d'image pour un PC avec carte
graphique ; 1 place de lecture pour un téléphone en charge. Elle annonce aussi combien
sont **occupées**.

- Une machine à 3 places qui en occupe 3 **ne reçoit rien**. Une machine à 5 places qui en
  occupe 4 reçoit le fragment suivant.
- Parmi les machines qui ont une place libre, le fragment va à **la plus rapide pour ce
  genre de travail** (vitesse mesurée, pas supposée).
- Si aucune machine n'a de place libre, **la machine de l'utilisateur fait le travail
  elle-même**.
- Le nombre de places borne aussi ce qu'un inconnu peut demander à ma machine : c'est une
  protection autant qu'une règle de partage.

**Décision de l'auteur (27/10)** : les places dépendent de la puissance de chaque machine,
sans jamais la prendre toute : **60 % de sa puissance par défaut, réglable par
l'utilisateur**. Application proposée :

- **Calcul** (processeur, carte graphique) : au plus 60 % des cœurs et du temps de calcul de
  la carte ; les places se déduisent de la vitesse mesurée dans cette limite.
- **Mémoire de la carte graphique** : un modèle y tient ou n'y tient pas ; 60 % de 8 Go
  (4,8 Go) exclurait qwen3.5:9b (5,6 Go). La limite y porte donc sur le **temps** de calcul,
  pas sur la mémoire, et la carte est rendue dès que l'utilisateur s'en sert (jeu, image).
- **Mémoire vive** : au moins 40 % laissés libres à l'utilisateur.
- **Téléphone** : en plus des règles existantes (en charge, batterie ≥ 80 %, température
  ≤ 40 °C).
- Un seul réglage « Puissance prêtée », qui remplace `share_max_gpu` (100 % aujourd'hui).

**Codé le 27/10** (`aiodos/node/places.py`) : places de processeur = part × processeurs
logiques ÷ 4, entre 1 et 8 (4 sur le PC de l'auteur, 32 processeurs ; 1 sur un téléphone) ;
une place de carte graphique, prêtée au plus cette part de son temps ; embeddings 2 (1 sous
50 %), réseau 3. Les places servent au travail de fond d'ici comme aux unités prêtées, et
sont dites à chaque demande de travail (`places`, `busy`). Écart à revoir avec l'auteur :
la **mémoire vive** bloque une nouvelle unité sous **la moitié** de la part gardée (20 % à
60 %), pas sous toute la part (40 %) : un portable de 8 Go avec un navigateur ouvert, ou un
téléphone (Android remplit sa mémoire de caches), n'aurait jamais rien calculé. Réglage
dans Réglages → Cette machine → Partage de puissance (20 à 100 %), avec les places qui en
découlent.

## 4. Une décision reposée sans cesse

Le choix des machines **ne se fait pas une fois pour toutes à l'envoi**. Comme un client
torrent qui redemande des morceaux en continu :

- La machine de l'utilisateur garde la **file des fragments** de son chantier. Dès qu'une
  place se libère quelque part sur le réseau, la machine qui a cette place reçoit le
  fragment suivant.
- Si la machine de l'utilisateur a commencé seule parce que personne n'était libre, elle
  **envoie la suite** aux machines qui se libèrent, en cours de route.
- **Fin de partie** (le « mode fin de partie » de BitTorrent) : quand il ne reste que
  quelques fragments en cours sur des machines lentes et qu'une machine rapide se libère,
  le fragment est aussi confié à la rapide ; le premier résultat arrivé gagne, l'autre
  calcul s'arrête.
- **Contribuer cinq minutes suffit** : les fragments sont courts (de quelques secondes à
  quelques minutes) ; un travail long (le rendu d'une vidéo pendant des heures) est fait de
  milliers de fragments, et chacun peut en prendre quelques-uns puis partir. Une machine qui
  part rend ses fragments à la file, ils repartent ailleurs.

## 5. Découper selon la demande

### 5.1 Une question simple

Un seul fragment, envoyé à la machine **la plus rapide qui a une place libre**. Plusieurs
machines peuvent aussi faire la course : la première réponse valable gagne.

### 5.2 Plusieurs questions dans une demande

Une question par fragment, toutes en même temps, puis réponses réunies dans l'ordre.

### 5.3 Une longue réponse : le plan-contrat

Le chef de chantier écrit en quelques secondes le **plan-contrat** : les parties de la
réponse, et ce que toutes doivent respecter (fil directeur, termes, faits et chiffres
communs, ton, longueur). Chaque ouvrier écrit **une partie en même temps** que les autres,
en connaissant le contrat et ce que disent les parties voisines. Les parties s'affichent
dans l'ordre dès qu'elles arrivent ; une couture finale relève les contradictions.

Exemple attendu : une réponse de 800 mots en 8 parties prend le temps du plan, d'une partie
et de la couture, au lieu de 8 parties à la suite. À mesurer (§ 12).

### 5.4 Lire en essaim

Les longs textes (historique, documents, pièces jointes, pages web, bibliothèques) sont
coupés en morceaux ; chaque ouvrier lit le sien et en tire des notes pour la question ;
l'écriture ne lit plus que les notes. C'est souvent la partie la plus lourde d'une demande.

### 5.5 Du code

Le contrat d'un code, ce sont ses **interfaces** : les fichiers, les fonctions, leurs
signatures, les types, les règles communes. Chaque ouvrier écrit un fichier ou une
fonction ; l'assembleur réunit le tout et **lance les essais** : le code se vérifie en
s'exécutant, ce qui en fait le meilleur terrain pour l'essaim.

### 5.6 Images et vidéos par le code

Une image ou une vidéo qu'un modèle de diffusion peint d'un bloc se partage mal. Mais une
image ou une vidéo **décrite par du code** se fragmente comme du code (idée de l'auteur) :

- **Images vectorielles (SVG)** : le contrat fixe le cadre, la composition, la palette ;
  chaque ouvrier écrit un calque ou un objet ; l'assembleur les superpose, puis l'image est
  dessinée. Modèles ouverts : StarVector (Apache-2.0, 1 et 8 milliards de paramètres,
  icônes, logos, schémas), OmniSVG (code Apache-2.0 ; ses données d'entraînement sont non
  commerciales). Pour des illustrations, des schémas, des logos, pas des photographies.
- **Vidéos animées** (Manim et semblables) : le contrat fixe le découpage en scènes, le
  style, la durée ; chaque ouvrier écrit une scène ; Code2Video (planificateur, codeur,
  critique) et TheoremExplainAgent (MIT) montrent la voie avec des modèles généralistes.
- **3D** (scripts Blender) : chaque ouvrier écrit les objets d'une scène ; BlenderLLM
  (affiné de Qwen2.5-Coder-7B) écrit des scripts `bpy`.
- **Le rendu se partage parfaitement** : une fois le code écrit, chaque machine dessine une
  plage d'images de la vidéo (comme une ferme de rendu). Le même code donne les mêmes
  images : on vérifie un ouvrier en redessinant une image prise au hasard.

### 5.7 Images et vidéos de diffusion (photographies)

- **Tuiles synchronisées** : l'image est coupée en tuiles qui se chevauchent, chaque ouvrier
  calcule la sienne ; à chacune des étapes du calcul (8 pour Z-Image Turbo), les ouvriers
  ne s'échangent que les bords, pour que l'image reste d'un seul tenant.
- **Composition puis détails** : une machine compose l'image en petit, plusieurs
  l'agrandissent et l'affinent par tuiles.
- **Vidéo par segments** : chaque ouvrier calcule quelques secondes ; des images-clés
  partagées aux jonctions gardent la continuité.

## 6. La défragmentation, elle aussi partagée

L'assemblage peut lui-même passer par plusieurs machines, en arbre : deux fragments voisins
sont recollés par une machine, deux paires par une autre, et ainsi de suite jusqu'au
résultat (comme un tri par fusion). Pour une vidéo, l'assemblage est une simple mise bout à
bout ; pour une image en tuiles, un fondu des bords ; pour un texte ou un code, une
couture qui vérifie les raccords.

## 7. Plusieurs modèles, chacun selon ses forces

Le chantier n'utilise pas un seul modèle : chaque fragment va au modèle le plus fort **pour
ce genre de travail** parmi les machines libres — un modèle de code pour le code, un petit
modèle pour lire et prendre des notes, un modèle qui voit les images pour les vérifier, le
plus capable pour le plan et la vérification. Chaque machine connaît, pour chaque modèle,
un **score par genre de travail**, mesuré (bancs d'essai) puis ajusté par les vérifications
(§ 9).

## 8. Qui reçoit la question, et qui tranche ? *(question ouverte)*

L'auteur : « Comment fait-on, si on envoie la question à plusieurs IA, pour que cela soit
tranché ? Par un vote, mais si le plus grand nombre est fait des IA les moins
intelligentes, on risque un résultat dégradé. » Proposition, à discuter :

1. **Séparer trois rôles.**
   - L'**orchestration** (qui fait quoi, quand, relances, fin de partie) n'est pas de
     l'IA : c'est du code, sur la machine de l'utilisateur, toujours la même règle.
   - Le **plan** (comment découper, quel contrat) est un travail d'IA, confié à un **chef
     de chantier**.
   - La **vérification** est confiée à un modèle capable qui n'a écrit aucun fragment.
2. **La compétence tranche, pas le nombre.** Le chef de chantier est le modèle **le plus
   capable qui a une place libre**, d'après son score mesuré pour ce genre de demande. Pas
   de vote à une voix par machine.
3. **Quand un vote sert** (deux plans possibles, deux réponses qui se contredisent), les voix
   sont **pondérées par le score mesuré** ; un désaccord entre modèles forts remonte au plus
   fort disponible.
4. **La parité vaut aussi pour le chef** : le rôle tourne entre les modèles capables qui ont
   une place libre, il n'est pas réservé à une machine.
5. **Ce que l'utilisateur choisit dans l'appli** : non plus « quel modèle », mais au plus
   le chef de chantier (automatique par défaut), et la préférence vitesse ou qualité.

## 9. Vérifier

- **Code** : les essais, exécutés par l'assembleur (et par un ouvrier tiré au hasard).
- **Rendu** (vidéo, 3D, SVG dessiné) : une image recalculée ailleurs, identique.
- **Texte** : le vérificateur relit l'assemblage contre le contrat (contradictions,
  répétitions, oublis) et renvoie les fragments fautifs dans la file.
- **Nouveaux venus** : leurs fragments sont calculés deux fois, comme dans BOINC ; un
  désaccord met leur clé de côté ([phase 3, § 5](../phase3-reseau-public.md)).
- Chaque fragment rendu donne un **reçu signé** : il nourrit la réciprocité et la
  réputation.

## 10. Vie privée

Le fragment est aussi une protection : un ouvrier inconnu ne voit que **son** morceau (un
passage de document, une partie de réponse, une tuile), le contrat et la question, jamais
toute la conversation. Le demandeur reste anonyme (clé jetable par chantier, passage par un
relais : décision du 27/10). Rien n'est gardé par celui qui calcule.

## 11. Limites, honnêtement

- Une **question courte** ne gagne presque rien : elle va à la plus rapide (§ 5.1). Mesuré
  le 27/10 : sur trois questions courtes, l'essaim était 1 à 14 fois plus lent qu'une
  réponse d'un trait (le plan coûte plus que la réponse).
- La **cohérence** d'une réponse écrite à plusieurs repose sur le contrat : c'est le pari à
  mesurer.
- Chaque étage (plan, fragments, couture) ajoute un **aller-retour réseau** (0,1 à 0,5 s).
- Des ouvriers de **forces inégales** : les fragments difficiles doivent aller aux modèles
  forts (§ 7), sinon la qualité baisse.
- Les **photographies** de diffusion se partagent moins bien que le code (§ 5.7).

## 12. Mesurer d'abord

`tools/bench_essaim.py` compare, question par question, une réponse d'un trait et la même
réponse en essaim (plan-contrat puis parties en parallèle), sur un llama-server à plusieurs
places qui joue les machines : temps total, temps jusqu'au premier mot, et un juge qui
compare les deux réponses à l'aveugle dans les deux ordres.

### Première mesure (27/10/2026)

PC au repos, qwen3.5:9b sur la RTX 4070 Laptop, un llama-server à 8 places qui joue 8
machines (elles se partagent donc **une seule** carte), plan-contrat en JSON borné.
Résultats dans `tools/bench_essaim_results.json`.

| | D'un trait | En essaim |
|---|---|---|
| Temps, 12 longues questions (médiane) | 100 à 200 s | 26 à 50 s (**×2,85** en médiane) |
| Longueur de la réponse (médiane) | 1 703 jetons | 617 jetons |
| Vitesse à longueur égale (jetons/s, médiane) | 15,9 | 16,9 |
| Premier mot affiché (médiane) | 0,7 s | 27 s |
| Juge à l'aveugle | préférée 11 fois sur 12 | 1 égalité |
| 3 questions courtes | — | 0,07 à 0,95 fois la vitesse d'un trait |

Ce que cela dit :

- **Le plan est le goulot** : 25 s en médiane (un JSON contraint, écrit mot à mot), contre
  11 s pour toutes les parties ensemble. Tant que le plan est lent, l'essaim ne gagne rien
  à longueur égale.
- **Les parties sont trop courtes** : l'essaim répond en 600 jetons là où une réponse d'un
  trait en écrit 1 700 ; le juge préfère la plus complète. Le gain apparent (×2,85) vient
  surtout de là.
- **Le premier mot arrive trop tard** (27 s au lieu de 0,7 s) : rien ne s'affiche pendant
  le plan.
- Les 8 « machines » se partageaient une seule carte : sur de vraies machines séparées,
  chaque partie irait à pleine vitesse. Cette mesure est donc défavorable à l'essaim sur
  ce point, favorable sur un autre (aucun aller-retour réseau).

### Ce qu'il faut changer : le plan en flux

1. **Un plan minuscule, en texte, lu pendant qu'il s'écrit** : une ligne par partie (titre,
   but, longueur), pas de JSON ; chaque partie part chez un ouvrier **dès que sa ligne est
   écrite**, sans attendre la fin du plan. Le contrat (termes, faits) tient en quelques
   lignes avant les parties.
2. **Le premier mot tout de suite** : la machine qui écrit le plan enchaîne elle-même sur la
   première partie, affichée en direct pendant que les autres écrivent la suite.
3. **Des parties à la bonne longueur** : le plan fixe une longueur totale proche d'une
   réponse d'un trait, répartie entre les parties.
4. **Le plan par un modèle rapide** (petit modèle, ou la machine la plus rapide libre), le
   chef de chantier vérifiant à la fin.

Objectif de la prochaine mesure : à longueur égale, l'essaim plus rapide qu'une réponse
d'un trait, le premier mot en moins de 3 s, et le juge sans préférence nette.

Codé dans le banc d'essai (`--methode flux`, mesure prévue le 28/10 au matin), avec une
variante du point 2 : plutôt que d'attendre la fin du plan, **une ouverture part à
l'instant zéro** chez une autre machine (l'essentiel de la réponse en quelques phrases,
sans chiffres précis pour ne contredire aucune partie), pendant que le plan s'écrit ;
chaque partie part dès que sa ligne est écrite. Le premier mot affiché est celui de
l'ouverture.

## 13. Étapes proposées

1. **Mesurer** les longues réponses (§ 12) et décider du seuil où l'essaim vaut la peine.
2. **Places et décision reposée** (§ 3-4) : file de fragments, places annoncées, fin de
   partie ; d'abord pour ce qui existe déjà (travaux de fond, images entre mes machines),
   puis pour les conversations. *Places à 60 % et annoncées : faites le 27/10. La file
   tirée par les machines libres existait déjà (« la file comme un torrent »,
   partage-puissance.md § 2.5) ; fin de partie : faite le 27/10 (partage-puissance.md
   § 2.7, `jobs/endgame.py`).*
3. **Conversations en essaim** entre mes machines et mes amis : plusieurs questions (§ 5.2),
   lecture (§ 5.4), plan-contrat (§ 5.3), chef de chantier et vérificateur (§ 8).
4. **Le réseau public qui calcule** : route publique dans le compagnon, clé jetable,
   limites par clé, prêt aux inconnus activé par défaut (décisions du 27/10).
5. **Code en essaim** avec essais (§ 5.5).
6. **Images et vidéos par le code** (§ 5.6), puis **diffusion en tuiles** (§ 5.7).
7. **Le chantier en direct dans l'appli** (accepté le 27/10) : pour chaque demande, les
   fragments, les machines qui les calculent, leur avancement, la fin de partie.

## 14. Décidé et encore ouvert

- **Chef de chantier** (§ 8) : la proposition est acceptée (27/10).
- **Places** : selon la puissance, 60 % par défaut, réglable (§ 3, 27/10).
- **Chantier en direct** dans l'appli : oui (§ 13, étape 7).
- **Ouvert** : à partir de quelle longueur de réponse l'essaim s'active-t-il ? L'auteur n'a
  pas d'avis : la mesure (§ 12) tranchera, et le chef de chantier peut aussi décider
  demande par demande (estimer la longueur dans le plan, et répondre d'un trait sous le
  seuil).

## Sources

- [StarVector (CVPR 2025)](https://openaccess.thecvf.com/content/CVPR2025/html/Rodriguez_StarVector_Generating_Scalable_Vector_Graphics_Code_from_Images_and_Text_CVPR_2025_paper.html)
- [OmniSVG sur Hugging Face](https://huggingface.co/OmniSVG/OmniSVG1.1_4B)
- [Code2Video](https://opencv.org/code2video/)
- [TheoremExplainAgent](https://dev.co/ai/rag/theoremexplainagent)
- [BlenderLLM](https://www.sourcepulse.org/projects/1847383)
- [SceneCraft (ICML 2024)](https://proceedings.mlr.press/v235/hu24g.html)
