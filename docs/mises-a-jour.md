# Mises à jour automatiques

Installé chez un ami, AIODOS se met à jour seul : personne n'a rien à lancer. Il n'y a
pas de serveur pour autant. Une version publiée passe **de machine en machine par
AIODOS lui-même**, comme un fichier de travail.

## En bref

- **Une version publiée**, c'est le code d'un commit, signé avec la **clé de
  publication** de l'auteur. Cette clé reste sur son PC, hors du dépôt.
- **Le compagnon Internet** `aiodos-net` voyage avec elle : un programme par
  plateforme (Windows, Android), construit à partir du même commit. Chaque machine ne
  prend que celui de sa plateforme (voir plus bas).
- **Elle se propage** : chaque machine qui a la version la propose à ses machines
  appairées, qui la proposent aux leurs.
- **Chaque machine vérifie la signature**, installe la version **à côté** de
  l'actuelle, puis redémarre dessus **quand elle est libre**.
- **Si la nouvelle version ne démarre pas**, la précédente reprend la main
  aussitôt. La nouvelle est réessayée une fois, puis écartée.
- **Le réglage** se trouve dans Réglages → Mises à jour, sous le nom « Installer
  automatiquement les nouvelles versions ». Il est activé par défaut. Une fois une
  version installée, un message dit « AIODOS a été mis à jour ».

## Publier une version (PC de développement)

1. **Une seule fois**, créer la clé de publication :
   ```
   python tools/publish.py key
   ```
   La clé est écrite dans `%USERPROFILE%\.aiodos\publication\cle-de-publication.pem`, avec
   un `LISEZMOI.txt`. Sa clé publique est ajoutée à `aiodos/updates/keys.py` ; enregistrer
   ce fichier par un commit. **Sauvegarder la clé à part** (clé USB, gestionnaire de mots
   de passe). Sans elle, plus aucune mise à jour ne peut être publiée pour les machines
   déjà installées.
2. Écrire le numéro de la version dans `aiodos/__init__.py` (ex. `2.0.1`), puis faire un
   commit.
3. Publier :
   ```
   python tools/publish.py release --notes "Ce qui change, en une phrase"
   ```
   L'outil construit d'abord le compagnon de chaque plateforme (voir ci-dessous), ce qui
   prend quelques minutes. Le paquet signé est rangé dans `data/updates/packages/2.0.1/`
   et l'étiquette `v2.0.1` marque le commit. Le nœud de ce PC le propose à ses machines
   appairées dans la minute. Il n'a pas besoin d'être redémarré.
4. Repasser le numéro en `2.0.2-dev` pour la suite, puis faire un commit.

Avec `--out dossier`, l'archive, les compagnons et le manifeste sont aussi copiés dans
ce dossier, pour une installation à la main (voir plus bas). L'outil refuse :

- un numéro « -dev », sauf avec `--dev`, pour des essais ;
- un numéro déjà publié ;
- un commit dont `keys.py` ne contient pas la clé : la version publiée ne pourrait
  pas recevoir les suivantes.

Les modifications qui ne sont pas dans un commit ne partent pas, et l'outil le signale.

### Le compagnon joint à la version

Pour chaque plateforme, `tools/companion_build.py` :

1. exporte le dossier `compagnon/` **du commit publié** dans un dossier temporaire
   (`git archive` : ni modification en cours, ni ancien binaire) ;
2. le construit avec `cargo build --profile distribution --target <cible> --locked`,
   en donnant le commit au programme (`AIODOS_NET_COMMIT`) ;
3. vérifie le binaire : format de la plateforme (PE x86-64, ELF AArch64 pour Android),
   commit retrouvé dans ses octets, et `aiodos-net --version` au numéro du
   `Cargo.toml` de ce commit quand le programme peut tourner sur le PC.

Le profil `distribution` (optimisé pour la taille, `lto = "fat"`) donne des programmes
bien plus petits : 10,6 Mo sous Windows au lieu de 17,5, et 9,3 Mo pour Android. Les
dépendances compilées se gardent dans `compagnon/target` d'une publication à l'autre.

