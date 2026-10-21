# AIODOS pour les applications, v1 : partager la puissance (conception finale)

> Versée dans le dépôt le 21/10/2026. Conception finale du partage de puissance entre les applications et toutes les machines (miennes, amis, inconnus) : livraisons L0 à L5. L0 et L1 sont réalisées (référence : [../api-applications.md](../api-applications.md)) ; les numéros de migration 18 à 22 sont devenus 20 et suivants (18 et 19 ont servi à l'audit de sécurité du 20/10). Les chemins absolus de l'annexe d'origine ont été retirés.

J'ai travaillé en lecture seule le 20/10/2026 : rien n'a été modifié ni lancé dans les projets, et je n'ai eu aucun accès réseau.

**Ce que c'est.** Voici la conception à relire, puis à réaliser.
- Elle part de la conception retenue, « livraison », appelée ici **C3**.
- Elle y ajoute les greffes des trois jurés, prises dans les conceptions « torrent » (**C1**) et « sécurité » (**C2**).
- Quand deux propositions se contredisent, je tranche et je le dis (§ 0.3).

**Ce qu'elle remplace et ce qu'elle garde.**
- Elle remplace la partie « créations » de la première conception (`conception-integration.md`) : § 4.7 à 4.10, la livraison L2, et ce qui s'y rattache aux § 6 et 9.
- Elle garde le reste, ajusté aux décisions du 20/10 : la découverte, l'accès par appli, la conversation au format d'OpenAI, les embeddings, le journal, les limites et les clients.
- « 1re conception, § x » renvoie à un passage qui ne change pas.

**Décisions de l'auteur appliquées partout**
- **20/10**
  1. Les applis gardent leurs moteurs. AIODOS répartit leurs travaux. Conversation, vision et embeddings restent calculés par AIODOS (llama.cpp).
  2. On calcule sur toutes les machines : les miennes, celles de mes amis, celles des inconnus. Le contenu part chez elles le temps du calcul. C'est dit à l'accord de l'appli, coché par défaut et décochable.
  3. Une file comme un torrent.
  4. La passerelle est fermée par défaut et s'ouvre au premier usage. Une clé par appli, accordée dans AIODOS.
  5. Un résultat est gardé jusqu'à sa récupération, puis 7 jours.
  6. Les clients sont publiés tout de suite, sous Apache-2.0.
- **18/10** :
  - des clés, pas de pseudonymes ;
  - le demandeur reste anonyme chez les inconnus : clé éphémère par création, demande relayée ;
  - des reçus signés avant toute blockchain ;
  - une réciprocité souple ;
  - les images ne se prêtent aux amis que sur autorisation ;
  - « Rejoindre le réseau public » est désactivé par défaut ;
  - pas de version signée avant ton essai.
- **Règles de l'auteur** :
  - un fichier par fonctionnalité ;
  - pas de serveur central ;
  - des téléchargements seulement sur accord ;
  - les licences des modèles affichées et acceptées, chacun responsable.

**Sources lues**
- `main` (0831d6c) et l'arbre de travail non validé : correctifs de l'audit du 20/10, `network/body_limit.py`, `compagnon/src/relay_body.rs`, `updates/private.py`.
- La branche `claude/distracted-satoshi-342231` (fadc9dc).
- La première conception (parties 0, 1, 3 et 9) et les rapports de besoins.
- Les deux rapports du 20/10 (« Moteurs de PANDORA », « Répartition dans AIODOS »).
- Les trois conceptions et les avis des jurés.
- `docs/phase3-reseau-public.md` et `docs/phase3-verification-images.md`.
- ComfyUI 0.35.1 installé sur ce PC : `server.py`, `nodes.py`, `folder_paths.py`, `main.py`, `comfy/cli_args.py`, `app/node_replace_manager.py`, `comfy_execution/jobs.py`.

**Repères**
- « À vérifier » : ce que je n'ai pas pu confirmer sans rien lancer.
- Prix :
  - **petit** : un fichier neuf de 100 à 200 lignes ;
  - **moyen** : 2 à 4 fichiers, une migration, ou un module central touché ;
  - **gros** : le protocole entre machines change.

---

## 0. En bref

### 0.1 Le principe

1. **AIODOS partage la puissance ; il ne remplace pas les moteurs.**
   - L'appli prépare son travail pour son moteur : PANDORA convertit et remplit ses gabarits ComfyUI au format API.
   - AIODOS le range en unités et les confie aux machines qui ont ce moteur et ces modèles.
   - Il rejoue chaque unité par l'API du moteur et rend les fichiers produits.
   - Le premier moteur est ComfyUI ; le sd-server de minimax-h3-local suivra en L5.
2. **AIODOS calcule lui-même, avec llama.cpp,** la conversation, la vision et les embeddings des applis.
3. **La file se comporte comme un torrent.**
   - Elle survit à la fermeture de l'appli, à celle d'AIODOS et au redémarrage du PC.
   - On la met en pause « après l'unité en cours » ou « tout de suite », puis on la reprend, travail par travail ou en entier.
   - Aucun calcul fini n'est jeté tant que son unité n'a pas été redonnée.
4. **Le plus de machines possible en même temps.**
   - Chaque machine libre qui a le moteur et les modèles prend une unité.
   - Mes machines passent d'abord, puis mes amis après 15 s, puis les inconnus après 60 s. Les délais tombent à 0 quand aucune de mes machines ne peut prendre l'unité.
5. **Quatre accords se cumulent :**
   - celui de l'appli : cercles cochés par défaut, décochables ;
   - le lieu choisi pour le travail, qui ne peut que resserrer ;
   - les réglages généraux ;
   - celui du prêteur, qui prête son moteur cercle par cercle, désactivé par défaut pour les amis et les inconnus.
6. **Un niveau de confiance par cercle pour ce qui s'exécute :**
   - **mes machines** : le graphe de l'appli tel quel, dans le ComfyUI de l'utilisateur ;
   - **mes amis** : un graphe vérifié, fait de classes d'une liste blanche signée, dans une instance de ComfyUI lancée et verrouillée par AIODOS ;
   - **les inconnus** : jamais un graphe. Ils reçoivent une **recette signée** et ses paramètres bornés, et c'est l'exécutant qui construit le graphe, dans une instance à part.
7. **Une appli, une clé.** La passerelle reste fermée jusqu'au premier « Se connecter à AIODOS » d'une appli, qui fait apparaître la carte d'accord. C'est l'accord qui ouvre la passerelle.
8. **Deux clients publiés dès L1**, `aiodos-client` sur PyPI et sur npm, sous Apache-2.0 et sans dépendance.

### 0.2 Ce qui change par rapport à la première conception

| Sujet | Première conception | Conception finale |
|---|---|---|
| Images et vidéos des applis | AIODOS les génère (`/aiodos/v1/creations`, `/v1/images/generations`) | Les applis gardent leurs moteurs. AIODOS répartit des **travaux de moteur** (`POST /aiodos/v1/jobs`, avec `engine`). Ni création ni images au format d'OpenAI en v1 |
| Lieu de calcul | `here`, `mine`, `friends`. « Amis » jamais coché d'avance, avec une seconde confirmation | `here`, `mine`, `friends`, `public`, **cochés par défaut**, avec la phrase sur le contenu. Les quatre accords se cumulent (I4) |
| Passerelle fermée | L'appli renvoie vers les réglages | `aiodos --app-request` (canal privé de l'instance), et l'accord ouvre la passerelle |
| Garde des résultats | 7 jours | Jusqu'à la récupération, puis 7 jours |
| Clients | Copiés | Publiés dès L1 |
| Protocole entre machines | Inchangé | Change (**gros**) : plusieurs entrées par unité ; sorties en morceaux avec reprise ; boîte d'envoi ; baux qui se raccrochent ; versions par tâche ; `work/decline` |
| Transcription, bibliothèques, machines | L3, L4 | Reportées après cette v1, sans changement |

### 0.3 Les contradictions tranchées

| Sujet | Positions | Choix | Pourquoi |
|---|---|---|---|
| Ce qu'un inconnu envoie | Graphe libre limité à une liste blanche (C3) ; recettes signées (C2, les trois jurés) | **Des recettes signées.** L'exécutant construit le graphe. Les graphes restent réservés à mes machines et à mes amis | Un inconnu n'envoie aucun programme. L'objection de C3, qui faisait valoir que le convertisseur est sous GPL, tombe : le catalogue est fabriqué chez l'auteur à partir de graphes déjà au format API (« Exporter (API) » de ComfyUI, ou PANDORA). C'est une donnée : aucun convertisseur n'entre dans AIODOS ni chez l'exécutant |
| Liste des nœuds | « Cœur moins une liste noire » (C3) ; liste blanche positive (C2, jurés) | **Liste blanche positive et signée**, revue à chaque version de ComfyUI. Une version non revue ne prête qu'aux recettes | Une liste noire laisse passer tout nœud du cœur ajouté demain (`LoadImageOutput` existe déjà) |
| Instances isolées | Une seule pour les amis et les inconnus (C3) ; une par cercle (C2, juré 2) | **Une instance par cercle** (`mine`, `friends`, `public`). Celle du public ne charge jamais de paquet. Une seule tourne à la fois sur la carte | `--whitelist-custom-nodes` recharge des paquets malgré `--disable-all-custom-nodes` (`nodes.py` l.2371), et un paquet s'exécute à l'import (l.2266) |
| Modèles vus par l'instance | Liens physiques par cercle (C2) ; dossiers de l'utilisateur en lecture (C3) | **Dossiers en lecture**, par `--extra-model-paths-config`. Seuls les modèles prêtés sont adressables : jetons réécrits par empreinte, tout autre nom refusé. Pas de liens physiques en v1 | Un lien physique partage le fichier et ses droits, et ne traverse pas les volumes (juré 2). Le juré 3 les écarte |
| Graphe dans chaque unité | Recopié (C3) ; gardé une fois par travail (C1, jurés) | **Une fois par travail** (`jobs.params`) ; `TaskSpec.expand(unit)` assemble l'ordre | Recopier 40 Ko pour 256 unités, relues à chaque page de `claim_remote`, coûte cher |
| Trouver les modèles du graphe | Par l'extension du fichier (C3) ; par les listes de choix d'`object_info` (C1, juré 1) ; jetons `@model` (C2, juré 3) | **Listes de choix** des entrées de fichiers de modèles, avec des jetons `@model:<clé>` facultatifs | L'extension est un indice faible ; les listes de choix sont celles que ComfyUI valide |
| Contrat des nœuds | `object_info` complet relu chaque heure (C3) ; empreinte par classe et `core` (C1, jurés) | **Empreinte par classe** (`/object_info/{classe}`), listes de fichiers normalisées, et `core` comme voie rapide | La liste complète est lourde (à mesurer : M1). Les listes de fichiers changent d'une machine à l'autre |
| `apply_replacements` (le serveur réécrit des nœuds) | « À vérifier » (C3, juré 2) | **Vérifié : aucun effet sur un graphe accepté** | Il ne remplace que les `class_type` absents de `NODE_CLASS_MAPPINGS` (`app/node_replace_manager.py` l.61-70). Or `check` exige que chaque classe existe chez l'exécutant |
| `_meta` avant le départ | Retiré (C1) | **Remplacé par `{}`**, nœuds renumérotés | `copy_node_struct` lit `node_struct["_meta"]` (l.21-25). `to_api` de PANDORA y écrit `title`, d'où `pandora_end_frame` |
| Réponse de `renew` | Un champ `why` (C3) ; des états (C1, juré 3) | **Des états** : `running`, `resumed`, `paused`, `cancelled`, `reassigned` | Le prêteur sait quoi faire sans analyser un texte |
| Compteurs d'une unité | `lost` (C3) ; `lost` et `interrupts` (C1) ; `handouts` (C2) | **Les trois**, chacun avec son rôle (§ 2.6) | Un arrêt d'AIODOS, une disparition et une unité qui fait planter tout le monde ne se traitent pas pareil |
| Délai de grâce après le redémarrage du propriétaire | 10 min (C3) ; 15 min (C1, jurés 1 et 2) | **15 min** (`RECOVER_GRACE = 900`) | Le temps qu'un prêteur se raccroche à son renouvellement suivant, même après un trou réseau |
| Garde de la boîte d'envoi | 72 h (C3, C1) ; selon le cercle (C2, jurés 1 et 3) | **7 jours pour mes machines, 48 h pour les amis, 24 h pour les inconnus** | Mes résultats valent d'attendre un week-end ; les créations des autres ne restent pas longtemps sur le disque du prêteur |
| L'utilisateur du prêteur revient | L'unité finit si elle a moins de 15 min (C3) ; `lend_in_use_max_s` (C2, juré 3) | **Chez un prêteur ami ou inconnu** : l'interactif d'abord, l'unité est rendue sans essai, sauf s'il lui reste moins de 120 s. **Chez mes machines** : l'unité finit s'il lui reste moins de 15 min, avec un bouton pour l'arrêter. Aucune unité estimée à plus de 120 s ne part chez une machine en usage | On ne fait pas attendre un ami pour son propre PC ; sur mes machines, c'est moi qui décide |
| Équité entre applis | Ordre d'arrivée (C3) ; tri par appli (C1, C2, juré 3) | **Tri par appli** : à priorité égale, l'appli qui a le moins d'unités en cours passe d'abord | Petit (un `ORDER BY`), et un lot de 500 images de PANDORA ne bloque plus ARKLIGHT |
| Avancement réel | WebSocket maison (C1) ; `websocket-client` en L2c (C3) ; interrogation seule (juré 3) | **Interrogation seule en v1**, avancement estimé par le temps et dit « estimé » | Aucune dépendance de plus. `/api/jobs/{id}` ne donne que l'état (`comfy_execution/jobs.py`), d'où l'estimation |
| Délais par cercle | Fixes, 15 et 60 s (C3) ; réglables, à 0 si mes machines sont prises (juré 1) | **Réglables (`reach_delays`), et à 0** quand aucune de mes machines capables n'est libre | Ne pas freiner le « simultanément » |
| Forme de `where` | Cercles cumulatifs (C2) ; liste (C3) | **Tout sous-ensemble non vide** de `here`, `mine`, `friends`, `public`. `["mine"]` sans `here` veut dire « sans occuper ce PC » | Demandé par les jurés 1 et 2 |
| Défaut de `gateway_apps.reach` | `'here,mine'` (C3) | **Les quatre cercles**, comme la carte. Le lieu effectif croise ce choix, les cercles validés à l'écran (`consents`) et ceux que la version installée sert | Aligné sur la carte (juré 1), sans accorder d'avance un cercle jamais montré |
| Ouvrir une passerelle fermée | Carte anonyme « Ouvrir ? » (C2) ; `--app-request` puis la relève habituelle (C3) ; tube NDJSON qui rend la clé (C1) | **`--app-request`, puis la relève RFC 8628 habituelle** | Une seule façon de relever la clé, qui ne passe jamais par le port de l'interface. L'appli est identifiée, pas anonyme (jurés 2 et 3) |
| Clé qui signe le catalogue | La clé de publication (C2, juré 3) ; une clé à part (juré 2) | **La clé de publication, avec un contexte propre `aiodos-catalogue\n`** | C'est le motif déjà en place : `APPROVAL_CONTEXT` (`updates/manifest.py` l.48). Une signature de catalogue ne vaut jamais manifeste, et l'inverse ; il n'y a pas de clé de plus à garder |
| Sortie en morceaux | `put`, puis `result` (C3) ; manifeste d'abord, puis `put` (C2) | **Manifeste d'abord** (`work/result {manifest}`), puis `work/put` | Le propriétaire borne les tailles avant de recevoir un octet, et le même appel sert à la reprise |
| Envoi des entrées par l'appli | `PUT` reprenable (C1, C2) ; `PUT` simple (C3) | **`PUT` simple par empreinte**, 100 Mo au plus | En bouclage, à 100 Mo, la reprise n'apporte rien |
| Publication des clients | OIDC (C1, C3) ; depuis le PC, avec épinglage (C2) | **OIDC depuis `github.com/AIODOS/aiodos`** (le dépôt distant, vérifié) avec `--provenance`. Les applis épinglent version et empreinte | Aucun jeton sur un disque, et la chaîne d'approvisionnement reste vérifiable |
| Métadonnées des PNG | `--disable-metadata` (C3) ; Pillow en plus (C2, jurés) | **Les deux** | Défense en profondeur |
| Chemins dans les consignes | Toute chaîne vérifiée (C3) ; consignes multilignes exemptées (C2, juré 1) | **Exemptées** : une consigne qui contient « :// » passe | Sinon on refuserait des consignes ordinaires |
| Graphe envoyé avec `public` dans `where` | Refus (C3) ; lieu resserré (C2) | **Accepté, `where_effective` sans `public`**, avec la raison dans la réponse et dans `plan` | C'est dit à l'appli, jamais tu, et le lot avance sur les autres cercles |
| Fin de lot (doubler les dernières unités) | En v1 sur mes machines (C1) ; plus tard (C2, C3) | **Plus tard**, sur mes machines seulement | Le calcul coûte cher |
| Fichiers jamais récupérés | Effacés après 90 jours (C1) ; jamais effacés seuls (C3) | **Jamais effacés seuls** ; un badge après 30 jours ; un quota | Décision 5 |
| Pays déclaré pour les licences | Oui (C2) | **Non** : licence affichée, seconde case pour une licence restrictive, chacun responsable | Règle de l'auteur |
| Clé éphémère | Par travail (C1, C3) ; par création (18/10) | **Par travail d'appli, qui est la création au sens du 18/10.** Ses unités sont les variantes d'une même demande | Le prêteur ne relie ni deux travaux, ni un travail à mon nœud. Une clé par unité coûterait une connexion par unité sans rien cacher de plus |
| Jeton à intégrité basse, AppContainer | Obligatoires (C2) ; écartés avant mesure (juré 3) | **À mesurer (M7)**, pas d'engagement en v1 | CUDA y est incertain |

### 0.4 Ce qu'on réutilise

| Besoin | Déjà dans AIODOS |
|---|---|
| File persistante, unités, priorités, reprise au démarrage | `jobs/queue.py`, `jobs/models.py` |
| Tâche portable : calcul ailleurs, intégration chez le propriétaire | `jobs/registry.register_portable` (`compute`, `assimilate`, `requires`, `validate`, `sample`, `here`, `shared_keys`) |
| Modèle « pull », baux, rendu sans essai, résultat tardif accepté | `jobs/leases.py` (`claim_remote`, `take_back`, `release`), `network/work.py`, `network/worker.py` |
| Transfert par morceaux, avec reprise et empreinte | `work/blob`, `worker._download`, `CHUNK_RETRY_DELAYS` |
| Règles des amis | `FRIEND_MAX_HELD`, jamais le dernier essai, échecs non redonnés, mise de côté propagée au groupe, `ledger` |
| Corps bornés avant lecture | `network/body_limit.py` (`SEALED_BODY`, `computing_for_me`), `compagnon/src/relay_body.rs` |
| Programmes tués avec AIODOS | `node/children.bind` (objet de tâche Windows, `KILL_ON_JOB_CLOSE`) |
| Port fermé aux navigateurs | `imaging/engine.listen_port` |
| Dossiers de modèles de ComfyUI, empreintes interruptibles | `imaging/local_copies._known`, `imaging/catalog.sha256_of`, `file_state` |
| Fichiers modifiables par d'autres comptes | `updates/private.foreign_writers` (arbre de travail) |
| Signature à contexte propre | `updates/manifest.py` (`APPROVAL_CONTEXT`) |
| Signal d'arrêt général | `pacing.stopping()`, `registry.Interrupted` |
| Canal privé de l'instance | `POST /instance/show`, `x-aiodos-show` (`api/routes/system.py` l.55) |
| Messages redits à une machine hors ligne | `group_outbox` (migration 17) |
| Seuils de vérification des images | `docs/phase3-verification-images.md` § 4 |

### 0.5 Les livraisons

| Liv. | Nom | Ce que l'auteur peut faire ensuite |
|---|---|---|
| L0 | Préalable | — |
| L1 | Branchable (texte) | L'assistant d'ARKLIGHT PC et les textes de PANDORA, par les clients publiés |
| L2a | Socle de la file | « Annuler » et « Pause » marchent pour de vrai, aussi pour l'indexation ; un arrêt d'AIODOS ne coûte plus d'essai |
| L2b | ComfyUI sur mes machines | Les travaux ComfyUI de PANDORA tournent sur ce PC et sur mes autres machines en même temps |
| L2c | Confort | ComfyUI lancé sans fenêtre là où il n'est pas ouvert ; PANDORA sur un PC sans ComfyUI ; reprise depuis l'historique |
| M | Mesures | Les seuils et les coûts mesurés, avant L3 et L4 |
| L3 | Mes amis | Mes amis prêtent leur ComfyUI, dans une instance verrouillée |
| L4 | Les inconnus | Le réseau public calcule les recettes d'images, vérifiées |
| L5 | Ensuite | sd-server (le H3 local de PANDORA) ; copie des modèles entre mes machines |

### 0.6 Vérifié dans le code

**La file et les baux (AIODOS)**
- **Une unité locale porte `node` = ce nœud et `lease_until` NULL ; une unité confiée ailleurs porte `lease_until`** (`queue.claim_next`, `leases.claim_remote`). C'est ce qui permet à `recover` de les distinguer.
- **Un arrêt d'AIODOS coûte un essai à chaque unité locale en calcul**, sauf si elle attendait dans `pacing` (`Interrupted`, puis `release_unit`).
  - `Executor.stop` (l.77) annule la boucle, pas les unités.
  - `queue.recover` remet en file sans rendre l'essai et efface `node` : le résultat tardif d'un prêteur est alors refusé par `take_back`.
- **`leases.renew` ne prolonge qu'une unité `running`.** Après un bail échu, l'unité est revenue en file en gardant `node`, et `take_back` accepterait encore son résultat. Mais le prêteur reçoit `ok: false` et jette son calcul (`worker._renew`).
- **`Worker.stop` lève `a.stop` de chaque unité** : une tâche qui l'écoute est rendue (`work/release`). Les autres meurent avec le processus.
- **Rien ne survit chez le prêteur.**
  - `Worker.start` efface `paths.work_dir()` (`data\tmp\work`).
  - `_send_result` fait 3 essais à 3 s d'écart, puis abandonne.
