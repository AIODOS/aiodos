# Travail de fond : laisser la place à l'utilisateur, ménager la batterie

Demandé le 16/10/2026 : qu'AIODOS puisse tourner en tâche de fond sans prendre toute la
puissance des machines, surtout sur batterie. Fait le 17/10/2026 ; mesures sur le PC
principal (Predator PHN16-72 : i9-14900HX, 8 cœurs performants et 16 efficaces, 32
processeurs logiques ; RTX 4070 Laptop 8 Go ; Windows 11 ; Ollama 0.40.5).

## En bref

| Quoi | Comment | Où |
|---|---|---|
| Le travail de fond passe après l'utilisateur | threads de fond en priorité « inférieure à la normale », EcoQoS, mémoire de priorité inférieure (Windows) ; nice 10 (Linux, Android) | `aiodos/node/priority.py` |
| Programmes lancés par AIODOS | compagnon `aiodos-net`, `nvidia-smi`, essai d'une nouvelle version : classe « inférieure à la normale » dès leur création ; compagnon en EcoQoS | `priority.CREATION_FLAGS`, `priority.lower_process` |
| Une conversation passe devant | pendant une réponse servie par cette machine, le calcul sur la carte graphique attend entre deux lots | `aiodos/jobs/pacing.py` (`wait_turn`) |
| Batterie ménagée | réglage « Sur batterie » : ménager (par défaut), attendre le secteur, ou pleine vitesse | `pacing.py`, vue « Cette machine » |
| Ollama | `num_thread` seulement sur batterie, et seulement si le modèle d'embedding tourne sur le processeur | `aiodos/services/ollama.py` |
| Fermer la fenêtre sans arrêter le nœud | icône dans la zone de notification de Windows | `aiodos/ui/tray.py`, `aiodos/ui/window.py` |
| Mesurer | indexation, conversations, processeur, carte graphique | `tools/bench_background.py`, `tools/cpu_probe.py` |

Les règles déjà en place ne changent pas : partager seulement quand le PC est inactif
(`share_when_idle`), seulement sur secteur (`share_on_ac_only`), batterie minimale et
température maximale d'un téléphone (`network/worker.py`). Elles disent *quand* une
machine calcule pour les autres ; ce qui suit dit *comment* avance le travail de fond, le
sien comme celui des autres.

## 1. Ce qui consomme pendant une indexation

Mesure de départ (code de `main` 65821f0, § 7) : indexation de 348 fichiers Python de la
bibliothèque standard (7 787 passages, modèle `embeddinggemma`), avec six conversations
pendant l'indexation (`qwen2.5:7b`).

- Le processeur travaille surtout **dans Ollama** (son `llama-server`, ~1,2 cœur), pas
  dans AIODOS (0,06 cœur) : pour des fichiers texte, AIODOS lit, découpe, et attend les
  vecteurs.
- La **carte graphique** passe de 15 à 19 W au repos à ~44 W : c'est elle qui vide la
  batterie.
- Une conversation pendant l'indexation d'un **gros fichier** répond ~30 % moins vite
  (120 caractères/s au lieu de ~173) : la file ne lançait plus de nouvelle unité, mais
  celle en cours continuait, lot après lot, sur la même carte graphique. Avec de petits
  fichiers, l'effet se voit à peine : chaque unité ne dure que quelques lots.