| Plateforme | Ce qu'il faut sur le PC qui publie | Durée mesurée (16/10/2026) |
|---|---|---|
| `windows-x86_64` | Rust (cible MSVC) et les outils C++ de Visual Studio | 100 s |
| `android-aarch64` | en plus : `rustup target add aarch64-linux-android` et le NDK Android, trouvé par `ANDROID_NDK_HOME`, `ANDROID_NDK_ROOT`, ou `ANDROID_HOME`/`ANDROID_SDK_ROOT` + `ndk/<version>` | 151 s |

Les durées valent pour des dépendances déjà compilées une fois. La toute première
publication compte quelques minutes de plus.

- **Par défaut**, l'outil joint les compagnons qu'il sait construire sur ce PC. Si les
  outils d'une plateforme manquent, il l'écrit (« ATTENTION : compagnon non joint ») et
  la version part sans ce compagnon. Les machines de cette plateforme gardent alors le
  programme qu'elles ont déjà.
- **`--targets windows-x86_64,android-aarch64`** exige exactement ces plateformes. Un
  outil manquant arrête tout.
- **`--no-companion`** publie sans compagnon.
- **Le moteur llama.cpp** des gros modèles répartis est joint aussi : l'archive officielle
  d'une version épinglée dans `tools/engine_release.py` (nom, taille, empreinte), gardée
  dans `~/.aiodos/moteurs/` après le premier téléchargement. S'il ne peut être téléchargé,
  ou ne correspond pas à l'empreinte épinglée, la version part sans lui
  (« ATTENTION : moteur llama.cpp non joint »). **`--no-engine`** publie sans lui.
- **Une construction ou une vérification qui échoue arrête tout** : rien n'est publié,
  et le numéro reste libre. Ce PC est sujet à des plantages : un `rustc` arrêté net
  (`STATUS_ACCESS_VIOLATION`) s'est produit une fois le 16/10. Il suffit de relancer.

## Ce qui se passe sur chaque machine

| Étape | Quand | Détail |
|---|---|---|
| Annonce | profil échangé toutes les 60 s | chaque machine annonce la version qu'elle peut transmettre (`updates.offer`) et les fichiers qu'elle a de chaque paquet, compagnons compris (`updates.files`) |
| Réception | toutes les 30 s, ou à l'activation du réglage | si une machine appairée en ligne a une version plus récente : manifeste signé, puis l'archive du code, en morceaux de 1 Mo par le canal chiffré. Un transfert coupé reprend où il en était. Signature et empreinte SHA-256 sont vérifiées. Une machine qui échoue est laissée de côté 10 minutes |
| Compagnon | aussitôt après | le compagnon de sa plateforme, demandé à une machine qui l'annonce, vérifié de même (voir plus bas) |
| Installation | aussitôt reçue | extraction dans `data/updates/versions/<version>/`, numéro vérifié, essai à blanc avec le Python de la machine. S'il manque un paquet Python, un message le dit, rien ne change, et un nouvel essai a lieu le lendemain |
| Redémarrage | quand la machine est libre | aucun calcul ici ou pour une autre machine, aucune unité confiée dehors, aucune conversation. Fenêtre ouverte sous Windows : personne au clavier depuis le délai d'inactivité du partage de puissance (10 min par défaut). Ailleurs, avec une fenêtre : au prochain démarrage, ou par le bouton « Redémarrer maintenant ». Au plus 3 redémarrages par heure |
| Essai | au démarrage | le lanceur de la version d'origine lance la nouvelle dans un processus à part. Après 10 s de service sans incident, elle devient la version courante. Si elle s'arrête avant, la précédente démarre aussitôt à sa place. Un second essai a lieu une heure plus tard ; après un second échec, la version est écartée et l'utilisateur prévenu |