- **`work.lease` exige `caps.work_api == WORK_API`** (= 1).
- **La règle « un ami seulement pour un modèle que je ne sais pas calculer »** vient de `mine_only` et de `here` (`work.py`, `acceptable`). Elle ne vaut que pour `generate_image` (`friends_out="images_from_friends"`, `friends_in="lend_images_friends"`).
- **`db.SCHEMA_VERSION` vaut 17.**

**Ailleurs dans AIODOS**
- Le dépôt distant est `https://github.com/AIODOS/aiodos.git`.

**ComfyUI 0.35.1 sur ce PC**
- **Chemins**
  - `folder_paths.annotated_filepath` (l.257) teste `endswith("[output]")`, `"[input]"` et `"[temp]"` : **sans espace obligatoire**.
  - `LoadImageOutput` (`nodes.py` l.1859) lit le dossier `output`.
- **Nœuds ajoutés**
  - `--whitelist-custom-nodes` (`cli_args.py` l.215) recharge les paquets nommés malgré `--disable-all-custom-nodes` (`nodes.py` l.2371).
  - Un paquet ne peut pas remplacer une classe du cœur : `ignore=base_node_names` (l.2357, l.2381).
- **Dossiers et base**
  - `--base-directory` (l.70) porte aussi `custom_nodes`.
  - Les sorties sont enregistrées dans la base de ComfyUI (`register_output_files`, `main.py` l.418), d'où `--database-url sqlite:///:memory:`.
- **Mémoire et cache** : `POST /free {"free_memory": true}` réinitialise l'exécuteur, donc le cache des nœuds (`e.reset()`, `main.py` l.430). La boucle de ComfyUI l'applique entre deux travaux.
- **Routes**
  - `POST /prompt` accepte un `prompt_id` en UUID canonique (l.1090-1101).
  - `POST /api/jobs/{id}/cancel` annule un seul travail, en cours ou en attente (l.947-971).
  - `/interrupt` sans `prompt_id` est global (l.1186).
  - `/api/jobs/{id}` ne donne que l'état : `pending`, `in_progress`, `completed`, `failed` ou `cancelled`.
- **Remplacements de nœuds** : `apply_replacements` ne remplace que les classes inconnues du serveur, et `copy_node_struct` lit `_meta` (`app/node_replace_manager.py`).

---

## 1. Principe et hors-périmètre

### 1.1 Les règles

1. **Partager la puissance, pas remplacer les moteurs.**
   - AIODOS reçoit un travail prêt à jouer. Il ne convertit rien et ne choisit aucun modèle à la place de l'appli.
   - Exception : pour les inconnus, il ne rejoue que des recettes de son catalogue.
2. **Ce qu'AIODOS calcule lui-même** : la conversation, la vision et les embeddings, avec llama.cpp. Rien de cela ne passe par un moteur d'appli.
3. **Le plus de machines possible en même temps**, dans l'ordre des cercles, avec des délais courts et réglables.
4. **Le contenu ne part que là où les quatre accords le permettent.** Un travail peut resserrer le lieu, jamais l'élargir.
5. **Des octets, jamais des chemins ni des adresses**, chez le demandeur comme chez celui qui calcule.
6. **Le travail d'un ami ou d'un inconnu ne tourne jamais dans le ComfyUI de l'utilisateur**, mais dans l'instance verrouillée de son cercle.
7. **Rien n'est téléchargé ni installé par un travail.** Une machine qui n'a pas le modèle n'est pas choisie.
8. **Celui qui calcule a accepté la licence** du modèle qu'il prête, pour ce cercle.
9. **Ne jamais dire qu'on a fait ce qu'on n'a pas fait.** Chaque fichier rendu dit :
   - la machine (son nom, ou « réseau public ») et le cercle ;
   - la version du moteur ;
   - le contrôle qu'il a passé.
10. **Les règles de l'auteur** (en tête du document).

### 1.2 Invariants

I1 à I12 sont ceux de la 1re conception (§ 1.2), avec I2, I3, I4 et I9 réécrits. I13 à I25 sont nouveaux. Chacun a son essai (§ 7.5).

| # | Invariant |
|---|---|
| I1 | La passerelle n'écoute que sur 127.0.0.1. |
| I2 *(réécrit)* | Elle reste fermée tant qu'aucune appli n'est autorisée. Seule l'interface d'AIODOS l'ouvre, par la carte d'accord ou par les réglages. |
| I3 *(réécrit)* | Aucun appel au cloud, quelle que soit la clé. Aucun nœud du cloud (`api_node` vrai, ou `python_module` sous `comfy_api_nodes`), dans aucun cercle. Aucun `extra_data` transmis à un moteur. |
| I4 *(réécrit)* | Une unité ne part chez une machine que si son cercle est à la fois accordé à l'appli (et validé à l'écran), dans le `where` du travail, permis par les réglages généraux, et prêté par cette machine pour ce moteur et ces modèles. Les réglages généraux sont : « Tout garder sur mes machines » coupé, et, pour le public, le réseau rejoint et la réception permise. |
| I5 | Une clé d'appli ne télécharge, ne supprime, ne charge ni ne décharge aucun modèle, et ne règle jamais le contexte. |
| I6 | Aucune page web n'atteint la passerelle. |
| I7 | Aucune appli ne fait lire un chemin de fichier à AIODOS : elle envoie des octets. |
| I8 | Aucun contenu au journal, ni dans les tables du prêteur : ni consigne, ni graphe, ni image, ni entrée, ni sortie. |
| I9 *(réécrit)* | Ne quittent jamais ce PC : le nom de l'appli, sa clé, le libellé, `client_ref`, les noms d'origine des fichiers, `_meta` et les identifiants de nœuds d'origine. Un inconnu reçoit la demande sous une clé éphémère par travail, par un relais. |
| I10 | Une appli ne voit que ses travaux, ses entrées et ses fichiers. Ceux d'une autre répondent 404. |
| I11 | Un droit ne s'accorde que dans l'interface d'AIODOS. |
| I12 | Une requête peut resserrer le lieu ou baisser la priorité, jamais les élargir. |
| I13 | Un graphe ne désigne aucun fichier hors des jetons d'entrée. Chemins absolus, `..`, adresses et annotations `[output]`, `[input]` et `[temp]` sont refusés chez le demandeur **et** chez celui qui calcule. |
| I14 | Celui qui calcule revérifie tout ce qu'il reçoit avec ses propres définitions de nœuds, sans se fier au demandeur. |
| I15 | Pour un ami ou un inconnu, le moteur tourne dans l'instance AIODOS de son cercle (§ 3.7). Celle du public ne charge aucun paquet ajouté. |
| I16 | Un prêteur ne calcule pour un cercle que les modèles qu'il a lui-même installés et cochés pour ce cercle, licence affichée. |
| I17 | AIODOS n'annule dans un moteur que ses propres travaux, par leur identifiant : jamais `/interrupt` sans `prompt_id`, jamais `/queue {"clear"}`. |
| I18 | Un fichier rendu à une appli n'est effacé qu'après avoir été récupéré en entier, puis 7 jours, ou par un geste de l'utilisateur ou de l'appli. |
| I19 | Aucun travail ne fait télécharger ni installer quoi que ce soit (modèle, nœud, paquet). Aucune route de la passerelle ne relaie une requête arbitraire vers un ComfyUI. |
| I20 | Quitter l'appli, quitter AIODOS ou redémarrer le PC ne fait perdre ni un travail en file, ni un résultat déjà calculé ailleurs. |
| I21 | Un inconnu n'envoie jamais de graphe : une recette du catalogue signé et des paramètres bornés. L'exécutant construit le graphe. |
| I22 | Rien n'est calculé pour une autre machine sans l'accord de mon propre prêt pour ce cercle et ce moteur. Pour les amis et les inconnus, il est désactivé par défaut. |
| I23 | Tout résultat venu d'ailleurs passe les contrôles de forme ; celui d'un ami ou d'un inconnu, en plus les contrôles de contenu. Chacun dit le contrôle qu'il a passé. |
| I24 | AIODOS ne prête pour autrui qu'une installation de ComfyUI, et des modèles, que seuls l'utilisateur, le système et les administrateurs peuvent modifier (`foreign_writers`). |
| I25 | Un travail ne lit ni les fichiers d'un autre travail, ni ceux du prêteur. Le cache des entrées n'est jamais partagé entre deux demandeurs. |

### 1.3 Contre qui on se défend

Ceci s'ajoute au § 1.3 de la 1re conception.

**Un demandeur malveillant**, ami ou inconnu, qui cherche à :
- lire les créations du prêteur (`LoadImage` avec `[output]`, `LoadImageOutput`) ;
- appeler un service payant (nœuds du cloud, `extra_data`) ;
- faire installer du code (ComfyUI-Manager) ;
- épuiser la carte ou le disque ;
- viser une faille d'un décodeur ;
- apprendre par le cache des nœuds ce qu'un autre a demandé.

**Un prêteur tricheur**, qui rend :
- une image unie ;
- un calcul bâclé (moins de pas) ;
- le résultat d'un autre modèle ;
- ou un résultat qu'il a inventé.

**Un prêteur curieux**, qui :
- lit les consignes et les images (c'est inévitable, et la carte le dit) ;
- ou cherche qui demande (parade : clé éphémère et relais).

**Des milliers de fausses clés.** Parades :
- la redondance pour les nouveaux venus ;
- peu d'unités à la fois ;
- 20 % des unités d'un travail au plus chez des nouveaux venus ;
- une réputation calculée ici.

**Hors de portée.** Une faille du cœur de ComfyUI, de torch, d'un pilote ou de Pillow ; un prêteur qui garde une copie ; un programme malveillant du même compte. C'est dit au prêteur et à l'utilisateur.

### 1.4 Dans la v1, plus tard, jamais

**Dans la v1 (L1 à L4)**
- Tout le L1 de la 1re conception, plus la connexion quand la passerelle est fermée.
- Les travaux de moteur ComfyUI :
  - plusieurs graphes, plusieurs unités et plusieurs entrées ;
  - les modèles désignés par leur empreinte ;
  - la file comme un torrent ;
  - les résultats gardés jusqu'à leur récupération, puis 7 jours.
- Mes machines (L2), mes amis (L3), les inconnus pour les images, en recettes (L4).
- Les deux clients publiés.

**Plus tard**
- sd-server ; la copie des modèles entre mes machines (L5).
- La vidéo calculée par des inconnus, après la mesure M2.
- L'avancement pas à pas par WebSocket.
- La fin de lot sur mes machines.
- Un dossier de modèles par cercle.
- Un compte Windows séparé pour l'instance publique.
- Le convertisseur de gabarits dans le client, une fois PANDORA sous Apache-2.0, si une autre appli le demande.
- La transcription, les bibliothèques et la liste des machines (1re conception, § 4.11 et 4.12).
- Les crédits monnayables.

**Jamais, ou pas sans une nouvelle décision**
- AIODOS qui génère lui-même les images des applis.
- Un graphe venu d'un inconnu ; un paquet de nœuds chez un inconnu.
- Un nœud du cloud ; un téléchargement par un travail.
- Un même graphe coupé entre plusieurs machines.

---

## 2. Le travail de moteur et la file

### 2.1 Vocabulaire

- **Moteur** : un programme installé par le propriétaire d'une machine, piloté par AIODOS par son API (`comfyui`, puis `sdserver`). AIODOS ne le livre ni ne l'installe.
- **Adaptateur** : le fichier d'AIODOS qui déclare un moteur et y rejoue une unité.
- **Travail de moteur** : ce qu'une appli dépose, soit :
  - un moteur ;
  - 1 à 64 graphes, ou une recette ;
  - des unités, des entrées, des modèles ;
  - un lieu, une priorité, des bornes.
- **Unité** : un `POST /prompt`, c'est-à-dire un graphe et ses retouches, joué une fois. Elle produit un ou plusieurs fichiers.
- **Recette** : un graphe au format API tiré d'un gabarit officiel, avec des paramètres nommés et bornés. Elle est publiée dans le catalogue signé, livré avec AIODOS (§ 3.9).
- **Contrat** : les empreintes des définitions des classes de nœuds employées (§ 3.3).
- **Propriétaire** : la machine où tourne l'appli, qui garde le travail et les fichiers.
- **Prêteur** : la machine qui calcule une unité, ce PC compris.
- **Cercle** : `here` (ce PC), `mine` (mes autres machines), `friends`, `public`.

### 2.2 La requête et ce qu'en garde la file

**La requête** (`POST /aiodos/v1/jobs`, § 6.7) :

```json
{ "engine": "comfyui",
  "label": "Moods · Séq. 3",
  "client_ref": "pandora:mood:5f2c9a",
  "where": ["here", "mine", "friends", "public"],
  "priority": "normal",
  "graphs": [ {
      "4":  { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "@model:ckpt" } },
      "6":  { "class_type": "CLIPTextEncode", "inputs": { "text": "", "clip": ["4", 1] } },
      "10": { "class_type": "LoadImage", "inputs": { "image": "@in:ref0" } },
      "3":  { "class_type": "KSampler", "inputs": { "seed": "@seed", "steps": 30, "…": "…" } },
      "9":  { "class_type": "SaveImage", "inputs": { "images": ["8", 0], "filename_prefix": "mood" } } } ],
  "inputs": { "ref0": { "sha256": "9f2c…", "type": "image/png" } },
  "models": { "ckpt": { "sha256": "31e3…", "folder": "checkpoints" } },
  "items": [ { "graph": 0, "label": "Mood 1", "seed": 1201, "set": [["6", "text", "Salon baigné de lumière dorée"]] },
             { "graph": 0, "label": "Mood 2", "seed": 1202, "set": [["6", "text", "Cuisine de nuit, néons"]] } ],
  "requires": { "min_version": "0.30.0", "vram_mib": 7000 },
  "bounds": { "unit_seconds": 1800, "output_mb": 200, "wait_s": 86400 },
  "strict": false }
```

Une **recette** remplace `graphs` et `models` :

```json
{ "recipe": { "id": "comfy-templates/image_sdxl_t2i", "version": "0.11.48-1" },
  "items": [ { "params": { "prompt": "Cabane au bord d'un lac, aube", "negative": "",
                           "width": 1024, "height": 1024, "seed": 101, "steps": 30 },
               "inputs": [] } ] }
```

**Les champs**

| Champ | Sens | Règles |
|---|---|---|
| `engine` | le moteur | `comfyui` (L2b), `sdserver` (L5). Droit `engines:<nom>` exigé |
| `graphs[]` | graphes **au format API, déjà remplis** | jamais le format éditeur (`graph_not_api`). 2 Mo par graphe, 8 Mo en tout, 64 graphes au plus. Chaque nœud vaut `{class_type, inputs[, _meta]}`. 2000 nœuds pour mes machines, 500 chez un ami |
| valeurs du graphe | ce que reçoivent les nœuds | textes (20 000 caractères au plus), nombres, booléens, liens `[nœud, sortie]` et listes de ceux-ci |
| `@in:<id>` | jeton d'entrée | la seule façon de donner un fichier. Valeur de texte exactement égale ; l'entrée est déclarée dans `inputs` et présente dans le dépôt de l'appli |
| `@model:<clé>` | jeton de modèle, facultatif | renvoie à `models[clé]`. Sans jeton, AIODOS relève lui-même les noms de modèles (§ 3.4) |
| `@seed` | jeton de graine, facultatif | remplacé par la graine de l'unité |
| `inputs` | entrées par empreinte | envoyées avant le travail (`PUT /aiodos/v1/inputs/{sha256}`, § 5.1) |
| `models` | `"auto"` (défaut) ou `{clé: {sha256?, folder, name?}}` | § 3.4 |
| `items[]` | une unité par élément : `graph` (indice), `label`, `seed`, `set` | 256 unités au plus. `set` remplace une valeur **littérale déjà présente**, du même type JSON, jamais un lien ni un `class_type`. Ses valeurs passent les mêmes contrôles |
| `repeat` | à la place d'`items` : `{count, vary: [{node, input, step}]}` | `count` ≤ 256, `valeur + i × step` |
| `recipe`, `items[].params` | recette et paramètres | bornés par la recette (`param_out_of_range`) |
| `where` | sous-ensemble non vide de `here`, `mine`, `friends`, `public` | compris dans l'accord, sinon 403 `where_denied`. Absent : l'accord entier |
| `priority` | `high`, `normal` ou `low` | `high` seulement si l'accord l'a donné |
| `requires` | version minimale, mémoire vidéo estimée | facultatif |
| `bounds` | durée d'une unité, poids des sorties, attente d'une machine | bornés par les limites de l'appli et par celles du prêteur |
| `strict` | contrat strict même chez mes machines | toujours vrai pour les amis |
| `label`, `client_ref` | libellé affiché ; référence de l'appli, aussi clé d'idempotence | ne quittent jamais ce PC (I9) |
| champ inconnu | — | 400 `invalid_request` |

**Ce qu'AIODOS vérifie avant la mise en file** (`jobs/kinds/engine.normalize`, en s'appuyant sur `engines/comfy_graph.py`, qui ne contient que des fonctions pures)
1. **Le droit, le lieu, la priorité et les quotas.**
2. **La forme**
   - Les graphes : liens vers des nœuds existants, pas de cycle, tailles.
   - Les jetons : chacun déclaré, et chaque entrée présente.
   - Les retouches.
3. **Les valeurs interdites** (§ 4.3) et les nœuds du cloud.
   - Avec les définitions du ComfyUI d'ici, ou de la machine de référence (L2c).
   - Sans définitions, seuls les jetons et les annotations sont vérifiés ici. La règle entière s'applique de toute façon chez celui qui calcule (I14).
4. **Les modèles** : chacun est résolu en empreinte. Pour mes machines seulement, un nom, un dossier et une taille suffisent (§ 3.4).
5. **Le contrat de chaque graphe** (§ 3.3).
6. **Le lieu effectif**
   - `public` est retiré de `where_effective` si le travail n'est pas une recette.
   - `friends` est retiré si un graphe sort de la liste blanche des amis ou n'a pas de contrat.
   - La raison est rendue (`where_reason`).
7. **Une machine possible.** Au moins une machine des cercles effectifs a déjà annoncé ce moteur et ces modèles.
   - Sinon : 409 `model_unavailable` (avec la liste des modèles) ou 409 `engine_unavailable`.
   - Exception : si `public` reste dans le lieu effectif, le travail attend `bounds.wait_s`.

**Ce que garde la file**
- **`jobs.params`** (chez le propriétaire seulement) :
  - les graphes ;
  - les entrées, avec leurs empreintes, tailles et types ;
  - les modèles résolus, `[{at: [graphe, nœud, entrée], folder, name, sha256, size}]` ;
  - le contrat de chaque graphe, les nœuds de sortie, les bornes, `strict`, `where_effective`.
- **`units.payload`**, petit :

  ```json
  { "engine": "comfyui", "api": 1, "graph": 0, "item": 12, "seed": 1213,
    "set": [["6", "text", "…"]], "model": "comfyui:3fd1…" }
  ```

  `model` vaut `comfyui:` suivi de l'empreinte de la liste triée des empreintes de modèles du graphe. `queue.not_here` et `registry.runnable_here` regroupent déjà par (tâche, modèle) : rien à y changer.

**L'ordre envoyé** est assemblé au moment de confier l'unité, par `TaskSpec.expand(unit)`, qui lit `jobs.params` par un petit cache en mémoire par travail :

```json
{ "engine": "comfyui", "api": 1, "run_uuid": "4f0c7c1e-…",
  "graph": { "1": { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "@model:0" }, "_meta": {} },
             "2": { "…": "…" } },
  "inputs": [ { "slot": "in-1", "sha256": "9f2c…", "size": 1648221, "type": "image/png" } ],
  "models": [ { "token": "@model:0", "sha256": "31e3…", "folder": "checkpoints", "size": 6938078334 } ],
  "contract": { "comfyui": "0.35.1", "core": "c0a1…", "classes": { "KSampler": "a41c…" } },
  "outputs": ["7"], "bounds": { "unit_seconds": 1800, "output_mb": 200 } }
```

- **Les nœuds sont renumérotés** de façon déterministe : `"1"`, `"2"`… dans l'ordre des identifiants d'origine, liens compris. La correspondance est recalculée à l'assimilation.
- **`_meta` est remplacé par `{}`.** Les jetons et les retouches sont appliqués.
- **Les noms de modèles** ne partent que vers mes machines (`name` en plus).
- **Pour une recette**, l'ordre porte `{recipe: {id, version, digest}, params, inputs}`, et pas de graphe.
- **`run_uuid`** est tiré par le propriétaire à chaque remise, puis gardé dans `units.run_uuid`. Il reste le même si l'unité se raccroche à la même machine (§ 2.5). Celui qui calcule l'emploie comme `prompt_id`.
- **`shared_keys`** pour un ami ou un inconnu : `engine`, `api`, `run_uuid`, `graph` (ou `recipe` et `params`), `inputs`, `models` sans noms, `contract`, `outputs` et `bounds`.

### 2.3 Découper un lot

- **AIODOS ne coupe pas un graphe.** L'appli découpe, comme PANDORA le fait déjà : un plan, une variation, un clip.
- **Deux formes**
  - `items` : jusqu'à 256 unités, chacune sur un des graphes (les 7 plans d'une séquence : 7 graphes et 7 éléments).
  - `repeat` : N variations d'un même graphe.
- **On conseille une image par unité** (`batch_size` = 1) pour mieux répartir.
- **Les entrées communes** ne passent qu'une fois par machine, grâce au cache (§ 5.1).
- **Les unités sont indépendantes.** Un enchaînement (agrandir après avoir créé) fait deux travaux.
- **Ressource** : `gpu`, donc une unité de moteur à la fois par machine (`DEFAULT_SLOTS`).

### 2.4 Où calculer : cercles, ordre, délais

**Cercles effectifs d'une unité** (`units.reach`) : l'intersection de cinq ensembles.
- Les cercles accordés à l'appli et validés à l'écran (`gateway_apps.reach` ∩ `consents`).
- `where_effective`.
- Les réglages généraux.
  - `apps_keep_mine` (« Tout garder sur mes machines », désactivé par défaut) retire `friends` et `public`.
  - `public` exige `public_network` (désactivé par défaut, 18/10) et `public_receive` (« Faire calculer mes applis par des inconnus », coché par défaut une fois le réseau rejoint, décision 2).