D'où les trois leviers : la priorité des threads d'AIODOS (utile surtout pour la lecture
des documents et Whisper, qui calculent dans AIODOS), la cadence des demandes faites à
Ollama (combien d'unités à la fois, des pauses), et céder la carte graphique aux
conversations.

## 2. Priorité basse et mode efficacité

Vérifié dans la documentation de Microsoft (pages de `SetThreadPriority`,
`SetThreadInformation`, `SetProcessInformation`, `PROCESS_INFORMATION_CLASS`,
`MEMORY_PRIORITY_INFORMATION`, et « Quality of Service »), le 17/10/2026 :

- **`SetThreadPriority(THREAD_PRIORITY_BELOW_NORMAL)`** : priorité de base un cran
  sous celle du processus (7 au lieu de 8). L'ordonnanceur ne sert un niveau que si aucun
  thread d'un niveau plus haut n'attend : quand le processeur est plein, les programmes
  de l'utilisateur passent d'abord.
- **`SetThreadInformation(ThreadPowerThrottling, THREAD_POWER_THROTTLING_EXECUTION_SPEED)`**
  : le thread devient « EcoQoS ». Microsoft : *« Always selects most efficient CPU frequency
  and schedules to efficient cores »* (Windows 11 ; avant, il est classé LowQoS). À
  réserver, dit la documentation, au travail qui ne sert pas directement l'utilisateur au
  premier plan. La priorité d'ordonnancement, elle, ne change pas.
- **`SetThreadInformation(ThreadMemoryPriority, MEMORY_PRIORITY_BELOW_NORMAL)`** : les
  pages que le thread fait entrer en mémoire (fichiers lus pour l'indexation) sont
  retirées avant celles des autres programmes quand la mémoire manque. C'est l'exemple
  donné par Microsoft (indexeur, antivirus).
- **`BELOW_NORMAL_PRIORITY_CLASS`** à la création d'un programme, puis
  **`SetProcessInformation(ProcessPowerThrottling, PROCESS_POWER_THROTTLING_EXECUTION_SPEED)`**
  : le même EcoQoS pour tout un programme (le « mode efficacité » du gestionnaire des
  tâches y ajoute la priorité la plus basse).

Écarté, et pourquoi :

- **`THREAD_MODE_BACKGROUND_BEGIN`** : il abaisse aussi les accès disque et la mémoire, et
  Microsoft prévient qu'un thread dans ce mode doit éviter de partager verrous et
  ressources avec les autres, sous peine d'inversion de priorité. Les threads d'AIODOS
  partagent le verrou de l'interpréteur Python (GIL) et la base SQLite : une écriture de
  fond ralentie au disque retiendrait l'enregistrement d'un message de conversation.
- **Priorité « inactive » (`THREAD_PRIORITY_IDLE`)** : sous charge, le thread peut
  attendre jusqu'à ce que Windows le relance (quelques secondes) en gardant le GIL.
- **Tout AIODOS en EcoQoS** : l'interface et les conversations passent par le même
  processus ; elles ralentiraient aussi. Windows le fait déjà de lui-même quand la fenêtre
  est réduite ou cachée (QoS « Low » sur batterie).
- **`PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION`** pour le compagnon : ses minuteries
  servent au protocole QUIC.
- **La priorité d'Ollama** : ce n'est pas un programme d'AIODOS (il est lancé par
  l'application d'Ollama) ; on n'y touche pas.

Ce qui est abaissé : un groupe de threads réservé au travail de fond (« aiodos-fond »), qui
exécute les unités de la file et le calcul fait pour d'autres machines (`priority.run`), et
le service des mises à jour. Les threads de l'interface et des conversations restent
normaux. Un groupe à part, parce que sous Linux un thread qui a pris nice 10 ne peut plus
revenir en arrière sans droits particuliers.

Linux et Android : **nice 10** (la priorité « background » d'Android), thread par thread
(sous Linux, le nice est propre à chaque thread et se transmet aux threads qu'il crée).
Pour le compagnon, chaque thread du programme (`/proc/<pid>/task`), au lancement puis une
fois prêt. macOS : rien (le nice y vaut pour tout le processus).

Vérifié : les tests relisent l'état par l'API de Windows (`GetThreadPriority`,
`GetThreadInformation`, `GetPriorityClass`, `GetProcessInformation`) ; pendant les mesures,
`tools/cpu_probe.py` compte les threads du nœud en priorité 7 et le temps de calcul qu'ils
prennent.

Limite : un thread créé par une bibliothèque compilée démarre, sous Windows, en priorité
normale, quel que soit le thread qui le crée. Les fils de calcul de Whisper (CTranslate2)
et ceux d'OpenMP ne sont donc pas abaissés sous Windows ; d'où, sur batterie, deux fils de
calcul pour Whisper au lieu de quatre.

## 3. Une conversation passe devant

Avant, pendant une conversation servie par cette machine (modèle local, ou modèle prêté à
une autre machine), aucune nouvelle unité « carte graphique » ne démarrait, mais celles en
cours continuaient jusqu'au bout. Maintenant, chaque lot (16 passages à vectoriser, une
image) attend la fin de la réponse avant de partir (`pacing.wait_turn`). Une unité calculée
pour une autre machine est, comme avant, rendue à son propriétaire.

Mesuré (§ 7) pendant l'indexation de gros fichiers : la conversation passe de 120 à
153 caractères/s sans bibliothèques (+28 %), de 113 à 168 avec (+49 %), presque sa vitesse
au repos (~170) ; l'indexation dure 8 % de plus, le temps des conversations. Le délai
avant le premier mot ne change pas : la question d'une conversation avec bibliothèques
attend encore, dans Ollama, le lot déjà parti.

## 4. Sur batterie

Réglage « Sur batterie » (vue « Cette machine », carte « Travail de fond » ; clé
`battery_mode`) :

- **Ménager la batterie** (par défaut, `eco`) : une unité à la fois par ressource
  (carte graphique, embeddings, processeur, réseau) ; après chaque lot, une pause aussi
  longue que lui (le calcul ne tourne que la moitié du temps, comme « Utiliser au plus 50 %
  du temps processeur » de BOINC ; pause d'une minute au plus) ; moitié moins de fils de
  calcul pour Whisper (2 au lieu de 4), et pour Ollama quand le modèle d'embedding tourne
  sur le processeur (§ 5).
- **Attendre le secteur** (`pause`) : rien ne démarre ; une unité en cours attend au lot
  suivant ; une unité calculée pour une autre machine lui est rendue.
- **Pleine vitesse** (`normal`) : comme sur secteur.

Les conversations sont servies dans tous les cas. Sur batterie veut dire « sur batterie
selon Windows » (`GetSystemPowerStatus`), `/sys/class/power_supply` ou Termux:API ; une
alimentation inconnue compte comme le secteur. Pour essayer sans débrancher :
`AIODOS_SIMULATE_POWER=battery` (ou `ac`) dans l'environnement du nœud.

Mesuré (§ 7, batterie simulée, « ménager ») : pendant l'indexation, la carte graphique tire
17 W au lieu de 44 (−60 %), Ollama occupe 0,50 cœur au lieu de 1,23, et la machine entière
12 % au lieu de 19 % ; les conversations gardent leur vitesse. L'indexation dure 2,2 à
5,3 fois plus longtemps selon que la carte graphique remonte en fréquence entre deux lots
ou reste en basse consommation (ce qu'elle a fait à midi, voir § 7).

À savoir : « ménager » abaisse la **puissance** tirée pendant qu'on travaille (la batterie
tient plus longtemps et la machine reste fraîche et silencieuse), pas l'**énergie** de
l'indexation elle-même, qui s'étale (énergie de la carte graphique pendant l'indexation :
~5,3 kJ sur secteur, 5,9 et 7,1 kJ en ménageant). Pour garder le plus de batterie, « Attendre le secteur ». « Ménager » reste
le choix par défaut : une indexation demandée par l'utilisateur avance, sans le gêner.

## 5. Ollama : ce qu'on peut lui demander

Sans toucher à son installation ni à ses réglages (`OLLAMA_NUM_PARALLEL`…), seulement par
les options de chaque requête :

- Ollama 0.40.5 lance un `llama-server` par modèle. `num_thread` devient `-t N`, mais c'est
  une option « de chargement » (`api.Runner`, avec `num_ctx`, `num_batch`, `num_gpu`) :
  toute valeur différente **recharge le modèle** (2,4 s pour `embeddinggemma` ici ; code
  d'Ollama, `server/sched.go`, `needsReload`). Une même valeur doit donc servir à toutes les
  requêtes d'un modèle, celle de la question d'une conversation comprise, sous peine de
  recharger à chaque alternance.
- Mesuré (480 passages, lots de 16) :

  | `embeddinggemma` | fils | passages/s | cœurs occupés par Ollama |
  |---|---|---|---|
  | sur la carte graphique | 16 (défaut) | 82 | 1,1 |
  | | 8 / 4 / 2 | 88 / 87 / 88 | 1,1 |
  | sur le processeur (`num_gpu` 0) | 16 (défaut) | 5 | 15,9 |
  | | 8 | 5 | 7,9 |
  | | 4 | 4 | 4,0 |
  | | 2 | 2 | 2,0 |

  Sur la carte graphique, `num_thread` ne change rien. Sur le processeur, 8 fils au lieu de
  16 donnent le même débit avec moitié moins de cœurs occupés (les cœurs efficaces
  n'apportent rien ici).
- D'où la règle : sur batterie (« ménager »), `num_thread` = un quart des processeurs
  logiques (8 ici), seulement pour les embeddings, et seulement si le modèle tourne au
  moins en partie sur le processeur (`size_vram` < `size` dans `/api/ps`). Jamais pour les
  modèles de conversation, partagés avec l'utilisateur.
- `keep_alive` n'est pas changé : décharger plus tôt le modèle d'embedding obligerait à le
  recharger à la question suivante, et lors d'un essai rapide, au repos avec le modèle
  chargé, la carte graphique était en P8 (1,4 à 2,9 W) : pas de gain visible.
- Pas de priorité dans l'API d'Ollama : la cadence (§ 4) et le passage des conversations
  (§ 3) sont les seuls leviers côté AIODOS.

## 6. Fermer la fenêtre sans arrêter le nœud (Windows)

Une icône AIODOS dans la zone de notification (`NotifyIcon` de WinForms, que pywebview
charge déjà pour la fenêtre) :

- fermer la fenêtre (croix, Alt+F4) la **cache** : le nœud continue de servir les machines
  appairées et d'avancer la file ; la première fois, une bulle l'explique ;
- clic sur l'icône, ou **relancer AIODOS** : la fenêtre revient (le second lancement le
  demande à l'instance en place par son API locale ; le verrou `instance.json` garde pour
  cela le port et le jeton, comme le journal d'un nœud sans fenêtre) ;
- clic droit → « Quitter AIODOS » : arrêt complet ;
- l'arrêt de Windows ou le gestionnaire des tâches ferment vraiment ;
- réglage « Garder AIODOS ouvert quand je ferme la fenêtre » (clé `close_to_tray`,
  activé par défaut) : désactivé, fermer la fenêtre arrête AIODOS comme avant ;
- fenêtre cachée ou réduite : l'interface ne rafraîchit plus le profil de la machine
  (qui lance `nvidia-smi`) ni la liste des machines.

Essai réel (17/10/2026, `python -m aiodos` dans un dossier de données jetable) : la croix
cache la fenêtre et l'API répond toujours ; relancer AIODOS la fait revenir et le second
lancement s'arrête (code 3) ; réglage désactivé, la croix arrête AIODOS (code 0). Le menu
de l'icône n'a pas été cliqué par l'essai automatique : « Quitter » passe par le même
chemin que l'arrêt pour mise à jour.

## 7. Mesures

Outil : `python tools/bench_background.py` (Windows). Il lance un nœud sans fenêtre dans
un dossier jetable, le laisse au repos 10 s, puis lui fait indexer 17 dossiers de la
bibliothèque standard de Python (ou, avec `--big 3`, trois fichiers de 1,5 Mo) ; deux
conversations au repos, six pendant l'indexation (une sur deux avec les bibliothèques).
Toutes les 0,5 s : charge de la machine, des cœurs P et E, du nœud (et de ses threads
abaissés), d'Ollama ; utilisation et puissance de la carte graphique (`nvidia-smi`) ; temps
de réponse de `GET /api/jobs` ; débranché pour de vrai, la décharge de la batterie. Il note
aussi les modèles vus dans Ollama : un modèle étranger veut dire qu'un autre programme s'en
servait. Résultats bruts : `tools/bench_background_results.json`.

Chaque condition a été mesurée deux fois, en alternance (avant, après, batterie, puis de
nouveau), le 17/10/2026 entre 10 h 44 et 12 h 47 ; entre crochets, les deux valeurs. Les
mesures du début de matinée ont été écartées : une autre session faisait tourner ses tests
dans le même Ollama (mistral:7b, nomic-embed-text) et compilait du Rust. Entre les deux
tours, l'état d'énergie de la machine a changé (à midi, la carte graphique restait à
765 MHz, en P4, sous charge) : les comparaisons valent à l'intérieur d'un tour.

**Bibliothèque standard** (348 fichiers, 7 787 passages) — indexation seule :

| Condition | Indexation | Passages/s | AIODOS (cœurs) | dont threads abaissés | Ollama (cœurs) | Carte graphique | API (médiane / p95) |
|---|---|---|---|---|---|---|---|
| avant, secteur | 121 s [110–132] | 65 [59–71] | 0,06 | 0,00 | 1,23 [1,18–1,28] | 41 %, 44 W [40–47] | 3,6 / 9,3 ms |
| après, secteur | 116 s [114–118] | 67 [66–68] | 0,13 [0,13–0,14] | 0,11 [0,10–0,12] | 1,27 [1,26–1,28] | 41 %, 47 W [47–48] | 3,7 / 9,9 ms |
| après, batterie (ménager) | 454 s [263–645] | 21 [12–30] | 0,04 [0,03–0,06] | 0,03 [0,02–0,04] | 0,50 [0,48–0,53] | 19 %, 17 W [11–22] | 2,7 / 5,7 ms |

Conversations (`qwen2.5:7b`, médiane de trois par mesure ; caractères par seconde, puis
délai avant le premier mot) :

| Condition | Au repos | Pendant l'indexation, sans bibliothèques | avec bibliothèques |
|---|---|---|---|
| avant, secteur | 185 car./s | 177 [172–183], 0,10 s | 166 [161–172], 0,53 s |
| après, secteur | 187 car./s | 173 [166–180], 0,09 s | 192 [185–198], 0,45 s |
| après, batterie (ménager) | 191 car./s | 187 [176–199], 0,08 s | 184 [184–185], 0,22 s |

**Gros fichiers** (3 × 1,5 Mo, 7 127 passages ; carte graphique en basse consommation,
~12 W, Ollama 1,18 cœur dans les deux cas) :

| Condition | Indexation | Au repos | Pendant l'indexation, sans bibliothèques | avec bibliothèques |
|---|---|---|---|---|
| avant, secteur | 321 s [319–324] | 173 car./s | 120 [119–121], 0,15 s | 113 [110–115], 1,27 s |
| après, secteur | 347 s [343–351] | 169 car./s | 153 [151–154], 0,16 s | 168 [165–171], 1,31 s |

Ce qu'on en retient :

- **Les conversations passent devant** : +28 à +49 % de vitesse pendant l'indexation d'un
  gros fichier, +16 % avec les bibliothèques sur le corpus de petits fichiers ; ailleurs,
  les écarts restent dans le bruit de mesure.
- **Sur secteur, la charge ne change pas** (Ollama, carte graphique) : le travail de fond y
  avance à pleine vitesse, il cède seulement la place. Les threads abaissés d'AIODOS
  prennent plus de temps de processeur (0,13 cœur au lieu de 0,06) : sur des cœurs
  efficaces à basse fréquence, le même travail dure plus longtemps, pour moins de
  puissance ; la charge totale de la machine, elle, n'augmente pas (17 % contre 19 %).
- **Sur batterie**, la puissance tirée baisse nettement (§ 4).
- **L'interface reste aussi réactive** : nœuds d'avant et d'après lancés côte à côte et
  interrogés en alternance (200 requêtes chacun), `GET /api/jobs` répond en 1,95 ms
  (médiane) contre 2,20 ms avant ; `GET /api/settings`, 1,99 contre 2,21 ms.
- Les autres mesures de cette page : `num_thread` (§ 5), essai de la fenêtre (§ 6).

## 8. Ce qui reste

- **Mesure débranché** : la batterie est simulée ici (`AIODOS_SIMULATE_POWER`). Sur
  batterie, Windows abaisse en plus fréquences et QoS ; l'outil peut tourner débranché
  (`--label …` sans `--battery`) pour comparer, et la décharge se lit dans
  `root\wmi:BatteryStatus.DischargeRate`.
- **Whisper sous Windows** : ses fils de calcul ne sont pas abaissés (§ 2). Le faire
  tourner dans un processus à part, créé en priorité basse, réglerait cela (et isolerait
  aussi son GIL).
- **Inversion de priorité** : un thread de fond abaissé qui tient le GIL quand le
  processeur est saturé par d'autres programmes peut retenir l'interface jusqu'à ce que
  Windows le relance. Les lots qui attendent une conversation ne tiennent pas le GIL ; ce
  cas n'a pas été mesuré (saturer les 32 processeurs logiques de ce PC instable n'a pas
  semblé prudent).
- **Premier mot d'une conversation avec bibliothèques** : la question attend, dans Ollama,
  le lot d'indexation déjà parti (jusqu'à ~1,3 s quand la carte graphique est en basse
  consommation). Des lots plus petits (4 passages au lieu de 16) raccourciraient cette
  attente, au prix d'un peu de débit.
- **Fenêtre cachée** : WebView2 garde sa mémoire (~200 à 400 Mo) ; `TrySuspendAsync`
  pourrait la réduire.
- **Jeu en plein écran** : `SHQueryUserNotificationState` dit quand l'utilisateur joue ou
  présente ; le travail de fond pourrait alors attendre, comme pendant une conversation.
- **Téléphones** : la cadence s'applique aussi sous Android (nice 10, réglage batterie),
  mais n'a pas été essayée sur un téléphone (le S23 était réservé à l'essai 4G).
