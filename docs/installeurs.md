# Installer AIODOS sans manipulation (étude)

> Étude du 16/10/2026. Rien n'est implémenté : ce document prépare des choix, dont
> plusieurs reviennent à l'utilisateur (cf. [§ 7](#7-questions-à-trancher)). Les prix, les
> règles des boutiques et les versions changent vite : les revérifier avant d'agir.

## En bref

- **C'est faisable sur les ordinateurs et sur Android, en partie seulement sur iOS.** Un
  installeur peut tout faire, sauf les gestes que les systèmes réservent à l'utilisateur
  (§ 5) : lancer l'installeur, autoriser l'accès au réseau local (macOS, iOS, Android 17),
  déclarer son réseau « privé » sous Windows, accepter le téléchargement des modèles.
- **Ordinateurs** : PyInstaller en mode dossier partout. Inno Setup sous Windows
  (installation pour l'utilisateur seul, sans droits administrateur), un `.dmg` signé et
  notarisé sous macOS, une AppImage sous Linux. Essai fait sur le PC de développement :
  104 Mo sans Whisper, nœud prêt en moins de 2 s, fenêtre ouverte. Flathub est à reporter
  (WebKitGTK 4.1 retiré du runtime GNOME, politique sur le code écrit par une IA).
- **Moteur d'IA : `llama-server` (llama.cpp) intégré, plutôt qu'Ollama.** Ollama calcule
  déjà avec lui depuis mai 2026. L'embarquer pèserait 1,37 à 2,8 Go, contre 30 à 50 Mo pour
  `llama-server` avec Vulkan. C'est le seul moteur présent sur les cinq systèmes, donc le
  seul qui permet le même fichier de modèle d'embedding sur toutes les machines. Ollama reste
  utilisable pour la conversation.
- **Modèles** : téléchargés au premier lancement, après accord, depuis un catalogue tenu
  par AIODOS (révision épinglée, SHA-256). EmbeddingGemma 2 (6/10/2026, Apache-2.0, 300 Mo)
  est à mesurer avant de figer le modèle par défaut.
- **Réseau local** : une règle de pare-feu propre à AIODOS (profil Privé, sous-réseau
  local), créée quand l'utilisateur active le réseau local. Jamais de modification de
  l'IPv6 ni du profil réseau : Windows 11 classe tout nouveau réseau en « Public », et
  c'est à l'utilisateur de le déclarer privé.
- **Signature : indispensable.** Smart App Control (Windows 11) bloque tout fichier non
  signé et inconnu, DLL comprises : il y en a 119 dans notre paquet d'essai. Une fois le
  dépôt public : SignPath (gratuit) ou Certum Open Source (25 à 69 € par an). macOS :
  99 $ par an, sans autre voie praticable pour un public non technique.
- **Android : une appli Kotlin**, avec le nœud Python par Chaquopy et llama.cpp dans le
  même processus. Plus de Termux, plus de *phantom process killer* (il ne vise que les
  processus enfants), calcul par tâches planifiées en charge, sans demande d'exemption de
  batterie (Google Play l'interdit dans ce cas). APK d'abord (enregistrement gratuit auprès
  de Google pour 20 appareils), Google Play ensuite.
- **iOS et iPadOS : un client**, pour converser avec les modèles des autres machines et
  chercher dans ses bibliothèques, et du calcul seulement appli ouverte et branchée. La
  découverte par diffusion n'y fonctionne pas : il faudra Bonjour ou un QR code.
- **Ordre** : préparer le code ; Windows pour les machines de l'utilisateur ; publication
  (dépôt public, signature, mises à jour) ; Android en parallèle ; macOS et Linux ; iOS
  (§ 6). Dix questions sont à trancher (§ 7).

## 1. Point de départ

### Ce qu'il faut faire aujourd'hui

| | Windows | macOS, Linux | Android | iOS / iPadOS |
|---|---|---|---|---|
| Programme | installer Python 3.12 ou plus, puis `pip install -r requirements.txt` | idem | Termux (F-Droid ou GitHub, pas Google Play), `pkg install …`, pip avec le dépôt communautaire TUR | rien |
| Moteur d'IA | installer Ollama | idem | `pkg install ollama` | — |
| Modèles | `ollama pull …` : 622 Mo pour embeddinggemma, 4 à 6 Go par modèle de conversation | idem | `ollama pull embeddinggemma` | — |
| Lancement | `lancer.bat`, où le chemin de Python est écrit en dur | `python -m aiodos` | `sh lancer-noeud.sh`, puis recopier l'adresse et son jeton dans Chrome | — |
| Réseau | accepter l'alerte du pare-feu pour Python, sur les réseaux privés seulement ; vérifier que le réseau est déclaré « privé » | — | — | — |
| Réglages du système | — | — | retirer Termux de l'optimisation de la batterie ; désactiver le *phantom process killer* (options pour les développeurs, ou `adb`) | — |

[docs/android.md](android.md) compte dix étapes, deux applications à prendre à la même
source, une compilation possible de 15 minutes et un tableau de dépannage. C'est un
parcours de développeur.

### Ce qu'un installeur doit prendre en charge

1. **Le programme**, avec son propre Python et ses dépendances : rien à installer avant.
2. **Le moteur d'IA** (Ollama ou llama.cpp), démarré et arrêté par AIODOS.
3. **Les modèles**, téléchargés au premier lancement **avec l'accord** de l'utilisateur :
   ils pèsent de 0,3 à 6,5 Go chacun.
4. **L'accès au réseau local** : une règle de pare-feu propre à AIODOS, limitée aux réseaux
   privés. **Jamais** de modification de l'IPv6, du profil réseau ni d'aucun autre réglage
   réseau du système.
5. **Les mises à jour.**
6. **La signature du code**, sans laquelle le système avertit, voire bloque.

### Mesures sur le PC de développement (16/10/2026)

Windows 11 25H2, Python 3.14.4, pywebview 6.2.1, Ollama 0.40.5. Les essais
d'empaquetage ont été faits dans un dossier temporaire, hors du dépôt.

| Mesure | Résultat |
|---|---|
| AIODOS empaqueté par PyInstaller 6.20 (un dossier, sans console), **avec** Whisper | 281 Mo sur disque, environ 86 Mo compressé (LZMA, l'algorithme d'Inno Setup) ; construit en 154 s, sans erreur |
| Le même **sans** Whisper (faster-whisper, CTranslate2, onnxruntime, PyAV) | 104 Mo, environ 39 Mo compressé ; 70 s |
| Démarrage du paquet | nœud prêt en 1,3 à 1,7 s (à chaud), 89 Mo de mémoire ; la fenêtre s'ouvre (processus WebView2, titre « AIODOS »), l'interface est servie |
| Binaires du paquet complet (`.exe`, `.dll`, `.pyd`) | 200, dont **119 non signés** (le lanceur de PyInstaller et la plupart des modules compilés venus de PyPI) ; les autres sont signés par Microsoft (54), la Python Software Foundation (26) et Intel (1) |
| Ollama 0.40.5 installé | **2,8 Go** : CUDA 12 (1,1 Go), ROCm 7.1 (979 Mo), CUDA 13 (635 Mo), Vulkan (40 Mo), processeur (environ 2 Mo par variante de jeu d'instructions). Installé pour l'utilisateur seul, dans `%LOCALAPPDATA%\Programs\Ollama`, par un installeur Inno Setup ; tous ses binaires sont signés « Ollama Inc. » |
| Comment Ollama calcule | il lance **`llama-server.exe` de llama.cpp** sur le fichier GGUF du modèle (`--model …\blobs\sha256-0800cbac… --embedding …`, journal `server.log`). Les 8 modèles utilisés ces derniers jours sont passés par là. Depuis sa version 0.30 (13/05/2026), Ollama compile une version de llama.cpp qu'il épingle et modifie |
| Ce `llama-server` d'Ollama lancé par nous sur le même fichier (processeur), contre Ollama (GPU) | **cosinus 0,99998 à 0,99999**, écart maximal 8·10⁻⁴ : le même ordre que téléphone contre PC (docs/android.md) |
| Format du fichier d'embeddinggemma d'Ollama | propre à Ollama : architecture `gemma3`, tenseurs `dense.0` et `dense.1`, BF16. Ollama a écrit un convertisseur vers le format de llama.cpp (`gemma-embedding`) : un llama.cpp officiel ne le lit sans doute pas tel quel (non essayé) |
| Ce qu'Ollama fait sur le réseau (application de bureau) | recherche une mise à jour **toutes les heures** et la télécharge seul (0.40.4 puis 0.40.5 le 15/10), pour l'installer au redémarrage ; appelle `ollama.com/api/me` (compte) |
| Registre d'Ollama | manifeste et fichier GGUF téléchargeables sans Ollama et sans compte, avec reprise (requête partielle : réponse 206) |
| Modèle Whisper « small » | 464 Mo, téléchargé depuis Hugging Face au premier fichier audio, sans que l'utilisateur le sache |
| Pare-feu | réseau Ethernet classé **« Public »** ; les règles créées par l'alerte de Windows autorisent `python.exe` en TCP et en UDP **sur le profil Public** : exactement ce qu'un installeur doit éviter (§ 2.5) |

### Ce qu'il faut changer dans le code avant tout installeur

Ces changements sont petits, mais aucun installeur ne marche sans eux.

- **Les données à leur place.** `paths.data_dir()` met tout dans `data/`, à côté du code.
  Une fois installé, le code est dans un dossier en lecture seule ou remplacé à chaque
  mise à jour (`Program Files`, `/Applications`, une AppImage, un Flatpak). Les données
  doivent aller dans le dossier de l'utilisateur : `%LOCALAPPDATA%\AIODOS`,
  `~/Library/Application Support/AIODOS`, `$XDG_DATA_HOME/aiodos` (le paquet
  `platformdirs` les connaît). Garder `AIODOS_DATA_DIR` et `--data-dir`, et reprendre un
  `data/` existant comme on l'a fait pour `coeur.db`.
- **Les messages d'erreur** disent « voir data/logs » (`aiodos/__main__.py`) : donner le
  vrai chemin, ou un bouton « Ouvrir le dossier des journaux ».
- **Le moteur d'IA piloté par AIODOS** : aujourd'hui, AIODOS suppose qu'Ollama tourne déjà
  (après un redémarrage, il faut relancer « ollama app.exe » à la main). Il faut un module
  qui trouve le moteur, le démarre, surveille son état et l'arrête (§ 2.3).
- **Un assistant de premier lancement** : AIODOS ne télécharge aucun modèle aujourd'hui
  (aucun appel à `/api/pull`). Il faut proposer les modèles, afficher leur taille et leur
  licence, et télécharger après accord (§ 2.4).
- **L'identité du modèle d'embedding.** AIODOS compare aujourd'hui le *digest* que donne
  Ollama (`85462619…` pour embeddinggemma), qui est celui du **manifeste** du registre
  d'Ollama. Le fichier réellement calculé a une autre empreinte (`0800cbac…`, SHA-256 du
  GGUF), et ce fichier est au format propre à Ollama. Une machine sans Ollama (téléphone,
  iPhone, PC avec llama.cpp intégré) n'a ni l'un ni l'autre. Il faut donc identifier le
  modèle par **l'empreinte d'un fichier GGUF qu'AIODOS distribue lui-même**, identique
  sur toutes les machines, plus la « recette » du calcul : pooling, normalisation, absence
  de préfixe, version de llama.cpp (§ 2.3).
- **Whisper en option** : 177 Mo de bibliothèques et 464 Mo de modèle. Le proposer comme
  un module à télécharger, avec accord, plutôt que de l'imposer à tous.
- **Le nom de l'application pour Windows** (`SetCurrentProcessExplicitAppUserModelID
  ("Arkane.Aiodos")`) doit être le même que celui des raccourcis créés par l'installeur,
  sans quoi la barre des tâches sépare l'icône épinglée de la fenêtre ouverte.

## 2. Ordinateurs : Windows, macOS, Linux

### 2.1 Empaqueter Python 3.14, FastAPI et pywebview

| Outil (version du 16/10/2026) | Python 3.14 | pywebview | Ce qu'il produit | Points faibles |
|---|---|---|---|---|
| **PyInstaller** 6.22.3 | depuis la 6.15 (août 2025) | hooks fournis (`webview`, `clr_loader`, `uvicorn`) | un dossier ou un fichier unique ; un `.app` sous macOS, signé avec runtime durci si l'on fournit une identité | notarisation à faire soi-même ; construction sur chaque système, et sur la plus ancienne distribution Linux visée ; faux positifs antivirus (recompiler son lanceur aide) ; la 6.22.1 corrige une faille du mode fichier unique : viser au moins la 6.22.3 |
| Briefcase 0.4.6 | depuis la 0.3.25 | **aucun modèle de projet pywebview** ; Toga et pywebview ne cohabitent pas | MSI ; `.app`, `.dmg`, `.pkg` signés et notarisés ; deb, rpm, Arch, Flatpak ; Android et iOS | AppImage « fortement déconseillée » ; paquets Linux liés au Python de la distribution (3.12 sur Ubuntu 24.04) |
| Nuitka 4.3 | officiel depuis la 4.2 (27/08/2026) | greffon intégré | dossier, fichier unique, `.app`, DMG | compilation en C longue ; binaires « souvent » signalés par les antivirus, avec une offre payante pour remède (Nuitka Commercial, dès 250 € par an) ; licence passée à l'AGPLv3, avec une exception pour les programmes produits (sans effet pour AIODOS) |
| cx_Freeze 8.7.2 | depuis la 8.5 | — | MSI, DMG, AppImage, deb, rpm | moins répandu |
| PyApp 0.30.0 | oui | — | un lanceur en Rust qui télécharge Python au premier lancement | ni installeur, ni signature |

À savoir :
- **Mesure** (§ 1) : PyInstaller 6.20 avec Python 3.14.4 et pywebview 6.2.1 donne un
  paquet de 104 Mo sans Whisper, qui démarre et ouvre sa fenêtre. Seule la version (6.20,
  antérieure au correctif de sécurité) est à relever.
- **Windows** : pywebview passe par pythonnet, qui ne gère Python 3.14 que depuis sa
  version 3.1.0 (23/05/2026 ; 3.2.1 installée ici), et par le .NET Framework 4.6.2 ou plus,
  intégré à Windows 10 1903 et à Windows 11 (pywebview 6.2 sait s'en passer). Le moteur
  **WebView2** est préinstallé sur Windows 11 et a été installé par Microsoft sur les
  Windows 10 à jour. Microsoft conseille quand même de vérifier sa présence et, au besoin,
  de lancer son petit installeur (2 Mo) en silence ; jamais la version « fixe », qui pèse
  plus de 250 Mo.
- **Exclure les moteurs inutiles** de pywebview : PyInstaller embarquerait sinon PyQt alors
  que la fenêtre utilise WebView2.
- **macOS** : PyInstaller doit être au moins en 6.13, qui donne à chaque appli un
  identifiant de binaire (UUID) unique. Avant, toutes les applis PyInstaller partageaient
  celui du lanceur, ce qui faussait l'invite « Réseau local » (§ 2.5). Un paquet universel
  (Intel et Apple) est impossible : numpy et cryptography n'ont pas de roue universelle,
  et onnxruntime (Whisper) n'existe que pour les puces Apple.
- **Linux** : le moteur GTK de pywebview utilise GTK 3 et WebKitGTK 4.1 **du système**
  (présents dans Ubuntu 24.04 et plus récent, Debian 12 et 13). Le moteur Qt ajouterait
  une roue de 116 à 132 Mo.

**Recommandation : PyInstaller, en mode dossier**, en version 6.22.3 ou plus récente,
construit sur chaque système par l'intégration continue de GitHub. Nuitka reste le plan B.
Le mode dossier est aussi celui qu'exigent Velopack et une règle de pare-feu stable : le
chemin du programme ne change pas d'un lancement à l'autre.

### 2.2 L'installeur, système par système

**Windows.**

| | Inno Setup 7.1 | Velopack | MSIX | MSI (WiX 7) |
|---|---|---|---|---|
| Par utilisateur, sans droits administrateur | oui (`PrivilegesRequired=lowest`) | oui (`%LocalAppData%`) | oui | possible |
| Une étape élevée (pare-feu) | oui : une ligne `[Run]` avec `Verb: runas` | à faire par l'appli | règles déclarées dans le manifeste, sans « sous-réseau local » | oui |
| WebView2 s'il manque | par un script | intégré (`--framework webview2`) | — | par un script |
| Mises à jour | à part (§ 2.7) | intégrées, partielles, **sans signature indépendante** | fichier `.appinstaller` ou Microsoft Store | à part |
| Limites | licence commerciale demandée aux usages commerciaux (pas le cas d'AIODOS) | outil de construction en .NET ; service hébergé de l'éditeur par défaut | les fichiers écrits dans AppData vont dans un dossier privé, **effacé à la désinstallation**, sauf réglage particulier ; signature obligatoire ; lancement de processus enfants à vérifier | WiX 7 soumis à une redevance de maintenance si le projet génère des revenus |

**Recommandation pour Windows : Inno Setup 7**, comme Ollama.
- Installation pour l'utilisateur seul, dans `%LOCALAPPDATA%\Programs\AIODOS`, sans droits
  administrateur.
- Raccourcis portant le même identifiant d'application que le programme (§ 1).
- WebView2 vérifié, installé en silence s'il manque.
- La règle de pare-feu est créée **au moment où l'utilisateur active le réseau local**,
  par un petit programme élevé (une fenêtre de contrôle de compte), avant la première
  écoute (§ 2.5). AIODOS n'écoute qu'une fois le réseau activé : ceux qui ne s'en servent
  pas ne voient jamais de demande de droits. La désinstallation retire la règle.
- Plus tard : le **Microsoft Store** en MSIX, canal gratuit et signé, une fois réglée la
  redirection des données ; un manifeste **winget**.

**macOS.**
- Un **`.dmg`** contenant le `.app`, à glisser dans Applications : le parcours habituel.
  Un `.pkg` n'est utile que pour installer des fichiers hors du `.app`, ce dont AIODOS n'a
  pas besoin.
- Signature « Developer ID » de chaque binaire, runtime durci, entitlements à minimiser
  (Briefcase met par défaut `allow-unsigned-executable-memory` et
  `disable-library-validation`), notarisation par `notarytool`, ticket agrafé au `.dmg`.
- Dans l'`Info.plist` : `NSLocalNetworkUsageDescription` (§ 2.5).
- **Puces Apple seulement** (arm64) : macOS 27 ne fonctionne plus sur les Mac Intel.

**Linux.**
- Une **AppImage** avec le runtime de type 2, lié statiquement : il n'exige plus
  `libfuse2`, absent des distributions récentes. Elle est construite sur la plus ancienne
  base visée (Ubuntu 24.04 ou Debian 12) et utilise GTK 3 et WebKitGTK 4.1 du système. À
  essayer sur Ubuntu 24.04 et 26.04, Debian 12 et 13, Fedora.
- Ensuite, des paquets `.deb` et `.rpm` qui déclarent ces dépendances.
- **Flatpak et Flathub sont à reporter**, pour deux raisons :
  - le runtime GNOME a **retiré WebKitGTK 4.1** (changement intégré le 28/09/2026 ; le
    runtime GNOME 51, qui l'a encore, est maintenu jusqu'en septembre 2027), et pywebview
    ne sait pas utiliser WebKitGTK 6.0 ;
  - Flathub exige de **déclarer tout code écrit par une IA**, interdit tout contenu généré
    par une IA dans le manifeste et toute soumission par un agent, et peut refuser sans
    examen selon l'ampleur de ce code. AIODOS est en grande partie écrit avec une IA.

### 2.3 Le moteur d'IA : llama.cpp intégré, Ollama en option

Quatre façons de faire (tailles pour Windows, versions du 16/10/2026) :

| | (a) Ollama installé à part | (b) Ollama installé en silence par AIODOS | (c) archive d'Ollama embarquée | (d) `llama-server` embarqué |
|---|---|---|---|---|
| Poids | 0, mais 1,48 Go à installer soi-même | installeur de 1,48 Go, 2,8 Go une fois installé | 1,37 Go | **30 à 50 Mo** (Vulkan, avec les variantes pour le processeur) ; CUDA en option, environ 550 Mo de plus |
| Geste de l'utilisateur | installer Ollama | aucun | aucun | aucun |
| Version du moteur | celle de l'utilisateur, mise à jour seule | celle d'Ollama, mise à jour seule | choisie par AIODOS | choisie par AIODOS |
| Ce qui vient avec | icône, lancement à l'ouverture de session, mises à jour automatiques sans interrupteur documenté, modèles « cloud » (désactivables par `OLLAMA_NO_CLOUD=1`), télémétrie limitée sans interrupteur documenté | idem ; et Ollama reste après la désinstallation d'AIODOS | les migrations de format d'Ollama | rien |
| Android, iOS | non (Termux seulement) | non | non | **oui** : binaires officiels (Android arm64 72 Mo, iOS 64 Mo) |
| Même fichier de modèle sur toutes les machines | non | non | non | **oui** |

**Ce qu'il faut savoir d'Ollama en 2026** :
- licence MIT ; depuis la 0.30 (13/05/2026), il calcule avec `llama-server` (llama.cpp)
  qu'il compile lui-même ; la 0.40.2 (8/10/2026) convertit en arrière-plan les modèles de
  son ancien moteur ; sur Mac, il utilise MLX par défaut depuis la 0.40.0, si bien qu'un
  même nom de modèle peut désigner des fichiers différents selon la machine ;
- l'installation silencieuse est possible et documentée : `OllamaSetup.exe` est un
  installeur Inno Setup par utilisateur, sans droits administrateur, et le script officiel
  `install.ps1` vérifie sa signature puis lance `/VERYSILENT /NORESTART /SUPPRESSMSGBOXES`.
  Mais l'application démarre quand même, s'inscrit au démarrage de la session et
  télécharge ses mises à jour toute seule ;
- son archive autonome (`ollama-windows-amd64.zip`, 1,37 Go) est prévue par la
  documentation « pour intégrer Ollama dans des applications » ;
- deux Ollama sur une machine ne se coordonnent pas pour le GPU, et partager le dossier
  de modèles expose à l'élagage de fichiers par l'autre instance ; le désinstalleur
  d'Ollama arrête **tous** les `llama-server.exe` : un moteur embarqué par AIODOS doit
  porter un autre nom.

**Ce qu'il faut savoir de llama.cpp** :
- licence MIT ; plusieurs versions par jour (b11696 le 16/10/2026), avec des binaires
  officiels pour chaque système :

  | Système | Paquets (taille) |
  |---|---|
  | Windows x64 | processeur 18,7 Mo ; **Vulkan 30,7 Mo** ; CUDA 12.4 254 Mo (+ bibliothèques CUDA 373 Mo) ; CUDA 13.4 148 Mo (+ 404 Mo) ; ROCm 247 Mo |
  | Windows ARM | processeur 11,9 Mo ; Vulkan 23,7 Mo ; OpenCL pour Adreno 12,7 Mo |
  | macOS (puces M) | 11,7 Mo, Metal compris |
  | Linux x64 | processeur 17,1 Mo ; Vulkan 29,1 Mo ; CUDA 13.4 147 Mo (+ 420 Mo) |
  | Android arm64 | 72,3 Mo |
  | iOS | 63,7 Mo (XCFramework) |

- sous Windows et Linux, `llama-server` charge ses backends à la demande, et le paquet
  « processeur » contient toutes les variantes de jeu d'instructions (un vieux PC sans AVX
  est servi). Les paquets GPU ne contiendraient que la bibliothèque de leur backend, à
  ajouter au paquet « processeur » (non vérifié) : d'où 30 à 50 Mo pour Vulkan. Si CUDA et
  Vulkan sont présents ensemble, la même carte apparaît deux fois : n'en livrer qu'un, ou
  choisir avec `--device` ;
- **mode routeur** (décembre 2025) : un seul `llama-server`, démarré sans modèle, charge à
  la demande celui que nomme chaque requête (`--models-dir` ou un fichier de préréglages
  `.ini`), en garde au plus `--models-max` en mémoire, et libère la mémoire après
  inactivité (`--sleep-idle-seconds`) ; chaque modèle tourne dans son propre processus ;
- embeddings : `--embeddings --pooling mean`, normalisation L2 par défaut ;
  `--list-devices` donne la mémoire libre de chaque carte, et `--fit` (actif par défaut)
  adapte les réglages à cette mémoire ;
- les bibliothèques CUDA de NVIDIA sont redistribuables sous conditions (contrat de
  licence de NVIDIA). Pour un projet GPL-3.0, le plus prudent est un **paquet séparé**,
  téléchargé à la demande sous la licence de NVIDIA (ce n'est pas un avis juridique) ;
- Vulkan contre CUDA : NVIDIA juge Vulkan « très compétitif » avec ses pilotes récents
  (2025), mais une mesure de 2024 donnait CUDA 2,6 fois plus rapide sur la lecture du
  texte, justement ce que font les embeddings. **À mesurer sur nos machines** avant de
  choisir le paquet par défaut (le `llama-server` d'Ollama ne s'y prête pas : lancé seul,
  il ne trouve pas ses backends GPU) ;
- les liaisons Python (`llama-cpp-python`, 0.3.36) sont maintenues, mais il faut une
  version par backend, et un plantage du moteur ferait tomber AIODOS : un processus à
  part est préférable sur ordinateur.

**Ce que font les applications comparables** : Jan intègre llama.cpp (en mode routeur
depuis mai 2026) et dit si un modèle tient en mémoire avant de le télécharger ;
AnythingLLM a **remplacé l'Ollama qu'il embarquait** par un serveur fondé sur llama.cpp
(v1.18.0) ; Msty gère un Ollama embarqué et propose llama.cpp ; l'extension Ollama
d'Alpaca (Flatpak) est restée à la version 0.13.2.

**Recommandation : (d), `llama-server` embarqué et piloté par AIODOS, partout.**
- C'est le seul moteur qui existe à l'identique sur les cinq systèmes, donc le seul qui
  permet un **même fichier de modèle d'embedding** sur toutes les machines.
- Embarquer Ollama revient aujourd'hui à embarquer `llama-server`, plus 1,37 Go et des
  conversions de format qui changent l'identité des modèles.
- **Ollama reste proposé en option** (« utiliser mon Ollama ») pour la **conversation**
  seulement : on réutilise ainsi les modèles déjà téléchargés (50 Go sur le PC de
  développement). Jamais pour les embeddings partagés entre machines.
- Sous Windows : livrer le paquet **Vulkan** (30 à 50 Mo) ; proposer CUDA seulement si une
  carte NVIDIA est détectée, que la mesure le justifie et que l'utilisateur accepte.
  Renommer l'exécutable (`aiodos-moteur.exe`, par exemple), le faire écouter sur 127.0.0.1 avec
  un port libre, l'arrêter avec AIODOS.
- Dans le code, tout passe déjà par `aiodos/services/ollama.py` (7 points d'accès :
  version, liste, description, modèles chargés, génération, conversation, embeddings).
  Il devient un module « moteur » à deux pilotes : `llama-server` (API compatible OpenAI :
  `/v1/chat/completions`, `/v1/embeddings`, `/models`) et Ollama (l'actuel).
- **Empreinte du modèle d'embedding** = SHA-256 du fichier GGUF (et de son projecteur,
  s'il en a un) + la recette (pooling `mean`, normalisation L2, sans préfixe) + la version
  de llama.cpp. Les machines la comparent avant d'accepter du travail, comme aujourd'hui
  avec le *digest* d'Ollama.

### 2.4 Les modèles au premier lancement, avec accord

**Un catalogue tenu par AIODOS**, versionné dans le code : pour chaque modèle, sa source
(dépôt Hugging Face et **révision épinglée**, ou registre d'Ollama), son fichier, sa
taille, son SHA-256, sa licence, son rôle (embedding, conversation, vision) et la
mémoire qu'il demande. Épingler la révision est indispensable : le GGUF d'embeddinggemma
de ggml-org a été **remplacé sous le même nom** le 29/04/2026.

**L'assistant du premier lancement** :
1. mesure la mémoire vive, la mémoire de la carte graphique (`llama-server
   --list-devices`) et l'espace disque libre ;
2. propose, avec leur taille, leur licence et leur source :
   - le modèle d'embedding, indispensable aux bibliothèques (300 à 600 Mo) ;
   - un modèle de conversation qui tient dans la carte graphique, ou, à défaut, « utiliser
     les modèles de mes autres machines » (déjà possible : modèles prêtés) au lieu d'en
     télécharger un ;
   - en option : la vision (6 Go), Whisper (464 Mo de modèle et 177 Mo de bibliothèques) ;
   - si Ollama est installé : réutiliser ses modèles de conversation, sans rien télécharger ;
3. ne télécharge qu'**après accord**, en tâche de fond, avec reprise (le registre d'Ollama
   accepte les requêtes partielles, vérifié ; Hugging Face, non vérifié ici), et vérifie le
   SHA-256 avant tout usage.

Tailles des modèles actuels (PC de développement) :

| Modèle | Rôle | Taille | Licence |
|---|---|---|---|
| embeddinggemma (BF16) | embedding | 0,62 Go | conditions Gemma |
| embeddinggemma-2:270m (Q8_0, registre d'Ollama) | embedding | 0,30 Go | Apache-2.0 |
| mistral:7b | conversation | 4,37 Go | Apache-2.0 |
| qwen2.5-coder:7b | conversation (défaut) | 4,68 Go | Apache-2.0 |
| qwen2.5vl:7b | vision | 5,97 Go | Apache-2.0 |
| qwen3.5:9b | conversation | 6,55 Go | Apache-2.0 |

**Licences** :
- **embeddinggemma** (conditions Gemma, modifiées le 1/04/2026) : qui le redistribue doit
  transmettre les restrictions d'usage, une copie de l'accord et un fichier « Notice ».
  Si des machines AIODOS s'échangent le fichier, c'est de la redistribution. Sur Hugging
  Face, le dépôt de Google exige un compte et une acceptation ; les GGUF de ggml-org, non,
  mais la licence s'applique quand même.
- **EmbeddingGemma 2** (Google, 6/10/2026) : **Apache-2.0**, sans acceptation ; 768
  dimensions ; versions texte seul de 270 M de paramètres (environ 300 Mo en Q8_0) ;
  GGUF officiel de ggml-org ; pris en charge par llama.cpp depuis le 6/10/2026. Ses
  vecteurs ne sont pas ceux de la version 1.
- Qwen2.5-Coder-7B, Qwen2.5-VL-7B, Qwen3.5 et Mistral-7B v0.3 sont sous Apache-2.0 ;
  attention, Qwen2.5-3B et Qwen2.5-72B ont des licences propres.

**Décision à prendre avant le premier installeur** : passer à un fichier distribué par
AIODOS changera de toute façon l'empreinte, donc imposera une réindexation (le type de
travail `reindex` existe). C'est le bon moment pour adopter **EmbeddingGemma 2**, si
`tools/embedding_bench.py` confirme en français ce que Google annonce : licence libre,
fichier deux fois plus léger. Son support dans llama.cpp n'a que dix jours : mesurer aussi
sur processeur, Vulkan, CUDA et Android.

**Une source de plus, sans Internet** : une machine peut recevoir le fichier du modèle
d'une machine appairée, par le canal chiffré existant. Les octets sont identiques par
construction, et un téléphone en 4G n'a rien à télécharger. Sous licence Apache-2.0, cette
copie ne pose aucun problème ; sous les conditions Gemma, c'est une redistribution.

### 2.5 Le pare-feu : une règle pour les réseaux privés, rien d'autre

**Ce qu'il faut ouvrir** : TCP 47701 en entrée (échanges chiffrés entre nœuds) et UDP
47700 en entrée (diffusions des autres machines). L'API locale, sur 127.0.0.1, ne
déclenche pas d'alerte (Project Zero, 2021 ; Microsoft ne le documente pas). Les besoins
du compagnon `aiodos-net` (Internet) seront à mesurer.

**Windows.**
- Sans règle, la première écoute déclenche l'alerte du pare-feu. Si l'utilisateur la
  ferme ou refuse, ou s'il n'est pas administrateur (quel que soit son choix), Windows crée
  des règles de **blocage**. Elles l'emportent sur toute autorisation : une règle posée
  ensuite par AIODOS ne suffirait plus. Microsoft recommande donc de créer les règles
  **avant le premier lancement**, surtout si des comptes non administrateurs utilisent la
  machine. Sur le PC de développement, c'est l'alerte qui a créé des autorisations, sur le
  profil Public (§ 1).
- AIODOS crée donc les règles dans **une seule étape élevée** (une fenêtre de contrôle de
  compte), au moment où l'utilisateur active le réseau local : le nœud n'écoute qu'après
  (§ 2.2). Ce sont deux règles limitées au programme, au profil Privé et au sous-réseau
  local, que la désinstallation retire (`Remove-NetFirewallRule -Group AIODOS`) :

  ```powershell
  New-NetFirewallRule -Group AIODOS -DisplayName "AIODOS (TCP 47701)" -Direction Inbound -Action Allow -Program "<dossier>\AIODOS.exe" -Protocol TCP -LocalPort 47701 -Profile Private -RemoteAddress LocalSubnet
  New-NetFirewallRule -Group AIODOS -DisplayName "AIODOS (UDP 47700)" -Direction Inbound -Action Allow -Program "<dossier>\AIODOS.exe" -Protocol UDP -LocalPort 47700 -Profile Private -RemoteAddress LocalSubnet
  ```

  Le chemin complet du programme est obligatoire. Cela exclut un exécutable « en un seul
  fichier » qui s'extrait à un endroit différent à chaque lancement (c'est le cas par
  défaut avec Nuitka).
- Un paquet MSIX peut déclarer ses règles dans son manifeste (`desktop2:FirewallRules`),
  mais sans restriction d'adresse : pas de « sous-réseau local ». Qu'elles s'appliquent
  sans élévation est probable, mais Microsoft ne l'écrit pas.
- **Le profil réseau reste l'affaire de l'utilisateur.** Windows 11 classe tout nouveau
  réseau en « Public », et Microsoft le recommande. Une règle « Privé » y est inactive.
  AIODOS **lit** le profil (`Get-NetConnectionProfile`, ou `INetwork::GetCategory`), le
  dit dans la vue « Réseau local » et montre où le changer : Paramètres, Réseau et
  Internet, le réseau, Type de profil réseau. Il ne le change jamais lui-même : c'est la
  consigne du projet, et `SetCategory` exige de toute façon des droits administrateur.
- Les réponses à une diffusion émise par AIODOS passent sans règle (pendant 3 s). Recevoir
  les diffusions des autres machines exige la règle UDP.

**macOS.**
- Aucune règle à poser. Si le pare-feu applicatif est activé (Apple ne dit pas s'il l'est
  par défaut), il demande « Autoriser » ou « Refuser » à la première écoute.
- La **confidentialité « Réseau local »** (macOS 15 et plus récent) s'applique aussi hors
  App Store. Envoyer ou recevoir une **diffusion** (la découverte d'AIODOS), se connecter
  à une machine du réseau local, utiliser Bonjour : tout cela exige l'accord de
  l'utilisateur, demandé par une invite au premier essai. La clé
  `NSLocalNetworkUsageDescription` est obligatoire. Écouter sur le port 47701 n'en a pas
  besoin, et les processus enfants sont rattachés à l'appli.
  - Aucune API ne permet de tester l'accès ou de provoquer l'invite. La première
    tentative peut échouer avant la réponse de l'utilisateur : il faut réessayer.
  - Tant que l'accès n'est pas accordé, l'envoi échoue aussitôt avec `EHOSTUNREACH`
    (en Python : `OSError: [Errno 65] No route to host`), y compris pendant que l'invite
    est affichée. AIODOS doit réessayer, et dire où se trouve le réglage : Réglages
    Système, Confidentialité et sécurité, Réseau local.
  - Revenir sur une décision n'est possible que depuis macOS 27.2.
  - Un programme lancé depuis le Terminal est exempté : tout marche en développement, et
    échoue une fois l'appli ouverte depuis le Finder. Les essais doivent donc se faire
    avec l'appli empaquetée et signée. Apple recommande une vraie identité de signature :
    une signature « ad hoc » rend le suivi peu fiable, et fait revenir l'invite du pare-feu
    à chaque nouvelle version.
  - Apple déconseille la diffusion vers `255.255.255.255`, dont le routage varie selon les
    versions : diffuser plutôt sur l'adresse de diffusion de chaque interface.

**Linux.**
- Ubuntu et Linux Mint : ufw installé mais inactif. Debian : aucun pare-feu actif.
  Fedora Workstation : firewalld laisse entrer les ports 1025 et plus, en TCP comme en UDP.
  openSUSE, en revanche, active firewalld par défaut (zone « public ») : les ports
  d'AIODOS y sont fermés.
- Un programme ne doit pas toucher au pare-feu, et un Flatpak ne le peut pas. AIODOS
  documente les commandes pour ceux qui en ont un actif : `sudo ufw allow 47700/udp` et
  `sudo ufw allow 47701/tcp`, ou `sudo firewall-cmd --permanent --add-port=47700/udp`,
  `--add-port=47701/tcp`, puis `sudo firewall-cmd --reload`.
- Un paquet `.deb` peut déposer un profil ufw, et un `.rpm` un service firewalld : ils
  n'ouvrent rien tant que l'administrateur ne les active pas.

**Ce qu'AIODOS ne fait jamais** : modifier l'IPv6, le profil réseau, la découverte ou le
partage du réseau, ni aucune règle de pare-feu qui ne lui appartient pas.

### 2.6 Rester actif pour prêter sa puissance

Aujourd'hui, fermer la fenêtre arrête le nœud (`aiodos/ui/window.py`). Pour prêter sa
puissance « quand le PC n'est pas utilisé », il faut que le nœud survive à la fenêtre :

- une **icône dans la zone de notification** (rouvrir, mettre en pause, quitter).
  pywebview ne semble pas en fournir : il faudra sans doute une bibliothèque de plus, comme
  `pystray`, qui doit tourner sur le fil principal sous macOS, comme pywebview (à
  vérifier) ;
- une option **« Lancer AIODOS à l'ouverture de session »**, **décochée par défaut** et
  proposée quand l'utilisateur active « Prêter ma puissance » :
  - Windows : raccourci dans le dossier Démarrage ou clé `Run` de l'utilisateur, sans droits
    administrateur ; visible et désactivable dans Paramètres, Applications, Démarrage ;
  - macOS : `SMAppService` (macOS 13 et plus récent) ; l'élément apparaît dans les
    réglages d'ouverture de session (Réglages Système, Général), où l'utilisateur peut le
    couper ;
  - Linux : fichier `.desktop` dans `~/.config/autostart` ; un Flatpak passe par le portail
    « Background », qui demande la permission. Sous GNOME tel quel (Fedora), les icônes de
    zone de notification ne s'affichent pas ; Ubuntu les affiche.

### 2.7 Les mises à jour

Trois exigences découlent des principes du projet :
- la vérification est **proposée** au premier lancement et désactivable : rien ne quitte
  la machine sans accord ;
- aucun service obligatoire : les fichiers sont publiés sur GitHub Releases, éventuellement
  sur des miroirs ;
- l'appli vérifie elle-même une **signature** des mises à jour, indépendante de
  l'hébergeur.

| Outil (version) | Systèmes | Pour Python | Signature vérifiée par l'appli | Remarques |
|---|---|---|---|---|
| Velopack (1.2.161, 29/09/2026) | Windows, macOS, Linux | SDK officiel (PyPI, MIT) | **non** : une simple empreinte SHA, qui selon son mainteneur ne protège ni d'une interception ni d'un hébergeur compromis | mises à jour partielles (deltas) ; PyInstaller en mode « dossier » obligatoire ; outil de construction qui demande .NET ; sans source indiquée, il interroge le service hébergé de l'éditeur : toujours en donner une |
| Sparkle 2 (2.10.0) | macOS | pas de liaison : framework à embarquer | oui (EdDSA), flux signé possible | demande l'accord au deuxième lancement ; désactivable |
| WinSparkle (0.9.4) | Windows | liaison Python ancienne, sans doute abandonnée | oui (Ed25519) | |
| tufup (0.10.0) | tous | oui | oui (The Update Framework) | un seul mainteneur ; dépend d'une version de TUF dont une faille n'est corrigée qu'en version 7, migration non prévue : risqué |
| PyUpdater | — | — | — | archivé depuis 2022 |
| MSIX et fichier `.appinstaller` | Windows 10 2004 et plus, Windows 11 | — | oui (certificat) | mise à jour automatique, désactivable par l'utilisateur |
| winget | Windows | — | à la publication : empreinte, antivirus, essai d'installation | mises à jour à la main (`winget upgrade`), aucun service prévu |
| Flathub | Linux | — | dépôt signé | publication en 1 à 2 heures ; politique sur l'IA (§ 2.2) |
| AppImageUpdate | Linux | — | GPG facultative | toujours en version alpha (octobre 2025) |

**Ce que font les autres** :
- **Ollama** interroge son serveur toutes les heures. Désactiver la mise à jour
  automatique n'arrête que le téléchargement : la vérification continue.
- **Jan** publie un `latest.json` sur GitHub, avec une signature minisign (Ed25519) dont la
  clé publique est dans l'appli. La vérification se désactive dans ses réglages.
- **LM Studio** ne documente aucun moyen de désactiver la sienne.

**Recommandation : un petit programme de mise à jour à la manière de Jan.**
1. À chaque version, publier sur GitHub Releases un `latest.json` (version, URL et SHA-256
   de chaque installeur) et sa signature Ed25519 (format minisign).
2. AIODOS embarque la clé publique, et vérifie la signature avec le paquet `cryptography`,
   déjà présent : aucune dépendance de plus.
3. La vérification est proposée au premier lancement, puis faite au plus une fois par
   semaine. Après accord, AIODOS télécharge l'installeur, vérifie l'empreinte, le lance
   en silence (Inno Setup : `/VERYSILENT`) et redémarre.
4. Sur macOS, remplacer l'appli de la même façon, ou passer par Sparkle 2. Sur Linux, laisser
   faire le format choisi (§ 2.2).
5. Velopack reste une option pour les mises à jour partielles, à condition d'y ajouter la
   même vérification de signature et de toujours lui donner une source explicite.

La clé privée de publication devient le bien le plus précieux du projet : qui la détient
et où elle est rangée est à décider (Q4).

### 2.8 Signer le code : obligatoire sous Windows 11 et macOS

**Windows.**
- **SmartScreen** : un installeur non signé affiche « Windows a protégé votre PC » ; il
  faut cliquer sur « Informations complémentaires », puis « Exécuter quand même ». Signé
  mais nouveau, il avertit encore, avec le nom de l'éditeur, le temps d'acquérir une
  réputation (des semaines, des centaines d'installations, sans seuil connu). Un
  certificat EV ne donne plus de réputation immédiate depuis 2024.
- **Smart App Control** (Windows 11) : il bloque **tout fichier non signé** que le service
  de réputation de Microsoft ne connaît pas, **sans exception possible par application**,
  et il vérifie **chaque DLL chargée**, pas seulement l'exécutable. Depuis avril 2026, on
  peut le réactiver sans réinstaller Windows. Un installeur signé qui dépose des fichiers
  non signés ne suffit pas : il faut signer les 119 binaires non signés du paquet
  (§ 1). Les modules très répandus (numpy…) sont peut-être connus de Microsoft : non
  vérifié, à essayer sur une machine où Smart App Control est actif.
- Règles communes : clé privée sur matériel (jeton ou HSM) depuis juin 2023 ; durée
  maximale de **460 jours** pour les certificats émis depuis le 1/03/2026 (vote CSC-31 du
  CA/Browser Forum) ; horodatage de chaque signature.

| Voie | Coût | Conditions | Éditeur affiché |
|---|---|---|---|
| **SignPath Foundation** | gratuit | projet libre (licence OSI), sans composant propriétaire ; binaires construits automatiquement et de façon vérifiable depuis le dépôt ; approbation de chaque version ; double authentification ; page « politique de signature » ; désinstalleur ; politique de confidentialité, affichée à l'installation et désactivable, pour tout envoi de données non demandé (la découverte du réseau local est concernée) ; dépôt public de fait | « SignPath Foundation » |
| **Certum Open Source** | 25 € (code seul), 49 € (signature dans le nuage SimplySign), 69 € (carte et lecteur) par an | personne physique ; projet public ; pas d'usage commercial | « Open Source Developer », avec le nom de la personne |
| Certum Standard, SSL.com IV/OV | 129 à 209 € par an, plus un jeton matériel (YubiKey : 379 $) ou un service de signature payant | aucune sur le dépôt | nom de la personne |
| Azure Artifact Signing | 9,99 $ par mois et un abonnement Azure payant | particuliers des États-Unis et du Canada seulement ; organisations de l'UE acceptées (une condition d'ancienneté a existé : à vérifier) | nom légal de l'organisation |
| Microsoft Store (MSIX) | gratuit (compte gratuit, Microsoft re-signe le paquet) | passer la certification du Store | Microsoft, pour les copies du Store seulement |

À noter :
- **SignPath interdit de signer les binaires des projets amont** avec son certificat
  (ses conditions le disent) : les modules de PyPI resteraient non signés, avec le risque
  Smart App Control ci-dessus.
- Un **paquet MSIX** porte une signature commune à tous ses fichiers. Qu'un MSIX passe
  Smart App Control sans signer chaque DLL est probable, mais Microsoft ne le confirme pas
  pour Smart App Control : à essayer.
- Le dépôt deviendra public (décision de l'utilisateur, 16/10/2026) : les deux voies
  gratuites ou presque s'ouvriront alors. D'ici là, des versions non signées suffisent pour
  les machines de l'utilisateur.

**macOS.**
- Apple Developer Program : **99 USD par an** (montant en euros non vérifié), seul moyen
  d'obtenir un certificat « Developer ID » et la **notarisation**. Dispense de frais :
  associations, établissements d'enseignement et administrations seulement, pas les
  particuliers. Le même abonnement sert pour iOS.
- Sans notarisation, depuis macOS 15 : le Ctrl-clic « Ouvrir » ne contourne plus
  Gatekeeper ; l'alerte ne propose que « Placer dans la corbeille » ; il faut aller dans
  Réglages Système, Confidentialité et sécurité, « Ouvrir quand même » (pendant environ une
  heure), puis donner son mot de passe. Homebrew refuse désormais les applications non
  notarisées. Pour un public non technique, la notarisation est indispensable.
- macOS 27 (septembre 2026) ne fonctionne que sur les puces Apple ; macOS 26 était le
  dernier pour les Mac Intel, qui reçoivent encore des correctifs de sécurité pendant
  trois ans. Un paquet **arm64 seul** suffit au départ.

**Linux.** Rien à acheter : Flathub signe son dépôt et construit lui-même les paquets ;
une AppImage peut porter une signature GPG, qu'aucun système ne vérifie au lancement.

### 2.9 Recommandation pour les ordinateurs

| | Windows | macOS | Linux |
|---|---|---|---|
| Empaquetage | PyInstaller 6.22.3 ou plus, en mode dossier | idem : `.app` arm64 | idem, construit sur la plus ancienne distribution visée |
| Installeur | Inno Setup 7, pour l'utilisateur seul | `.dmg` signé et notarisé | AppImage (runtime de type 2) ; `.deb` et `.rpm` ensuite |
| Moteur d'IA | `llama-server` Vulkan (30 à 50 Mo) ; CUDA en option, après mesure | `llama-server` Metal (11,7 Mo) | `llama-server` Vulkan (30 à 50 Mo) |
| Modèles | assistant de premier lancement, avec accord ; Ollama réutilisé pour la conversation s'il est là | idem | idem |
| Réseau local | règles « Privé, sous-réseau local » créées à l'activation du réseau local ; profil réseau expliqué, jamais modifié | invite « Réseau local » ; rien à poser | rien à poser ; commandes documentées |
| Signature | SignPath ou Certum Open Source, une fois le dépôt public | Developer ID et notarisation (99 $ par an) | aucune à acheter |
| Mises à jour | manifeste signé (Ed25519), installeur relancé en silence | idem, ou Sparkle 2 | idem, ou mise à jour par zsync |
| Canaux en plus | winget ; Microsoft Store (MSIX) plus tard | — | Flathub plus tard (WebKitGTK, politique sur l'IA) |

## 3. Android : une vraie application à la place de Termux

Termux impose aujourd'hui (docs/android.md) :
- deux applications à prendre à la même source ;
- des paquets à installer en ligne de commande ;
- l'optimisation de la batterie à retirer à la main ;
- une option pour les développeurs, ou `adb`, contre le *phantom process killer* ;
- une adresse avec un jeton à recopier dans Chrome ;
- une lecture de la batterie par Termux:API, qui se bloque parfois.

Une application Android règle tout cela, à condition de respecter trois règles : **tout
calculer dans son propre processus**, **ne pas demander d'exemption de batterie sur
Google Play**, et **utiliser le même moteur et le même fichier de modèle que les PC**.

### 3.1 Garder le code Python ou réécrire le nœud

| | Chaquopy (Python embarqué) | Briefcase (BeeWare) | Réécriture en Kotlin | Cœur en Rust partagé |
|---|---|---|---|---|
| Code existant | gardé | gardé | réécrit (protocole en double, PC et téléphone) | réécrit une fois pour tous les systèmes |
| Fonctions d'Android (tâches, service, batterie) | par une coquille Kotlin | modèle de projet à compléter ; rien sur les services au premier plan | directes | par une coquille Kotlin |
| Paquets natifs | numpy, cryptography, lxml, Pillow, **jusqu'à Python 3.13** ; pas de pydantic-core | ceux de Chaquopy | — | bibliothèques Rust (lecture des documents : Xberg ; Iroh : liaisons Kotlin officielles) |
| Effort pour le nœud | faible | faible, mais peu adapté à un nœud de calcul | moyen | gros |

Ce qu'il faut savoir (16/10/2026) :
- **Chaquopy** 17.1.0 (12/10/2026), licence MIT : Python 3.10 à 3.15, processeurs arm64 et
  x86_64, Android 7 (API 24) et plus récent. Depuis la 17.0, pip n'installe que des paquets
  précompilés. Son dépôt de paquets natifs s'arrête à **Python 3.13** (numpy 1.26.2,
  cryptography 42.0.8, lxml 5.3.0, Pillow 11.0.0) et n'a **pas de pydantic-core**. Coût :
  environ 12 Mo par architecture pour Python, plus 8 Mo pour ces paquets. Un service Kotlin
  démarre Python et appelle n'importe quel module.
- **CPython** prend officiellement Android en charge (niveau 3 depuis 3.13, binaires
  officiels depuis 3.14), et cibuildwheel sait compiler des paquets pour Android. Mais le
  16/10/2026, PyPI n'a **aucune roue Android** pour numpy, cryptography, pydantic-core,
  lxml ni Pillow.
- **pydantic-core est le seul vrai blocage** : FastAPI en dépend. L'API locale (interface)
  utilise FastAPI et pydantic dans six modules de routes. Mais le serveur entre nœuds
  (`aiodos/network/p2p_app.py`) n'utilise que `Request`, `HTTPException` et
  `StreamingResponse`, que Starlette, en pur Python, fournit aussi.
- **pywebview** fonctionne sous Android, mais par la chaîne de Kivy (Buildozer), sans
  rien pour les services : inutile ici.

**Recommandation : une coquille Kotlin et le nœud Python existant, par Chaquopy.**
- L'interface du téléphone est **native** : code d'appairage à confirmer, « Prêter ma
  puissance », seuils de batterie et de température, état. Elle appelle Python
  directement, sans serveur web local. L'interface actuelle, faite pour un écran large,
  oblige d'ailleurs à faire défiler la page de côté.
- Python 3.13 sur le téléphone, la seule version qui a ses paquets natifs : le code doit
  rester compatible 3.13 (à vérifier, avec numpy 1.26 et cryptography 42).
- `p2p_app.py` passe de FastAPI à Starlette : plus de pydantic-core sur le téléphone. À
  défaut, compiler pydantic-core pour Android avec cibuildwheel et maturin.
- Plus tard, si le projet grandit : un cœur en Rust partagé par tous les systèmes, y
  compris iOS (liaisons UniFFI pour Kotlin, Swift et Python ; Iroh ; llama.cpp ; Xberg pour
  les documents).

### 3.2 Le moteur d'embeddings dans l'application

- **llama.cpp, appelé directement dans le processus de l'appli** (bibliothèque JNI ;
  paquet officiel Android de 72 Mo ; exemple officiel `examples/llama.android` avec une
  liaison Kotlin). Les routines optimisées pour les processeurs ARM récents (KleidiAI :
  dotprod, i8mm, SVE, SME2) sont choisies au démarrage.
- **Sur le processeur seulement**, au début :
  - le backend OpenCL des Adreno ne cite pas le BF16 ;
  - Vulkan reste peu fiable sous Android (24 s contre 1,6 s au processeur dans une mesure ;
    sorties corrompues sur un Adreno 740 en mars 2026, ticket fermé sans correctif) ;
  - le NPU de Qualcomm (Hexagon) n'est qu'expérimental.
- **NNAPI** est déprécié depuis Android 15. Son successeur, **LiteRT**, a un EmbeddingGemma
  (179 à 220 Mo, int4/int8) huit fois plus rapide sur le NPU d'un Galaxy S25 Ultra que sur
  son processeur (7,8 ms contre 66 ms pour 256 jetons). Mais c'est un autre fichier, donc
  d'autres vecteurs : incompatible avec la règle du même modèle.
- **Même fichier et même version de llama.cpp que le moteur d'AIODOS sur les PC**
  (§ 2.3), comparés avec une tolérance (cosinus), comme aujourd'hui : l'identité au bit
  près entre processeurs ARM et x86 n'est pas garantie.

### 3.3 Calculer en charge sans service permanent

**Ce qui ne marche plus** : se réveiller au branchement du chargeur. L'événement
`ACTION_POWER_CONNECTED` n'est plus livré aux applis qui le déclarent dans leur manifeste
(depuis l'API 26), même si une page de la documentation le suggère encore.

**Ce qui marche : des tâches planifiées.** Le téléphone demande déjà du travail aux PC
(`network/worker.py`) : ce fonctionnement colle aux tâches de WorkManager (JobScheduler),
avec les contraintes « en charge » et « batterie pas faible ».
- D'après le code d'Android, une tâche dispose de 10 minutes garanties, et **jusqu'à 30
  minutes en charge**. Le quota de temps ne s'applique pas en charge (sauf pour les applis
  classées « restreintes »). D'où des tranches de 10 à 25 minutes, replanifiées.
- Branché, le téléphone sort de la mise en veille (Doze) et de la mise en sommeil des
  applis (App Standby).
- **Piège à essayer** : « en charge » ne veut pas dire « branché ». Un téléphone qui
  plafonne sa charge à 80 ou 85 % (protection de batterie de Samsung) ne compte comme en
  charge que s'il signale une charge adaptative et dépasse 70 %. Les tâches pourraient alors
  ne plus partir : à essayer sur le S23.
- Android 16 : les tâches lancées pendant que l'appli est visible, ou à côté d'un service au
  premier plan, consomment désormais le quota.

**Un bouton « Prêter maintenant »**, lancé depuis l'interface, démarre un **service au
premier plan**. C'est aussi le recours si le plafond de charge bloque les tâches.
- Type `specialUse` : justification écrite dans le manifeste, puis description et vidéo
  dans la Play Console ; aucune liste publique des cas acceptés.
- `dataSync` (6 h par 24 h depuis Android 15, réservé par Google Play au travail lancé par
  l'utilisateur) et `mediaProcessing` (transcodage) ne correspondent pas.
- Un service ne peut pas démarrer depuis l'arrière-plan (depuis l'API 31), sauf
  exceptions, dont une appli que l'utilisateur a lui-même retirée de l'optimisation de
  batterie.
- La notification reste obligatoire. Si l'utilisateur refuse les notifications (Android 13
  et plus), le service tourne quand même, et sa notification n'apparaît plus que dans le
  gestionnaire de tâches.

**La batterie se lit sans permission** (`ACTION_BATTERY_CHANGED` : niveau, état de charge,
source, température), tout comme l'état thermique du téléphone (`PowerManager`). Termux:API
devient inutile. Une vraie appli sait aussi si l'écran est allumé : la règle « c'est la
charge qui fait foi » de docs/android.md peut s'assouplir.

### 3.4 L'optimisation de la batterie et les règles de Google Play

- Google Play **interdit de demander directement l'exemption** d'optimisation de la
  batterie (`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`), sauf si la fonction principale de
  l'appli en souffre. Les cas acceptés sont la messagerie et les appels sans Firebase, la
  sécurité, l'automatisation et les applis compagnons d'un appareil. **Le calcul bénévole
  n'y figure pas**, et contourner la gestion de l'énergie est une infraction. Syncthing et
  Tasker ont déjà eu, pour cette raison, une version refusée ou un retrait du Play Store.
- Sur Google Play, donc : aucune demande d'exemption. Les tâches en charge n'en ont pas
  besoin. Au besoin, ouvrir simplement l'écran des réglages de batterie
  (`ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS`), ce que « la plupart des applis peuvent »
  faire. Une version distribuée hors Play pourrait proposer la demande directe.
- Les réglages des constructeurs (applis « en veille » de Samsung, démarrage automatique de
  Xiaomi) ne se règlent pas par programme : il faut guider l'utilisateur
  (dontkillmyapp.com).
- **BOINC n'est plus sur Google Play** (sa page renvoie une erreur le 16/10/2026). Il reste
  sur F-Droid et en APK, cible encore Android 9 (API 28), et lance son client comme un
  programme séparé. Google Play exige désormais l'API 36.
- La règle de Google Play sur le minage : « We don't allow apps that mine cryptocurrency on
  devices. » Rien sur le calcul bénévole.

### 3.5 Pourquoi les « limites des processus enfants » ne s'appliquent plus

- **Ce que vise le *phantom process killer***, d'après le code d'Android : les processus
  qu'une appli crée elle-même (`fork`, `exec`) et que le système ne connaît pas. Deux
  règles : **32 au plus, pour toutes les applis réunies**, et un arrêt pour usage excessif
  du processeur quand l'appli est en arrière-plan ou en cache (seuils de 25, 10 puis 2 %,
  contrôlés toutes les 5 minutes).
- **Sous Termux**, `python`, `ollama` et `llama-server` sont des processus enfants de
  Termux : ils tombent sous ces deux règles, notamment au branchement du chargeur. D'où
  l'option pour les développeurs.
- **Dans une vraie appli**, Python (chargé comme bibliothèque par Chaquopy) et llama.cpp
  (appelé par JNI) tournent comme des fils d'exécution du processus de l'appli : ce ne sont
  pas des processus enfants, les deux règles ne les voient pas. Pendant une tâche ou un
  service au premier plan, l'appli n'est pas « en cache » non plus (d'après le code, à
  mesurer pour les tâches).
- **Règle de conception** : aucun programme lancé à part sur Android. Lancer `ollama serve`
  ou `llama-server` ferait revenir les deux règles. Et depuis l'API 29, une appli ne peut
  plus exécuter un fichier de son propre dossier, ce qui explique sans doute que BOINC reste
  bloqué à l'API 28. Le compagnon `aiodos-net` doit donc être chargé comme bibliothèque
  (Iroh publie des liaisons Kotlin officielles).

### 3.6 Le réseau local

- **Android 17** (applis ciblant l'API 37) rend obligatoire une **permission « réseau
  local »** (`ACCESS_LOCAL_NETWORK`, groupe « Appareils à proximité »). Elle couvre l'envoi
  et la réception d'UDP, y compris les diffusions, et la résolution des noms en `.local`.
  Sous Android 16, c'est une protection facultative ; les applis qui ciblent une API
  inférieure à 37 gardent l'accès.
- Recevoir les diffusions demande un `WifiManager.MulticastLock` (accordé sans
  question), qui coûte un peu de batterie. Sans lui, le filtre de paquets du Wi-Fi les
  jette, surtout écran éteint.

### 3.7 Distribution

| Canal | Conditions (16/10/2026) | Geste de l'utilisateur |
|---|---|---|
| **APK sur GitHub** (suivi par Obtainium) | signé avec notre propre clé | autoriser « Installer des applis inconnues » pour la source |
| **Google Play** | 25 $ une fois, pièce d'identité ; compte personnel récent : **test fermé avec 12 testeurs pendant 14 jours** avant publication ; API 36 exigée depuis le 31/08/2026 ; déclaration du service au premier plan avec vidéo ; pages mémoire de 16 Ko exigées pour les mises à jour à partir du 1/02/2027 (les bibliothèques de Chaquopy le sont déjà) | installer depuis le Play Store |
| **F-Droid** | logiciel libre compilé depuis les sources ; binaires précompilés tolérés seulement de « sources de confiance » ; quelques jours par version. Une appli Chaquopy y est entrée **sans aucun paquet natif** : nos paquets natifs sont un risque (non vérifié) | installer F-Droid, puis l'appli |

**Vérification des développeurs Android.** Depuis le 30/09/2026 au Brésil, en Indonésie, à
Singapour et en Thaïlande, puis partout en 2027, une appli installée sur un appareil
certifié devra venir d'un développeur enregistré auprès de Google.
- L'installation directe d'un APK n'est « pas encore » concernée ; `adb` reste exempté.
- Un compte de **distribution limitée**, gratuit et sans pièce d'identité, couvre jusqu'à
  **20 appareils** : de quoi couvrir les téléphones de l'utilisateur et d'un premier cercle
  de confiance.
- Un parcours « avancé » permet aux utilisateurs expérimentés d'installer une appli non
  enregistrée : mode développeur, redémarrage, une attente de 24 h la première fois.
- Pour une appli signée par F-Droid, le développeur devra enregistrer la clé de F-Droid.

### 3.8 Recommandation pour Android

1. **Prototype** sur le S9 (Android 10), le S23 (Android 16) et un émulateur Android 17 :
   llama.cpp dans une appli Kotlin. Comparer ses vecteurs à ceux du moteur des PC, mesurer
   le débit (8 passages par minute sur le S9 sous Termux) et la mémoire, et essayer les
   tâches planifiées avec un plafond de charge à 85 %.
2. **Le nœud Python par Chaquopy** (Python 3.13), avec `p2p_app` sous Starlette et une
   interface native.
3. **Tâches en charge** et bouton « Prêter maintenant » (service `specialUse`), verrou
   multicast, permission « réseau local », notification.
4. **Distribution** :
   - APK sur GitHub, signé avec notre clé, enregistré auprès de Google (distribution
     limitée gratuite) ;
   - puis Google Play (12 testeurs pendant 14 jours, sans demande d'exemption de batterie) ;
   - F-Droid plus tard, si ses règles acceptent les paquets natifs de Chaquopy, ou après un
     passage à Rust.

### 3.9 Ce qui est fait (20/10/2026)

Décision de l'auteur : « Nœud dans l'appli », par **Chaquopy et nos compilations**
([android.md](android.md), « L'appli AIODOS »). Essayé sur le S23 le jour même.

- **Python 3.13 par Chaquopy 17.1**, avec ses paquets natifs ; **pydantic-core compilé
  ici** (Rust et NDK, `tools/android_wheels.py`) plutôt que de passer `p2p_app` sous
  Starlette : FastAPI reste partout, et tout le code d'AIODOS tourne tel quel.
- **L'interface reste celle des PC**, dans une WebView : elle a sa barre de navigation en
  bas sur un écran étroit ; une interface native n'est plus nécessaire pour commencer.
- **llama.cpp en programme, pas par JNI** : l'archive officielle pour Android de la
  version des PC, rangée parmi les bibliothèques de l'appli avec le compagnon, comme
  Syncthing pour Android. Le moteur intégré des PC (`llm/`) sert donc tel quel. C'est une
  entorse assumée à la règle du § 3.5 : avec le service au premier plan, l'appli n'est
  jamais en arrière-plan, et la règle d'arrêt pour usage du processeur ne s'applique pas.
- **Un service au premier plan permanent** (`specialUse`), plutôt que des tâches
  planifiées : le nœud sert ses machines à tout moment, comme sur les PC (« Démarrer avec
  le téléphone » activé dès la première ouverture). Verrou de veille seulement en charge,
  verrou multicast. Les tâches planifiées et la sobriété exigée par Google Play restent à
  faire avant toute publication sur le Play Store.

## 4. iOS et iPadOS : un client d'abord, du calcul appli ouverte

iOS 27 est sorti le 14/09/2026 ; les règles de l'App Store datent du 8/06/2026.

### Ce qu'Apple permet en arrière-plan

| Mécanisme | Ce qu'il permet | Limites |
|---|---|---|
| `BGProcessingTask` | quelques minutes de calcul quand l'appareil est inactif, de préférence la nuit, en charge (`requiresExternalPower`) | le système décide, selon l'usage qu'on fait de l'appli ; tâche arrêtée dès que l'utilisateur reprend l'appareil ; rien si l'actualisation en arrière-plan est coupée, en mode économie d'énergie, ou après une fermeture forcée de l'appli |
| `BGContinuedProcessingTask` (iOS 26) | poursuivre en arrière-plan un travail **lancé par un geste de l'utilisateur**, avec une progression affichée par le système | Apple exclut les travaux automatiques (maintenance, synchronisation) ; durée non documentée ; GPU en arrière-plan seulement sur les iPad à puce M3 ou plus récente, pas sur iPhone (support technique d'Apple, 2025 ; iOS 27 non vérifié) |
| `BGAppRefreshTask`, `beginBackgroundTask` | environ 30 s | trop court pour une unité de travail |
| Notifications silencieuses | 30 s, 2 ou 3 fois par heure, sans garantie | il faut un serveur qui détient la clé APNs du développeur : contraire au principe « pas de serveur central » |
| Modes d'arrière-plan (audio, VoIP, localisation…) | — | règle 2.5.4 : seulement pour leur usage prévu |

**iOS n'offre aucun moyen de calculer en continu en arrière-plan.** Ce qui reste :

- **le calcul appli ouverte** : écran maintenu allumé (`isIdleTimerDisabled`), en charge
  (`UIDevice.batteryState`), avec un freinage selon la chaleur (`ProcessInfo.thermalState`) ;
- **un bonus de nuit** de quelques minutes par `BGProcessingTask`, sans garantie.

DreamLab (Vodafone Foundation), qui calculait pour la recherche contre le cancer, ne
faisait pas autre chose sur iPhone : un bouton, le téléphone posé déverrouillé et branché.
L'appli a fermé le 2/04/2025.

### Les règles de l'App Store

| Règle | Ce qu'elle dit | Pour AIODOS |
|---|---|---|
| 2.4.2 | pas de processus d'arrière-plan sans rapport avec l'appli, « such as cryptocurrency mining » ; pas de chauffe excessive | un calcul lancé par l'utilisateur, appli ouverte, pour **ses propres machines**, est lié à la fonction de l'appli. C'est défendable, sans garantie (analyse, pas un avis d'Apple) |
| 3.1.5 (ii) | pas de minage de cryptomonnaie sur l'appareil | expliquer la différence dans les notes destinées à la revue |
| 2.5.2 | pas de code téléchargé qui change les fonctions de l'appli | les tâches restent des **données** (texte à vectoriser), jamais un script : c'est déjà le cas (tâches prédéfinies, `jobs/registry.py`) |
| 4.2.3 (i) | l'appli doit fonctionner sans exiger une autre appli | une appli qui ne sert à rien sans PC risque le refus : lui donner une valeur propre (bibliothèque sur l'appareil, mode démonstration) |
| 4.2.3 (ii) | annoncer la taille d'un téléchargement nécessaire au premier lancement et demander l'accord | c'est le principe retenu pour tous les systèmes (§ 2.4) |
| 4.2.7 | un client de bureau à distance ne se connecte qu'à un appareil de l'utilisateur | l'analogie la plus proche d'un client de ses propres machines |
| 2.1 (a), 5.1.2 (i) | donner aux testeurs d'Apple de quoi essayer ; ne pas exiger les notifications | prévoir un mode démonstration ; notifications facultatives |

Aucune règle ne parle de pair-à-pair. Les risques sont ailleurs : être pris pour du
minage, et la chauffe.

### Le moteur et le réseau local

- **Moteur** : llama.cpp publie à chaque version un paquet pour iOS (XCFramework, version
  b11696 le 16/10/2026), avec un exemple officiel en SwiftUI ; MLX Swift est l'autre voie.
  Le modèle d'Apple (Foundation Models) ne fournit pas d'embeddings, et
  `NLContextualEmbedding` produit des vecteurs d'un autre espace : inutilisables avec les
  bibliothèques d'AIODOS. Il faut **le même fichier GGUF que toutes les autres
  machines** : celui qu'AIODOS distribue (§ 2.3). Le GGUF de ggml-org (Q8_0, 334 Mo) et
  celui d'Ollama (BF16, 622 Mo) sont deux fichiers différents, aux vecteurs différents.
- **Autorisation « Réseau local »** (note technique TN3179) : une invite au premier
  accès, qu'aucune API ne peut déclencher ni lire. Elle est exigée pour se connecter en TCP,
  envoyer de l'UDP ou utiliser Bonjour, pas pour écouter.
- **La découverte actuelle ne marche pas sur iPhone.** Envoyer **ou recevoir** une
  diffusion UDP (`255.255.255.255`) exige une permission spéciale
  (`com.apple.developer.networking.multicast`), accordée sur demande par Apple et
  réservée aux protocoles qui l'imposent ; Apple recommande Bonjour. Il faut donc que les
  ordinateurs annoncent **aussi** un service Bonjour (`_aiodos._tcp`, trouvé par
  `NWBrowser`), ou un appairage par **QR code** (adresse et empreinte de la clé).
- **Une appli en arrière-plan est suspendue** et perd ses connexions : l'iPhone ouvre
  toujours la connexion, et n'est jamais supposé joignable.
- **Pas de programme à part** : iOS interdit de lancer un processus (PEP 730 : `os.fork`
  et `os.spawn` lèvent une erreur). Pour Internet, le compagnon `aiodos-net` devra être
  chargé comme bibliothèque, ou remplacé par les liaisons Swift officielles d'Iroh
  (IrohLib, générées par UniFFI).

### Python ou Swift

- Python fonctionne sous iOS (niveau 3 depuis 3.13). python.org publie un paquet iOS
  depuis Python 3.15.0 (9/10/2026), pas pour 3.14.
- Mais le 16/10/2026, PyPI n'a **aucune roue iOS** pour numpy, cryptography ni
  pydantic-core (dont FastAPI a besoin). L'index de BeeWare a numpy et cryptography,
  toujours pas pydantic-core. Et le serveur local serait suspendu dès que l'appli passe en
  arrière-plan.
- **Recommandation : une appli native en Swift (SwiftUI).** CryptoKit fournit
  ChaCha20-Poly1305, X25519 et HKDF depuis iOS 13 : de quoi réécrire l'enveloppe et
  l'appairage, qui sont courts. Il ne reste à écrire que le client et une seule tâche
  portable, l'embedding.
- **Ce qui change dans le protocole** : baux courts avec signal de vie, flux de réponse
  qu'on peut reprendre après une suspension, découverte Bonjour et QR code, et un nœud qui
  annonce ses contraintes (premier plan seulement, batterie, chaleur).

### Distribution

- **App Store** : le même Apple Developer Program que pour signer sur macOS (99 USD par
  an) couvre aussi iOS. TestFlight permet des essais de 90 jours, jusqu'à 10 000 testeurs
  externes. L'installation gratuite par Xcode ne dure que 7 jours : bonne pour le
  développement, pas pour le public.
- **Dans l'UE**, depuis le 1/10/2026 : la notarisation par Apple est obligatoire quel que
  soit le canal ; la distribution par un site web exige par exemple un million
  d'installations par an, hors de portée ; AltStore PAL reste possible (compte payant et
  notarisation ; rien à payer pour une appli gratuite).
- **GPL-3.0 et App Store** : le détenteur des droits peut publier lui-même (Signal,
  Mastodon, Nextcloud sont sur l'App Store sous AGPL ou GPL). Le risque vient des
  contributions de tiers : en 2011, la plainte d'un contributeur avait fait retirer VLC.
  Il faut donc décider, **avant d'accepter des contributions**, d'un accord de contribution
  ou d'une permission additionnelle (article 7 de la GPLv3) pour les boutiques (Q8).

### Recommandation pour iOS et iPadOS

1. **Un client natif** : converser avec les modèles des autres machines et chercher dans
   ses bibliothèques. Le PC calcule le vecteur de la question : aucun modèle sur l'iPhone.
   Forte valeur, faible risque.
2. **« Prêter cet appareil »**, appli ouverte et branchée, d'abord sur iPad à puce M.
3. **Le bonus de nuit** par `BGProcessingTask`.
4. **Après la phase 2** (Internet) : un iPhone sert surtout loin de la maison.

**Ne pas promettre** :
- des heures de calcul en arrière-plan ;
- le GPU en arrière-plan sur iPhone ;
- la découverte automatique par diffusion ;
- un iPhone joignable ou réveillable à distance ;
- des modèles de 7 milliards de paramètres à l'aise sur 8 Go.

## 5. Les gestes qui restent obligatoires, et pourquoi

| Geste | Windows | macOS | Linux | Android | iOS, iPadOS |
|---|---|---|---|---|---|
| Installer | lancer l'installeur | ouvrir le `.dmg`, glisser l'appli dans Applications | rendre l'AppImage exécutable, ou installer un paquet | Play Store ; ou, pour un APK, autoriser « Installer des applis inconnues » | App Store ou TestFlight |
| Première ouverture | rien si l'appli est signée et connue ; sinon « Exécuter quand même », et blocage complet si Smart App Control est actif | confirmer l'ouverture d'une appli téléchargée | rien | rien | rien |
| Droits administrateur | une fenêtre de contrôle de compte, en activant le réseau local | non | non, sauf pour un paquet système | — | — |
| Réseau déclaré « Privé » | si Windows l'a classé « Public » (c'est le cas par défaut) | — | — | — | — |
| Accès au réseau local | — (couvert par la règle) | invite « Réseau local » (macOS 15 et plus) ; invite du pare-feu s'il est activé | une commande, si un pare-feu est actif (openSUSE…) | permission « Appareils à proximité » (Android 17, applis ciblant l'API 37) | invite « Réseau local » |
| Télécharger les modèles | accord dans l'assistant | idem | idem | idem, ou copie depuis une machine appairée | idem, s'il calcule |
| Appairer | confirmer le code à 6 chiffres sur chaque machine | idem | idem | idem | idem |
| Prêter sa puissance | l'activer | idem | idem | l'activer ; régler à la main les économies d'énergie du constructeur si besoin | l'activer, et garder l'appli ouverte, branchée, hors mode économie d'énergie |
| Lancer avec la session | case à cocher, facultative | idem ; macOS l'affiche dans ses réglages | idem | sans objet : tâches planifiées en charge | impossible |
| Notifications | — | — | — | facultatives (Android 13 et plus) | facultatives |

Pourquoi ces gestes restent :
- **Installer, première ouverture** : aucun système ne laisse un logiciel s'installer seul.
  Signé, le geste est un clic ; non signé, il devient le contournement d'un
  avertissement, ce qu'on ne peut pas demander à un public non technique.
- **Droits administrateur** : une règle de pare-feu vaut pour toute la machine, Windows
  exige donc un administrateur.
- **Profil réseau** : c'est un réglage de sécurité de l'utilisateur, que la consigne du
  projet interdit de modifier. Microsoft recommande d'ailleurs « Public » par défaut.
- **Accès au réseau local** : macOS, iOS et Android réservent cette décision à
  l'utilisateur ; aucune API ne permet de la prendre à sa place, ni même de provoquer
  l'invite.
- **Modèles** : de 0,3 à 6 Go chacun, peut-être sur une connexion limitée. Rien ne part ni
  n'arrive sans accord (principe du projet, et règle 4.2.3 d'Apple).
- **Appairage, prêt de puissance** : voulus. L'appairage confirmé par un humain sur chaque
  machine fait la sécurité du protocole ; prêter sa puissance reste le choix de chacun.
- **Économies d'énergie des constructeurs** : aucune API ne les règle (Samsung, Xiaomi), et
  Google Play interdit à une appli de calcul de demander l'exemption de batterie.
- **iOS** : il n'offre aucun moyen de calculer longtemps en arrière-plan.

## 6. Recommandation et ordre de réalisation

| Plateforme | Recommandation | Rôle |
|---|---|---|
| **Windows** | PyInstaller et Inno Setup, pour l'utilisateur seul ; `llama-server` Vulkan intégré ; règle de pare-feu créée à l'activation du réseau local ; signature SignPath ou Certum Open Source ; mises à jour signées | machine complète |
| **macOS** | le même paquet, en `.app` arm64 dans un `.dmg` signé et notarisé ; `llama-server` Metal | machine complète |
| **Linux** | AppImage, puis `.deb` et `.rpm` ; `llama-server` Vulkan ; Flathub plus tard | machine complète |
| **Android** | appli Kotlin, nœud Python par Chaquopy (3.13), llama.cpp dans le processus ; tâches planifiées en charge ; APK, puis Google Play | calcul des embeddings en charge |
| **iOS, iPadOS** | appli native en SwiftUI ; client d'abord, calcul appli ouverte ensuite | client, calcul d'appoint |

| Étape | Contenu | Ampleur | Pourquoi à ce rang |
|---|---|---|---|
| **0. Décisions** | les questions du § 7 | — | elles changent le contenu des étapes suivantes |
| **1. Préparer le code** | données dans le dossier de l'utilisateur ; module « moteur » avec un pilote `llama-server` ; catalogue de modèles et assistant de premier lancement ; empreinte du modèle = SHA-256 du GGUF et recette ; Whisper en option ; icône de zone de notification et lancement avec la session ; règle de pare-feu à l'activation du réseau ; script de construction PyInstaller. Mesures : EmbeddingGemma 2 (`tools/embedding_bench.py`), Vulkan contre CUDA | moyenne | sans cela, aucun installeur ne marche ; tout s'essaie encore avec `python -m aiodos` |
| **2. Windows, pour les machines de l'utilisateur** | installeur Inno Setup non signé, construit par GitHub Actions ; essais sur les PC de la phase 1 et sur le vieil ASUS | petite | c'est le parc actuel et la cible de la phase 1. Non signé, il suffit à l'utilisateur : « Exécuter quand même », Smart App Control désactivé |
| **3. Publier** | dépôt public ; signature Windows ; mises à jour signées ; politique de confidentialité (exigée par SignPath, la découverte du réseau local étant un envoi de données) ; manifeste winget | moyenne, surtout des démarches | **à faire avant toute distribution à d'autres personnes** : sans signature, Smart App Control bloque et SmartScreen effraie |
| **4. Android** | prototype llama.cpp (S9, S23, émulateur Android 17) ; appli Kotlin et Chaquopy ; APK enregistré auprès de Google (distribution limitée, gratuite), qui remplace Termux sur les téléphones de l'utilisateur ; puis Google Play | grosse | le téléphone est déjà dans le parc et sert à la phase 2 (4G) ; c'est le parcours actuel le plus pénible. Ne dépend que de l'étape 1 : peut avancer en même temps que les étapes 2 et 3 |
| **5. macOS et Linux** | `.dmg` notarisé (compte Apple ; un Mac ou une machine macOS de GitHub Actions) ; diffusion par interface sur macOS ; AppImage | moyenne | aucune machine de ce type dans le parc pour l'instant. Linux peut passer avant si le vieil ASUS est installé sous Linux |
| **6. iOS et iPadOS** | client natif ; découverte Bonjour et QR code, utiles aussi aux autres systèmes ; calcul appli ouverte ensuite | grosse | après la phase 2 (Internet) : un iPhone sert surtout loin de la maison, et son rôle de calcul est limité |

## 7. Questions à trancher

| | Question | Recommandation de l'étude | Bloque |
|---|---|---|---|
| Q1 | **Moteur d'IA** : `llama-server` intégré partout, avec Ollama en option pour la conversation ; ou Ollama embarqué ; ou Ollama exigé | `llama-server` intégré (§ 2.3) | étape 1 |
| Q2 | **Modèle d'embedding par défaut** : EmbeddingGemma 2 (Apache-2.0, 300 Mo), embeddinggemma (conditions Gemma, 622 Mo), ou bge-m3 (MIT) | EmbeddingGemma 2, si la mesure le confirme (§ 2.4) | étape 1 |
| Q3 | **Date du passage en public** | avant toute distribution à d'autres personnes | étape 3 |
| Q4 | **Mises à jour** : vérification proposée au premier lancement ou active d'emblée ; qui garde la clé de publication, et où (hors ligne chez l'utilisateur, ou dans l'intégration continue) | proposée ; clé hors ligne | étape 3 |
| Q5 | **Signature Windows** : SignPath (gratuit ; éditeur affiché « SignPath Foundation » ; binaires tiers non signés), Certum Open Source (25 à 69 € par an ; nom de l'utilisateur affiché), et le Microsoft Store en plus ? | à décider : chaque voie a un inconvénient (§ 2.8) | étape 3 |
| Q6 | **Apple Developer Program** (99 $ par an, pour macOS et iOS) : maintenant, ou quand un Mac ou un iPhone entre dans le parc | quand un Mac ou un iPhone entre dans le parc | étapes 5 et 6 |
| Q7 | **Identité publique** : le nom de l'utilisateur sur les certificats et dans les boutiques, ou une structure (une association peut obtenir la dispense de frais d'Apple ; Azure Artifact Signing est ouvert aux organisations de l'UE) | à décider | étapes 3 à 6 |
| Q8 | **Contributions extérieures** : accord de contribution, ou permission additionnelle (article 7 de la GPLv3) pour les boutiques d'applications | à décider avant d'accepter la première contribution | App Store, et plus largement l'ouverture |
| Q9 | **Android** : Google Play en plus de l'APK ? F-Droid ? | APK d'abord, Google Play ensuite, F-Droid plus tard | étape 4 |
| Q10 | **Flathub** : déclarer la part de code écrite avec une IA et rédiger soi-même le manifeste, ou s'en passer | s'en passer pour l'instant (AppImage) | étape 5 |

## Sources (consultées le 16/10/2026)

**Mesures locales** (PC de développement) : journaux d'Ollama (`%LOCALAPPDATA%\Ollama\server.log`),
manifestes et fichiers de `~/.ollama/models`, registre d'Ollama
(`registry.ollama.ai/v2/library/…`), règles du pare-feu (`Get-NetFirewallRule`), signatures
(`Get-AuthenticodeSignature`) ; empaquetage d'essai par PyInstaller 6.20 dans un dossier
temporaire.

**Empaquetage**
- PyInstaller : <https://pyinstaller.org/en/stable/CHANGES.html>,
  <https://pyinstaller.org/en/stable/operating-mode.html>,
  <https://pyinstaller.org/en/stable/feature-notes.html>,
  <https://pyinstaller.org/en/stable/bootloader-building.html>,
  <https://github.com/pyinstaller/pyinstaller/issues/9056> (UUID et réseau local sous macOS)
- pywebview : <https://pywebview.flowrl.com/guide/freezing.html>,
  <https://pywebview.flowrl.com/guide/installation.html>,
  <https://github.com/r0x0r/pywebview/releases> ; pythonnet :
  <https://github.com/pythonnet/pythonnet/releases>
- Briefcase : <https://briefcase.beeware.org/en/stable/reference/platforms/>,
  <https://github.com/beeware/briefcase/issues/1570>
- Nuitka : <https://nuitka.net/changelog/Changelog.html>,
  <https://nuitka.net/posts/nuitka-release-28.html>,
  <https://nuitka.net/user-documentation/common-issue-solutions.html>
- Autres : <https://cx-freeze.readthedocs.io/en/stable/versions.html>,
  <https://github.com/ofek/pyapp>, <https://docs.velopack.io/getting-started/python>,
  <https://platformdirs.readthedocs.io/en/latest/platforms.html>

**Windows**
- Inno Setup : <https://jrsoftware.org/files/is7-whatsnew.htm>,
  <https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm>,
  <https://jrsoftware.org/ishelp/topic_runsection.htm>
- MSIX : <https://learn.microsoft.com/en-us/windows/msix/desktop/flexible-virtualization>,
  <https://learn.microsoft.com/en-us/uwp/schemas/appxpackage/uapmanifestschema/element-desktop2-firewallrules>,
  <https://learn.microsoft.com/en-us/windows/msix/app-installer/auto-update-and-repair--overview>
- WebView2 : <https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution>
- Pare-feu : <https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/rules>,
  <https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule>,
  <https://learn.microsoft.com/en-us/windows/win32/api/netlistmgr/nf-netlistmgr-inetwork-getcategory>,
  <https://support.microsoft.com/en-us/windows/experience/connectivity-networking/essential-network-settings-and-tasks-in-windows>,
  <https://projectzero.google/2021/08/understanding-network-access-windows-app.html>
- Identité de l'application : <https://learn.microsoft.com/en-us/windows/win32/shell/appids>
- Signature : <https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation>,
  <https://support.microsoft.com/en-us/topic/what-is-smart-app-control-285ea03d-fa88-4d56-882e-6698afdb7003>,
  <https://textslashplain.com/2026/04/28/smart-app-control/>,
  <https://cabforum.org/2025/11/17/ballot-csc-31-maximum-validity-reduction>,
  <https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart>,
  <https://signpath.org/terms>, <https://shop.certum.eu/open-source-code-signing.html>,
  <https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msi/app-package-requirements>,
  <https://blogs.windows.com/windowsdeveloper/2025/09/10/free-developer-registration-for-individual-developers-on-microsoft-store/>

**macOS**
- <https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy>
- <https://developer.apple.com/documentation/technotes/tn3178-checking-for-and-resolving-build-uuid-problems>
- <https://developer.apple.com/forums/thread/765285> (`EHOSTUNREACH`),
  <https://developer.apple.com/forums/thread/772642> (diffusion)
- <https://developer.apple.com/news/?id=saqachfa> (Gatekeeper, macOS 15),
  <https://support.apple.com/en-us/102445>,
  <https://eclecticlight.co/2026/08/11/how-can-you-run-code-that-hasnt-been-notarised/>
- <https://developer.apple.com/programs/>, <https://developer.apple.com/support/fee-waiver/>
- <https://support.apple.com/guide/mac-help/block-connections-to-your-mac-with-a-firewall-mh34041/mac>
- macOS 27 et les Mac Intel : <https://www.macrumors.com/2026/05/26/macos-27-will-mark-the-end-of-an-era/>

**Linux**
- <https://docs.appimage.org/user-guide/troubleshooting/fuse.html>,
  <https://github.com/AppImage/type2-runtime>
- <https://docs.flathub.org/docs/for-app-authors/requirements> (permissions, politique sur
  l'IA), <https://docs.flatpak.org/en/latest/sandbox-permissions.html>
- WebKitGTK 4.1 retiré du SDK GNOME :
  <https://raw.githubusercontent.com/GNOME/gnome-build-meta/master/elements/sdk/webkitgtk.bst> ;
  moteur GTK de pywebview :
  <https://raw.githubusercontent.com/r0x0r/pywebview/master/webview/platforms/gtk.py>
- Pare-feu : <https://wiki.ubuntu.com/UFW>, <https://fedoramagazine.org/control-the-firewall-at-the-command-line/>,
  <https://wiki.debian.org/nftables>,
  <https://doc.opensuse.org/documentation/leap/security/html/book-security/cha-security-firewall.html>
- Icônes de zone de notification sous GNOME :
  <https://lists.stg.fedoraproject.org/archives/list/desktop@lists.fedoraproject.org/thread/DA2AEAAS4QDCBL3SSPKTHTLREKDFSZQV/>

**Moteur d'IA et modèles**
- Ollama : <https://github.com/ollama/ollama/releases> (0.30, 0.40.0, 0.40.2, 0.40.5),
  <https://docs.ollama.com/windows>, <https://docs.ollama.com/faq>,
  <https://raw.githubusercontent.com/ollama/ollama/main/app/ollama.iss>,
  <https://raw.githubusercontent.com/ollama/ollama/main/scripts/install.ps1>,
  <https://raw.githubusercontent.com/ollama/ollama/main/compatmigrate/embeddinggemma.go>,
  <https://raw.githubusercontent.com/ollama/ollama/main/app/updater/updater.go>,
  <https://ollama.com/privacy>
- llama.cpp : <https://github.com/ggml-org/llama.cpp/releases> (b11696),
  <https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md>,
  <https://huggingface.co/blog/ggml-org/model-management-in-llamacpp> (mode routeur),
  <https://docs.nvidia.com/cuda/eula/index.html>,
  <https://github.com/ggml-org/llama.cpp/discussions/10879> (Vulkan contre CUDA)
- Applications comparables : <https://www.jan.ai/changelog/2026-05-22-jan-v0.8.0>,
  <https://docs.anythingllm.com/changelog/v1.18.0>,
  <https://github.com/flathub/com.jeffser.Alpaca.Plugins.Ollama>
- Modèles : <https://ai.google.dev/gemma/terms>,
  <https://huggingface.co/google/embeddinggemma-300m>,
  <https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/commits/main>,
  <https://developers.googleblog.com/embeddinggemma-2-the-developer-guide/>,
  <https://huggingface.co/ggml-org/embeddinggemma-2-GGUF>

**Mises à jour**
- <https://pypi.org/project/velopack/>, <https://github.com/velopack/velopack/issues/105>,
  <https://sparkle-project.org/documentation/>, <https://github.com/vslavik/winsparkle>,
  <https://github.com/dennisvang/tufup/issues/184>, <https://github.com/jedisct1/minisign>,
  <https://github.com/microsoft/winget-pkgs/blob/master/doc/Validation.md>,
  <https://raw.githubusercontent.com/janhq/jan/main/src-tauri/tauri.conf.json>,
  <https://github.com/AppImage/AppImageSpec/blob/master/draft.md>

**Android**
- Chaquopy : <https://chaquo.com/chaquopy/doc/current/changelog.html>,
  <https://chaquo.com/chaquopy/doc/current/android.html>, <https://chaquo.com/pypi-13.1/>
- CPython : <https://peps.python.org/pep-0738/>, <https://docs.python.org/3/using/android.html>,
  <https://cibuildwheel.pypa.io/en/stable/platforms/>
- llama.cpp : <https://github.com/ggml-org/llama.cpp/blob/master/docs/android.md>,
  <https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENCL.md>,
  <https://github.com/ggml-org/llama.cpp/issues/20603> ; LiteRT :
  <https://huggingface.co/litert-community/embeddinggemma-300m>,
  <https://developer.android.com/ndk/guides/neuralnetworks/migration-guide>
- Services et tâches : <https://developer.android.com/develop/background-work/services/fgs/service-types>,
  <https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start>,
  <https://developer.android.com/about/versions/15/behavior-changes-15>,
  <https://developer.android.com/about/versions/16/behavior-changes-all>,
  <https://developer.android.com/develop/background-work/background-tasks/broadcasts/broadcast-exceptions>,
  <https://developer.android.com/develop/ui/views/notifications/notification-permission> ;
  code d'Android (`QuotaController.java`, `PhantomProcessList.java`) sur
  <https://android.googlesource.com/platform/frameworks/base>
- Batterie : <https://developer.android.com/training/monitoring-device-state/doze-standby>,
  <https://support.google.com/googleplay/android-developer/answer/9888379>,
  <https://support.google.com/googleplay/android-developer/answer/13607354>,
  <https://dontkillmyapp.com/samsung>, <https://f-droid.org/packages/edu.berkeley.boinc/>
- Processus fantômes : <https://github.com/agnostic-apollo/Android-Docs/blob/master/en/docs/apps/processes/phantom-cached-and-empty-processes.md>,
  <https://developer.android.com/about/versions/10/behavior-changes-10>
- Réseau local : <https://developer.android.com/privacy-and-security/local-network-permission>,
  <https://source.android.com/docs/core/connect/android-packet-filter>
- Distribution : <https://support.google.com/googleplay/android-developer/answer/14151465>,
  <https://support.google.com/googleplay/android-developer/answer/11926878>,
  <https://developer.android.com/guide/practices/page-sizes>,
  <https://f-droid.org/docs/Inclusion_Policy/>, <https://developer.android.com/developer-verification>,
  <https://github.com/n0-computer/iroh-ffi>, <https://github.com/xberg-io/xberg>

**iOS et iPadOS**
- <https://developer.apple.com/documentation/backgroundtasks/bgprocessingtask>,
  <https://developer.apple.com/documentation/backgroundtasks/bgcontinuedprocessingtask>,
  <https://developer.apple.com/documentation/backgroundtasks/performing-long-running-tasks-on-ios-and-ipados>,
  <https://developer.apple.com/forums/thread/801229> (GPU en arrière-plan),
  <https://developer.apple.com/forums/thread/685525>
- <https://developer.apple.com/app-store/review/guidelines/>
- <https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.networking.multicast>
- <https://developer.apple.com/documentation/foundationmodels>,
  <https://developer.apple.com/documentation/cryptokit>, <https://docs.iroh.computer/languages/swift>
- <https://www.python.org/downloads/ios/>, <https://docs.python.org/3/using/ios.html>,
  <https://peps.python.org/pep-0730/>, <https://developer.apple.com/forums/thread/747499>
  (pas de processus enfant)
- <https://developer.apple.com/testflight/>, <https://developer.apple.com/support/dma-and-apps-in-the-eu/>,
  <https://faq.altstore.io/developers/distribute-with-altstore-pal.md>
- DreamLab : <https://en.wikipedia.org/wiki/DreamLab> ; GPL et App Store :
  <https://www.fsf.org/news/2010-05-app-store-compliance>,
  <https://www.videolan.org/press/ios2out.html>
