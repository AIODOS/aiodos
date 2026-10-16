# Un téléphone Android comme nœud de calcul (Termux)

> **État : à l'essai.** Le code est prêt et testé sur PC (alimentation du téléphone
> simulée), mais il n'a pas encore tourné sur un vrai téléphone. Les commandes
> ci-dessous ont été vérifiées dans les sources (dépôts Termux, Ollama, BOINC, AOSP)
> le 16/10/2026, pas exécutées. Ce qui reste à vérifier est listé
> [en fin de page](#ce-qui-reste-à-vérifier-sur-un-vrai-téléphone).

Un téléphone récent a 6 à 12 Go de mémoire et 8 cœurs. C'est assez pour calculer des
embeddings pour les bibliothèques du PC pendant qu'il recharge, la nuit par exemple. Il
fait tourner Cœur IA **sans fenêtre** dans Termux, avec son propre Ollama, et calcule
pour le PC comme n'importe quelle machine appairée.

## Ce que fait le téléphone, et quand

**Ce qu'il calcule :**
- les textes ;
- les documents : PDF, Word, Excel, PowerPoint, RTF, EPUB, HTML, OpenDocument ;
- les pages web.

Pour chacun, il lit le contenu, le découpe en passages et calcule les embeddings avec
**le même modèle que le PC, à l'empreinte près**. Le résultat est enregistré
**uniquement sur le PC**. Les fichiers reçus sont effacés dès la fin du calcul.

**Ce qu'il ne calcule pas :**
- les images, faute de modèle de vision : trop lourd pour un téléphone ;
- l'audio et la vidéo, faute de Whisper : il n'est pas installé sous Termux ;
- les conversations.

Le PC ne lui confie que les unités qu'il sait traiter (`can_serve`,
`coeur/network/work.py`). Le téléphone annonce les formats de document qu'il sait
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

## Ce qu'il faut

| | |
|---|---|
| Téléphone | Android 7 ou plus récent, processeur 64 bits (arm64) : le paquet `ollama` de Termux n'existe pas en 32 bits |
| Mémoire | 4 Go conseillés : le modèle embeddinggemma occupe à lui seul environ 650 Mo |
| Espace libre | environ 2,5 Go : Termux, Python, compilateur, Ollama, modèle (622 Mo) |
| Réseau | le même Wi-Fi que le PC. Pas un réseau « invité », qui isole les appareils les uns des autres |
| PC | Cœur IA dans la même version que le téléphone, avec le réseau local activé |
| Chargeur | un vrai chargeur secteur de préférence. Branché sur le port USB d'un PC, le téléphone peut se décharger en calculant |

## 1. Installer Termux et Termux:API

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

## 2. Installer les paquets

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

## 3. Copier Cœur IA sur le téléphone

**Sur le PC**, dans le dossier de Cœur IA (dans la même version que celle qui tourne sur
le PC) :

```sh
git archive --format=zip -o coeur-ia.zip HEAD
```

- L'archive ne contient que le code versionné : rien du dossier `data/`, donc ni
  conversations, ni bibliothèques, ni clés API.
- Copier `coeur-ia.zip` dans le dossier « Download » du téléphone, par exemple avec un
  câble USB en mode « Transfert de fichiers ».

**Dans Termux** :

```sh
termux-setup-storage
python -m zipfile -e ~/storage/downloads/coeur-ia.zip ~/coeur-ia
cd ~/coeur-ia
```

- `termux-setup-storage` autorise l'accès aux fichiers du téléphone. Il ne sert qu'une
  fois.
- Le code reste dans le dossier de Termux (`~/coeur-ia`), pas dans la mémoire partagée
  du téléphone.

**Mettre à jour** : refaire l'archive sur le PC, puis l'extraire au même endroit. Le
dossier `data/` du téléphone est conservé, et avec lui son identité et son appairage.

## 4. Installer les dépendances Python

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

## 5. Le modèle d'embedding

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

## 6. Empêcher Android d'arrêter le nœud

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
   | 12 | `adb shell "/system/bin/device_config set_sync_disabled_for_tests persistent; /system/bin/device_config put activity_manager max_phantom_processes 2147483647"`. Attention : rien ne désactive l'arrêt pour « usage excessif du processeur ». Baisser « Calcul prêté au maximum » pour en limiter le risque. |

   - Pour afficher les options pour les développeurs : toucher 7 fois « Numéro de build »
     dans « À propos du téléphone ».
   - `adb` fait partie des *platform-tools* du SDK Android, sur le PC.
3. **Garder la notification de Termux.** C'est elle qui maintient Termux en vie en
   arrière-plan. Depuis Android 15, une application qui n'en a pas perd l'accès au
   réseau.

## 7. Lancer le nœud