- Ce que l'unité a vécu : deux disparitions (`lost` ≥ 2) la ramènent à `here` et `mine`.
- Ce que le prêteur prête : vérifié à chaque demande de bail (§ 3.5).

**Ordre de service**
- **Le modèle « pull » ne change pas** : le propriétaire sert toute machine qui demande, dans l'ordre de la file (§ 2.9). Il applique ces délais :
  - à mes machines et à l'exécuteur local : tout de suite ;
  - à un ami : après `reach_delays.friends` (15 s) passées en file depuis `units.queued_at` ;
  - à un inconnu : après `reach_delays.public` (60 s).
- **Les délais tombent à 0** quand aucune de mes machines capables n'est libre : aucune machine à moi, ni ce PC, à la fois en ligne, ayant annoncé ce moteur et ces modèles, et sans unité de moteur en cours. `work.mine_free(need)` le lit dans `engine_inventory`, dans les unités tenues et dans `peers.online`.
- **L'exécuteur local** ne prend que les unités dont `reach` contient `here`. `["mine"]` sans `here` épargne ce PC.

**Les règles des amis valent aussi pour les inconnus** :
- jamais le dernier essai ;
- une unité rendue en échec par une machine ne lui est jamais redonnée ;
- `FRIEND_MAX_HELD` (4 pour un ami ; 1 puis 2 pour un inconnu) ;
- 60 demandes par minute pour un ami ; 20 par clé et 200 en tout pour les inconnus.

**Chez le prêteur, `worker._candidates`** trie par cercle (mes machines, puis amis, puis public), puis par réciprocité (`received_s − given_s`) dans chaque cercle. C'est la règle de `phase3-reseau-public.md` § 4.

**Plus de règle « un ami seulement pour ce que je ne sais pas calculer »** pour `engine_unit`. Elle reste pour `generate_image`, les images d'AIODOS lui-même, qui ne changent pas.

### 2.5 La file comme un torrent

**États d'un travail, vus par l'appli** : `queued`, `in_progress`, `pausing`, `paused`, `cancelling`, `completed`, `failed`, `cancelled`.
- En base, `jobs.status` reste `queued`, `running`, `paused`, `done`, `failed` ou `cancelled`.
- `pausing` et `cancelling` se lisent par les unités encore en calcul.
- `failed` seulement si aucune unité n'est faite ; sinon `completed`, avec `failed` > 0.

**Étapes d'une unité** (`units.progress.stage`) : `queued`, `waiting_machine`, `sending_inputs`, `loading`, `running`, `receiving`, `checking`, `done`.

**Commandes** (API, client, vue Travaux) :

| Commande | Effet ici | Effet chez un prêteur | Ce qu'on perd |
|---|---|---|---|
| Pause après l'unité en cours (**par défaut**) | Plus aucune unité ne part. Celles en cours finissent et leurs fichiers arrivent | `renew` répond `running` | rien |
| Pause tout de suite | `jobs.pause_mode = 'now'`. Les unités locales reçoivent `stop` (raison `pause`) et sont rendues sans essai | `renew` répond `paused` (30 s au plus). Le prêteur annule dans son moteur, puis `work/release`, sans essai | le calcul en cours. L'interface le chiffre : « 2 calculs perdus, environ 6 min » |
| Reprendre | Retour en file, au rang de la date de création | les unités repartent, si possible chez la même machine (cache) | — |
| Annuler | Unités en file annulées ; `stop` aux unités locales ; `cancelled_by` posé. Les fichiers déjà rendus restent | `renew` répond `cancelled` ; `take_back` refuse le résultat | le calcul en cours |
| Priorité | `high`, `normal`, `low` | aucun effet sur ce qui calcule | — |
| Tout mettre en pause (appli : ses travaux ; AIODOS : `queue_paused`) | `claim_next` et `claim_remote` ne donnent plus rien | selon « après » ou « tout de suite » | idem |

Une appli n'agit que sur ses propres travaux (I10). L'utilisateur, dans AIODOS, agit sur tous.

**Les scénarios**

| Ce qui arrive | Ce qui se passe | Où |
|---|---|---|
| L'appli se ferme | Rien : le travail est dans AIODOS, les fichiers attendent leur récupération | § 5.3 |
| L'appli revient | `jobs(active=True)` ou `jobs(client_ref=…)` retrouve ses travaux ; elle récupère les fichiers dont `fetched` est faux. Renvoyer le même `client_ref` rend le même travail | § 6.7 |
| On quitte AIODOS pendant des calculs | Une question : « 3 calculs en cours, dont une vidéo à 70 %. [Attendre la fin] [Quitter quand même] [Annuler] ». L'arrêt de Windows ne demande rien | `jobs/shutdown.py` |
| AIODOS s'arrête chez le propriétaire | Les unités locales reçoivent `stop` (raison `shutdown`) et sont rendues sans essai (`interrupts` + 1). Un arrêt brutal est rattrapé par `recover` | `jobs/shutdown.py`, `queue.recover` |
| AIODOS redémarre chez le propriétaire | Unités locales : en file, essai rendu, `interrupts` + 1. Unités confiées ailleurs : elles **gardent leur machine et `run_uuid`**, avec un bail de grâce de 15 min (`RECOVER_GRACE`) ; le prêteur se raccroche, ou livre depuis sa boîte d'envoi | `queue.recover` corrigé |
| … et le ComfyUI Desktop de ce PC a fini pendant l'arrêt | Au départ de l'unité, `recover(run_uuid)` lit `/history/{run_uuid}` et rapatrie les sorties sans recalcul (L2c). Une instance AIODOS meurt avec AIODOS (`node/children.bind`) : rien à reprendre | `engines/comfyui.recover` |
| Le PC redémarre | AIODOS démarre avec la session (`node/autostart.py`, `--background`) et reprend seul | — |
| Le PC du propriétaire va se mettre en veille | Sur secteur, AIODOS l'empêche tant que des unités de moteur tournent, ici ou ailleurs (`SetThreadExecutionState`, `keep_awake_jobs`, vrai). Sur batterie, il laisse faire | `node/keep_awake.py` |
| Coupure de moins de 5 min | Le bail d'une unité de moteur dure 300 s et se renouvelle toutes les 30 s | `TaskSpec.lease_s`, `renew_s` |
| Coupure plus longue | Le bail échoit : l'unité revient en file **en gardant sa machine** (`lost` + 1, essai rendu). Tant qu'elle n'est pas redonnée, le renouvellement suivant la reprend (`resumed`) et le calcul continue | `leases.expire`, `leases.renew` |
| Le propriétaire est éteint quand le prêteur finit | Le résultat attend dans la boîte d'envoi du prêteur (7 j, 48 h ou 24 h selon le cercle), même après un redémarrage, et part au retour du propriétaire | `network/outbox.py` |
| AIODOS s'arrête chez le prêteur | Arrêt propre : `work/release` pour chaque unité commencée, sans essai ; les sorties déjà en boîte d'envoi y restent. Au redémarrage, pour chaque ligne de `work_assignments` : sortie dans la boîte d'envoi si le Desktop l'a finie (`/history`), sinon `work/release` | `network/assignments.py` |
| Le prêteur disparaît | L'unité revient en file à l'échéance et part ailleurs | `leases.expire` |
| L'utilisateur d'un prêteur ami ou inconnu revient | L'interactif d'abord : l'unité est annulée dans le moteur et rendue sans essai, sauf s'il lui reste moins de `lend_in_use_max_s` (120 s, estimation). Aucune unité estimée à plus de 120 s ne part chez une machine en usage | `network/worker.py`, `work.can_serve` |
| Je reviens sur une de mes machines | L'unité finit si elle tourne depuis plus de 30 s et qu'il lui reste moins de 15 min ; sinon elle est rendue. Bouton « Arrêter le calcul de PANDORA » | `network/worker.py` |
| Une conversation commence sur une machine qui calcule | Même règle que le retour de l'utilisateur. La conversation part sur une autre de mes machines si `placement` le permet ; sinon elle attend, ou l'utilisateur arrête l'unité | `engines/gpu_share.py` |

### 2.6 Les compteurs d'une unité