La version d'origine, c'est le dossier où AIODOS a été installé. Elle n'est jamais
modifiée : c'est le dernier recours. Une version installée par mise à jour tourne
depuis `data/updates/versions/`. Une machine garde la version courante, celle en
attente, et les deux paquets les plus récents, qu'elle transmet aux autres.

## Le compagnon Internet livré avec les versions

Le compagnon `aiodos-net` relie les machines appairées par Internet
([compagnon/README.md](../compagnon/README.md)). C'est un programme compilé, propre à
chaque plateforme. Il voyage avec les versions.

**Dans le manifeste**, chaque compagnon est un fichier de plus, signé comme le reste :

| Type (`kind`) | Fichier | Pour |
|---|---|---|
| `source` | `aiodos-2.0.3.zip` | toutes les machines |
| `aiodos-net-windows-x86_64` | `aiodos-net-2.0.3-windows-x86_64.exe` | PC Windows 64 bits |
| `aiodos-net-android-aarch64` | `aiodos-net-2.0.3-android-aarch64` | téléphones Android 64 bits (Termux, Android 7 et plus) |
| `llama.cpp-windows-x86_64` | `llama-b11707-bin-win-vulkan-x64.zip` | PC Windows 64 bits : moteur des gros modèles répartis (docs/gros-modeles.md) |

Le format du manifeste ne change pas (`FORMAT = 1`).

- **Machines en 2.0.x** : elles ignorent les types qu'elles ne connaissent pas et ne
  téléchargent que l'archive du code. C'est vérifié avec le code même de la 2.0.1 sur
  un manifeste à deux compagnons. Elles installent la version, redémarrent dessus, et
  c'est la nouvelle version qui va chercher son compagnon.
- **Versions suivantes** : chacune ne télécharge que le compagnon de sa plateforme
  (`manifest.wanted`). La plateforme vient de `aiodos/updates/platforms.py` :
  `windows-x86_64`, `android-aarch64`, `linux-x86_64`… Une machine pour laquelle rien
  n'est construit (téléphone 32 bits…) n'en prend aucun.

**À qui le demander.** L'archive du code, toute machine qui propose la version l'a. Le
compagnon d'une plateforme, seules certaines l'ont : la machine qui a publié, et celles
de cette plateforme qui l'ont déjà reçu. Chaque machine annonce donc dans son profil les
fichiers qu'elle a de chaque paquet (`updates.files`). Le service de mise à jour demande
le compagnon de sa plateforme à une machine qui l'annonce (actions `update/manifest` et
`update/chunk`, empreinte vérifiée), dans trois cas :

- **pour la version reçue**, juste après l'archive et avant de redémarrer dessus ;
- **pour la version qui tourne**, si elle l'attend encore : installée par une machine
  2.0.x, ou depuis une archive. Dans ce dernier cas, il n'y a pas même de manifeste :
  il est demandé avec le compagnon ;
- **une machine qui échoue** à le fournir est laissée de côté 10 minutes, comme pour
  les versions.

Une machine transmet aussi les fichiers d'un paquet incomplet, s'ils sont entiers : par
exemple le compagnon d'une machine installée depuis une archive.

Le moteur llama.cpp suit le même chemin que le compagnon (`_obtain_engine`). Un fichier
déjà reçu avec une autre version, de même empreinte, est copié au lieu d'être téléchargé
de nouveau (`store.reuse`) : le moteur, épinglé, ne change pas à chaque version.

**Où il est rangé.** Le compagnon reçu reste dans le paquet
(`packages/<version>/aiodos-net-…`), pour les machines de même plateforme. Pour le
lancer, AIODOS le copie à un emplacement fixe, `data/updates/bin/aiodos-net(.exe)`,
décrit par `bin/aiodos-net.json` (version, type, empreinte) :

- l'empreinte est revérifiée pendant la copie. Un fichier altéré est effacé et sera
  téléchargé de nouveau ;
- le chemin ne change pas d'une version à l'autre : le pare-feu de Windows ne redemande
  donc pas son autorisation à chaque mise à jour ;
