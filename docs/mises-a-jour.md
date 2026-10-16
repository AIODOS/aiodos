# Mises à jour automatiques

Installé chez un ami, AIODOS se met à jour seul : personne n'a rien à lancer. Il n'y a
pas de serveur pour autant. Une version publiée passe **de machine en machine par
AIODOS lui-même**, comme un fichier de travail.

## En bref

- **Une version publiée**, c'est le code d'un commit, signé avec la **clé de
  publication** de l'auteur. Cette clé reste sur son PC, hors du dépôt.
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
   Le paquet signé est rangé dans `data/updates/packages/2.0.1/` et l'étiquette `v2.0.1`
   marque le commit. Le nœud de ce PC le propose à ses machines appairées dans la
   minute. Il n'a pas besoin d'être redémarré.
4. Repasser le numéro en `2.0.2-dev` pour la suite, puis faire un commit.

Avec `--out dossier`, l'archive et son manifeste sont aussi copiés dans ce dossier,
pour une installation à la main (voir plus bas). L'outil refuse :

- un numéro « -dev », sauf avec `--dev`, pour des essais ;
- un numéro déjà publié ;
- un commit dont `keys.py` ne contient pas la clé : la version publiée ne pourrait
  pas recevoir les suivantes.

Les modifications qui ne sont pas dans un commit ne partent pas, et l'outil le signale.

## Ce qui se passe sur chaque machine

| Étape | Quand | Détail |
|---|---|---|
| Annonce | profil échangé toutes les 60 s | chaque machine annonce la version qu'elle peut transmettre (`updates.offer`) |
| Réception | toutes les 30 s, ou à l'activation du réglage | si une machine appairée en ligne a une version plus récente : manifeste signé, puis morceaux de 1 Mo par le canal chiffré. Un transfert coupé reprend où il en était. Signature et empreinte SHA-256 sont vérifiées. Une machine qui échoue est laissée de côté 10 minutes |
| Installation | aussitôt reçue | extraction dans `data/updates/versions/<version>/`, numéro vérifié, essai à blanc avec le Python de la machine. S'il manque un paquet Python, un message le dit, rien ne change, et un nouvel essai a lieu le lendemain |
| Redémarrage | quand la machine est libre | aucun calcul ici ou pour une autre machine, aucune unité confiée dehors, aucune conversation. Fenêtre ouverte sous Windows : personne au clavier depuis le délai d'inactivité du partage de puissance (10 min par défaut). Ailleurs, avec une fenêtre : au prochain démarrage, ou par le bouton « Redémarrer maintenant ». Au plus 3 redémarrages par heure |
| Essai | au démarrage | le lanceur de la version d'origine lance la nouvelle dans un processus à part. Après 10 s de service sans incident, elle devient la version courante. Si elle s'arrête avant, la précédente démarre aussitôt à sa place. Un second essai a lieu une heure plus tard ; après un second échec, la version est écartée et l'utilisateur prévenu |

La version d'origine, c'est le dossier où AIODOS a été installé. Elle n'est jamais
modifiée : c'est le dernier recours. Une version installée par mise à jour tourne
depuis `data/updates/versions/`. Une machine garde la version courante, celle en
attente, et les deux paquets les plus récents, qu'elle transmet aux autres.

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

## Fichiers

```
data/updates/
├── packages/<version>/     manifeste signé + archive : ce que la machine transmet
├── downloads/<version>/    téléchargements en cours (.part)
├── versions/<version>/     versions installées (.installed.json : installation complète)
└── state.json              version courante, version à essayer, versions écartées, message
```

Le code : `aiodos/updates/` (`version`, `keys`, `manifest`, `store`, `state`, `install`,
`launcher`, `restart`, `p2p`, `service`) et `tools/publish.py`.

## Limites actuelles

- **Le compagnon `aiodos-net`** (binaire Rust) ne voyage pas encore dans les paquets.
  Le manifeste prévoit des fichiers par plateforme ; les versions actuelles les
  ignorent.
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