| Colonne | Monte quand | Effet |
|---|---|---|
| `attempts` (existant) | une unité est prise (`claim`). Rendu par `release`, `decline`, `expire` et `recover`, sauf au-delà des seuils | `MAX_ATTEMPTS` = 2 : seul un vrai échec du calcul compte. Le dernier essai ne part jamais chez un ami ni chez un inconnu |
| `interrupts` (neuf) | AIODOS s'arrête chez le propriétaire pendant que l'unité calcule ici | au-delà de 5 (`MAX_INTERRUPTS`), l'essai n'est plus rendu : une unité qui fait planter AIODOS finit en échec |
| `lost` (neuf) | un bail échoit sans nouvelles | à 2, l'unité ne part plus que chez mes machines ; à 5 (`MAX_LOST`), elle échoue (`lost`) |
| `handouts` (neuf) | chaque remise, ici ou ailleurs ; jamais rendu | à 8 (`MAX_HANDOUTS`), elle échoue (`no_progress` : « Aucune machine n'a pu la finir ») |

### 2.7 Plusieurs machines en parallèle

- Chaque machine libre prend une unité de moteur à la fois. Un lot de 16 se fait sur autant de machines qu'il y en a qui ont le moteur et les modèles.
- Les entrées communes passent une fois par machine.
- Pas de fin de lot (doubler les dernières unités) en v1. La redondance ne sert qu'à vérifier les inconnus.
- Pour les inconnus, ce n'est plus seulement la machine libre qui demande : le travail disponible est annoncé par capacité (L4, § 3.5).

### 2.8 L'avancement, honnêtement (`jobs/estimates.py`)

- **La part d'une unité en cours** vaut `p = min(0,95 ; écoulé / durée habituelle)`. La durée habituelle se cherche dans cet ordre :
  1. la médiane des unités déjà faites du même travail, sur cette machine, puis sur toutes ;
  2. la médiane des unités de même signature sur cette machine (empreinte des classes et des tailles, `engine_durations`) ;
  3. la valeur `typical_seconds` de la recette ;
  4. sinon `null` : « Premier calcul de ce genre : durée inconnue ; 1 min 12 écoulée ».
- **Le prêteur envoie son `p`** et son étape à chaque renouvellement (`work/renew {progress}`).
- **`percent` du travail** vaut `100 × (faites + Σ p) / total`.
- **L'ETA** vaut `restantes / débit`, où le débit est `Σ 1/durée` de chaque machine active. Affichage : « environ 6 min, 4 machines ».
- **`estimated: true`** tant que c'est une estimation, c'est-à-dire toujours en v1.

### 2.9 Priorité et équité

- `high`, `normal` et `low` deviennent `URGENT`, `NORMAL` et `BACKGROUND`.
- `claim_next` et `claim_remote` trient par :
  1. la priorité ;
  2. le nombre d'unités en cours de la même appli (`owner_app`, et « aiodos » pour ses propres travaux) ;
  3. la date de création.
- Les conversations passent toujours devant (`GPU_BOUND`).

---

## 3. Les moteurs : déclarer, apparier, rejouer

### 3.1 L'interface d'un adaptateur (`engines/registry.py`)

```python
class Adapter(Protocol):
    name: str                      # "comfyui"
    api: int                       # version du format d'unité de ce moteur
    def detect(self) -> Found | None                       # installation, version, instance ouverte
    def inventory(self, circle, stop) -> Inventory         # version, classes, modèles, mémoire vidéo
    def check(self, order, circle, inv) -> None            # revérifie l'ordre ici ; lève Refused(reason)
    def run(self, order, inputs, folder, stop, progress) -> list[Output]
    def recover(self, run_uuid) -> list[Output] | None     # sorties d'un calcul déjà fait
    def cancel(self, run_uuid) -> None                     # ce travail seulement (I17)
    def free(self, mine: bool) -> None                     # rendre la mémoire vidéo
    def fingerprint(self) -> dict                          # version, torch, carte, options
```

- `stop` vient de `jobs/control.py`. C'est un `threading.Event` qui porte sa raison (`cancel`, `pause`, `shutdown`, `user_back`, `chat`, `bounds`) et qui suit aussi `pacing.stopping()`.
- `jobs/kinds/engine.py` est le seul à s'en servir. Il enregistre la tâche portable `engine_unit` et appelle l'adaptateur nommé par l'ordre.

### 3.2 Déclarer ComfyUI

**Le trouver** (`engines/comfy_find.py`)
1. **L'installation**, dans l'ordre :
   - le réglage `engines_comfyui_path` ;
   - Comfy Desktop : `%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs\<nom>\`, avec `ComfyUI\`, `standalone-env\python.exe` et `manifest.json` ;
   - la version portable : `python_embeded\python.exe`.
2. **L'instance ouverte** : l'adresse réglée (`engines_comfyui_url`), puis 127.0.0.1:8188, puis :8000.
   - Une connexion TCP de 0,5 s, puis `GET /system_stats` avec 8 s de délai (le premier appel réveille CUDA).
   - On garde `comfyui_version`, `pytorch_version`, la mémoire vidéo de la première carte et `argv`.
   - On note le programme qui tient le port (`gateway/caller.py`).
3. **Le partage des modèles** : `imaging/local_copies._known` connaît déjà le dossier partagé de Desktop.

**La carte de détection**, une fois sur chaque machine :

> **AIODOS a trouvé ComfyUI 0.35.1 sur ce PC.**
> Le mettre au service de tes applications et de tes autres machines ? AIODOS y enverra les travaux de PANDORA et des autres applis que tu autorises, venus de ce PC ou de tes machines. Ils s'ajoutent à la file de ComfyUI sous le nom « aiodos ».
> [x] Si ComfyUI n'est pas ouvert, le lancer sans fenêtre quand un travail attend *(à partir de L2c)*
> [Pas maintenant] · **[Oui]**

Sans ce « oui », la machine n'annonce rien et ne calcule rien (`engines_comfyui_enabled`, faux par défaut). Prêter à des amis ou à des inconnus demande la carte de prêt (§ 4.1).

**L'inventaire** (`engines/comfy_inventory.py`)
- **Les classes**
  - `GET /object_info` est lu à chaque changement de version ou de paquets de nœuds (liste de `custom_nodes`), et au plus une fois par heure. Son poids est à mesurer (M1).
  - Par classe : `python_module`, `api_node`, `output_node`, et l'empreinte de définition (§ 3.3).
- **Les modèles**
  - `GET /models`, puis `GET /models/{dossier}`, donnent les noms. Les chemins viennent des dossiers connus (`local_copies._known`, `argv`, `extra_model_paths.yaml` : **à vérifier**). La taille et la date sont lues tout de suite.
  - L'empreinte SHA-256 est calculée **en fond**, à priorité basse, par un travail local visible, « Empreintes des modèles de ComfyUI », avec `imaging.catalog.sha256_of`, qui sait s'interrompre.
  - Elle est gardée dans `model_hashes` et revalidée par la taille et la date.
  - Un fichier encore sans empreinte ne sert qu'à mes machines, par son nom.
- **L'empreinte de l'inventaire, par cercle** : `digest = sha256(version, core, classes permises, modèles annoncés à ce cercle)`.

**Les réglages, par machine** (`settings.py`)

| Réglage | Défaut | Rôle | Liv. |
|---|---|---|---|
| `engines_comfyui_enabled` | faux (la carte le propose) | annoncer ComfyUI à mes applis et à mes machines | L2b |
| `engines_comfyui_path`, `engines_comfyui_url` | vides (détection) | installation, adresse | L2b |
| `engines_comfyui_use_desktop` | vrai | mes travaux vont au ComfyUI ouvert de l'utilisateur (ses nœuds ajoutés, ses modèles déjà chargés) | L2b |
| `engines_comfyui_launch` | proposé coché par la carte | lancer l'instance « mes machines » si ComfyUI n'est pas ouvert | L2c |
| `engines_reference_machine` | vide | ma machine dont `object_info` et les gabarits sont relayés | L2c |
| `engines_comfyui_lend_friends`, `engines_comfyui_lend_public` | faux | prêt aux amis, aux inconnus | L3, L4 |
| `engines_comfyui_friend_packs` | vide | paquets de nœuds acceptés pour les amis, version épinglée | L3 |
| `engines_lent_models` | `{}` | `{sha256: ["friends", "public"]}` | L3 |
| `engines_friend_clips` | faux | accepter les clips de mes amis en entrée | L3 |
| `engines_unit_minutes` | `{"friends": 60, "public": 20}` | durée maximale d'une unité calculée ici pour autrui | L3 |
| `lend_in_use_max_s` | 120 | unités courtes seulement pendant que j'utilise ce PC | L2b |
| `queue_paused`, `reach_delays`, `apps_keep_mine`, `public_receive` | faux, `{"friends": 15, "public": 60}`, faux, vrai | § 2.4, § 2.5 | L2a à L4 |
| `outbox_hours` | `{"mine": 168, "friends": 48, "public": 24}` | garde de la boîte d'envoi | L2b |
| `keep_awake_jobs` | vrai | empêcher la veille sur secteur | L2b |
| `gateway_out_days`, `gateway_unfetched_badge_days`, `gateway_app_quota_gb`, `gateway_inputs_quota_gb` | 7, 30, 20, 10 | garde et quotas | L2b |
| `gateway_link_secret` | tiré au premier usage, dans `SECRET_KEYS` (DPAPI) | liens signés | L2b |

### 3.3 Le contrat des nœuds (`engines/comfy_contract.py`)

- **L'empreinte d'une classe** est `sha256` du JSON canonique (clés triées, séparateurs compacts, UTF-8) de :
  - `input` et `input_order` ;
  - `output`, `output_is_list` et `output_node` ;
  - `python_module`.

  Elle est tronquée à 16 octets et lue classe par classe (`GET /object_info/{classe}`).
- **Normalisation.** Les listes de choix qui énumèrent des fichiers sont remplacées par `"<fichiers:dossier>"` avant le hachage : modèles, et images d'entrée (`LoadImage.image`, `image_upload`, `image_folder`).
  - On les reconnaît aux options du schéma (`image_upload`, `image_folder`, `remote`) et aux entrées dont toutes les valeurs sont des fichiers d'un dossier de `/models`.
  - **À vérifier** : la règle exacte, contre l'`object_info` de deux machines (M1).
- **`core`** : l'empreinte de la liste triée des empreintes de toutes les classes du cœur (`python_module` hors `custom_nodes.*`, sans `api_node`). Deux machines au même `core` acceptent les mêmes graphes du cœur : c'est la voie rapide.
- **D'où vient le contrat d'un travail**
  - Du ComfyUI de ce PC.
  - Sinon, de la machine de référence (L2c).
  - Sans l'un ni l'autre, il reste vide : seules mes machines prennent l'unité, et c'est leur ComfyUI qui valide.
- **Chez celui qui calcule**
  - Chaque classe doit exister et avoir la même empreinte que dans le contrat, si `strict` est vrai ou si l'ordre vient d'un ami.
  - Sinon : `work/decline {reason: "contract"}`, et ce travail ne revient plus à cette machine (`unit_declines`).
- **`apply_replacements`** ne touche que les classes inconnues du serveur (vérifié). Puisque `check` exige chaque classe, aucun remplacement n'a lieu sur un ordre accepté. Rien à ajouter au contrat.

### 3.4 Les modèles par empreinte (`engines/comfy_models.py`)

**Chez le propriétaire, la résolution**
- Les valeurs à résoudre :
  - les jetons `@model:<clé>` ;
  - avec `models: "auto"`, chaque valeur d'une entrée que les définitions désignent comme une liste de fichiers de modèles ;
  - sans définitions, chaque valeur qui finit par une extension de modèle (`.safetensors`, `.ckpt`, `.pt`, `.pth`, `.bin`, `.gguf`, `.sft`), en dernier recours et pour mes machines seulement.
- Chaque nom est cherché dans `model_hashes` d'ici, puis dans les inventaires de **mes** machines. Eux seuls voient les noms.
- Deux contenus sous un même nom donnent 409 `model_ambiguous`, avec les candidats et les machines : l'appli choisit l'empreinte. Un nom introuvable donne 409 `model_unresolved`.
- La barre des sous-dossiers est ramenée à `/`.

**Chez celui qui calcule, la réécriture**
- Chaque `@model:<n>` devient le nom relatif local du fichier de même empreinte, avec la barre de son système.
- Un modèle absent fait refuser l'ordre (`work/decline {reason: "model"}`).
- Pour un ami ou un inconnu, un modèle non prêté à ce cercle fait aussi refuser l'ordre (`reason: "license"`), et toute autre valeur de fichier de modèle est refusée. L'instance voit les dossiers de l'utilisateur en lecture, mais ne peut charger que ce qui est prêté (I16).

### 3.5 Annonce et appariement

**À chaque demande de bail** (`Worker.capabilities`, filtré par cercle par `engines/lending.py`) :

```json
"tasks":   { "engine_unit": 1 },
"engines": { "comfyui": { "api": 1, "version": "0.35.1", "core": "c0a1…", "digest": "5e0a…",
                          "vram_mib": 8188, "state": "ready", "max_unit_s": 3600,
                          "max_input_mb": 100, "recipes": ["comfy-templates/image_sdxl_t2i@0.11.48-1"] } }
```

- **Une version par tâche.** `engine_unit` ne part qu'aux machines qui annoncent `tasks.engine_unit ≥ 1`. `WORK_API` reste à 1 : les machines d'avant continuent d'indexer.
- **L'inventaire complet ne voyage qu'une fois par `digest`.**
  1. Un propriétaire qui ne le connaît pas répond `{"unit": null, "inventory": "comfyui"}`.
  2. La demande suivante apporte `caps.engines.comfyui.inventory`.
  3. Le propriétaire le garde dans `engine_inventory` (machine, moteur, cercle, `digest`).
- **Ce que voit chaque cercle**

  | Champ | Mes machines | Amis | Public |
  |---|---|---|---|
  | `version`, `core`, `api`, `state`, `max_unit_s` | oui | oui | oui |
  | modèles | tous, avec les noms | les empreintes prêtées aux amis | les empreintes prêtées au public |
  | classes | toutes, sauf les nœuds du cloud | la liste blanche des amis, plus les paquets nommés | celles des recettes |
  | `vram_mib` | exact | arrondi à 2 Go | classe : 8, 12, 16 ou 24 Go et plus |
  | `recipes` | oui | oui | oui |

  Un ami ne voit jamais les nœuds ajoutés du prêteur hors des paquets nommés : ce serait une empreinte de sa machine.
- **`state`** vaut `ready` ou `in_use` (le prêteur se sert de son PC).
- **Le profil** (`node/identity.profile()["engines"]`) n'en garde qu'un résumé (moteur, version, nombre de modèles, cercles prêtés), pour l'interface et pour `GET /aiodos/v1/engines`.
- **Le profil public v2** (L4, `public_profile.rs`, 4096 octets) annonce :
  - `lends: true`, seulement si le prêt public est actif ;
  - `engines: [{"id": "comfyui", "vram_class": 8}]` ;
  - au plus 32 identifiants de recettes.

  Le reste se négocie dans la connexion de travail.

**`work.can_serve`, la branche `engine`.** Une machine convient si :
- elle annonce le moteur, avec `api` et une version suffisants ;
- les classes sont présentes : même `core` et graphe du cœur ; sinon chaque classe à la même empreinte quand le contrat est strict ; une recette doit figurer dans `recipes` ;
- chaque modèle est présent par son empreinte ou, chez mes machines seulement et faute d'empreinte, par son dossier, son nom et sa taille ;
- la mémoire vidéo suffit, quand elle est connue des deux côtés ;
- la durée estimée tient dans `min(bounds.unit_seconds, max_unit_s)`, et dans `lend_in_use_max_s` si la machine est en usage ;
- son cercle est dans `units.reach`, une fois passé le délai de ce cercle (§ 2.4) ;
- aucun refus n'est noté pour ce couple (travail, machine) dans `unit_declines`.

**`work/decline {unit, reason}`** : nouvelle action, cousine de `work/release`, rendue sans essai.

| `reason` | Effet |
|---|---|
| `contract`, `model`, `license`, `class` | ce travail ne va plus chez cette machine |
| `memory` (CUDA à court de mémoire) | idem, et c'est noté pour sa classe de mémoire |
| `busy` | rien n'est retenu |

**Pour les inconnus (L4)**, la machine libre ne peut pas interroger des milliers de pairs. Le travail public est donc annoncé sur le gossip public (`compagnon/src/work_announce.rs`) :
- l'annonce porte `{clé éphémère, moteur, recette, préfixes d'empreintes de modèles, unités restantes, échéance}`, signée par la clé éphémère, en 4096 octets au plus ;
- le prêteur filtre selon ses recettes et ses modèles prêtés, puis rappelle le point éphémère par les relais ;
- chaque prêteur borne les annonces lues par minute et tire au hasard.

### 3.6 Rejouer une unité ComfyUI (`engines/comfyui.run`)

Sur la machine qui calcule, ce PC compris, dans un thread de fond à priorité abaissée :

1. **Revérifier** (`check`), avec l'inventaire de cette machine (I14) :
   - le cercle et le prêt ;
   - la forme du graphe et les valeurs interdites (§ 4.3) ;
   - les nœuds du cloud et la liste blanche du cercle (§ 4.4) ;
   - pour une recette, sa signature et ses paramètres (§ 3.9) ;
   - les modèles par empreinte et la licence prêtée, le contrat, les bornes (§ 4.7), la place sur le disque.

   Un refus rend l'unité : `work/decline` avec la raison, `work/release` si la cause est passagère.
2. **Noter l'unité** dans `work_assignments` (propriétaire, unité, cercle, `run_uuid`, instance, étape), sans aucun contenu (I8).
3. **Les entrées** sont tirées dans le cache (§ 5.1). Pour un ami ou un inconnu, chaque image est relue et réécrite par Pillow (§ 4.8).
4. **Réécrire le graphe** (`engines/comfy_graph.py`, des fonctions pures) :
   - `@in:<slot>` devient `aiodos/<run_uuid>/in-<n>.<ext>` ;
   - `@model:<n>` devient le nom local ;
   - `@seed` devient la graine ;
   - chaque nœud de sortie reçoit `filename_prefix = "aiodos/<run_uuid>/out"`. Ce préfixe neuf à chaque remise fait aussi qu'un graphe déjà calculé produit bien ses fichiers malgré le cache des nœuds ;
   - rien d'autre ne change.
5. **Envoyer les entrées**
   - Dans le ComfyUI de l'utilisateur (mes machines) : `POST /upload/image` en multipart, `subfolder=aiodos/<run_uuid>`, `overwrite=false`.
   - Dans une instance AIODOS : écriture directe dans son dossier `input`.
6. **Lancer** : `POST /prompt {"prompt", "client_id": "aiodos-<16 hex propres à cette instance d'AIODOS>", "prompt_id": "<run_uuid>"}`, **sans `extra_data`** (I3).
   - Un refus 400 donne `engine_error`, avec le nœud et le message.
   - Pour un ami ou un inconnu, seuls la classe et le nœud remontent, jamais un chemin.
7. **Suivre** : `GET /api/jobs/{run_uuid}` toutes les 2 s, pour l'état et la place dans la file. L'avancement est estimé par le temps (§ 2.8).
8. **Arrêter**, si `stop` est levé ou si `bounds.unit_seconds` est dépassé : `POST /api/jobs/{run_uuid}/cancel` (I17).
9. **Récupérer les sorties**
   - Seulement celles des nœuds de sortie de l'ordre, dont le `subfolder` commence par `aiodos/<run_uuid>`.
   - Par `GET /view?…&type=output`, en flux vers le dossier de l'unité, octets comptés. Au-delà de `bounds.output_mb` : `output_too_large`.
   - Pour un ami ou un inconnu, les PNG sont réécrits par Pillow sans les blocs `tEXt`, `iTXt` et `zTXt`, même avec `--disable-metadata`.
10. **Nettoyer**
    - `POST /history {"delete": [run_uuid]}`.
    - Effacer `input/aiodos/<run_uuid>` et `output/aiodos/<run_uuid>` quand on connaît les dossiers (`argv`). **Vérifié le 21/10/2026** : Desktop range ses entrées et ses sorties dans `%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Shared\input` et `\output`, et les dit dans sa ligne de commande (`--input-directory`, `--output-directory`).
    - Pour une instance AIODOS, vider tout son dossier d'unité.
11. **Rendre** : sur ce PC, directement à `assimilate` ; ailleurs, dans la boîte d'envoi (§ 5.2), avec `fingerprint()`. L'étape devient `done` dans `work_assignments`.

**`recover(run_uuid)`** (L2c) : au départ d'une unité qui a déjà un `run_uuid`, `GET /history/{run_uuid}`.
- Fini : on rapatrie les sorties.
- En file ou en cours : on suit.
- Absent : on relance.

L'historique ne vit qu'en mémoire : après un redémarrage de ComfyUI, on recalcule.

**`here(model)`** est vrai quand ComfyUI est accordé, joignable et a tous les modèles de l'unité. L'exécuteur local passe par le même chemin.

**Vérifié le 21/10/2026 sur ComfyUI 0.35.1 (Desktop de l'auteur, RTX 4070 portable)**, par un vrai travail passé par la passerelle (`engines/comfyui.py`) : `POST /prompt` prend `prompt_id` (UUID en minuscules) ; `GET /api/jobs/{id}` donne l'état (`pending`, `in_progress`, `completed`, `failed`, `cancelled`) ; `POST /api/jobs/{id}/cancel` n'arrête que ce calcul (I17) ; `POST /upload/image` range une entrée sous `input/aiodos/<run_uuid>/` et `LoadImage` la lit par ce chemin relatif ; deux images SDXL 768 × 768 en 10,6 s (chargement du modèle compris) puis 4,4 s ; une image à image depuis une entrée envoyée par l'appli ; une annulation en plein calcul. Desktop écoute sur 127.0.0.1:8188 (et non 8000).

### 3.7 Les instances lancées par AIODOS (`engines/comfy_instance.py`)

**Pourquoi**
- Sur mes machines, Desktop n'est pas toujours ouvert (L2c).
- Un ami ou un inconnu ne touche jamais au ComfyUI de l'utilisateur (L3, L4).

**Trois sortes, une seule active à la fois sur la carte**

| Instance | Pour | Nœuds ajoutés | En plus |
|---|---|---|---|
| `mine` (L2c) | mes travaux quand Desktop n'est pas ouvert | ceux de l'utilisateur | dossiers propres |
| `friends` (L3) | mes amis | aucun, sauf `--whitelist-custom-nodes <paquets nommés>` | verrouillée |
| `public` (L4) | les inconnus | **jamais** | verrouillée, recettes seulement |

**La ligne de commande**, sans shell :

```
<python> -s <ComfyUI>\main.py --listen 127.0.0.1 --port <port fermé aux navigateurs, tiré au hasard>
  --base-directory <d>  --input-directory <d>\input  --output-directory <d>\output
  --temp-directory <d>\temp  --user-directory <d>\user  --database-url sqlite:///:memory:
  --extra-model-paths-config <d>\models.yaml  --max-upload-size 100  --disable-auto-launch
  [friends, public] --disable-all-custom-nodes --disable-api-nodes --disable-metadata
  [friends]         --whitelist-custom-nodes <paquets nommés par le prêteur>
```

- **`<d>`** vaut `data\engines\comfyui\<instance>\`, sous les droits de l'utilisateur seulement (`paths.keep_private`).
  - `--base-directory <d>` donne un `custom_nodes` vide.
  - `models.yaml` désigne les dossiers de modèles de l'utilisateur, en lecture : rien n'y est écrit.
- **Jamais `--enable-manager`.**
- **`<python>`**
  - pour Desktop, `ComfyUI\.venv\Scripts\python.exe` (**vérifié le 21/10/2026**, M3 : `standalone-env\python.exe` n'est que la base du .venv, sans torch ; le Python du .venv est un relais qui lance celui de `standalone-env`, d'où l'objet de tâche pour que les deux meurent avec AIODOS) ;
  - `python_embeded\python.exe` pour la version portable.
- **Environnement**
  - Copié et nettoyé : `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME` et `PYTHONUSERBASE` retirés.
  - `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1`, `DO_NOT_TRACK=1`.
  - `HTTP_PROXY`, `HTTPS_PROXY` et `ALL_PROXY` pointés sur `http://127.0.0.1:9`, un port fermé, avec `NO_PROXY=127.0.0.1,localhost`.

  C'est une parade, pas une garantie : aiohttp ignore ces variables par défaut.
- **`node/children.bind(pid)`** range le processus dans l'objet de tâche d'AIODOS : il meurt avec lui, même s'il est tué. Le plafond de mémoire est à mesurer, pas en v1.
- **Son cycle de vie**
  - Lancée à la demande, arrêtée après 10 min sans unité.
  - Passer d'un cercle à l'autre arrête l'instance précédente.
  - Entre deux demandeurs différents : `POST /free {"unload_models": true, "free_memory": true}` vide le cache des nœuds (vérifié), contre le canal caché du cache (I25). Dossiers vidés et historique effacé après chaque unité.
- **Préalable au prêt pour autrui (I24)** : `foreign_writers` est vide sur `ComfyUI\`, sur `standalone-env\` (ou `python_embeded\`) et sur chaque modèle prêté. Sinon, refus affiché : « ComfyUI est dans un dossier que d'autres comptes peuvent modifier ».
- **Pas d'authentification sur le port.** Un programme du PC du prêteur qui le trouve peut y envoyer un graphe, comme pour sd-server aujourd'hui. Port aléatoire, vie courte.
- **Pas de coupure du réseau en v1** : il faudrait les droits d'administrateur. Les nœuds du cloud coupés, le cœur ne fait aucun appel réseau (rapport des moteurs, § 7.1). C'est dit au prêteur.

### 3.8 Partager la carte graphique (`engines/gpu_share.py`)

- **Avant une unité de moteur** : AIODOS décharge ses modèles de conversation inactifs (comme `imaging/engine.py` l.368) et vérifie que son sd-server ne calcule pas.
- **Prêt suspendu** tant que le Desktop de l'utilisateur occupe la carte au-delà du besoin estimé de l'unité (`/system_stats`).
- **Après l'unité**, `POST /free {"unload_models": true}` n'est appelé que si AIODOS a besoin de la carte et que le dernier travail de ce ComfyUI était le sien. On ne vide jamais le travail de l'utilisateur dans son propre ComfyUI.
- **Une conversation pendant une unité** : la règle du § 2.5.

### 3.9 Les recettes (L4 : `engines/recipes.py`, `engines/catalogue.py`, `tools/comfy_recipes.py`)

**Une recette est un JSON** :
- `id`, `version`, et `source` : le gabarit d'origine et la version de `comfyui-workflow-templates`, sous licence MIT, cité dans `NOTICE` ;
- `graph`, au format API, avec des jetons ;
- `params` : nom, type, bornes, défaut, et le `"nœud.entrée"` où chacun va ;
- `models` : empreinte, dossier, taille et identifiant de licence ;
- `contract`, `outputs`, `inputs` (genres et nombre) ;
- `vram_mib` et `typical_seconds` par classe de carte ;
- `verify` : le seuil SSIM mesuré (M2) et les règles de contenu.

**Le catalogue** (`engines/recipes/comfyui-<date>.json`) et la liste blanche (§ 4.4)
- Ils sont signés avec la clé de publication, sous le contexte `aiodos-catalogue\n` (§ 0.3).
- Ils sont vérifiés par `engines/catalogue.py`, avec les primitives de `updates/manifest.py`.
- Ils voyagent avec les versions d'AIODOS, y compris par les mises à jour pair à pair. Pas de serveur central.

**Qui le fabrique** : l'auteur, avec `tools/comfy_recipes.py`.
- L'entrée : des graphes au format API exportés de ComfyUI (« Exporter (API) ») ou de PANDORA, avec l'index des gabarits.
- L'outil :
  - repère les paramètres d'après une table écrite à la main par gabarit ;
  - hache les modèles de la machine de l'auteur, ce qui donne une empreinte aux modèles qui n'en déclarent pas (900 sur 910) ;
  - calcule le contrat, puis signe par `tools/publish.py`.
- Une recette dont un modèle manque chez l'auteur n'est pas publiée.
- Le convertisseur de PANDORA n'est jamais embarqué.

**Chez l'exécutant**
- Seule une recette de son catalogue, signature vérifiée et `digest` égal, est acceptée.
- Les `params` sont revalidés contre leurs bornes.
- Le graphe est construit **à partir de la recette**, jamais reçu, puis passé à `check` au niveau « public » par prudence.
- Une recette inconnue de sa version est refusée (`recipe_unknown`).

**Chez le demandeur**
- `GET /aiodos/v1/engines/comfyui/recipes` donne le catalogue.
- Le client propose `comfy.recipe_for(template)`.
- Un gabarit qui a une recette part en recette et peut aller chez les inconnus ; les autres partent en graphe, chez mes machines et mes amis.

### 3.10 sd-server ensuite (L5, `engines/sdserver.py`)

- **Pour** le H3 local de PANDORA (`minimax-h3-local`).
- **La charge** est le corps de `POST /sdcpp/v1/vid_gen` fait par l'appli, avec `init_image` et `end_image` en jetons. Suivi par `GET /sdcpp/v1/jobs/{id}`, arrêt par `/cancel`.
- **Le modèle n'est pas dans la requête.**
  - Le prêteur ne prête qu'**un sd-server lancé par AIODOS** avec des fichiers choisis par empreinte (`imaging/engine` sait déjà le lancer ; `local_copies` connaît `PANDORA\externals\minimax-h3-local\models`).
  - Un sd-server lancé par quelqu'un d'autre n'est pas prêté.
- **H3** n'est prêté qu'après l'acceptation de sa licence par le prêteur, cercle par cercle.

---

## 4. La sécurité

### 4.1 Côté prêteur : l'accord explicite

**Trois niveaux, dans la vue « Moteurs des applications » de chaque machine**

| Niveau | Défaut | Geste |
|---|---|---|
| Mes applis et mes machines | non ; proposé par la carte de détection (§ 3.2) | « Oui » |
| Mes amis | non (« prêtées aux amis sur autorisation », 18/10) | la carte de prêt |
| Les inconnus | non, et le réseau public doit être rejoint | la même carte, case à part, avec sa carte de confirmation |

**La carte de prêt**

> **Prêter ComfyUI à tes amis et au réseau public**
> Leurs travaux tournent dans un ComfyUI séparé, lancé par AIODOS : sans tes nœuds ajoutés (sauf ceux que tu nommes pour tes amis), sans les nœuds qui appellent Internet, sans accès à tes créations. Ses dossiers sont vidés après chaque travail.
> Les inconnus n'envoient pas de programme : seulement le nom d'une recette du catalogue d'AIODOS et ses réglages. Ils ne savent pas qui tu es, et tu ne sais pas qui demande.
> Ils ne tournent que dans tes conditions de partage (secteur, inactivité, part de la carte graphique). Pendant que tu utilises ce PC, seulement des calculs de moins de 2 min.
> Ce qui reste possible : une faille de ComfyUI, de PyTorch ou du pilote de ta carte. Les consignes et les images des autres passent par ta machine le temps du calcul.
> **Modèles prêtés** (aucun n'est coché d'avance) :
> - [ ] sd_xl_base_1.0.safetensors · 6,9 Go · CreativeML Open RAIL++-M · [lire]
> - [ ] minimax_h3_fl2va_pruned_int8_convrot.safetensors · 21 Go · **MiniMax H3 Community License : exclut l'UE, le Royaume-Uni, la Corée et les États-Unis** · [lire] · [ ] J'ai lu la licence et je peux faire tourner ce modèle pour d'autres
> - [ ] un_modele.safetensors · licence inconnue : à toi de vérifier (amis seulement)
>
> [ ] À mes amis · [ ] Aux inconnus du réseau public
> **Nœuds ajoutés prêtés à mes amis** : aucun · [choisir…]
> [Annuler] · **[Prêter]** (cliquable après 1,5 s)

**Les règles de la carte**
- Une licence restrictive s'affiche en ambre et demande la seconde case. Chacun est responsable (règle de l'auteur), sans pays déclaré.
- Une licence inconnue ne se prête qu'à mes machines et à mes amis, jamais au public.
- Retirer le prêt arrête les unités en cours, qui sont rendues, et vide l'instance.
- Le prêt est refusé, avec sa raison, si `foreign_writers` n'est pas vide (I24).

### 4.2 Par cercle

| | Mes machines | Amis | Inconnus |
|---|---|---|---|
| Forme reçue | graphe API | graphe API vérifié | **recette signée et paramètres** |
| Où il tourne | le Desktop ouvert de l'utilisateur, sinon l'instance `mine` | l'instance `friends` | l'instance `public` |
| Classes | toutes celles installées, sauf le cloud | liste blanche signée, plus les paquets nommés par le prêteur | celles de la recette, revérifiées contre la liste blanche |
| Version de ComfyUI non revue | — | recettes seulement | recettes seulement |
| Modèles | par empreinte, ou par nom | par empreinte, prêtés aux amis | par empreinte, prêtés au public, licence connue |
| Contrat | lâche, strict sur demande | strict | celui de la recette |
| Chemins, adresses, annotations | refusés | refusés | sans objet (paramètres typés) |
| Entrées | telles qu'envoyées (EXIF retiré seulement hors de mes machines) | images réécrites ; clips si le prêteur les accepte | images seulement, réécrites, 16 Mpx |
| Métadonnées des PNG | gardées | retirées (`--disable-metadata` et Pillow) | retirées |
| Cache des entrées | par propriétaire | par propriétaire | par travail (clé éphémère) |
| Garde de la boîte d'envoi | 7 jours | 48 h | 24 h |
| Ordre de service chez le prêteur | d'abord | ensuite | en dernier, débit limité |

### 4.3 Chemins et adresses (I7, I13 ; `engines/comfy_graph.forbidden`)

**Toute valeur de texte est refusée si :**
- elle finit par `[output]`, `[input]` ou `[temp]`, **avec ou sans espace** (`folder_paths.py` l.257) ;
- elle commence par `/`, `\`, une lettre de lecteur, `\\` ou `~` ;
- un de ses segments vaut `..`, après décodage des `%2e` et unification des barres ;
- elle contient `://`, ou commence par `file:`, `http`, `ftp` ou `www.` ;
- elle commence par `data:` hors d'un champ connu ;
- elle contient un caractère nul ;
- elle désigne un nom réservé de Windows (`CON`, `NUL`, `COM1`…).

**Deux exceptions**
- Les entrées `STRING` déclarées `multiline` par les définitions (les consignes, qui vont aux encodeurs de texte) : seuls le caractère nul et la longueur y sont contrôlés.
- Les entrées que les définitions désignent comme des listes de fichiers de modèles : un chemin relatif `dossier/fichier.safetensors`, sans `..`, présent dans la liste du moteur. Pour un ami, seulement un jeton réécrit.

**Toujours**
- L'entrée de fichier de `LoadImage`, `LoadImageMask`, `LoadVideo` et `LoadAudio` doit être un jeton `@in:` existant.
- `filename_prefix` est réécrit.
- `extra_data`, `client_id` et `prompt_id` venus du demandeur sont ignorés.

La règle s'applique deux fois : à la mise en file, chez le demandeur, puis avant le lancement, chez celui qui calcule, avec ses propres définitions (I14).

### 4.4 La liste blanche des nœuds (L3 : `engines/comfy_allowlist.py`, `engines/comfy_allowlist.json`, `tools/comfy_allowlist.py`)

- **Toujours refusés**, dans tous les cercles : `api_node` vrai, ou `python_module` sous `comfy_api_nodes`.
- **Pour un ami : une liste positive**, signée, par version de ComfyUI revue.
  - **Le point de départ** : les 247 classes des 178 gabarits officiels en cache chez PANDORA, toutes `comfy-core`.
  - **Moins les exclusions nommées**, vérifiées dans le code :
    - `LoadImageOutput` ;
    - les nœuds de `comfy_extras/nodes_dataset.py` ;
    - `SaveText` ;
    - les `*Save` de modèles (`CheckpointSave`, `CLIPSave`, `VAESave`, `ModelSave`, `LoraSave`…) ;
    - `SaveLatent` et `LoadLatent` ;
    - `Load3D` et les sorties 3D ;
    - les nœuds d'entraînement ;
    - `PreviewImage` et `PreviewAny`, retirés du graphe puisqu'ils écrivent dans `temp`.
  - **Pour chaque classe** :
    - le `python_module` attendu, contre un nœud ajouté qui prendrait le nom d'une classe du cœur (en plus de `ignore=base_node_names`, vérifié) ;
    - les entrées numériques à borner (§ 4.7).

    Une classe dont une entrée ouvre un coût sans borne connue n'y entre pas.
  - **Les paquets nommés par le prêteur pour ses amis** s'ajoutent, avec leur version épinglée (`pyproject.toml` ou commit) et `python_module = custom_nodes.<paquet>`, seulement dans l'instance `friends`.
- **Pour un inconnu** : les classes des recettes, revérifiées contre la liste blanche.
- **La revue** : `tools/comfy_allowlist.py` part d'un `object_info` de référence et des gabarits ; l'auteur relit et signe. Une version de ComfyUI absente de la liste ne prête qu'aux recettes.

### 4.5 Les modèles prêtés et leurs licences (L3 : `engines/licenses.py`, `engines/model_licenses.json`)

- Un modèle n'est calculé pour un cercle que s'il y est coché (I16). L'inventaire de ce cercle ne contient que ceux-là.
- **La table des licences**
  - Par empreinte quand on la connaît, sinon par famille de nom (`sd_xl_*`, `flux1-schnell*`, `flux1-dev*`, `minimax_h3_*`…).
  - Pour chacune : identifiant, adresse, restrictions (territoire, usage commercial), et un texte court en français et en anglais.
  - **À constituer** : l'index des gabarits n'en donne aucune.
- Une licence inconnue est dite « inconnue », jamais devinée.
- Côté demandeur, `GET /aiodos/v1/engines` et `plan` rendent les licences connues, que l'appli affiche.

### 4.6 Ce que garde le prêteur (I8)

- **Instance AIODOS** : rien ne reste.
  - Dossiers vidés après chaque unité, base en mémoire, historique effacé, métadonnées coupées.
  - `/free` entre deux demandeurs.
- **Desktop de l'utilisateur** (mes machines) : l'historique et les fichiers de l'unité sont effacés quand AIODOS connaît les dossiers. Sinon, la vue Moteurs propose de vider `input\aiodos\` et `output\aiodos\`.
- **`work_assignments` et `work_outbox`** : sans contenu. Les sorties en attente sont des fichiers, effacés à l'accusé ou à l'échéance.
- **Le journal** : l'unité, le cercle, le moteur, la recette ou le nombre de nœuds, la durée et l'issue (`shared_log` étendu).
- **Le cache des entrées** est vidé après 2 h sans usage (à la fin du travail pour un inconnu) et à chaque démarrage.

### 4.7 Les bornes (`engines/comfy_bounds.py`, réglables par le prêteur, jamais au-dessus)

| Borne | Mes machines | Amis | Inconnus |
|---|---|---|---|
| Durée d'une unité | `bounds.unit_seconds`, 4 h au plus | 60 min (réglage du prêteur) | 20 min |
| Nœuds | 2000 | 500 | ceux de la recette |
| Côtés, pixels | ceux du moteur | 4096 px, 16 Mpx | 2048 px, 4 Mpx |
| `batch_size`, `amount` | ceux du moteur | 4 | 1 |
| Images d'une vidéo | celles du moteur | 257 | pas de vidéo en v1 |
| Pas (`steps`) | ceux du moteur | 150 | 100, ou la recette |
| Sorties d'une unité | `bounds.output_mb`, 2 Gio au plus | 500 Mo | 64 Mo |
| Entrées d'une unité | 512 Mo (64 Mo par Internet) | 100 Mo | 32 Mo, images seulement |
| Texte | 20 000 caractères par chaîne | idem, 256 Ko en tout | les bornes de la recette |
| Unités détenues | sans borne | 4 | 1 pour un nouveau venu, puis 2 |
| Disque pendant l'unité | sorties + 1 Gio (`DISK_MARGIN`) | idem | idem |

- Les bornes de taille se lisent dans les entrées numériques connues (`width`, `height`, `length`, `batch_size`, `steps`…). Ce n'est qu'un premier filtre.
- La vraie garde, contrôlée pendant le calcul, est la durée (annulation ciblée à l'échéance), le poids des sorties (lecture arrêtée) et le disque.
- Les colonnes « amis » et « inconnus » sont à régler après les mesures.

### 4.8 Entrées hostiles et isolement (I15)

- **Images**
  - Réécrites par Pillow des deux côtés : chez le propriétaire, pour ne pas envoyer l'EXIF hors de mes machines ; chez le prêteur, pour ne pas donner un fichier piégé au décodeur de ComfyUI.
  - Les dimensions sont lues avant le décodage (16 Mpx, `Image.MAX_IMAGE_PIXELS`).
  - Le type est reconnu par les premiers octets.
- **Vidéos et sons**
  - AIODOS n'a ni ffmpeg ni PyAV pour les réécrire.
  - Chez les inconnus : aucune entrée vidéo ni sonore en v1.
  - Chez les amis : seulement si le prêteur coche « Accepter les clips de mes amis » ; signature (`ftyp`, EBML), 100 Mo, durée lue dans le conteneur.
- **L'instance** : § 3.7.
- **Plus fort, à mesurer (M7) avant d'ouvrir le public en version signée** : un jeton à intégrité basse ou un AppContainer, avec CUDA. Un compte Windows séparé viendra plus tard (il faut l'administrateur).

### 4.9 Côté demandeur : vérifier ce qui revient (`engines/media_check.py` en L2b ; `engines/content_check.py`, `engines/media_clean.py`, `engines/witness.py`, `jobs/verify.py` en L3 ; `jobs/redundancy.py` en L4)

| Contrôle | Mes machines | Amis | Inconnus |
|---|---|---|---|
| Signature (PNG, JPEG, WebP, MP4 par ses boîtes `ftyp`, `moov` et `mvhd`, WebM, FLAC), poids, nombre de sorties, nœud d'origine | oui | oui | oui |
| Taille de l'image égale à celle du graphe, quand elle s'y lit | oui | oui | oui |
| Contenu non vide (écart type > 5, compressibilité > 0,05, hautes fréquences < 0,5) | — | oui | oui |
| PNG réécrit par Pillow avant la livraison | — | oui | oui |
| Échantillon refait sur une de mes machines | — | 5 % des images, puis 2 % après 50 sans écart | 5 %, puis 1 % selon la réputation |
| Redondance avant la livraison | — | — | nouveau venu (moins de 10 unités vérifiées) : chaque unité, aussi chez une machine sûre ou chez un inconnu établi ; désaccord tranché par une troisième machine, à moi |
| Vidéo | signature, poids, durée | idem, plus des **images témoins** comparées sur 2 % des clips (M9) | pas de vidéo en v1 |

**La comparaison**
- **À l'identique, au pixel près**, si l'empreinte du moteur des deux machines est égale : version de ComfyUI, torch, CUDA, carte, attention, options. Mentir sur cette empreinte n'aide pas : on exige alors l'égalité exacte. C'est le seul contrôle qui voit « 4 pas au lieu de 8 ».
- **Sinon, par le SSIM**, au seuil de la recette, mesuré pour ComfyUI (M2). Il est de 0,60 pour sd.cpp.

**Les images témoins** (amis, vidéo)
- Le propriétaire ajoute au graphe un `ImageFromBatch`, puis un `SaveImage`, sur la première, la médiane et la dernière image avant l'encodeur vidéo.
- La comparaison se fait sur elles, sans décoder le MP4.
- **À vérifier** sur les gabarits vidéo (M9).

**En cas d'écart**
- La machine est mise de côté : `peers.set_aside`, propagé à mes machines (`group/set_aside`). Pour un inconnu, la réputation (L4) prend le relais.
- Le fichier déjà livré passe à `verified: "doubt"`, et l'événement `file_doubt` part vers l'appli.
- L'unité est refaite ailleurs, et le nouveau fichier est livré à côté.

**Autres règles**
- **Pas plus de 20 % des unités d'un travail** chez des nouveaux venus.
- **Le signalement par l'appli** : `POST /aiodos/v1/jobs/{id}/units/{n}/report {"reason": "off_topic" | "blank" | "corrupt"}`, 10 par heure et par appli. La machine d'un ami ou d'un inconnu est mise de côté, et l'unité repart si le travail est actif.
- **`verified`** vaut `none`, `content`, `sampled_exact`, `sampled_close`, `redundant` ou `doubt`.
- **Le temps de calcul d'un inconnu** est estimé ici, d'après la durée habituelle, jamais cru sur parole.

### 4.10 Côté demandeur : le contenu envoyé

- **La phrase de la carte d'accord** (décision 2) :

  > Le contenu des demandes de PANDORA (consignes, images et vidéos envoyées) est lu par la machine qui calcule, le temps du calcul : tes machines, celles de tes amis, et celles d'inconnus du réseau public. AIODOS efface tout après le calcul, mais une machine malveillante pourrait en garder une copie. Elles ne savent pas que c'est PANDORA, ni, pour les inconnus, que c'est toi. Décoche ce que tu ne veux pas.

- **Un cercle que la version installée ne sert pas encore** n'est pas validé d'avance (`consents`). À son arrivée (L3, L4), une carte cochée se valide une fois : « PANDORA peut maintenant faire calculer ses travaux chez tes amis ».
- **Un travail peut resserrer** : `sensitive=True` dans le client donne `where=["here", "mine"]`.
- **L'anonymat chez les inconnus** : une clé éphémère par travail (`network/ephemeral.py`, `compagnon/src/ephemeral.rs`) et des demandes relayées. La clé est effacée à la fin du travail.
- **L'EXIF** des photos est retiré avant tout envoi hors de mes machines.

### 4.11 Ce qui reste, dit franchement

| Risque | Parade | Reste |
|---|---|---|
| Faille de ComfyUI, torch, d'un pilote ou de Pillow | instance verrouillée, liste blanche, recettes pour le public, objet de tâche, pas de vidéo d'inconnu | une exécution de code avec les droits de l'utilisateur ; isolement plus fort à mesurer (M7) |
| Classe mal jugée dans la liste blanche | liste positive, `python_module` vérifié, revue à chaque version | une erreur de revue |
| Un ami ou un inconnu garde le contenu | dit à l'accord ; `where` par travail ; EXIF retiré | voulu par la décision 2 |
| Résultat bâclé entre deux échantillons | contenu, échantillon, redondance, signalement | une image vraisemblable mais fausse, prise après coup |
| Licence | affichée, seconde case | affichée, pas contrôlée : le prêteur s'engage |
| Adresse IP vue par un relais tenu par un inconnu | relais de n0 par défaut (18/10) | limite d'iroh, connue |

---

## 5. Le transfert

### 5.1 Les entrées

**De l'appli à AIODOS** (`gateway/inputs_api.py`, `gateway/input_store.py`)
- **`HEAD /aiodos/v1/inputs/{sha256}`** répond 200 si **cette appli** l'a déjà envoyé, 404 sinon (I10).
- **`PUT /aiodos/v1/inputs/{sha256}`**, corps brut, avec un `Content-Type` déclaré.
  - Les octets sont comptés pendant la lecture, l'empreinte recalculée, et le type reconnu par les premiers octets.
  - 100 Mo au plus. Réponse 201 (créée) ou 200 (déjà là) : `{sha256, size, type}`.
- **Le rangement** : `data\gateway\inputs\<app_id>\<aa>\<sha256>`, une ligne dans `gateway_inputs`, et le lien au travail dans `job_inputs`.
- **La garde** : tant qu'un travail non fini cite l'entrée, puis 24 h. Quota de 10 Gio par appli.

**D'AIODOS à celui qui calcule** (`network/work.py`, `network/input_cache.py`)
- L'ordre liste `inputs: [{slot, sha256, size, type}]`.
- `work/blob` reçoit `sha256`, en plus d'`unit`, `offset` et `size`. Le propriétaire ne sert que les entrées de l'unité confiée à cette machine.
- Les morceaux font 2 Mo sur le réseau local et 512 Ko par Internet, avec reprise (`CHUNK_RETRY_DELAYS`). L'empreinte est vérifiée à la fin.
- **Le cache chez celui qui calcule** : `data\work_cache\<propriétaire, ou clé éphémère>\<sha256>`, 5 Gio au plus. Il est vidé après 2 h sans usage, au démarrage, et à la fin du travail pour un inconnu (I25).
- Les tâches existantes gardent `file_key` : rien ne change pour l'indexation ni pour les images d'AIODOS.

### 5.2 Les sorties

**Chez celui qui calcule, la boîte d'envoi** (`network/outbox.py`)
- Les fichiers produits vont dans `data\work_outbox\<propriétaire8>-<unité>\`, hors de `paths.work_dir()`, que `Worker.start` efface. Une ligne de `work_outbox` décrit : propriétaire, unité, cercle, manifeste, octets, échéance et essais.
- Une boucle tourne toutes les 30 s, au réveil, et dès que `peers.online` montre le propriétaire.
- La boîte est effacée à l'acceptation, au refus, ou à l'échéance du cercle (7 j, 48 h, 24 h).

**Le protocole, manifeste d'abord**
1. **`work/result {unit, compute_s, manifest: {files: [{n, node, type, size, sha256}], engine: {version, device}}}`**
   - Le propriétaire vérifie :
     - que l'unité est tenue par cette machine (`running`, ou revenue en file en gardant sa machine) ;
     - que le travail n'est pas annulé ;
     - le nombre et le genre des fichiers ;
     - `bounds.output_mb` ;
     - qu'il reste 1 Gio libre sur le disque.
   - Il répond `{"accepted": "pending", "next": [{n, offset}]}`.
2. **`work/put {unit, n, offset, data}`** (`network/work_put.py`) : 4 Mio sur le réseau local, 1 Mio par Internet. Le propriétaire écrit à la suite dans `data\tmp\work_in\<unité>\<n>.part` et répond `{"next": offset}`.
3. **Après le dernier octet**
   - Empreintes vérifiées, puis `validate`, puis `sample` ou la redondance, puis `assimilate`.
   - La réponse au dernier `put` vaut `{"accepted": true}`, ou `false` avec la raison.
4. **Reprise** : après une coupure ou un redémarrage d'un côté, le prêteur rappelle `work/result`, et le propriétaire répond les `next` atteints. C'est toujours le prêteur qui appelle : cela marche derrière une box et par les relais.
5. **`assimilate`**
   - Les fichiers vont dans `data\gateway\out\<app_id>\<travail>\`, une ligne chacun dans `gateway_files`, et l'événement `file` part vers l'appli.
   - Les `.part` d'une unité redonnée ou abandonnée sont effacés au bout de 24 h.

**Les bornes et la compatibilité**
- `body_limit.py` accepte `work/put` jusqu'à `SEALED_BODY` (16 Mio), seulement d'une machine qui calcule pour celle-ci (`computing_for_me`). Un morceau de 4 Mio, scellé et passé en base64, fait environ 7,1 Mio.
- Le compagnon laisse passer 64 Mio : rien à changer côté Rust.
- Les tâches existantes gardent leur sortie dans `work/result` (champ `output`). La présence de `manifest` choisit le nouveau chemin.

### 5.3 La garde chez le propriétaire (décision 5, `gateway/retention.py`)

- **Récupéré** : le fichier a été servi jusqu'à son dernier octet, et les octets servis à l'appli atteignent sa taille (200 complet, ou des plages qui finissent le fichier). `fetched_at` est alors posé. Un lien signé consommé en entier compte aussi.
- **L'effacement** : 7 jours après `fetched_at` (`gateway_out_days`), jamais avant (I18), sauf `DELETE` de l'appli ou geste de l'utilisateur.
- **Un fichier jamais récupéré** n'est jamais effacé seul. Après 30 jours, un badge : « PANDORA n'est pas venu chercher 12 fichiers (3,1 Go) : [Les garder dans Images] [Les supprimer] ».
- **Le quota** : 20 Gio de fichiers non récupérés par appli. Au-delà, un nouveau travail reçoit 429 `quota_exceeded`.
- **Le retrait de l'appli** fait commencer les 7 jours.
- **Le dossier** est hors du dossier Images, qui peut être synchronisé par OneDrive.

### 5.4 Les tailles

| | v1 | D'où |
|---|---|---|
| Graphe API | 2 Mo, 8 Mo par travail | gabarits de 45 à 71 Ko avant élagage |
| Une entrée | 100 Mo ; le client refuse avant l'envoi (`input_too_large`) | `--max-upload-size` de ComfyUI |
| Entrées d'une unité | 512 Mo en local, 64 Mo par Internet (`internet_max_blob_mb`), 32 Mo pour un inconnu | `work.MAX_BLOB` |
| Sorties d'une unité | 2 Gio au plus, selon `bounds.output_mb` | SeedVR2 : à mesurer (M4) |
| Morceaux | entrées : 2 Mo et 512 Ko ; sorties : 4 Mio et 1 Mio | sous `SEALED_BODY` |
| Bail d'une unité de moteur | 300 s, renouvelé toutes les 30 s | `TaskSpec.lease_s`, `renew_s` |

### 5.5 Le chiffrement

- **Entre machines appairées** : chaque message est scellé (`network/envelope.py`), sur le réseau local ou par QUIC (iroh).
- **Avec un inconnu** : le même scellement, entre la clé éphémère du travail et la clé publique du prêteur, par l'ALPN `aiodos/public-work/1` et un relais.
- **À l'arrivée** : SHA-256 de chaque fichier, dans les deux sens. Un morceau plus long que demandé est refusé.
- **Sur le disque** : pas de chiffrement de plus. Dossiers privés, durées courtes, effacement au démarrage.

### 5.6 Les modèles absents

- **En v1**, seule une machine qui a tous les modèles reçoit l'unité. `plan` et la carte du lot le disent : « Portable : il lui manque `qwen3vl_32b_minimax_h3…` (15,7 Go) ».
- L'appli propose d'installer le modèle chez elle. PANDORA le sait, sur un clic. AIODOS ne télécharge rien (I5, I19).
- **En L5, entre mes machines** : « Copier ce modèle sur Portable » (`imaging/share.py`), licence affichée et acceptée sur la machine qui reçoit. Jamais chez un ami ni chez un inconnu.

### 5.7 Les versions

- `tasks.engine_unit` est la version du contrat de l'unité ; `engines.comfyui.api`, celle du format de ComfyUI.
- Une machine d'avant ne reçoit simplement pas ces unités. `WORK_API` ne bouge pas.

---

## 6. L'API de la passerelle et les clients

### 6.1 Ce qui reste de la 1re conception

| Partie | Gardé | Changé |
|---|---|---|
| § 2 Découverte (`gateway.json`, `info`, preuve, états, lancement) | tout | `gateway_closed` mène à `connect()` (§ 6.2) ; `features` ajoute `engines`, `engines.comfyui`, `jobs`, `jobs.pause`, `inputs`, `files`, puis `engines.friends` (L3), `engines.public` et `recipes` (L4) |
| § 3 Accès (clés, RFC 8628, carte, identité, droits, retrait, journal, limites, clé générale) | tout | le cercle `public` ; les cases cochées par défaut ; `consents` ; le droit `engines:<moteur>` remplace `images` et `videos` ; la carte du § 6.3 ; les limites du § 6.8 |
| § 4.1 à 4.6 (conventions, erreurs, catalogue, conversation, embeddings, Ollama, MCP) | tout | les codes du § 6.9 ; `X-Aiodos-Place` peut valoir `public`, jamais pour une conversation en v1 |
| § 4.7 et 4.8 (créations, images au format d'OpenAI) | **retirés** | travaux de moteur |
| § 4.9 Travaux | l'objet, le SSE, l'annulation réelle | unités, machines, pauses, carte du lot |
| § 4.10 Fichiers et liens signés | tout | la garde du § 5.3 |
| § 4.11 et 4.12 (transcription, bibliothèques) | reportés après la v1 | — |
| § 5 Clients | règles communes, coffre, erreurs, faux serveurs, contrat partagé | publiés ; `connect()`, `comfy`, `jobs`, `inputs` ; `image()` et `video()` retirés |

### 6.2 Se connecter quand la passerelle est fermée (`gateway/app_connect.py`)

1. **L'appli appelle `status()`.** `gateway.json` dit `closed`, ou AIODOS ne tourne pas. L'appli affiche **[Se connecter à AIODOS]**.
2. **Sur ce clic, le client lance** `<racine>\runtime\python.exe -m aiodos --app-request`, depuis `<racine>\app`, sans shell et sans fenêtre (`CREATE_NO_WINDOW`). La racine vient du registre (1re conception, § 2.5). La demande est écrite sur l'entrée standard : `{client_name, client_id, client_version, scope, where, purpose}`.
3. **Ce programme d'aide**
   - lance AIODOS en arrière-plan s'il ne tourne pas, et l'attend 60 s ;
   - lit `data\instance.json` (port de l'interface, `show_key`) ;
   - envoie `POST /instance/app-request`, avec l'en-tête `x-aiodos-show` et le pid de son parent, le programme de l'appli ;
   - écrit sur sa sortie standard `{device_code, user_code, interval, expires_in}`, puis s'arrête.
4. **AIODOS identifie le parent** (`gateway/caller.py` : chemin et compte Windows ; le compte doit être le même), ramène sa fenêtre et montre la carte d'accord.
5. **« Autoriser » ouvre la passerelle** : `gateway_apps` et `gateway_requests` passent à vrai, et `Gateway.apply()` démarre l'écoute. La carte le dit : « AIODOS s'ouvrira aux applications de ce PC, sur 127.0.0.1:11435 ».
6. **Le client interroge `POST /aiodos/v1/access/token`** toutes les 2 s. Tant que la passerelle n'écoute pas, la connexion est refusée et il continue, jusqu'à `expires_in`. La clé est relevée une seule fois.

**Les autres cas**
- **Passerelle déjà ouverte** : `POST /aiodos/v1/access/requests` directement.
- **Passerelle refermée, clé encore valide** : le même programme d'aide, avec l'identifiant de la clé, montre « PANDORA veut se reconnecter : rouvrir AIODOS aux applications ? ». Aucune clé n'est créée.
- **Copie de développement** : « Lance AIODOS et ouvre Réglages → Applications ». `AIODOS_HOME` désigne une autre racine pour les essais.

**La sécurité**
- La route `/instance/app-request` est sur le port de l'interface, derrière la `show_key`, que seul le même compte lit.
- Les règles contre l'insistance s'appliquent (1re conception, § 3.3).
- Un refus laisse la passerelle fermée.
- **À vérifier** : le passage au premier plan quand c'est le programme d'aide qui le demande. L'appli appelle `AllowSetForegroundWindow` avant de le lancer.

### 6.3 La carte d'accord (version L2b)

> **PANDORA demande à utiliser AIODOS**
> PANDORA 3.2.0 : c'est le nom que donne le programme, AIODOS ne peut pas le vérifier. Programme vu par Windows : `C:\…\PANDORA.exe`, sous ton compte.
> **Code : 482 197** : il doit s'afficher en ce moment dans PANDORA.
>
> **Ce que PANDORA pourra faire**
> - [x] Converser avec tes modèles (textes, images jointes, outils)
> - [x] Faire tourner ses travaux ComfyUI sur les machines qui ont ComfyUI
>
> **Où ses calculs peuvent se faire**
> - [x] Ce PC
> - [x] Mes machines (PC fixe, Portable)
> - [x] Chez mes amis qui prêtent leur puissance
> - [x] Chez des inconnus du réseau public *(travaux ComfyUI en recettes seulement ; les conversations restent chez moi et chez mes amis)*
>
> *[La phrase du § 4.10.]*
> *Un cercle pas encore servi par cette version sera confirmé à son arrivée.*
>
> ▸ Plus de réglages : priorité, durée de l'accès, lier la clé au programme
> AIODOS s'ouvrira aux applications de ce PC (127.0.0.1:11435).
> [Refuser] · [Refuser et bloquer ce programme] · **[Autoriser]** (cliquable après 1,5 s)

`gateway_apps.reach` garde les cases cochées. `consents` note, par capacité et par cercle, la date à laquelle le cercle a été montré alors qu'il était servi.

### 6.4 Toutes les routes

| Méthode et chemin | Clé | Droit | Liv. |
|---|---|---|---|
| `GET /`, `GET /aiodos/v1/info` | non, ou facultative | — | existe, L1 |
| `POST /aiodos/v1/access/requests`, `/token` ; `/revoke` (avec la clé) | non | — | L1 |
| `POST /aiodos/v1/open` | oui | — | L1 |
| `GET /aiodos/v1/models?kind=chat\|embed` | oui | filtré | L1 |
| `/v1/models`, `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | oui | `chat`, `embed` | existe, retouché en L1 |
| `/api/*`, `/mcp` | oui | `chat`, `embed`, `libraries` | existe, retouché en L1 |
| `GET /aiodos/v1/engines` | oui | `engines:*` | L2b |
| `POST /aiodos/v1/engines/comfyui/plan` | oui | `engines:comfyui` | L2b |
| `GET /aiodos/v1/engines/comfyui/object_info[?class=]` | oui | `engines:comfyui` | L2c |
| `GET /aiodos/v1/engines/comfyui/templates/{nom}` | oui | `engines:comfyui` | L2c |
| `GET /aiodos/v1/engines/comfyui/recipes` | oui | `engines:comfyui` | L4 |
| `HEAD`, `PUT /aiodos/v1/inputs/{sha256}` | oui | `engines:*` | L2b |
| `POST /aiodos/v1/jobs` | oui | `engines:<moteur>` | L2b |
| `GET /aiodos/v1/jobs?active=&status=&client_ref=&after=&limit=` | oui | les siens | L2b |
| `GET /aiodos/v1/jobs/{id}` ; `GET /aiodos/v1/jobs/{id}/events` (SSE) | oui | le sien | L2b |
| `GET /aiodos/v1/jobs/{id}/files?unfetched=1&after=` | oui | le sien | L2b |
| `POST /aiodos/v1/jobs/{id}/pause` (`{"when": "after_current" \| "now"}`), `/resume`, `/cancel`, `/retry` | oui | le sien | L2b |
| `PATCH /aiodos/v1/jobs/{id}` (`{"priority", "label"}`) ; `DELETE /aiodos/v1/jobs/{id}?files=keep\|delete` | oui | le sien | L2b |
| `POST /aiodos/v1/jobs/{id}/units/{n}/report` | oui | le sien | L3 |
| `POST /aiodos/v1/jobs/pause`, `/aiodos/v1/jobs/resume` : tous ses travaux ; `GET /aiodos/v1/queue` | oui | les siens | L2b |
| `GET /aiodos/v1/files/{id}`, `/content` (`Range`) | oui, ou lien signé | le sien | L2b |
| `POST /aiodos/v1/files/{id}/link` ; `DELETE /aiodos/v1/files/{id}` | oui | le sien | L2b |

**Côté interface d'AIODOS**, derrière le jeton de lancement :
- les routes de la 1re conception pour les applis ;
- `POST /instance/app-request`, derrière la `show_key` ;
- `/api/engines/*` : détection, accord, inventaire, prêt, licences, paquets, vider les dossiers, machine de référence ;
- `/api/jobs/*` : modes de pause, tout mettre en pause, carte du lot.

**Entre machines**, de nouvelles actions dans `p2p_app.HANDLERS` :
- `work/put` et `work/decline` ;
- `engines/read`, en lecture, pour le relais d'`object_info` et des gabarits, de **mes** machines seulement.

`work/lease`, `work/renew`, `work/blob` et `work/result` sont étendus. Pour les inconnus (L4) : les mêmes actions sous `/p2p/v1/public/work/*`, par le compagnon.

### 6.5 Les moteurs

**`GET /aiodos/v1/engines`**, filtré selon les cercles de l'appli :

```json
{ "engines": [
  { "id": "comfyui", "label": "ComfyUI",
    "here": { "enabled": true, "version": "0.35.1", "vram_mib": 8188 },
    "machines": { "here": 1, "mine": 2, "friends": 1, "public": null },
    "models": [ { "folder": "checkpoints", "name": "sd_xl_base_1.0.safetensors", "sha256": "31e3…",
                  "size": 6938078334, "where": { "here": true, "mine": ["Portable"], "friends": 1, "public": null },
                  "license": { "id": "creativeml-openrail-m", "restrictive": false } } ],
    "object_info": { "available": true, "machine": "PC fixe", "digest": "5e0a…" },
    "recipes": 0 } ] }
```

- `public: null` veut dire « inconnu d'avance ».
- Pour les amis, l'appli reçoit un nombre, jamais un nom.

**`POST /aiodos/v1/engines/comfyui/plan`** prend le corps d'un travail, `items` facultatif, et ne met rien en file :

```json
{ "ok": true, "where_effective": ["here", "mine", "friends"],
  "where_reason": "graphe : les inconnus ne reçoivent que des recettes",
  "machines": { "here": true, "mine": 1, "friends": 2, "public": null },
  "missing": [ { "folder": "checkpoints", "name": "…", "machine": "Portable", "size": 6938078334 } ],
  "refused": { "friends": [ { "class": "SomeCustomNode", "reason": "class_not_allowed" } ] },
  "licenses": [ { "sha256": "31e3…", "id": "creativeml-openrail-m", "restrictive": false } ],
  "recipe": { "id": "comfy-templates/image_sdxl_t2i", "version": "0.11.48-1" },
  "estimate": { "seconds_per_unit": 16, "machines": 3 } }
```

**`object_info` et les gabarits relayés** (L2c)
- Celui du ComfyUI de ce PC, sinon celui de ma machine de référence. Jamais celui d'un ami (il révélerait ses nœuds ajoutés) ni d'un inconnu.
- En-têtes `X-Aiodos-Machine` et `X-Aiodos-Digest`.
- PANDORA convertit avec lui sur un PC sans ComfyUI. Le contrat vient alors de cette machine.

### 6.6 Les entrées

- `PUT` répond `{"sha256", "size", "type"}`.
- Erreurs : `input_hash_mismatch`, `input_too_large`, `unsupported_media_type`, `quota_exceeded`.

### 6.7 Les travaux

**`POST /aiodos/v1/jobs`** répond 202, avec `Location: /aiodos/v1/jobs/job_…` et l'objet travail.
- Un `client_ref` ou un `Idempotency-Key` déjà vus rendent le travail existant (200).
- `X-Aiodos-Where` et `X-Aiodos-Priority` ne peuvent que resserrer (I12).

**L'objet travail**

```json
{ "id": "job_8Hk2Qp…", "object": "aiodos.job", "engine": "comfyui",
  "label": "Moods · Séq. 3", "client_ref": "pandora:mood:5f2c9a",
  "status": "in_progress", "pause": null, "priority": "normal",
  "where": ["here", "mine", "friends", "public"], "where_effective": ["here", "mine", "friends"],
  "where_reason": "graphe : les inconnus ne reçoivent que des recettes",
  "progress": { "total": 16, "done": 5, "running": 3, "queued": 8, "failed": 0,
                "percent": 38, "estimated": true, "elapsed_s": 61, "eta_s": 140, "stage": "running",
                "machines": [ { "name": "PC fixe", "place": "here", "running": 1, "done": 3, "median_s": 15.8 },
                              { "name": "Portable", "place": "mine", "running": 1, "done": 1 },
                              { "name": "Paul", "place": "friend", "running": 1, "done": 1, "checked": 0 } ],
                "message": "5 sur 16 · 3 en cours sur 3 machines · environ 2 min 20" },
  "waiting": null,
  "units": [ { "index": 0, "label": "Mood 1", "status": "done", "place": "here", "machine": "PC fixe",
               "seconds": 16.2, "files": ["file_3Lq…"], "verified": "none" },
             { "index": 9, "status": "queued", "stage": "waiting_machine",
               "message": "En attente d'une machine libre qui a sd_xl_base_1.0" } ],
  "files": [ { "id": "file_3Lq…", "unit": 0, "node": "9", "type": "image/png", "bytes": 1648221,
               "sha256": "…", "width": 1024, "height": 1024, "machine": "PC fixe", "place": "here",
               "engine": "comfyui 0.35.1", "verified": "none", "fetched": false, "expires_at": null } ],
  "error": null, "cancelled_by": null,
  "created_at": 1760950000, "started_at": 1760950003, "completed_at": null }
```

- `machine` vaut le nom pour mes machines et mes amis, `null` pour un inconnu (« réseau public »).
- `pause` vaut `{"when": "after_current", "pending": 2}` pendant une pause.
- `waiting` vaut, par exemple, `{"reason": "no_machine_online", "could": [{"name": "Portable", "missing": []}]}`.
- La liste `units` est paginée au-delà de 64.

**Les événements** (`GET /aiodos/v1/jobs/{id}/events`, SSE, filtrés par travail et par appli)
- `job` : l'état complet, en premier, puis à chaque changement, 4 fois par seconde au plus.
- `unit` : une unité change d'état.
- `file` : un nouveau fichier.
- `file_doubt` : un échantillon contredit un fichier livré.
- `machine` : une machine rejoint le lot ou le quitte.
- `end` : la fin du travail.
- `: ping` toutes les 15 s. Rien n'est rejoué : à la reconnexion, l'état complet, puis les fichiers `unfetched`.

**Les commandes** suivent le tableau du § 2.5.
- `cancel` répond 202 `cancelling`. `end` arrive quand le calcul est vraiment arrêté (30 s au plus pour une machine distante).
- `DELETE` oublie un travail fini ; ses fichiers suivent leur garde, sauf avec `files=delete`.

### 6.8 Les fichiers et les limites

**Les fichiers** : la 1re conception, § 4.10, ne change pas (`Range`, liens signés par HMAC, 60 min et 5 usages au plus, `no-store`, `nosniff`). La garde est celle du § 5.3, et l'en-tête `X-Aiodos-Keep-Until` vaut `pending` avant la récupération.

**Les limites, par appli**

| Limite | Défaut |
|---|---|
| Travaux actifs | 50 |
| Unités en file | 1024 (256 par travail) |
| Entrées gardées | 10 Gio |
| Une entrée | 100 Mo |
| Fichiers non récupérés | 20 Gio |
| `PUT` en même temps | 4 |
| Flux SSE | 8 |
| Mises en file par minute | 30 |
| Signalements par heure | 10 |

### 6.9 Les codes d'erreur nouveaux

Ils ont l'enveloppe de la 1re conception (§ 4.2).

| HTTP | `code` | Quand |
|---|---|---|
| 400 | `graph_not_api` | format éditeur envoyé |
| 400 | `graph_invalid` | graphe mal formé, lien ou cycle (`detail.node`) |
| 400 | `patch_invalid` | `set` vise un lien, un nœud absent ou un autre type |
| 400 | `forbidden_value` | chemin, adresse ou annotation (`detail.node`, `input`, `reason`) |
| 400 | `forbidden_node` | nœud du cloud, ou classe refusée dans tous les cercles demandés (`detail.class`) |
| 400 | `input_missing` | jeton sans entrée, ou entrée jamais envoyée |
| 400 | `input_hash_mismatch` | l'empreinte du corps ne correspond pas |
| 400 | `param_out_of_range` | paramètre de recette hors bornes (`detail.min`, `max`) |
| 403 | `scope_denied` | `detail.scope` vaut par exemple `engines:comfyui` |
| 403 | `where_denied` | lieu plus large que l'accord |
| 404 | `engine_not_found`, `input_not_found`, `job_not_found`, `file_not_found`, `recipe_unknown` | introuvable, ou appartient à une autre appli |
| 409 | `engine_unavailable` | aucune machine des cercles permis n'annonce ce moteur |
| 409 | `model_unavailable`, `model_unresolved`, `model_ambiguous` | modèles introuvables, nom inconnu, ou même nom pour plusieurs contenus (`detail.models`, `detail.candidates`) |
| 409 | `license_not_accepted` | licence restrictive non acceptée ici |
| 409 | `job_not_cancellable`, `job_not_pausable` | travail déjà fini |
| 413 | `graph_too_large`, `input_too_large`, `file_too_large` | — |
| 415 | `unsupported_media_type` | type d'entrée non reconnu |
| 429 | `quota_exceeded`, `rate_limited` | `detail.quota`, `Retry-After` |

**Codes d'échec d'une unité** (`units[].error.code`) :
- `engine_error` : le message de ComfyUI et le nœud ;
- `engine_timeout` ;
- `output_invalid`, `output_too_large` ;
- `verification_failed` ;
- `lost` : disparue 5 fois ;
- `no_progress` : remise 8 fois ;
- `no_machine` : `bounds.wait_s` dépassé ;
- `refused_by_lender` : `detail.reason` vaut `contract`, `model`, `license`, `class` ou `memory`. Il n'est montré que si l'unité finit en échec ;
- `cancelled`.

### 6.10 Les clients

**La publication** (décision 6)
- **Les paquets** : `aiodos-client` sur PyPI (module `aiodos_client`) et sur npm (module ES, Node 20.3 et plus). Disponibilité à vérifier (Q6).
- **Les sources**, dans le dépôt d'AIODOS :
  - `clients/python/` (`pyproject.toml`, `src/aiodos_client/`) ;
  - `clients/js/` (`package.json`, `src/`, `index.d.ts`) ;
  - `clients/contract/`.

  `SPDX-License-Identifier: Apache-2.0`, avec `LICENSE` et `NOTICE`. Aucune dépendance.
- **Les versions** :
  - 0.1.0 en L1, avec toute la surface : ce qui n'est pas encore servi lève `NotSupported` selon `features` ;
  - 0.2.0 en L2b, puis une version mineure par livraison ;
  - `API_VERSION` vaut 1.
- **`tools/publish_clients.py`** vérifie avant de construire :
  - les versions concordantes, les essais et le contrat ;
  - les en-têtes de licence ;
  - qu'aucune ligne n'est reprise de PANDORA ni d'ARKLIGHT.

  Puis il construit la roue, l'archive des sources et `npm pack`.
- **La publication** passe par `.github/workflows/clients-publish.yml`, en « trusted publishing » (OIDC) depuis `github.com/AIODOS/aiodos`, avec `npm publish --provenance`. Elle est déclenchée par une étiquette `clients-v0.1.0`. Aucun jeton sur un disque. L'auteur crée les comptes et relie les projets.
- **Les applis épinglent** version et empreinte : `--require-hashes` pour PANDORA, `package-lock.json` pour ARKLIGHT.

**Python : ce que le client offre**

| Fonction | Rôle |
|---|---|
| `Aiodos(app, app_id, version, keystore, lang="fr", url=None)` | le client ; sans état global, sûr entre threads |
| `status()` | l'état, plus `features`, `scopes` et `where` ; ne lève jamais |
| `connect(scopes, where=None, purpose=None, on_code=None, cancel=None, timeout=600)` | le § 6.2 |
| `disconnect()` | `access/revoke`, puis efface la clé du coffre |
| `models(kind)`, `chat(...)`, `embed(texts, model=None)` | 1re conception, § 5.2 |
| `engines()` | `GET /aiodos/v1/engines` |
| `comfy.plan(...)` | pré-vol |
| `comfy.object_info(cls=None)`, `comfy.template(name)` | relais (L2c) |
| `comfy.recipes()`, `comfy.recipe_for(template)` | catalogue (L4) |
| `comfy.tokenize(graph, {node_id: bytes})` | met les jetons `@in:` dans les `LoadImage` et `LoadVideo` donnés, et rend `inputs` |
| `comfy.submit(graph=None, graphs=None, recipe=None, items=None, repeat=None, inputs=None, models="auto", where=None, priority="normal", label=None, client_ref=None, bounds=None, strict=False, sensitive=False) -> Job` | calcule les empreintes, envoie les entrées absentes (`HEAD`, puis `PUT`), puis le travail. `sensitive=True` donne `where=["here", "mine"]` |
| `comfy.run(...) -> list[Path]` | `submit`, puis `wait`, puis `download` |
| `jobs(active=True, client_ref=None, status=None)`, `job(id)` | retrouver ses travaux après un redémarrage |
| `Job.watch(cancel=None, timeout=None)` | itère sur les événements (SSE, reconnexion automatique, étape `unreachable` si AIODOS s'arrête) ; si `cancel()` devient vrai, appelle `Job.cancel()` |
| `Job.wait()`, `pause(when="after_current")`, `resume()`, `cancel()`, `retry_failed()`, `set_priority(p)`, `delete(files=False)`, `report(n, reason)` | § 2.5, § 4.9 |
| `Job.files(unfetched=True)`, `Job.download(dest_dir, stem=None, on_file=None)` | téléchargement avec reprise par `Range` ; un fichier servi en entier est récupéré |
| `File.content()`, `save(path)`, `link(ttl=900, uses=3)`, `delete()` | un fichier |
| `inputs.put(data, type=None) -> sha256` | une entrée à part |

**Erreurs** : celles de la 1re conception, plus `EngineUnavailable`, `ModelUnavailable`, `ModelAmbiguous`, `ForbiddenValue`, `ForbiddenNode`, `GraphInvalid`, `InputTooLarge` et `QuotaExceeded`. `FileDoubt` est un événement, pas une exception.

**JavaScript** : la même surface en camelCase, avec des promesses et `AbortSignal` (`createClient`, `connect`, `comfy.submit`, `job.watch()` en itérateur asynchrone, `job.download()`). Elle tourne dans Node ou dans le processus principal d'Electron, jamais dans le rendu (I6).

**Exemple : PANDORA, quatre variations d'un mood** (fonction bloquante, appelée depuis un QThread)

```python
from aiodos_client import Aiodos, Keystore, errors

ai = Aiodos(app="PANDORA", app_id="arkane.pandora", version=VERSION, lang="fr",
            keystore=Keystore.dpapi(r"%LOCALAPPDATA%\PANDORA\aiodos.key"))
if ai.status().state != "ready":
    ai.connect(scopes=["chat", "engines:comfyui"], purpose="Textes, moods et vidéos de PANDORA",
               on_code=self.code.emit, cancel=self.isInterruptionRequested)

recipe = ai.comfy.recipe_for(template)                      # L4 : jusqu'aux inconnus
if recipe:
    job = ai.comfy.submit(recipe=recipe.id, items=[{"params": recipe.fill(prompt=p, width=w, height=h,
                          seed=base_seed + i)} for i, p in enumerate(prompts)],
                          label=f"Mood · {plan}", client_ref=f"pandora:mood:{uid}")
else:
    oi = ai.comfy.object_info()                             # ou celui du ComfyUI de ce PC
    api = fill(to_api(template_json, oi), oi, info, "", negative, w, h, 0)   # code de PANDORA
    api, inputs = ai.comfy.tokenize(api, {ref_node: png_bytes})
    job = ai.comfy.submit(graph=api, inputs=inputs,
                          items=[{"seed": base_seed + i, "set": [[text_node, "text", p]]}
                                 for i, p in enumerate(prompts)],
                          label=f"Mood · {plan}", client_ref=f"pandora:mood:{uid}",
                          sensitive=has_actor_photo)
for ev in job.watch(cancel=self.isInterruptionRequested):
    self.progress.emit(ev.job.progress.percent or 0, ev.job.progress.message)
paths = job.download(dest_dir, stem=plan)
```

**Exemple : PANDORA, reprise au lancement**

```python
for job in ai.jobs(active=None):
    if job.client_ref and job.client_ref.startswith("pandora:") and job.files(unfetched=True):
        job.download(project_dir_for(job.client_ref))
```

**Exemple : ARKLIGHT PC, l'assistant (L1)**

```js
import { createClient, pickModel } from 'aiodos-client';
const aiodos = createClient({ app: 'ARKLIGHT PC', appId: 'arkane.arklight', version: APP_VERSION,
  key: { get: () => keys.get('aiodos'), set: (k) => keys.set('aiodos', k) }, lang: () => config.lang });
if ((await aiodos.status()).state !== 'ready')
  await aiodos.connect({ scopes: ['chat'], where: ['here'], purpose: 'Assistant lumière',
                         onCode: (code) => broadcast({ type: 'ai.aiodos.code', code }), signal });
const model = pickModel(await aiodos.models({ use: 'chat' }), { needs: ['tools'], vision: true });
const r = await aiodos.chat({ model: model.id, system, messages, tools, think: false, signal, onText });
```

**La recette pour PANDORA** (dans le guide)
- **Dépendance** : `requirements.txt` gagne `aiodos-client==0.2.*`, épinglé par empreinte.
- **`core/aiodos.py`**, sans Qt : le client, l'état en cache, `connect`, les licences.
- **`core/comfy_transport.py`** est le seul changement de fond.
  - Il offre deux transports aux mêmes fonctions : « ComfyUI de ce PC » (celui d'aujourd'hui) et « Mes machines par AIODOS ».
  - Les fonctions : `object_info()`, `put_input(bytes, kind) -> valeur`, `queue(api, label) -> poignée`, `wait(poignée, progress, is_cancelled) -> sorties` et `cancel(poignée)`.
  - `core/comfy_image`, `core/comfy_video`, `api/comfy.ComfyWorker`, `api/comfy_edit` et `api/upscale` (`seedvr_local`) prennent leur transport dans `core.comfy.transport()`. Le réglage `comfy_runs_on` choisit.
- **Pièges corrigés par le transport AIODOS** :
  - plus d'interruption globale (piège 1) ;
  - des noms neutres (piège 4) ;
  - un clip de plus de 100 Mo refusé avant l'envoi (piège 5).
- **À corriger quel que soit le transport** :
  - ne plus téléverser les références chez fal pour un moteur `comfy:` (piège 2) ;
  - ne plus télécharger une adresse quelconque comme référence (piège 6) ;
  - des noms de référence uniques (piège 3).
- **Les images** passent par un lien signé (`File.link()`) pour `image_call.subscribe`, qui fait `requests.get(url)`.
- **L'affichage** : `machine` et `place` de chaque fichier (« calculé chez un ami », « calculé par le réseau public »).
- **`sensitive=True`** pour les fiches de casting et les visages de référence (Q2).

**La recette pour ARKLIGHT PC** : la 1re conception, § 5.6, avec `npm i aiodos-client` et `connect()`. Avant de brancher le client, réserver les messages `ai.*` aux clients de la machine.

---

## 7. Dans AIODOS : fichiers, migrations, essais

### 7.1 Fichiers neufs, par livraison (un par fonctionnalité)

**L1**

La liste du § 6.1 de la 1re conception pour sa L1 : `llm/context.py`, `gateway/errors.py`, `web_guard.py`, `apps.py`, `access_flow.py`, `caller.py`, `scopes.py`, `limits.py`, `usage.py`, `discovery.py`, `info.py`, `model_card.py`, `models_api.py`, `open_page.py`, `api/routes/gateway_apps.py`, `views/gateway-apps.js`, `views/gateway-request.js`, les clients et `tools/gateway_openapi.py`. S'y ajoutent :

| Fichier | Rôle | Prix |
|---|---|---|
| `aiodos/gateway/app_connect.py` | `--app-request` (côté programme d'aide) et `POST /instance/app-request` (côté instance) ; ouverture de la passerelle à l'accord | petit |
| `aiodos/gateway/consents.py` | cercles montrés et validés, par capacité ; carte « nouveau cercle » | petit |
| `tools/publish_clients.py`, `.github/workflows/clients-publish.yml` | vérifications, construction, publication par OIDC | petits |
| `clients/python/pyproject.toml`, `clients/js/package.json`, `clients/js/index.d.ts` | paquets publiables | petits |

**L2a**

| Fichier | Rôle | Prix |
|---|---|---|
| `aiodos/jobs/control.py` | un signal d'arrêt par unité, avec sa raison, branché sur `pacing.stopping()` | petit |
| `aiodos/jobs/shutdown.py` | à l'arrêt : compter les calculs (pour la question), rendre les unités locales sans essai, faire rendre celles calculées ici pour d'autres | petit |
| `aiodos/jobs/estimates.py` | durées habituelles, part estimée d'une unité, débit et ETA d'un lot (fonctions pures et table `engine_durations`) | petit |

**L2b**

| Fichier | Rôle | Prix |
|---|---|---|
| `aiodos/engines/__init__.py`, `aiodos/engines/registry.py` | interface d'adaptateur, registre | petit |
| `aiodos/engines/comfy_find.py` | installation, Python, version, instance ouverte | petit |
| `aiodos/engines/comfyui.py` | l'adaptateur et son client HTTP : `system_stats`, `object_info`, `models`, `upload`, `prompt` avec `prompt_id`, `api/jobs`, annulation ciblée, `view` en flux, `history` (lecture et effacement), `free` | moyen |
| `aiodos/engines/comfy_graph.py` | fonctions pures : forme, jetons, retouches, renumérotation, `_meta`, valeurs interdites, préfixes, sorties | petit à moyen |
| `aiodos/engines/comfy_contract.py` | empreintes de classe, normalisation, `core` | petit |
| `aiodos/engines/comfy_models.py` | résolution par listes de choix, ambiguïtés, réécriture par empreinte | petit |
| `aiodos/engines/comfy_inventory.py` | classes, modèles, `digest` par cercle | petit |
| `aiodos/engines/model_hashes.py` | cache des empreintes, travail de fond qui les calcule | petit |
| `aiodos/engines/media_check.py` | signatures, dimensions, boîtes MP4, durée WebM | petit |
| `aiodos/engines/gpu_share.py` | la carte entre llama.cpp, sd-server et ComfyUI ; règle de la conversation | petit |
| `aiodos/node/keep_awake.py` | `SetThreadExecutionState` sur secteur | petit |
| `aiodos/jobs/kinds/engine.py` | type `engine_job`, tâche `engine_unit` : `normalize`, `split`, `expand`, `requires`, `here`, `compute`, `validate`, `assimilate`, `shared_keys`, `lease_s` | moyen |
| `aiodos/jobs/declines.py` | `work/decline`, table `unit_declines` | petit |
| `aiodos/network/input_cache.py` | chez celui qui calcule : plusieurs entrées, cache par (propriétaire, empreinte) | petit |
| `aiodos/network/work_put.py` | chez le propriétaire : manifeste, morceaux, `.part`, bornes, reprise | petit |
| `aiodos/network/outbox.py` | chez celui qui calcule : boîte d'envoi persistante, garde par cercle | petit à moyen |
| `aiodos/network/assignments.py` | chez celui qui calcule : unités notées, rendues à l'arrêt, reprises ou rendues au redémarrage | petit |
| `aiodos/gateway/input_store.py`, `aiodos/gateway/inputs_api.py` | dépôt des entrées par appli, quota ; `HEAD`, `PUT` | petits |
| `aiodos/gateway/engines_api.py` | `GET /engines`, `plan` | petit |
| `aiodos/gateway/jobs_bridge.py` | un travail de moteur pour une appli : droits, lieu, idempotence | petit |
| `aiodos/gateway/jobs_api.py`, `job_events.py`, `progress.py` | routes, SSE filtré, avancement (fonction pure) | petits |
| `aiodos/gateway/files_api.py`, `links.py`, `retention.py` | fichiers, liens signés, garde | petits |
| `aiodos/api/routes/engines.py` | routes de l'interface | petit |
| `aiodos/ui/static/js/views/engines.js` | vue « Moteurs des applications » | petit |
| `aiodos/ui/static/js/views/job-map.js` | carte du lot : une case par unité, une ligne par machine, « en attente d'une machine : Portable l'a, mais il est hors ligne » | petit |
| `tests/comfy_stub.py` | le faux ComfyUI (§ 7.6) | petit |

**L2c**

| Fichier | Rôle | Prix |
|---|---|---|
| `aiodos/engines/comfy_instance.py` | instances lancées par AIODOS : `mine` en L2c, puis `friends` (L3) et `public` (L4) | moyen |
| `aiodos/engines/comfy_relay.py` | relais d'`object_info` et des gabarits depuis ma machine de référence (`engines/read`) | petit |

**L3**

| Fichier | Rôle | Prix |
|---|---|---|
| `aiodos/engines/comfy_allowlist.py`, `aiodos/engines/comfy_allowlist.json` | liste blanche signée, par version | petit |
| `aiodos/engines/catalogue.py` | vérification de la signature (contexte `aiodos-catalogue\n`) de la liste et, en L4, des recettes | petit |
| `aiodos/engines/comfy_bounds.py` | bornes par cercle | petit |
| `aiodos/engines/lending.py` | prêt par cercle : réglages, modèles prêtés, paquets, filtre des capacités, `foreign_writers` | petit |
| `aiodos/engines/licenses.py`, `aiodos/engines/model_licenses.json` | table des licences | petit |
| `aiodos/engines/media_clean.py` | Pillow : entrées et sorties réécrites, EXIF et blocs de texte retirés, bombes refusées | petit |
| `aiodos/engines/content_check.py` | contenu non vide, SSIM en numpy, égalité exacte | petit |
| `aiodos/engines/witness.py` | images témoins des vidéos | petit |
| `aiodos/jobs/verify.py` | échantillons après livraison, doute, unité refaite | petit à moyen |
| `aiodos/gateway/report_api.py` | signalement par l'appli | petit |
| `aiodos/ui/static/js/views/engines-lend.js` | carte de prêt | petit |
| `tools/comfy_allowlist.py`, `tools/bench_comfy_verify.py` | fabrication de la liste ; mesure M2 | petits |

**L4** (**gros**)

| Fichier | Rôle |
|---|---|
| `compagnon/src/public_work.rs` | ALPN `aiodos/public-work/1` sur le point public, actif seulement si le prêt public l'est ou qu'un travail public est en cours ; débit par clé ; corps bornés |
| `compagnon/src/work_announce.rs` | annonces signées « travail disponible » sur le gossip public |
| `compagnon/src/ephemeral.rs` | un point de terminaison éphémère par travail public |
| `aiodos/network/public_work.py` | bail, entrées, sorties et résultat avec un inconnu |
| `aiodos/network/ephemeral.py` | clés éphémères (DPAPI), effacées à la fin du travail |
| `aiodos/network/strangers.py` | table des inconnus, réputation, mise de côté |
| `aiodos/jobs/redundancy.py` | double calcul pour les nouveaux venus, quorum à trois |
| `aiodos/network/receipts.py` | reçus signés, forme minimale (§ 9.3, Q7) |
| `aiodos/engines/recipes.py`, `aiodos/engines/recipes/` | recettes : vérification, paramètres, construction du graphe |
| `aiodos/engines/prompt_filter.py` | filtre de consignes signé, si Q3 le retient |
| `tools/comfy_recipes.py` | fabrication du catalogue |

**L5** : `aiodos/engines/sdserver.py` ; `aiodos/engines/model_copy.py`.

### 7.2 Fichiers retouchés

**L1** : le § 6.2 de la 1re conception, plus `aiodos/__main__.py` (`--app-request`), `aiodos/api/routes/system.py` (`/instance/app-request`) et `aiodos/gateway/scopes.py` (`engines:<moteur>`, cercle `public`).

**L2a**

| Fichier | Changement |
|---|---|
| `aiodos/jobs/executor.py` | un signal par unité (`control`), passé à `run_local` (l.45) ; `stop()` appelle `shutdown.release_running()` ; l'exécuteur ne prend que les unités dont `reach` contient `here` |
| `aiodos/jobs/queue.py` | `create_job(…, owner_app, public_id, label, client_ref, reach)` ; `cancel_job` lève les signaux et pose `cancelled_by` ; `pause_job(when)` ; `recover` corrigé (§ 2.5) ; `queue_paused` ; tri d'équité ; `queued_at` posé à chaque retour en file ; `handouts` |
| `aiodos/jobs/leases.py` | `renew(unit, node, progress) -> state` (`running`, `resumed`, `paused`, `cancelled`, `reassigned`), qui raccroche une unité non redonnée ; `expire` : essai rendu, `lost` + 1, `reach` ramené à mes machines à 2, échec à 5 ; `claim_remote` : cercle, délai, `mine_free`, `unit_declines`, `handouts`, équité ; `take_back` refuse un travail annulé |
| `aiodos/jobs/models.py` | `MAX_LOST = 5`, `MAX_INTERRUPTS = 5`, `MAX_HANDOUTS = 8`, `RECOVER_GRACE = 900`, `ENGINE_LEASE_S = 300`, `RENEW_S = 30` |
| `aiodos/jobs/registry.py` | `TaskSpec` : `expand`, `inputs(payload)`, `outputs_as_files`, `lease_s`, `renew_s`, `version` ; `run_local(name, payload, stop, progress)` |
| `aiodos/network/work.py` | `renew` : `state` et `progress` ; `result` refuse un travail annulé |
| `aiodos/network/worker.py` | `_renew` suit `state` (continue sur `resumed`, arrête sur `paused`, `cancelled` ou `reassigned`) et envoie `progress` ; `renew_s` lu dans l'ordre |
| `aiodos/db.py` | migration 19 |
| `aiodos/api/routes/jobs.py`, `aiodos/ui/static/js/views/jobs.js` | pause « après » et « tout de suite », tout mettre en pause, « pour PANDORA », libellé |
| `aiodos/ui/tray.py`, `aiodos/ui/window.py` | la question à la fermeture |

**L2b**

| Fichier | Changement |
|---|---|
| `aiodos/network/work.py` | `can_serve` : la branche `engine` ; `lease` : `tasks`, inventaire à la demande, cercles, délais ; `_order` : `expand`, plusieurs entrées, `lease_s`, `renew_s` ; `blob` : `sha256` ; `result` : manifeste ; `HANDLERS` : `work/put`, `work/decline` |
| `aiodos/network/worker.py` | `capabilities` : `tasks` et `engines` filtrés par cercle ; entrées par `input_cache` ; sorties par `outbox` ; `assignments` ; `_candidates` trié par cercle ; règles du retour de l'utilisateur et de `lend_in_use_max_s` |
| `aiodos/network/body_limit.py` | `work/put` jusqu'à `SEALED_BODY`, d'une machine qui calcule pour celle-ci |
| `aiodos/network/p2p_app.py` | les nouvelles actions |
| `aiodos/node/identity.py`, `aiodos/network/sharing.py` | `engines` dans le profil, filtré par cercle ; `order_for_friend` pour `engine_unit` ; `shared_log` étendu |
| `aiodos/imaging/local_copies.py` | expose ses dossiers connus |
| `aiodos/paths.py` | `inputs_dir`, `out_dir`, `outbox_dir`, `work_in_dir`, `work_cache_dir`, `engines_dir` |
| `aiodos/settings.py` | les réglages du § 3.2 ; `gateway_link_secret` dans `SECRET_KEYS` |
| `aiodos/gateway/server.py` | les nouveaux routeurs ; `features` |
| `aiodos/ui/static/js/views/jobs.js`, `settings.js`, `gallery.js` | machines par unité, carte du lot, fichiers non récupérés, vue Moteurs, filtre « Créations des applications » et « Garder » |
| `requirements.txt` | Pillow, déjà importé, enfin listé |

**L3** :
- `network/work.py` et `worker.py` : cercle `friends` pour les unités de moteur, instance `friends` ;
- `network/sharing.py` : inventaire réduit aux modèles prêtés ;
- `jobs/kinds/engine.py` : `check` par cercle, `sample`, images témoins ;
- `tools/publish.py` : signer la liste blanche (contexte `aiodos-catalogue\n`) ;
- `updates/manifest.py` : exposer la vérification par contexte ;
- `ui/views/network.js` : « Ce PC calcule pour… ».

**L4** :
- `compagnon/src/public.rs` et `public_profile.rs` : ALPN de travail, profil v2 ;
- `network/public.py` : `lends`, `engines`, `recipes`, `public_receive` ;
- `docs/phase3-reseau-public.md`.

### 7.3 Migrations

**Les numéros.** 18 à 22 sont à réserver dès maintenant sur toutes les branches. On ne complète jamais une migration déjà livrée.

**La compatibilité.** Les migrations ne font qu'ajouter, et `_replayable` saute les colonnes déjà présentes. Une version précédente qui rouvre la base ne plante pas (`db._ensure_schema`, vérifié par la 1re conception) : ses unités `engine_unit` restent en file, sans machine pour elles.

**18 (L1)** : celle du § 6.3 de la 1re conception, avec deux colonnes de plus à `gateway_apps` :
- `reach TEXT NOT NULL DEFAULT 'here,mine,friends,public'` ;
- `consents TEXT NOT NULL DEFAULT '{}'` : `{capacité: {cercle: date}}`.

**19 (L2a)** : la file.

```sql
ALTER TABLE jobs ADD COLUMN public_id TEXT;
ALTER TABLE jobs ADD COLUMN owner_app TEXT;
ALTER TABLE jobs ADD COLUMN label TEXT;
ALTER TABLE jobs ADD COLUMN client_ref TEXT;
ALTER TABLE jobs ADD COLUMN reach TEXT;          -- 'here,mine,friends,public' ; NULL : règles d'avant
ALTER TABLE jobs ADD COLUMN pause_mode TEXT;     -- after_current | now
ALTER TABLE jobs ADD COLUMN cancelled_by TEXT;   -- app | aiodos | revoked
ALTER TABLE jobs ADD COLUMN wait_until REAL;     -- bounds.wait_s
ALTER TABLE units ADD COLUMN reach TEXT;
ALTER TABLE units ADD COLUMN queued_at REAL;
ALTER TABLE units ADD COLUMN lost INTEGER NOT NULL DEFAULT 0;
ALTER TABLE units ADD COLUMN interrupts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE units ADD COLUMN handouts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE units ADD COLUMN place TEXT;         -- here | mine | friend | public
ALTER TABLE units ADD COLUMN progress TEXT;      -- JSON {stage, p, at, estimated}
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_public ON jobs(public_id) WHERE public_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_ref ON jobs(owner_app, client_ref) WHERE client_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_jobs_owner_app ON jobs(owner_app, created);
CREATE TABLE IF NOT EXISTS engine_durations (
    signature TEXT NOT NULL, machine TEXT NOT NULL, seconds TEXT NOT NULL,   -- JSON des 20 dernières
    PRIMARY KEY (signature, machine));
```

**20 (L2b)** : moteurs, entrées, fichiers ; boîte d'envoi et unités notées, côté prêteur.

```sql
ALTER TABLE units ADD COLUMN run_uuid TEXT;
ALTER TABLE units ADD COLUMN verified TEXT;      -- none | content | sampled_exact | sampled_close | redundant | doubt
CREATE TABLE IF NOT EXISTS gateway_inputs (
    app_id TEXT NOT NULL, sha256 TEXT NOT NULL, type TEXT NOT NULL, size INTEGER NOT NULL,
    created REAL NOT NULL, last_used REAL NOT NULL, PRIMARY KEY (app_id, sha256));
CREATE TABLE IF NOT EXISTS job_inputs (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, sha256 TEXT NOT NULL,
    PRIMARY KEY (job_id, sha256));
CREATE TABLE IF NOT EXISTS gateway_files (
    public_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, job_id INTEGER NOT NULL, unit_index INTEGER NOT NULL,
    node TEXT, type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, width INTEGER, height INTEGER,
    machine TEXT, place TEXT, engine TEXT, verified TEXT NOT NULL DEFAULT 'none',
    served INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, fetched_at REAL, expires_at REAL,
    kept INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS engine_inventory (
    node_id TEXT NOT NULL, engine TEXT NOT NULL, circle TEXT NOT NULL, digest TEXT NOT NULL,
    data TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (node_id, engine, circle));
CREATE TABLE IF NOT EXISTS model_hashes (
    path TEXT PRIMARY KEY, engine TEXT NOT NULL, folder TEXT, name TEXT, size INTEGER NOT NULL,
    mtime REAL NOT NULL, sha256 TEXT, checked REAL);
CREATE TABLE IF NOT EXISTS unit_declines (
    job_id INTEGER NOT NULL, node TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (job_id, node));
CREATE TABLE IF NOT EXISTS work_assignments (      -- chez le prêteur, sans contenu
    owner TEXT NOT NULL, unit INTEGER NOT NULL, circle TEXT NOT NULL, task TEXT NOT NULL,
    run_uuid TEXT, instance TEXT, stage TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY (owner, unit));
CREATE TABLE IF NOT EXISTS work_outbox (           -- chez le prêteur
    owner TEXT NOT NULL, unit INTEGER NOT NULL, circle TEXT NOT NULL, manifest TEXT NOT NULL,
    folder TEXT NOT NULL, bytes INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0, next_try REAL, PRIMARY KEY (owner, unit));
CREATE UNIQUE INDEX IF NOT EXISTS ux_units_run ON units(run_uuid) WHERE run_uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_files_app ON gateway_files(app_id, created);
CREATE INDEX IF NOT EXISTS ix_files_expiry ON gateway_files(expires_at);
CREATE INDEX IF NOT EXISTS ix_model_hashes_sha ON model_hashes(sha256);
```

Les colonnes `images.public_id`, `owner_app` et `expires_at` de l'ancienne migration 19 disparaissent : il n'y a plus de créations.

**21 (L3)** :
- `engine_samples (unit_id, node_id, kind, score, verdict, at)` ;
- `unit_reports (unit_id, app_id, reason, at)`.

**22 (L4)** :
- `strangers (key PRIMARY KEY, first_seen, last_seen, units_done, units_checked, mismatches, reports, failures, given_s, received_s, reputation, aside_at, aside_reason, excluded_at)` ;
- `public_job_keys (job_id PRIMARY KEY, public_key, secret, created)` : `secret` protégé par DPAPI, effacé à la fin du travail ;
- `receipts (id, key, unit_ref, compute_s, at, signature)`.

### 7.4 Essais fonctionnels

**L1** : ceux de la 1re conception, plus `tests/test_app_connect.py`.
- La passerelle est fermée, la demande passe par le canal de l'instance, et la carte s'affiche.
- L'accord ouvre la passerelle ; la clé est relevée une fois.
- Un refus la laisse fermée.
- Une reconnexion avec une clé valide ne crée pas de clé.
- Une `show_key` fausse donne 403 ; un autre compte Windows donne 403.

**L2a**
- `tests/test_jobs_cancel_real.py` : une unité locale reçoit `stop` ; ailleurs, `renew` répond `cancelled` et le résultat est refusé.
- `tests/test_jobs_pause.py` : « après » laisse finir ; « tout de suite » arrête et rend sans essai ; pause de toute la file ; reprise au rang.
- `tests/test_leases_resilience.py` :
  - un trou de 120 s ne fait pas jeter le calcul (`resumed`) ;
  - une unité redonnée entre-temps répond `reassigned` ;
  - `recover` garde les unités confiées ailleurs pendant 15 min, et leur résultat tardif est accepté ;
  - un arrêt ne coûte pas d'essai, mais au sixième, l'essai compte ;
  - `lost` monte, pas `attempts` ; à 2, l'unité ne va plus qu'à mes machines ; à 5, elle échoue ;
  - à 8 remises, `no_progress`.
- `tests/test_jobs_fairness.py` : à priorité égale, la seconde appli n'attend pas la fin du lot de la première.
- `tests/test_jobs_shutdown.py` : la question compte les calculs ; l'arrêt propre rend tout.
- `tests/test_jobs_queue.py` et `tests/test_images_e2e.py`, existants, passent toujours.

**L2b**
- `tests/test_comfy_graph.py` :
  - format éditeur refusé ;
  - jetons `@in`, `@model`, `@seed` ;
  - `set` refusé sur un lien ou un autre type ;
  - renumérotation déterministe et correspondance inverse ;
  - `_meta` vaut `{}` ;
  - `filename_prefix` forcé.
- `tests/test_comfy_contract.py` : même empreinte malgré des listes de fichiers différentes ; empreinte différente si une entrée change ; `core`.
- `tests/test_comfy_models.py` : résolution par listes de choix, `model_ambiguous`, `model_unresolved`, réécriture avec l'autre barre de dossier.
- `tests/test_engines_inventory.py` : empreintes en cache, recalculées si le fichier change ; un fichier sans empreinte ne sert qu'à mes machines.
- `tests/test_engine_job_split.py` : `items`, `repeat`, 64 graphes et 256 unités au plus, graphe gardé une fois, `expand`.
- `tests/test_engine_job_local.py`, avec le faux ComfyUI :
  - `prompt_id` égal à `run_uuid` ;
  - suivi, `view` limité au sous-dossier ;
  - historique effacé ;
  - fichiers dans `gateway_files`.
- `tests/test_engine_job_remote.py`, avec deux nœuds d'essai :
  - l'inventaire voyage une fois par `digest` ;
  - trois entrées, dont une commune envoyée une seule fois ;
  - le manifeste, puis `work/put` repris à l'octet après une coupure ;
  - la boîte d'envoi survit au redémarrage du prêteur et se vide au retour du propriétaire ;
  - `work_assignments` après un arrêt brutal du prêteur ;
  - `work/decline {contract}` écarte cette machine pour ce travail.
- `tests/test_engine_can_serve.py` : cercles, délais (0 quand mes machines sont prises), modèles, `core`, mémoire, durée et `in_use`, refus notés.
- `tests/test_worker_order.py` : mes machines, puis les amis, puis le public, puis la réciprocité.
- `tests/test_gateway_engine_jobs.py` :
  - mise en file, `client_ref` idempotent ;
  - 404 avec la clé d'une autre appli ;
  - `where` qui resserre, 403 s'il élargit ;
  - `where_effective` sans `public` pour un graphe ;
  - `plan` ;
  - SSE : l'état complet d'abord, puis `file`, puis `end`.
- `tests/test_gateway_inputs.py` : `HEAD` et `PUT`, empreinte fausse, taille, type, quota, isolement entre applis.
- `tests/test_gateway_files_retention.py` : rien n'est effacé avant la récupération ; 7 jours après ; des plages partielles ne comptent pas, des plages qui couvrent tout comptent ; quota ; liens.
- **Bout en bout** : un lot de 20 unités contre le faux ComfyUI. On ferme et rouvre le client, on arrête et relance AIODOS : 20 fichiers récupérés, une seule fois chacun.

**L2c**
- `tests/test_comfy_recover.py` : AIODOS redémarre pendant que ComfyUI finit ; le résultat vient de l'historique.
- `tests/test_comfy_instance.py` : ligne de commande, environnement nettoyé, dossiers propres, `children.bind`, arrêt après 10 min, une seule instance active.
- `tests/test_comfy_relay.py` : `object_info` de ma machine de référence, jamais d'un ami.
- `tools/essai_comfy_reel.py`, sur un geste de l'auteur : un gabarit SDXL sur ce PC, puis sur le Portable.

**L3**
- `tests/test_engine_lending.py` : rien n'est annoncé aux amis par défaut ; modèles filtrés ; licence restrictive non acceptée non annoncée ; paquet non nommé refusé ; `foreign_writers` refuse.
- `tests/test_engine_verify_friends.py` : 5 % d'échantillon, égalité exacte à empreinte égale, SSIM sinon, mise de côté, `file_doubt`, images témoins.
- `tests/test_media_clean.py` : EXIF et blocs de texte retirés, bombes refusées.

**L4**
- `compagnon/tests/public_work.rs` : ALPN, annonces, débit, corps bornés ; un nœud qui ne prête pas n'accepte pas l'ALPN.
- `tests/test_recipes.py` : signature et contexte ; paramètres bornés ; graphe construit chez l'exécutant ; recette d'une autre version.
- `tests/test_public_work.py` : clé éphémère par travail ; redondance des nouveaux venus ; 20 % au plus ; réputation ; temps estimé, pas déclaré.

**Clients** : rejouer le contrat ; `connect()` avec un faux programme d'aide ; `download()` qui reprend ; `jobs()` après un redémarrage ; `sensitive` qui donne `here` et `mine`.

### 7.5 Essais de sécurité (les invariants)

| Invariant | Essai |
|---|---|
| I1, I5 à I8, I10 à I12 | ceux de la 1re conception (§ 6.5), étendus aux nouvelles routes ; I8 : un mot témoin dans une consigne, une retouche et une image ne se trouve ni au journal, ni dans `gateway_log`, `shared_log`, `work_assignments` ou `work_outbox` |
| I2 | `tests/test_app_connect.py` : seule la carte d'accord ou les réglages ouvrent la passerelle |
| I3 | `tests/test_sec_comfy_api_nodes.py` : `api_node` refusé dans tous les cercles ; le faux ComfyUI fait échouer l'essai s'il reçoit `extra_data` |
| I4 | `tests/test_sec_engine_reach.py` : cercle décoché, non validé ou coupé par `apps_keep_mine` ; une unité `mine` jamais louée à un ami ; un ami rien avant 15 s, un inconnu rien avant 60 s, sauf si mes machines sont prises ; le prêteur qui ne prête pas |
| I9 | `tests/test_sec_label_privacy.py` : ni nom d'appli, ni `label`, ni `client_ref`, ni `_meta` d'origine (`pandora_end_frame`), ni identifiant de nœud d'origine, ni nom de fichier d'origine dans un ordre envoyé |
| I13, I14 | `tests/test_sec_comfy_paths.py` : `x.png [output]`, `x.png[output]`, `[temp]`, `C:\`, `\\serveur`, `/etc`, `../`, `%2e%2e`, `http://`, `file:`, `CON`, caractère nul refusés à la mise en file **et** chez celui qui calcule, en contournant la première vérification ; `://` accepté dans une consigne multiligne |
| I15 | `tests/test_sec_isolated_instance.py` : `--base-directory` à part, `--disable-all-custom-nodes`, `--disable-api-nodes`, `--disable-metadata`, jamais `--enable-manager`, 127.0.0.1, port fermé aux navigateurs, base en mémoire, dossiers vidés même après un échec ; l'instance `public` ne reçoit jamais `--whitelist-custom-nodes` ; une unité d'inconnu ne va jamais dans `mine` ni `friends` |
| I16 | `tests/test_sec_lent_models.py` : un modèle non coché n'apparaît pas dans l'inventaire du cercle, et un ordre qui le demande est refusé |
| I17 | `tests/test_sec_comfy_cancel_scoped.py` : le faux ComfyUI fait échouer l'essai sur `/interrupt` sans `prompt_id` ou sur `/queue {"clear"}` |
| I18 | `tests/test_gateway_files_retention.py` |
| I19 | `tests/test_sec_no_download.py` : aucune route du gestionnaire appelée ; un modèle absent écarte la machine ; méta-essai : aucune route de la passerelle ne relaie une requête arbitraire vers ComfyUI |
| I20 | `tests/test_leases_resilience.py`, `tests/test_engine_job_remote.py` |
| I21 | `tests/test_sec_recipes.py` : un ordre public qui porte `graph` est refusé ; une recette non signée, signée par une autre clé ou sous le contexte des manifestes est refusée |
| I22 | `tests/test_sec_lending.py` : rien n'est calculé pour un ami ou un inconnu sans le réglage du cercle |
| I23 | `tests/test_media_check.py` : exécutable renommé en `.png`, PNG annonçant 50 000 × 50 000, MP4 dont `mvhd` annonce 10⁹ s, image unie, bruit, mauvaise taille, trop de fichiers : tous refusés |
| I24 | `tests/test_sec_lending.py` : `foreign_writers` simulé, prêt refusé avec sa raison |
| I25 | `tests/test_sec_comfy_isolation.py` : deux unités de deux demandeurs ne voient pas leurs fichiers ; `/free {"free_memory": true}` entre deux demandeurs ; cache des entrées jamais partagé |

**Essais de plus**
- `tests/test_sec_comfy_outputs.py` : seuls les fichiers de `aiodos/<run_uuid>` des nœuds de sortie sont récupérés ; un `filename_prefix` piégé est remplacé.
- `tests/test_sec_engine_bounds.py` : trop de nœuds, une taille trop grande, une unité trop longue (annulée), une sortie trop lourde (lecture arrêtée).
- `tests/test_sec_work_put.py` : un `put` d'une machine qui ne tient pas l'unité est refusé ; une taille au-delà de la borne est refusée sans rien écrire ; le disque est préservé.
- `tests/test_sec_comfy_allowlist.py` :
  - chaque classe de la liste a son `python_module` et ses entrées bornées ;
  - aucune ne figure parmi les exclusions nommées ;
  - une classe du cœur annoncée par un autre module est refusée ;
  - une version de ComfyUI non revue ne prête qu'aux recettes.
- `tests/test_sec_comfy_metadata.py` : aucun bloc `prompt` ni `workflow` dans un PNG rendu à un ami ou à un inconnu.

### 7.6 Le faux ComfyUI (`tests/comfy_stub.py`)

- Sur le modèle de `tests/router_stub.py` (branche de la passerelle).
- Il sert `/system_stats`, `/object_info`, `/object_info/{classe}`, `/models`, `/models/{dossier}`, `/upload/image`, `/prompt` (avec `prompt_id`), `/api/jobs/{id}`, `/api/jobs/{id}/cancel`, `/history`, `/history/{id}`, `/view`, `/interrupt`, `/queue` et `/free`.
- Il note chaque appel, et fait échouer un essai sur `/interrupt` sans `prompt_id`, `/queue {"clear"}` ou un `extra_data`.
- Il simule un calcul qui avance, une erreur de nœud, un redémarrage qui vide l'historique, une sortie énorme et une lenteur.

---

## 8. La documentation

| Fichier | Contenu |
|---|---|
| `docs/integrer-aiodos.md` (neuf, le principal) | Le guide de la 1re conception (§ 7). Le chapitre des créations devient « Faire calculer votre moteur par d'autres machines » : graphe ou recette, jetons, modèles par empreinte, `plan`, lots, file (pauses, reprise, récupération), qui a calculé et comment c'est vérifié, signalement, `where` et `sensitive`. Recettes pour PANDORA (`core/comfy_transport.py`) et ARKLIGHT PC. Installation par `pip` et `npm` |
| `docs/api-applications.md`, `docs/api-applications.openapi.json` | La référence, route par route, avec un exemple `curl` ou PowerShell pour chacune |
| `docs/preter-un-moteur.md` (neuf, pour le prêteur) | Ce que veut dire prêter son ComfyUI, par cercle ; l'instance séparée ; ce qui ne peut pas tourner ; les licences ; les réglages ; comment arrêter ; ce que voit chacun ; ce qui reste possible, sans fard |
| `docs/moteurs-applications.md` (neuf, technique) | Les adaptateurs, le format d'unité, le rejeu, l'inventaire, le contrat, les bornes, la liste blanche et sa revue à chaque version, les recettes et leur fabrication, comment ajouter un moteur, les mesures M1 à M9 |
| `docs/file-de-travaux.md` (neuf) | La file comme un torrent : états, pauses, reprise, arrêt, redémarrage, disparitions, compteurs, estimations, carte du lot |
| `docs/phase3-reseau-public.md` (retouché) | § 1 et § 4 réécrits selon la décision 2 : ce qui part chez un inconnu est ce que l'appli y a accordé, coché par défaut, en recettes, images d'abord. § 8 : 3.1 à 3.4 replacées dans L4. § 9 : les décisions du 20/10 |
| `docs/phase3-verification-images.md` (complété) | Les mesures de ComfyUI (M2, M9) |
| `docs/generation-repartie.md` (retouché) | « Les moteurs des applis » : AIODOS répartit, l'appli garde son moteur ; ComfyUI piloté par HTTP, jamais embarqué |
| `docs/autres-outils.md`, `README.md` | La carte « Applications et autres outils », les moteurs, le prêt |
| `clients/python/README.md`, `clients/js/README.md`, `clients/CHANGELOG.md` | Installation, épinglage par empreinte, exemples, faux serveurs, versions, licence |

---

## 9. Les livraisons et les questions

### 9.1 Les livraisons, dans l'ordre

| # | Livraison | Contenu | Prix | Fini quand | Ce que l'auteur peut faire ensuite |
|---|---|---|---|---|---|
| 0 | **L0 Préalable** | Valider l'arbre de travail (correctifs de l'audit du 20/10 : `body_limit`, `relay_body`, `admission`, `private`, essais `test_securite_*`) ; pousser et fusionner `distracted-satoshi`, qui contient `upbeat-chaum` ; réserver les migrations 18 à 22 ; réserver `aiodos-client` sur PyPI et npm | moyen | `pytest` et `cargo test` verts sur `main` | — |
| 1 | **L1 Branchable (texte)** | Étapes 1 à 4 de la 1re conception ; `app_connect` ; `consents` ; migration 18 ; clients 0.1.0 publiés ; le guide | moyen à gros | Une appli se connecte alors que la passerelle était fermée. L'assistant d'ARKLIGHT PC marche par `aiodos-client` (outils, vision, 16 k jetons). PANDORA fait passer translate, enhance, video_prompt, la vision et les conversations | Brancher l'assistant d'ARKLIGHT PC et les textes de PANDORA |
| 2 | **L2a Socle de la file** | Vraie annulation ; pause « après » et « tout de suite » ; pause de toute la file ; `recover` corrigé ; raccrochage des baux ; `lost`, `interrupts`, `handouts` ; équité ; question à la fermeture ; estimations ; migration 19 | moyen, au cœur de la file | Les essais L2a passent ; « Annuler » arrête pour de vrai ; quitter AIODOS pendant une indexation ne coûte plus d'essai | Profite déjà à l'indexation et aux images d'AIODOS |
| 3 | **L2b ComfyUI sur mes machines** | Adaptateur, inventaire, empreintes, contrat ; tâche `engine_unit` et `expand` ; plusieurs entrées et cache ; manifeste, `work/put`, boîte d'envoi ; `work_assignments` ; `work/decline` ; délais par cercle ; passerelle (moteurs, `plan`, entrées, travaux, fichiers, garde) ; vue Moteurs ; carte du lot ; veille empêchée ; migration 20 ; clients 0.2.0 | gros | Un lot de 8 moods de PANDORA se fait sur PC fixe et Portable en même temps. On ferme PANDORA et on la rouvre : les fichiers sont là. On coupe le Wi-Fi du Portable 6 min : rien n'est perdu. On redémarre le PC fixe pendant que le Portable calcule : son résultat arrive | PANDORA : `core/comfy_transport.py` et le réglage « Mes machines par AIODOS » ; images, H3 par ComfyUI, « Modifier un clip », SeedVR2 |
| 4 | **L2c Confort** | Instance `mine` lancée sans fenêtre ; `object_info` et gabarits relayés depuis ma machine de référence ; reprise depuis l'historique | moyen | Une machine où ComfyUI n'est pas ouvert calcule ; PANDORA tourne sur un portable sans ComfyUI | — |
| 5 | **Mesures** | M1 à M9 (§ 9.2) | petit | Les chiffres sont dans `docs/moteurs-applications.md` et règlent les bornes et les seuils | — |
| 6 | **L3 Mes amis** | Instance `friends` ; liste blanche signée (`tools/comfy_allowlist.py`, contexte `aiodos-catalogue\n`) ; bornes ; carte de prêt ; licences ; `foreign_writers` ; Pillow des deux côtés ; contrôle du contenu ; échantillons, images témoins, doute ; signalement ; carte « nouveau cercle » ; migration 21 | gros | Un ami prête son ComfyUI ; les travaux de PANDORA y passent avec les miens ; un graphe piégé est refusé chez lui | Les amis |
| 7 | **L4 Les inconnus (images)** | Recettes et catalogue signés (`tools/comfy_recipes.py`) ; instance `public` ; ALPN de travail public, annonces, point éphémère par travail ; table des inconnus, redondance, quorum, réputation ; temps estimé ici ; profil public v2 ; reçus minimaux ; filtre et charte selon Q3 ; migration 22 | gros | Deux AIODOS sur deux réseaux différents s'échangent un travail d'images vérifié par redondance. **La version signée attend ton essai** (18/10) et l'étude d'isolement (Q4) | Le réseau public |
| 8 | **L5** | sd-server (H3 local) ; copie des modèles entre mes machines, licence acceptée | moyen | — | H3 local réparti |

### 9.2 Les mesures

- **M1** : le poids de `/object_info` complet et par classe ; la règle qui reconnaît une liste de fichiers ; la durée de l'inventaire (empreintes de 100 à 500 Go).
- **M2** : la reproductibilité de ComfyUI, sur la même machine deux fois, sur deux cartes, avec et sans `--deterministic`. On compare au SSIM des tricheries (moitié des pas, autre graine, plus petite taille, image unie, bruit) : seuil par recette, et l'égalité exacte tient-elle à empreinte égale ?
- **M3** : lancer une instance depuis `standalone-env` ; son temps de démarrage ; la mémoire vidéo quand Desktop tourne aussi, sur 8 Go.
- **M4** : le poids des sorties de SeedVR2 et de « Modifier un clip ».
- **M5** : nvfp4 et int8 « convrot » de H3 sur une autre carte.
- **M6** : le coût de `GetExtendedTcpTable` à chaque requête (1re conception).
- **M7** : un jeton à intégrité basse, ou un AppContainer, avec CUDA et le chargement des modèles.
- **M8** : le débit par les relais de n0 pour des sorties de 50 à 500 Mo, par `work/put` de 1 Mio.
- **M9** : les images témoins (`ImageFromBatch`) sur les gabarits vidéo, et la stabilité d'un MP4 d'une passe à l'autre.

### 9.3 Les questions à l'auteur, avec ma recommandation

1. **La décision 2 vaut-elle aussi pour les conversations, la vision et les embeddings des applis ?**
   *Recommandation :*
   - chez mes amis, oui pour la conversation en texte seul, cochée par défaut comme les travaux de moteur ;
   - chez les inconnus, non en v1 : une réponse ne se vérifie pas à bas prix, le prêteur lit toute la conversation, et le protocole public n'a pas de conversation ;
   - la vision et les outils restent sur ce PC (le prêt de modèles n'en a pas) ;
   - les embeddings aussi, pour que les vecteurs viennent du même modèle.
2. **Les photos de personnes réelles** (casting, visages de référence) partiraient chez des inconnus avec le « coché par défaut ».
   *Recommandation :* PANDORA marque ces travaux `sensitive=True` (ce PC et mes machines) ; l'utilisateur peut élargir dans PANDORA. La décision 2 reste vraie pour tout le reste.
3. **Le risque légal du prêteur public** : sa carte calcule, pour un anonyme, une image qu'il ne voit pas.
   *Recommandation :* avant L4, une courte charte du réseau public. Dans le code :
   - l'avis sur la carte de prêt ;
   - la garde la plus courte (24 h, rien au journal) ;
   - un filtre de consignes signé, appliqué par l'exécutant ;
   - des recettes marquées (sans image de référence) entre lesquelles le prêteur choisit.

   Pas de vignette chez le prêteur, pour garder l'anonymat du demandeur.
4. **L'isolement exigé avant une version signée qui prête au public.**
   *Recommandation :*
   - l'instance `public` (sans paquets, dossiers vidés, `/free` entre demandeurs) et l'objet de tâche, toujours ;
   - le jeton à intégrité basse, obligatoire si M7 montre que CUDA le supporte ;
   - le compte Windows séparé, proposé plus tard ;
   - pas d'entrée vidéo d'inconnu ;
   - L4 n'est publiée qu'après ton essai.
5. **Le premier catalogue de recettes publiques.**
   *Recommandation :*
   - des images seulement, à licence ouverte : SDXL base (Open RAIL++-M), Flux.1 schnell et Z-Image Turbo (licences à confirmer dans la table) ;
   - tirées de `comfyui-workflow-templates` 0.11.48, fabriquées sur ce PC ;
   - pas de H3 ni de licence restrictive au public en v1.
6. **La publication des clients.**
   *Recommandation :* `aiodos-client` sur PyPI et npm, publiés par GitHub Actions en « trusted publishing » depuis `github.com/AIODOS/aiodos`, avec provenance ; réserver aussi `aiodos` s'il est libre. C'est toi qui crées les comptes, avec la double authentification. Si un nom est pris : `aiodos-sdk`.
7. **La réciprocité avec les inconnus en v1.** Avec une clé éphémère par travail, un inconnu qui donne beaucoup n'est pas reconnu quand il demande.
   *Recommandation :* l'accepter en v1. Chez le prêteur, l'ordre de service est mes machines, puis mes amis, puis le public dans l'ordre d'arrivée. Les reçus signés servent au prêteur, pour son propre compte. La priorité par crédits viendra avec l'étape 3.3, par une preuve détachable de la clé éphémère, à étudier.
8. **La vidéo chez mes amis.**
   *Recommandation :* dès L3, avec le contrôle de forme et la durée, les images témoins comparées sur 2 % des clips quand une de mes machines a les modèles, et « non vérifiée au pixel » dit sur le fichier. Les clips en entrée seulement si le prêteur les accepte. Chez les inconnus, rien avant M2.

**Tranchées ici, plus à reposer** : la liste blanche, les recettes, les défauts du prêt, la garde de la boîte d'envoi, l'équité, la pause par défaut, la conversation chez le prêteur, Desktop ou instance, le relais d'`object_info`, le WebSocket, la clé éphémère, la clé du catalogue, les fichiers jamais récupérés.

**Les questions de la 1re conception**
- Tranchées par les décisions du 20/10 : 1 et 2 (décision 4), 3 (décision 2), 7 (décision 5), 18 (décision 6).
- Caduques : 9, 10, 11 et 19 (plus de créations ; entrées en corps brut).
- Les autres (4 à 6, 8, 12 à 17, 20 et 21) sont tenues pour adoptées avec leur recommandation. Elles se rouvrent si tu le demandes.

---
