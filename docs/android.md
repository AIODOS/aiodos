# Un téléphone Android comme nœud de calcul

> **État : l'appli AIODOS fait tourner le nœud elle-même depuis le 20/10/2026**, essayée sur
> un Galaxy S23 (Android 16) : ni Termux, ni Ollama, ni rien d'autre à installer. Mesures
> [plus bas](#mesures-du-20102026-galaxy-s23). L'ancienne méthode, le nœud dans Termux
> (essayée le 16/10 sur un Galaxy S9), est décrite [en fin de page](#lancienne-méthode--le-nœud-dans-termux).

Un téléphone récent a 6 à 12 Go de mémoire et 8 cœurs. C'est assez pour calculer des
embeddings pour les bibliothèques du PC pendant qu'il recharge, la nuit par exemple.
L'appli AIODOS fait tourner le nœud **en arrière-plan**, avec son propre moteur
(llama.cpp), et calcule pour le PC comme n'importe quelle machine appairée.

## Ce que fait le téléphone, et quand

**Ce qu'il calcule :**
- les textes ;
- les documents : PDF, Word, Excel, PowerPoint, RTF, EPUB, HTML, OpenDocument ;
- les pages web.

Pour chacun, il lit le contenu, le découpe en passages et calcule les embeddings avec
**le même modèle que le PC, à l'empreinte près**. Le résultat est enregistré
**uniquement sur le PC**. Les fichiers reçus sont effacés dès la fin du calcul.

**Ce qu'il ne calcule pas (encore) :**
- l'audio et la vidéo, faute de Whisper : il n'est pas dans l'appli ;
- les images et les conversations, tant qu'aucun modèle de conversation n'y est installé
  (avec qwen3.5:2b, à essayer).

Le PC ne lui confie que les unités qu'il sait traiter (`can_serve`,
`aiodos/network/work.py`). Le téléphone annonce les formats de document qu'il sait
lire : s'il lui manque un paquet de lecture (python-pptx par exemple), les
présentations restent au PC ou partent vers une autre machine.

**Quand il calcule :** seulement si **son** utilisateur a activé « Prêter ma
puissance », et seulement dans ces conditions, comme BOINC sous Android :

| Condition | Par défaut | Pourquoi |
|---|---|---|
| En charge (secteur, USB ou sans fil) | oui | jamais sur batterie |
| Batterie suffisante | 80 % pour commencer ; pause sous 75 % une fois lancé | il se recharge d'abord ; l'écart évite de s'arrêter et repartir sans cesse quand le chargeur est faible |
| Batterie pas trop chaude | 40 °C au plus ; reprise à 37 °C | la chaleur use la batterie |

- BOINC exige 90 % de charge. Ici, la valeur par défaut est 80 % : beaucoup de téléphones
  s'arrêtent de charger à 80 ou 85 % pour ménager leur batterie, et n'atteindraient
  jamais 90 %.
- Débranché, trop chaud ou pas assez chargé, le téléphone **rend aussitôt** ses unités en
  cours. L'état de la batterie est relu toutes les 30 s au plus.
- Contrairement à un PC, on ne peut pas savoir sans `adb` si quelqu'un utilise le
  téléphone (écran allumé). C'est donc la charge qui fait foi.

## L'appli AIODOS

### Installer

| | |
|---|---|
| Téléphone | Android 9 ou plus récent, processeur 64 bits (arm64) |
| Mémoire | 4 Go au moins ; 8 Go pour un modèle de conversation |
| Espace libre | environ 200 Mo pour l’appli, plus les modèles (334 Mo pour embeddinggemma) |
| Réseau | le même Wi-Fi que le PC pour l'appairage (pas un réseau « invité ») ; ensuite, Internet suffit |

1. **Sur le PC** : `python tools/build_android.py --install` construit l'appli et
   l'installe sur le téléphone branché en USB (débogage USB autorisé). Sans câble :
   `dist/AIODOS-<version>.apk`, à copier sur le téléphone et ouvrir (autoriser
   « Installer des applis inconnues » pour l'appli qui l'ouvre).
2. **Ouvrir AIODOS.** Android demande d'autoriser les notifications : accepter. Sans
   elles, le nœud tourne quand même, mais sa notification n'apparaît que dans le
   gestionnaire de tâches.
3. L'interface s'affiche dès que le nœud est prêt : 6 secondes à la première ouverture
   (mise en place de Python), 3 ensuite.

### Ce qui tourne, et comment l'arrêter

- Le nœud tourne **en arrière-plan**, appli fermée : c'est lui qui calcule pour les autres
  machines. Android l'exige pour tout travail en arrière-plan : une notification discrète
  « AIODOS — Le nœud AIODOS est en marche » l'accompagne. Son bouton **« Arrêter »** arrête
  le nœud ; rouvrir l'appli le redémarre.
- **« Démarrer avec le téléphone »** est activé dès la première ouverture (vue « Cette
  machine », où on peut le retirer) : après un redémarrage, le nœud repart seul.
- Le retour en arrière, sur la première page, met l'appli en arrière-plan sans arrêter le
  nœud.

### Les modèles

« Installer un modèle » propose ceux qui conviennent au téléphone, avec leur licence :
- **embeddinggemma** (334 Mo), le modèle des bibliothèques : celui du PC, au fichier
  près ;
- **qwen3.5:2b** pour converser, conseillé avec 8 Go de mémoire (qwen3.5:0.8b en dessous).

Les fichiers viennent de Hugging Face, épinglés et vérifiés (SHA-256) avant d'être mis en
place (docs/moteur-integre.md). **De préférence en Wi-Fi** : plusieurs centaines de Mo.

### Si Android arrête le nœud

Avec son service au premier plan, le nœud n'a besoin d'aucun réglage. Si un constructeur
l'arrête quand même en arrière-plan (certains Samsung, Xiaomi…) : Paramètres,
Applications, AIODOS, Batterie, « Non restreinte » (le libellé varie selon la marque,
voir [dontkillmyapp.com](https://dontkillmyapp.com)).

### Ce qu'il y a dans l'appli

- **Python 3.13 dans le processus de l'appli**, par Chaquopy 17.1 (licence MIT) : le même
  code d'AIODOS que sur les PC, mis dans l'appli à sa construction. Les paquets natifs
  viennent du dépôt de Chaquopy (numpy 1.26.2, cryptography 42.0.8, lxml 5.3.0,
  Pillow 11.0.0), sauf **pydantic-core**, que FastAPI demande et que personne ne publie
  pour Android : compilé ici avec Rust et le NDK (`tools/android_wheels.py`). Le code
  d'AIODOS est vérifié compatible avec 3.13 (les annotations sont évaluées dès la
  définition, contrairement à 3.14).
- **Les programmes rangés parmi les bibliothèques** de l'appli, le seul dossier d'où
  Android permet d'exécuter un programme : `llama-server` et `ggml-rpc-server` de l'archive
  officielle de llama.cpp pour Android, de la même version que sur les PC (b11707, avec
  une variante par famille de processeur ARM, choisie au démarrage), et le compagnon
  Internet `aiodos-net`, compilé avec le NDK. L'appli les retrouve sous leurs vrais noms
  par des liens (`node/android_app.py`).
- **Un service au premier plan** (type `specialUse`) garde le nœud en marche : Android ne
  met pas en veille une appli qui en a un. Un verrou de veille garde le processeur éveillé,
  écran éteint, **seulement en charge** (le téléphone ne calcule qu'en charge), et un
  verrou multicast laisse passer les annonces des machines du réseau local.
- **La batterie et le Wi-Fi** se lisent par Android lui-même (`Device.java`) : plus de
  Termux:API.
- **Les mises à jour** arrivent avec une nouvelle appli (même clé de signature, gardée
  hors du dépôt dans `~/.aiodos/android/`) : le code d'une appli ne se remplace pas par le
  réseau.
- **Les données** (identité, appairages, bibliothèques, modèles) restent dans le dossier
  privé de l'appli, effacé avec elle : ni sauvegarde, ni transfert vers un nouveau
  téléphone (l'identité du nœud y serait copiée).
- **Pas de serveur RPC** pour les gros modèles répartis : `ggml-rpc-server` n'a ni mot de
  passe ni chiffrement, et toute appli du téléphone joindrait son port de 127.0.0.1.

**Les « limites des processus enfants »** (le *phantom process killer*, qui gênait le
nœud sous Termux) : `llama-server` et le compagnon sont bien des processus enfants de
l'appli, qu'Android suit comme tels. Mais sa règle d'arrêt pour usage du processeur ne vise
que les applis en arrière-plan, et celle-ci, avec son service au premier plan, n'y est
jamais ; l'autre règle (32 processus au plus, toutes applis réunies) n'en compte que deux
ou trois ici. La règle de conception de l'étude ([installeurs.md](installeurs.md),
§ 3.5 : aucun programme lancé à part) est donc assouplie, comme le fait Syncthing pour
Android.

### Mesures du 20/10/2026 (Galaxy S23)

Galaxy S23 (SM-S911B, Snapdragon 8 Gen 2, 8 Go, Android 16), en 4G.

- **Appli** : 58,6 Mo, dont Python et ses paquets (37 Mo) et les bibliothèques natives,
  programmes compris (22 Mo compressés, 51 Mo une fois dépliés).
- **Démarrage** : nœud prêt en 6 s à la première ouverture, 3 s ensuite ; compagnon
  Internet démarré, llama.cpp 11707 reconnu (processeur).
- **embeddinggemma** (Q8_0, le fichier du PC copié par le câble) : chargé en 14 s ;
  32 passages d'environ 490 caractères en 4,8 s, soit **environ 400 passages par minute**
  (le S9 sous Termux et Ollama en faisait 8, sur des passages deux fois plus longs).
- **Mêmes vecteurs que le PC** : similarité 0,9997 en moyenne (0,9995 au moins) avec le
  processeur comme avec la carte graphique du PC, mêmes 10 premiers résultats d'une
  recherche ; identiques au bit près d'une fois sur l'autre sur le téléphone.
- **« Arrêter »** dans la notification : nœud, compagnon et notification arrêtés ensemble.

**Reste à essayer** : l'appairage et le calcul pour le PC (en Wi-Fi), le démarrage avec le
téléphone, plusieurs heures écran éteint en charge, un modèle de conversation.

### Passer de Termux à l'appli

- La nouvelle appli s'installe par-dessus l'ancienne (même signature) et n'utilise plus
  Termux. Son nœud a **une nouvelle identité** : l'appairer avec le PC (sur le Wi-Fi), puis
  retirer l'ancien téléphone de « Mes machines » sur le PC.
- **Arrêter le nœud de Termux** : dans Termux, `pkill -f "python3 -m aiodos"; pkill
  ollama; pkill aiodos-net`, ou redémarrer le téléphone. Ses programmes survivent à
  « Forcer l'arrêt » de Termux (constaté le 20/10). Tant qu'il tient le port 47701, le
  nœud de l'appli prend le suivant (47702), l'annonce aux autres machines, et la vue Réseau
  dit quel nœud garde le port et quoi faire.
- Termux et Termux:API peuvent ensuite être désinstallés : l'ancien nœud part avec eux.

### Construire l'appli (sur le PC)

```sh
python tools/build_android.py            # dist/AIODOS-<version>.apk
python tools/build_android.py --install  # et l'installer sur le téléphone branché
```

Il faut : le SDK Android (`ANDROID_HOME`) avec le NDK, un JDK 17 ou plus récent, Rust avec
la cible `aarch64-linux-android`, et **Python 3.13** (`uv python install 3.13`) : Chaquopy
installe les paquets de l'appli avec la même version qu'elle. Depuis le 21/10/2026
([securite.md](securite.md), § 3.7), l'outil refuse aussi de construire tant que
`aiodos/` ou `compagnon/` ont des modifications non commitées ou des fichiers non suivis,
ou que le dépôt, ses dossiers de travail, le SDK, le JDK ou ce Python sont modifiables par
d'autres comptes de la machine ; chaque refus dit quoi corriger.

- `android/` : le projet Gradle (assistant `gradlew` pour Gradle 8.11.1, empreinte
  vérifiée ; greffon Android 8.9.1 ; Chaquopy 17.1.0), le manifeste, le code Java et
  `requirements.txt` (les paquets Python, aux versions des PC quand Chaquopy le permet).
- `tools/android_deps.py` épingle ce que l'appli prend ailleurs (archive Android de
  llama.cpp, sources de pydantic-core, Python 3.13 de Chaquopy) : taille et SHA-256,
  téléchargés une fois dans `~/.aiodos/android/telechargements/`.
- **La clé de signature** (`~/.aiodos/android/aiodos-debug.keystore`) n'est jamais
  recréée en silence : avec une autre clé, Android refuse la mise à jour, et désinstaller
  l'appli efface l'identité du nœud. Sur un autre PC, la recopier ; `--nouvelle-cle`
  seulement pour un tout premier téléphone. La sauvegarder.
- Gardés d'une construction à l'autre dans `build/android-cache/` : la roue de
  pydantic-core (une minute à compiler) et le compagnon (une fois par état de
  `compagnon/`). Une nouvelle construction prend ensuite une dizaine de secondes.

## Appairer avec le PC

1. **Sur le téléphone** : ouvrir l'appli AIODOS et la laisser ouverte.
2. **Sur le PC**, vue « Réseau local » (activer le réseau si besoin) :
   - le téléphone apparaît parfois tout seul dans « Machines connectées » (filtre « Réseau
     local »), sous le nom de son modèle, par exemple « Google Pixel 7 » ;
   - sinon, saisir son adresse (`192.168.1.42`) puis « Ajouter ».
3. **Sur le PC**, « Appairer » : un code à 6 chiffres s'affiche sur les deux écrans.
   **Confirmer sur chacun**, seulement si les codes sont identiques. Le PC et l'appli
   doivent être de la même génération : depuis le 21/10/2026, l'appairage par code passe
   au protocole 2, et une version d'avant reçoit un refus qui dit de la mettre à jour
   ([securite.md](securite.md), § 5).
   - Si la fenêtre n'apparaît pas sur le téléphone : vue « Réseau local », puis
     « Vérifier le code ».
   - Une demande expire au bout de 3 minutes.
4. Le téléphone apparaît dans « Mes machines » sur le PC, avec l'état de sa batterie.

À savoir :
- Écran éteint, Android filtre les annonces diffusées sur le réseau. Le téléphone voit donc
  mal les autres machines, mais ce n'est pas gênant : l'appairage part du PC, et les
  échanges se font ensuite directement d'une machine à l'autre.
- L'adresse du téléphone peut changer, puisque la box l'attribue. Réserver son adresse
  dans la box évite de devoir le rajouter.
- Le téléphone doit pouvoir joindre le PC sur le port 47701. Sous Windows, autoriser
  Python sur les **réseaux privés** et vérifier que le Wi-Fi du PC est bien déclaré
  « privé ».

## Prêter la puissance du téléphone

**Dans l'interface du téléphone**, vue « Cette machine » :
- « Prêter ma puissance à mes autres machines » : **activer**. C'est le geste de
  l'utilisateur du téléphone : rien ne se fait sans lui.
- « Seulement en charge » (activé), « Batterie minimale » (80 %), « Température
  maximale de la batterie » (40 °C) : à ajuster si besoin.
- « Puissance prêtée » (60 % par défaut depuis le 27/10/2026) : le nombre de calculs faits en
  même temps et des pauses entre deux ; plus bas, le téléphone chauffe moins. BOINC n'utilise
  que la moitié des cœurs d'un téléphone.
- « Alimentation » doit afficher par exemple **« En charge · batterie 84 % · 31,2 °C »** :
  - « Charge inconnue » : dans l'appli, Android la donne toujours ; sous Termux,
    voir l'étape 2 de l'ancienne méthode ;
  - l'état du partage, en haut de la carte, dit « Prêt », « En pause » (et pourquoi) ou
    « Calcule pour tes machines ».

**Sur le PC** :
- « Me faire aider par mes autres machines » doit être activé (c'est le cas par défaut) ;
- une bibliothèque « gardée sur ce PC » (cadenas) ne part jamais.

## Vérifier que ça marche

1. Brancher le téléphone, attendre « Prêt » sur son interface.
2. Sur le PC, indexer un dossier de textes et de PDF.
   - Le PC calcule aussi de son côté : le téléphone prend ce qu'il peut quand il demande
     du travail.
   - Pour voir le téléphone travailler seul, lancer le PC sans embeddings locaux :
     `python -m aiodos --slots embed=0`.
3. Vue « Tâches » du PC : les unités calculées ailleurs indiquent la machine. Sur le
   téléphone, l'état passe à « Calcule pour tes machines » avec le nom du fichier.
4. Débrancher le téléphone : en moins d'une minute, il se met en pause et rend son unité,
   que le PC reprend.

## Hors du Wi-Fi de la maison : le compagnon Internet

Pour que le téléphone continue de calculer en 4G/5G, ou chez un ami, chaque machine fait
tourner le compagnon `aiodos-net` (Rust, Iroh) à côté du nœud. **L'appli le contient** et le
démarre avec le nœud (« Relier mes machines par Internet », activé par défaut) :

1. Appairer le téléphone et le PC sur le Wi-Fi, comme d'habitude : ils s'échangent alors
   leurs clés Internet.
2. Ensuite, quand le réseau local ne répond plus, les requêtes passent par le compagnon
   (en direct, ou par un relais de n0).

En 4G, le téléphone n'est pas joignable directement de l'extérieur : le PC le joint par
le relais, ou se sert de la connexion que le téléphone a ouverte vers lui. Sous Termux, le
compagnon se construisait à part : voir [compagnon/README.md](../compagnon/README.md).

## Dépannage

| Symptôme | Cause probable | Que faire |
|---|---|---|
| « AIODOS n'a pas démarré : … » à l'ouverture de l'appli | erreur du nœud | toucher l'écran pour redémarrer AIODOS ; journal par le câble USB : `adb logcat -s python.stdout AIODOS` |
| Le nœud de l'appli s'arrête écran éteint | réglage de batterie du constructeur | « Si Android arrête le nœud » |
| « Port 47701 pris par un autre programme » au journal | ancien nœud sous Termux encore en marche | « Passer de Termux à l'appli » |
| `termux-battery-status` ne rend pas la main ; « Charge inconnue » (Termux) | application Termux:API absente, ou pas de la même source que Termux | l'installer depuis la même source (étape 1) |
| « En pause : batterie à 74 % (reprise à 80 %) » | charge insuffisante | attendre, ou baisser « Batterie minimale » ; préférer un chargeur secteur |
| « En pause : batterie à 41 °C (reprise à 37 °C) » | batterie trop chaude | retirer la coque, aérer, baisser « Puissance prêtée » |
| Le téléphone ne reçoit jamais rien | modèle d'embedding absent du téléphone ou différent de celui du PC (embeddinggemma : « Installer un modèle » ; sous Termux, colonne ID de `ollama list`), « Me faire aider » désactivé sur le PC, bibliothèque gardée sur le PC, travail fait seulement d'images, de sons ou de documents que le téléphone ne sait pas lire (paquet de lecture manquant), ou versions d'AIODOS différentes | vérifier dans cet ordre ; le journal du nœud (`data/logs/aiodos.log`) dit pourquoi une unité a été rendue |
| Le PC n'arrive pas à ajouter le téléphone | autre réseau Wi-Fi, réseau « invité », adresse du téléphone changée, nœud arrêté | rouvrir l'appli (sous Termux : relancer `sh lancer-noeud.sh`) ; sur le même Wi-Fi, le téléphone s'annonce tout seul dans « Machines connectées » (Réseau local) |
| `[Process completed (signal 9) - press Enter]` (Termux) | phantom process killer | étape 6 de l'ancienne méthode |
| pip : « no matching distribution » ou « has no usable wheels » (Termux) | le TUR n'a pas (encore) cette version | étape 4 de l'ancienne méthode |
| `ImportError … _cffi_backend` (Termux) | `cffi` non compilé (clang manquait) | `pkg install clang && pkg reinstall python-cryptography` |

## L'ancienne méthode : le nœud dans Termux

Avant le 20/10/2026, le nœud tournait dans Termux, avec son propre Ollama ; l'appli ne
faisait qu'afficher son interface. La méthode reste décrite ici pour mémoire (et pour les
téléphones sous Android 7 ou 8, que l'appli ne prend pas).

### Ce qu'il faut

| | |
|---|---|
| Téléphone | Android 7 ou plus récent, processeur 64 bits (arm64) : le paquet `ollama` de Termux n'existe pas en 32 bits |
| Mémoire | 4 Go conseillés : le modèle embeddinggemma occupe à lui seul environ 650 Mo |
| Espace libre | environ 2,5 Go : Termux, Python, compilateur, Ollama, modèle (622 Mo) |
| Réseau | le même Wi-Fi que le PC. Pas un réseau « invité », qui isole les appareils les uns des autres |
| PC | AIODOS dans la même version que le téléphone, avec le réseau local activé |
| Chargeur | un vrai chargeur secteur de préférence. Branché sur le port USB d'un PC, le téléphone peut se décharger en calculant |

### 1. Installer Termux et Termux:API

1. Installer **Termux** et **Termux:API**, tous deux depuis **F-Droid** (`com.termux`,
   `com.termux.api`), ou tous deux depuis les versions GitHub :
   - les deux applications doivent venir **de la même source**, car elles partagent la
     même signature ;
   - **ne pas** prendre Termux sur Google Play : c'est une version expérimentale
     séparée, pour Android 11 et plus, avec des paquets en retard et sans application
     Termux:API.
2. Ouvrir Termux, attendre la fin de son installation, puis :
   ```sh
   pkg update && pkg upgrade
   ```

### 2. Installer les paquets

```sh
pkg install python python-pip clang
pkg install python-numpy python-cryptography python-lxml python-pillow
pkg install termux-api ollama
```

- numpy, cryptography, lxml et Pillow arrivent **déjà compilés pour Android**. Il ne
  faut jamais les mettre à jour avec `pip install -U`.
- `clang` sert à l'installation de `python-cryptography`, qui compile au passage sa
  dépendance `cffi` avec pip.
- Au 16/10/2026, Termux fournit Python 3.14, Ollama 0.40.3 et termux-api 0.60.0.

Vérifications :

```sh
python -c "import _cffi_backend, numpy, lxml.etree, PIL; print('ok')"
termux-battery-status
```

- Si la première commande signale `_cffi_backend` : `pkg reinstall python-cryptography`.
- La seconde doit afficher l'état de la batterie en JSON. Si elle ne rend pas la main,
  l'application Termux:API manque ou ne vient pas de la même source que Termux.
  Interrompre avec <kbd>Ctrl</kbd>+<kbd>C</kbd>.

### 3. Copier AIODOS sur le téléphone

**Sur le PC**, dans le dossier d'AIODOS (dans la même version que celle qui tourne sur
le PC) :

```sh
git archive --format=zip -o aiodos.zip HEAD
```

- L'archive ne contient que le code versionné : rien du dossier `data/`, donc ni
  conversations, ni bibliothèques, ni clés API.
- Copier `aiodos.zip` dans le dossier « Download » du téléphone, par exemple avec un
  câble USB en mode « Transfert de fichiers ».

**Dans Termux** :

```sh
termux-setup-storage
python -m zipfile -e ~/storage/downloads/aiodos.zip ~/aiodos
cd ~/aiodos
```

- `termux-setup-storage` autorise l'accès aux fichiers du téléphone. Il ne sert qu'une
  fois.
- Le code reste dans le dossier de Termux (`~/aiodos`), pas dans la mémoire partagée
  du téléphone.

**Mettre à jour** : refaire l'archive sur le PC, puis l'extraire au même endroit. Le
dossier `data/` du téléphone est conservé, et avec lui son identité et son appairage.

### 4. Installer les dépendances Python

```sh
pip install --only-binary=:all: \
    --extra-index-url https://termux-user-repository.github.io/pypi/ \
    -r requirements-node.txt
```

- FastAPI dépend de `pydantic-core`, écrit en Rust, dont PyPI ne publie pas de version
  pour Android.
- Le **TUR** (*Termux User Repository*, projet communautaire) en publie, compilées pour
  le Python de Termux (`pydantic_core-2.50.1-cp314-cp314-android_24_arm64_v8a.whl`
  le 16/10/2026).
- `--only-binary=:all:` évite toute longue compilation : pip choisit une version qui
  existe déjà compilée.
- Toutes les autres dépendances sont en pur Python.

**Si cela échoue** (par exemple « no matching distribution » parce que le TUR n'a pas
encore compilé pour une nouvelle version de Python), il faut compiler `pydantic-core`
sur le téléphone. Cela prend environ 15 minutes et beaucoup de mémoire :

```sh
pkg install rust
export ANDROID_API_LEVEL=24
pip install --extra-index-url https://termux-user-repository.github.io/pypi/ -r requirements-node.txt
```

`ANDROID_API_LEVEL=24` est le niveau de Termux, et pas celui du téléphone : avec ce
dernier, pip refuserait la version compilée.

> Quand `pkg upgrade` installe une nouvelle version de Python (3.15 est annoncé), il
> faut refaire cette étape.

### 5. Le modèle d'embedding

Le téléphone doit avoir **exactement** le modèle d'embedding du PC. Pour le connaître, sur
le PC : Réglages, puis « Modèle d'embedding ». C'est `embeddinggemma` par défaut, mais une
ancienne installation peut être restée sur `nomic-embed-text`.

```sh
ollama serve > ~/ollama.log 2>&1 &
ollama pull embeddinggemma
ollama list
```

- `ollama pull embeddinggemma` télécharge 622 Mo.
- La colonne **ID** de `ollama list` doit être identique sur le téléphone et sur le PC.
  Sinon, le PC ne lui confie rien, puisque leurs vecteurs ne seraient pas comparables.
  En cas de différence, refaire `ollama pull` du même modèle sur les deux machines.
- Ollama calcule ici sur le processeur. Ne pas installer `ollama-backend-vulkan` : il
  n'est validé que sur quelques puces graphiques.

### 6. Empêcher Android d'arrêter le nœud

1. **Optimisation de la batterie.**
   - Au premier `termux-wake-lock` (le script de lancement l'appelle), Android propose de
     retirer Termux de l'optimisation de la batterie : **accepter**.
   - Sinon, passer par Paramètres, Applications, Termux, Batterie, et choisir « Non
     restreinte » (le libellé varie selon la marque, voir
     [dontkillmyapp.com](https://dontkillmyapp.com)).
   - Faire de même pour Termux:API.
2. **Le « phantom process killer » (Android 12 et plus)** tue les processus lancés
   depuis Termux.
   - Symptôme : `[Process completed (signal 9) - press Enter]`.
   - Il se déclenche notamment quand on **branche ou débranche le chargeur**, donc
     justement quand le nœud démarre ou s'arrête.

   | Android | Correctif |
   |---|---|
   | 14 et plus | Options pour les développeurs, puis « Désactiver les limites pour les processus enfant » (en anglais *Disable child process restrictions*). L'option se remet à zéro si on désactive les options pour les développeurs. |
   | 12L, 13 et plus | depuis le PC, avec `adb` : `adb shell "settings put global settings_enable_monitor_phantom_procs false"` |
   | 12 | `adb shell "/system/bin/device_config set_sync_disabled_for_tests persistent; /system/bin/device_config put activity_manager max_phantom_processes 2147483647"`. Attention : rien ne désactive l'arrêt pour « usage excessif du processeur ». Baisser « Puissance prêtée » pour en limiter le risque. |

   - Pour afficher les options pour les développeurs : toucher 7 fois « Numéro de build »
     dans « À propos du téléphone ».
   - `adb` fait partie des *platform-tools* du SDK Android, sur le PC.
3. **Garder la notification de Termux.** C'est elle qui maintient Termux en vie en
   arrière-plan. Depuis Android 15, une application qui n'en a pas perd l'accès au
   réseau.

### 7. Lancer le nœud

```sh
cd ~/aiodos
sh lancer-noeud.sh
```

**Ce que fait le script :**
- il garde le processeur éveillé écran éteint (`termux-wake-lock`) ;
- il démarre Ollama s'il ne tourne pas ;
- il lance le nœud avec le réseau local, sans fenêtre, et limite le téléphone à une unité
  d'embeddings et une page web à la fois (`--slots gpu=0,cpu=0,embed=1,net=1`). Les
  options ajoutées après `sh lancer-noeud.sh` sont passées au nœud.

**Lignes à repérer au démarrage :**

```
… INFO aiodos.network : Réseau local actif sur 0.0.0.0:47701 (pour ajouter cette machine depuis une autre : 192.168.1.42:47701)
Nœud AIODOS prêt : http://127.0.0.1:8800/?t=…
```

- La première ligne donne l'**adresse du téléphone** à saisir sur le PC.
- La seconde donne l'**interface du nœud**, à ouvrir dans le navigateur du téléphone :
  appui long dans Termux, puis « More… », puis « Select URL », puis appui long sur
  l'adresse. On peut aussi la copier dans Chrome.
- C'est l'interface du PC. Sur un écran étroit, il faut faire défiler la page de côté.
- Le jeton (`?t=…`) change à chaque lancement.

**Arrêter :**
- <kbd>Ctrl</kbd>+<kbd>C</kbd> dans Termux (touche CTRL de la rangée du bas) ;
- puis `termux-wake-unlock` ;
- Ollama continue de tourner : l'arrêter avec `pkill ollama`.

#### 7 bis. L'application AIODOS (17/10/2026)

> Remplacée le 20/10/2026 par l'appli qui fait tourner le nœud elle-même (plus haut).

Une application Android, avec son icône, remplace ces gestes : elle démarre le nœud dans
Termux s'il ne tourne pas, puis affiche son interface en plein écran. C'est la première
étape de l'application de l'étude ([installeurs.md](installeurs.md), § 3) : le nœud tourne
encore dans Termux.

- **Construire** sur le PC : `python tools/build_android.py` (outils du SDK Android, sans
  Gradle ; `dist/AIODOS-<version>.apk`, 107 Ko, signé d'une clé de développement gardée
  dans `~/.aiodos/android/`). `--install` l'installe sur le téléphone branché.
- **Une fois, dans Termux** : autoriser les commandes venues d'autres applications :

  ```sh
  echo allow-external-apps=true >> ~/.termux/termux.properties
  ```

- **Au premier lancement**, Android demande d'autoriser AIODOS à « exécuter des commandes
  dans l'environnement Termux » : accepter.
- **Comment elle trouve l'interface.** Elle demande à Termux (intention `RUN_COMMAND`) de
  lire `~/.aiodos-ui`, où le nœud écrit l'adresse de son interface, jeton compris (option
  `--ui-file`, fichier lisible par Termux seul, effacé à l'arrêt). Si ce fichier manque ou
  ne répond plus, elle arrête un nœud lancé à la main et le relance avec cette option ;
  journal : `~/aiodos-noeud.log`.
- **Options du nœud** : `~/.aiodos-options`, s'il existe, est ajouté à la commande (par
  exemple `--internet` pour lancer le compagnon Internet quel que soit le réglage).
- Il faut la version d'AIODOS qui connaît `--ui-file` (17/10/2026 et après) dans
  `~/aiodos` : elle arrive par les mises à jour, ou par une copie (« Piloter le téléphone
  depuis le PC », plus bas).
- **Sur un écran étroit**, l'interface passe sur une colonne : la liste des conversations
  s'ouvre par le bouton ☰ de l'en-tête.
- **Essai du 17/10/2026 sur le Galaxy S23** (Android 16, Termux 0.118.3) : nœud démarré
  par l'application en 36 s, compagnon Internet compris, interface affichée.

### Essai sur un Galaxy S9, et ce qui reste à vérifier

Galaxy S9 (SM-G960F, Exynos 9810 de 2018, 4 Go, Android 10), le 16/10/2026, appairé
avec le PC par le câble USB (tunnels `adb`, voir plus bas).

**Vérifié :**
- [x] **Installation.** Les étapes 2 à 4 passent telles quelles avec Python 3.14, en
  moins de 3 minutes : roue `pydantic_core-2.50.1-cp314-cp314-android_24_arm64_v8a` du
  TUR, `cffi` compilé par `python-cryptography`, aucune autre compilation.
- [x] **Démarrage.** Le nœud porte le nom du modèle (« Samsung SM-G960F ») et affiche
  l'adresse à saisir sur le PC.
- [x] **Alimentation.** `/sys/class/power_supply` n'est pas lisible sur ce modèle :
  c'est `termux-battery-status` qui sert. Mesures justes (« en charge par USB, 87 %,
  24 °C »).
- [x] **Appairage.** Même code à 6 chiffres des deux côtés ; l'appairage a même survécu
  au renommage du projet (nouveau protocole `aiodos/1`, clés conservées).
- [x] **Calcul.** Le PC lui confie des unités et accepte ses résultats. Vecteurs du
  processeur du téléphone contre ceux du GPU du PC, même modèle : **cosinus ≥ 0,99998**.
- [x] **Débit : environ 8 passages par minute** (fichiers de code, passages d'environ
  1 000 caractères), soit une centaine de fois moins que le GPU d'un PC récent. Un fichier
  de 170 passages a pris 21 minutes. Ce téléphone sert donc à essayer le réseau, pas à
  accélérer ; un téléphone plus récent fera mieux.
- [x] **Chargeur.** Branché sur le port USB d'un PC, il s'est **déchargé** de 88 à 79 %
  en 35 minutes de calcul, en chauffant de 24 à 35 °C : un vrai chargeur secteur est
  indispensable pour durer.

**Pas encore vérifié :**
- [ ] **Interface** dans le navigateur du téléphone (confirmation du code d'appairage).
- [ ] **Passage en pause** au débranchement, en moins d'une minute.
- [ ] **Blocages de `termux-battery-status`** après des heures de fonctionnement
  (`ps -e | grep termux-api`).
- [ ] **Wi-Fi.** Appairage par l'adresse Wi-Fi, et apparition seule dans « Machines
  détectées ».
- [ ] **Durée.** Plusieurs heures écran éteint (wakelock, Doze), température sous la limite.

**Incompatible :** les tablettes et téléphones restés sous Android 4, 5 ou 6, ou en
32 bits (par exemple la Galaxy Note 10.1 de 2014, Android 5.1.1) : Termux demande
Android 7, et son paquet `ollama` n'existe qu'en 64 bits.

#### Piloter le téléphone depuis le PC (essais, développement)

Pour un téléphone branché en USB avec le débogage USB activé :
- les versions GitHub de Termux sont « débogables » : depuis le PC, `adb shell run-as
  com.termux …` exécute des commandes dans Termux et `adb exec-in run-as com.termux sh -c
  "cat > files/home/aiodos.zip" < aiodos.zip` y dépose des fichiers, sans rien taper sur
  le téléphone ;
- dans ce cas, définir `TERMUX_EXEC__SYSTEM_LINKER_EXEC__MODE=disable` (avec `PREFIX`,
  `HOME`, `PATH`, `TMPDIR` et `LD_PRELOAD` de Termux). Sinon, lancés hors de
  l'application Termux, les programmes passent par le chargeur d'Android et Ollama ne
  retrouve plus son moteur (« llama-server binary not found ») ;
- tunnels : `adb forward tcp:47701 tcp:47701` (le PC joint le téléphone),
  `adb reverse tcp:<port P2P du PC> tcp:<même port>` (le téléphone joint le PC), et
  `adb forward tcp:8800 tcp:8800` pour ouvrir l'interface du téléphone sur le PC.

## Sources (consultées le 16 et le 20/10/2026)

- L'appli (20/10/2026) :
  - Chaquopy, versions et greffon Gradle : <https://chaquo.com/chaquopy/doc/current/versions.html>,
    <https://chaquo.com/chaquopy/doc/current/android.html> ; paquets pour Python 3.13 :
    <https://chaquo.com/pypi-13.1/> ;
  - llama.cpp b11707 pour Android : <https://github.com/ggml-org/llama.cpp/releases/tag/b11707> ;
  - services au premier plan et type `specialUse` :
    <https://developer.android.com/develop/background-work/services/fgs/service-types> ;
  - programmes exécutables depuis le dossier des bibliothèques (API 29 et plus) :
    <https://developer.android.com/about/versions/10/behavior-changes-10#execute-permission>.

- Termux :
  - installation et règle de la même source : <https://github.com/termux/termux-app#installation> ;
  - paquets : <https://packages-cf.termux.dev/apt/termux-main/dists/stable/main/binary-aarch64/Packages> ;
  - recettes : <https://github.com/termux/termux-packages> (`packages/python`,
    `packages/ollama`, `packages/python-pip`).
- Roues du TUR : <https://termux-user-repository.github.io/pypi/>, et le niveau d'API
  attendu par maturin : <https://github.com/termux/termux-packages/issues/30428>.
- `termux-battery-status` :
  - <https://github.com/termux/termux-api/blob/master/app/src/main/java/com/termux/api/apis/BatteryStatusAPI.java> ;
  - blocages : <https://github.com/termux/termux-api/issues/402>, `/466`, `/861`.
- SELinux et `/sys/class/power_supply` : AOSP `system/sepolicy` (`app_neverallows.te`,
  `domain.te`).
- Phantom process killer :
  - <https://github.com/termux/termux-app/issues/2366> ;
  - <https://github.com/agnostic-apollo/Android-Docs/blob/master/en/docs/apps/processes/phantom-cached-and-empty-processes.md>.
- Doze et chargeur : <https://developer.android.com/training/monitoring-device-state/doze-standby>.
- Filtrage des annonces écran éteint (APF) :
  <https://source.android.com/docs/core/connect/android-packet-filter>.
- BOINC sous Android :
  - 90 %, 40 °C, 5 min d'attente après une pause, moitié des cœurs ;
  - <https://github.com/BOINC/boinc> (`lib/prefs.cpp`, `client/cs_prefs.cpp`,
    `android/BOINC/app/src/main/res/xml/root_preferences.xml`).
- Ollama :
  - embeddinggemma (Ollama 0.11.10 ou plus récent, 622 Mo) :
    <https://ollama.com/library/embeddinggemma> ;
  - nomic-embed-text : <https://ollama.com/library/nomic-embed-text>.