- sous Android et Linux, la copie reçoit le droit d'exécution ;
- si l'ancien compagnon tourne encore et ne peut pas être remplacé (Windows), le
  nouveau est lancé depuis le paquet.

**Quel programme est lancé** (`network/companion.py`, `candidates`), par ordre de
préférence :

1. `AIODOS_NET_BIN`, choix explicite de l'utilisateur, et alors rien d'autre ;
2. dans une copie de développement, celui qu'on y a compilé (`compagnon/target/release`) ;
3. celui livré avec la version qui tourne, puis avec une version plus ancienne que la
   machine garde. Jamais celui d'une version plus récente, écrit pour un autre code ;
4. celui compilé dans le dossier de la version, puis dans celui de la version d'origine ;
5. celui du `PATH` (Termux : `$PREFIX/bin`).

Les lanceurs 2.0.x mettent dans `AIODOS_NET_BIN` le compagnon compilé dans la version
d'origine. AIODOS le reconnaît (il est dans `<version d'origine>/compagnon/target/`) et
le fait passer au rang 4 : ce programme plus ancien ne l'emporte plus sur celui livré
avec la version. Les lanceurs suivants ne posent plus cette variable.

**Sans jamais perdre le lien.**

- Un programme qui ne démarre pas (construction ratée pour cette plateforme…), ou
  qu'on ne peut pas mettre en place (disque plein…), laisse place au suivant de la
  liste. Quand tous ont échoué, la liste reprend du début. Une machine joignable
  seulement par Internet garde ainsi son lien, et recevra la version corrigée.
- Une machine dont la version tourne avec un autre programme (celui du `PATH`, d'une
  version plus ancienne…) relance son compagnon sur celui qui vient d'arriver, dès
  qu'aucun travail ne passe par lui : aucun calcul, aucune unité confiée, aucune
  conversation. Les machines appairées perdent le lien deux ou trois secondes.
- S'il n'y avait aucun compagnon, il démarre dans la minute qui suit son arrivée.
- La machine garde le paquet du compagnon en service et celui de la version d'origine,
  même anciens. Le compagnon en service est aussi le dernier recours d'une version
  arrivée sans le sien.

L'interface montre la réception (« Réception du compagnon Internet de la version… »).
L'état du réseau (`GET /api/network`, `internet.program`) dit d'où vient le programme
lancé, par exemple « livré avec la version 2.0.3 ». Le journal le dit aussi, avec la
version et le commit du compagnon.

**Vérifié le 16/10/2026** (sans rien publier : clé et clone jetables) :

- `publish.py release` a construit les deux compagnons à partir du commit, les a
  vérifiés et joints, en 252 s. Le compagnon Windows publié démarre et annonce son
  commit. Le compagnon Android est un exécutable ELF AArch64 avec le chargeur
  d'Android et le commit.
- Le code de la 2.0.1 vérifie ce manifeste et n'en prend que l'archive du code.
- `tests/test_updates_companion_end_to_end.py`, avec de vrais nœuds et le vrai
  compagnon : une machine sans compagnon, dont la version ne connaît pas les
  compagnons (comme une 2.0.x), reçoit une version. Celle-ci, une fois lancée, reçoit
  son compagnon, en vérifie l'empreinte, le met en place et le lance. Le tout prend
  16 s.

## Essai réel (16/10/2026)

Le téléphone est un Galaxy S23 (Android 16, Termux, Python 3.14). Il a été installé à la
main depuis l'archive de la version 2.0.0, comme chez un ami. Il est relié au PC par le
compagnon Internet, par un chemin direct en IPv6. Le nœud de démonstration du PC (une
copie de développement) propose la 2.0.1 à partir de 15:58:40.

| Étape | Heure | Durée |
|---|---|---|
| Version reçue (1,3 Mo) | 15:59:58,5 | 78 s après l'offre (profil relu toutes les 60 s, service toutes les 30 s) |
| Installée : extraction et essai à blanc | 15:59:59,8 | 1,3 s |
| Redémarrage dans le même processus (exec), lancement à l'essai | 16:00:01,0 | 1,2 s |
| Nœud 2.0.1 prêt | 16:00:01,6 | 0,6 s |
| En service, après 10 s sans incident | 16:00:11,8 | 10 s |

Après la mise à jour :

- le processus d'origine (2.0.0, dans `~/aiodos`) accompagne la 2.0.1, qui tourne depuis
  `data/updates/versions/2.0.1` et a relancé le compagnon ;
- le téléphone propose à son tour la 2.0.1 à ses machines appairées.

La même chaîne est vérifiée sous Windows à chaque passage des tests
(`tests/test_updates_end_to_end.py`). Le redémarrage y lance un nouveau processus.

## Sécurité

- **Signature Ed25519** du manifeste, qui donne la taille et l'empreinte SHA-256 de
  chaque fichier. Seules les clés de `aiodos/updates/keys.py` font foi. Une machine
  qui transmet une version n'a donc pas à être de confiance : un ami dont la machine
  est compromise ne peut rien faire passer.
- **Qui détient la clé de publication peut installer du code sur toutes les machines
  AIODOS.** Elle ne doit jamais entrer dans le dépôt (elle n'est pas sous `data/`, et
  l'outil ne l'affiche jamais).
- **Jamais de retour en arrière** : seule une version strictement plus récente que
  tout ce que la machine a est acceptée. Une version qui n'a pas démarré n'est plus
  jamais relancée.
- **Archive contrôlée** avant d'écrire quoi que ce soit, en plus de la signature :
  - ni chemin absolu, ni `..`, ni lien ;
  - aucun nom réservé par Windows ;
  - taille et nombre de fichiers bornés ;
  - le numéro écrit dans le code doit être celui du manifeste.
- **Ce qui est servi** : seulement à une machine appairée, par messages scellés, et
  seulement les fichiers d'un paquet complet nommés dans son manifeste.
- **Changer de clé** (perdue, ou copiée par quelqu'un) :
  1. créer une nouvelle clé ;
  2. publier, signée par l'ancienne, une version qui fait aussi confiance à la
     nouvelle ;
  3. retirer l'ancienne dans la version suivante.

  Tant que toutes les machines n'ont pas reçu cette dernière version, l'ancienne
  clé reste acceptée par celles qui ne l'ont pas encore.

## Approbation sur un deuxième appareil

Décision du 17/10/2026 : chaque version doit aussi être approuvée sur un deuxième
appareil, en général le téléphone de l'auteur. Une clé de publication volée ne suffit
alors plus à pousser une version chez les amis : il faudrait aussi le téléphone.
Code : `aiodos/updates/approval.py`, `manifest.approve` et `manifest.approved`.

- **Mise en place.**
  1. Sur le téléphone, ouvrir Réglages, Mises à jour, et cliquer « Approuver depuis cet
     appareil ». La clé d'approbation se crée dans `data/updates/approbation.pem` et ne
     quitte jamais l'appareil.
  2. Coller la ligne affichée dans `APPROVER_KEYS` de `aiodos/updates/keys.py`, faire un
     commit, puis publier.
  3. **En déclarer deux** : le téléphone et un appareil de secours. Sans approbateur
     joignable, plus aucune version ne peut partir.
- **Publier.** L'outil signe comme avant, puis écrit « EN ATTENTE D'APPROBATION ». La
  version est gardée, mais ni installée ni proposée : le stock ne voit pas une version
  sans approbation.
- **Approuver.** Sur le téléphone, le bouton « Versions en attente » demande à mes
  machines en ligne les versions qui attendent (action `update/pending`). Il montre le
  numéro, les notes, le commit, chaque fichier avec sa taille et son empreinte. Le
  bouton « Approuver » signe le texte même du manifeste, précédé du contexte
  `aiodos-approbation` (une approbation ne vaut jamais signature d'auteur, ni l'inverse),
  et renvoie l'approbation (`update/approve`). La machine qui a publié la vérifie,
  l'ajoute à l'enveloppe, et la version part. Les deux actions ne répondent qu'à mes
  machines, jamais à un ami.
- **Compatibilité.** L'approbation est un champ de plus dans l'enveloppe signée
  (`approvals`) ; le format du manifeste ne change pas. Les versions qui ne la
  connaissent pas l'ignorent. Celles qui la connaissent l'exigent dès que
  `APPROVER_KEYS` n'est pas vide. Tant qu'il l'est, rien ne change.

## Copie de développement

Un dossier qui contient un dépôt git (`.git`) n'est **jamais** remplacé : on y fait
tourner le code qu'on écrit. Il reçoit et transmet quand même les versions publiées.
`AIODOS_NO_UPDATE=1` désactive aussi le lanceur, quel que soit le dossier.

## Installer chez un ami (en attendant l'installeur)

Ne pas copier le dossier du dépôt : avec son `.git`, il ne se mettrait jamais à jour.

1. `python tools/publish.py release --out dist` (ou reprendre l'archive d'une version
   déjà publiée).
2. Chez l'ami :
   - décompresser `aiodos-<version>.zip` ;
   - installer Python, puis `pip install -r requirements.txt` ;
   - installer Ollama et le modèle `embeddinggemma` ;
   - lancer `lancer.bat`.
3. Appairer sa machine avec l'une des tiennes. Les versions suivantes arriveront seules.
4. Pour le lien par Internet, activer « Internet » dans ses réglages. Le compagnon de sa
   plateforme arrive d'une machine appairée qui l'a, sur le réseau local, juste après
   l'appairage. L'appairage passe encore par le réseau local : il faut qu'il soit
   arrivé avant que les machines se séparent.

## Fichiers

```
data/updates/
├── packages/<version>/     manifeste signé, archive, compagnon de cette plateforme :
│                           ce que la machine transmet
├── downloads/<version>/    téléchargements en cours (.part)
├── versions/<version>/     versions installées (.installed.json : installation complète)
├── bin/                    compagnon en service (aiodos-net.exe) et sa description (.json)
└── state.json              version courante, version à essayer, versions écartées, message
```

Le code : `aiodos/updates/` (`version`, `keys`, `manifest`, `platforms`, `store`,
`state`, `install`, `binaries`, `launcher`, `restart`, `p2p`, `service`),
`aiodos/network/companion.py` (choix du programme) et `tools/publish.py` avec
`tools/companion_build.py`.

## Limites actuelles

- **Le compagnon d'une plateforme** ne se trouve que chez la machine qui a publié et
  chez celles de cette plateforme qui l'ont reçu. Un téléphone qui n'est appairé qu'à
  des PC Windows ne le reçoit que du PC qui a publié.
- **Le compagnon Android livré** a été construit et vérifié sur le PC (format, commit),
  mais pas encore lancé sur un téléphone. Le Galaxy S23 servait à un autre essai.
- **Une machine passée par une 2.0.x** lance d'abord, après la mise à jour, le
  compagnon qu'elle avait déjà, s'il y en a un. Elle passe au compagnon livré dès qu'il
  est arrivé et que rien ne passe par le compagnon.
- **Une future appli Android** (hors de Termux) ne pourra pas exécuter un programme
  téléchargé dans son dossier (Android 10 et plus) : le compagnon y sera une
  bibliothèque (cf. l'étude sur les installeurs).
- **Un nouveau paquet Python** n'est pas installé tout seul : la version qui le demande
  attend, avec un message. L'installeur, avec son Python embarqué, réglera cela.
- **Il faut une machine appairée qui a la version.** Pas de source sur GitHub tant que
  le dépôt est privé.
- **Le format** du manifeste (`FORMAT = 1`) et celui de `state.json` ne doivent pas
  changer. Les lanceurs déjà installés les lisent ; on peut seulement y ajouter des
  clés.
- **Avec un installeur** qui empaquette Python (PyInstaller, cf. l'étude sur les
  installeurs), seule la partie « installer et lancer » changera. Signature,
  transmission, état et retour en arrière restent.