```sh
cd ~/coeur-ia
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
… INFO coeur.network : Réseau local actif sur 0.0.0.0:47701 (pour ajouter cette machine depuis une autre : 192.168.1.42:47701)
Nœud Cœur IA prêt : http://127.0.0.1:8800/?t=…
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

## 8. Appairer avec le PC

1. **Sur le téléphone** : ouvrir son interface (étape 7) et la laisser ouverte.
2. **Sur le PC**, vue « Réseau local » (activer le réseau si besoin) :
   - le téléphone apparaît parfois tout seul dans « Machines détectées », sous le nom de
     son modèle, par exemple « Google Pixel 7 » ;
   - sinon, saisir son adresse (`192.168.1.42`) puis « Ajouter ».
3. **Sur le PC**, « Appairer » : un code à 6 chiffres s'affiche sur les deux écrans.
   **Confirmer sur chacun**, seulement si les codes sont identiques.
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

## 9. Prêter la puissance du téléphone

**Dans l'interface du téléphone**, vue « Cette machine » :
- « Prêter ma puissance à mes autres machines » : **activer**. C'est le geste de
  l'utilisateur du téléphone : rien ne se fait sans lui.
- « Seulement en charge » (activé), « Batterie minimale » (80 %), « Température
  maximale de la batterie » (40 °C) : à ajuster si besoin.
- « Calcul prêté au maximum » : entre 50 et 75 %, le téléphone fait des pauses entre deux
  unités et chauffe moins. BOINC n'utilise que la moitié des cœurs d'un téléphone.
- « Alimentation » doit afficher par exemple **« En charge · batterie 84 % · 31,2 °C »** :
  - « Charge inconnue (Termux:API absent ?) » : voir l'étape 2 ;
  - l'état du partage, en haut de la carte, dit « Prêt », « En pause » (et pourquoi) ou
    « Calcule pour tes machines ».

**Sur le PC** :
- « Me faire aider par mes autres machines » doit être activé (c'est le cas par défaut) ;
- une bibliothèque « gardée sur ce PC » (cadenas) ne part jamais.

## 10. Vérifier que ça marche

1. Brancher le téléphone, attendre « Prêt » sur son interface.
2. Sur le PC, indexer un dossier de textes et de PDF.
   - Le PC calcule aussi de son côté : le téléphone prend ce qu'il peut quand il demande
     du travail.
   - Pour voir le téléphone travailler seul, lancer le PC sans embeddings locaux :
     `python -m coeur --slots embed=0`.
3. Vue « Tâches » du PC : les unités calculées ailleurs indiquent la machine. Sur le
   téléphone, l'état passe à « Calcule pour tes machines » avec le nom du fichier.
4. Débrancher le téléphone : en moins d'une minute, il se met en pause et rend son unité,
   que le PC reprend.

## Dépannage

| Symptôme | Cause probable | Que faire |
|---|---|---|
| `termux-battery-status` ne rend pas la main ; « Charge inconnue » | application Termux:API absente, ou pas de la même source que Termux | l'installer depuis la même source (étape 1) |
| « En pause : batterie à 74 % (reprise à 80 %) » | charge insuffisante | attendre, ou baisser « Batterie minimale » ; préférer un chargeur secteur |
| « En pause : batterie à 41 °C (reprise à 37 °C) » | batterie trop chaude | retirer la coque, aérer, baisser « Calcul prêté au maximum » |
| Le téléphone ne reçoit jamais rien | modèle d'embedding différent (colonne ID de `ollama list`), Ollama arrêté sur le téléphone, « Me faire aider » désactivé sur le PC, bibliothèque gardée sur le PC, travail fait seulement d'images, de sons ou de documents que le téléphone ne sait pas lire (paquet de lecture manquant), ou versions de Cœur IA différentes | vérifier dans cet ordre ; le journal du nœud (`data/logs/coeur.log`) dit pourquoi une unité a été rendue |
| Le PC n'arrive pas à ajouter le téléphone | autre réseau Wi-Fi, réseau « invité », adresse du téléphone changée, nœud arrêté | relancer `sh lancer-noeud.sh` et reprendre l'adresse affichée |
| `[Process completed (signal 9) - press Enter]` | phantom process killer | étape 6 |
| pip : « no matching distribution » ou « has no usable wheels » | le TUR n'a pas (encore) cette version | étape 4, « Si cela échoue » |
| `ImportError … _cffi_backend` | `cffi` non compilé (clang manquait) | `pkg install clang && pkg reinstall python-cryptography` |

## Ce qui reste à vérifier sur un vrai téléphone

- [ ] **Installation.** Les étapes 2 à 4 passent telles quelles avec Python 3.14 :
  - roue `pydantic-core` du TUR ;
  - `cffi` compilé par `python-cryptography` ;
  - dépendances en pur Python.
- [ ] **Démarrage.** `sh lancer-noeud.sh` démarre :
  - Ollama et le nœud ;
  - l'adresse du téléphone s'affiche ;
  - le nœud porte le nom du modèle du téléphone (et non « localhost »).
- [ ] **Interface.** Elle est utilisable dans le navigateur du téléphone, en particulier
  la confirmation du code d'appairage.
- [ ] **Alimentation.** Elle est bien lue :
  - `/sys/class/power_supply` est-il lisible sur ce modèle, ou est-ce `termux-battery-status`
    qui sert ?
  - les mesures (en charge, %, °C) sont-elles justes ?
  - le passage en pause se fait-il au débranchement, en moins d'une minute ?
- [ ] **Blocages de `termux-battery-status`.** Ils surviennent au hasard ; vérifier
  qu'aucun processus `termux-api` ne reste bloqué après des heures de fonctionnement
  (`ps -e | grep termux-api`).
- [ ] **Appairage.** Il fonctionne avec le PC par adresse IP. Le téléphone apparaît-il
  aussi tout seul dans « Machines détectées » ?
- [ ] **Calcul.** Le PC lui confie des unités et accepte ses résultats. Les vecteurs
  calculés sur le processeur du téléphone doivent être quasi identiques à ceux du GPU du
  PC : comparer un même texte des deux côtés (similarité cosinus ≥ 0,999).
- [ ] **Durée.** Le nœud tient plusieurs heures écran éteint (wakelock, phantom process
  killer, Doze), et la température reste sous la limite.
- [ ] **Débit.** Combien de passages par minute sur ce téléphone, pour juger de l'intérêt.

## Sources (consultées le 16/10/2026)

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
