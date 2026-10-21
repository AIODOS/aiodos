# AIODOS pour les applications : conception finale de la v1

> Versée dans le dépôt le 21/10/2026. Première conception de l'accès des applications ; sa partie « créations » (§ 4.7 à 4.10, L2) est remplacée par [partage-puissance.md](partage-puissance.md) (décision de l'auteur, 20/10/2026 : AIODOS partage la puissance, les applis gardent leurs moteurs). Ce qui est réalisé : la livraison L1, décrite dans [../api-applications.md](../api-applications.md) (la migration « 18 » est devenue la 20). Les chemins absolus de l'annexe d'origine ont été retirés.

Lecture seule, au 20/10/2026. Ce document réunit en une seule conception :
- la conception retenue par le jury (« au plus près des standards ») ;
- les greffes proposées par les trois jurés ;
- les meilleures idées des deux autres conceptions (« expérience » et « sécurité ») ;
- les deux rapports de besoins (PANDORA et ARKLIGHT PC).

Code lu :
- `main` (0831d6c) ;
- `claude/upbeat-chaum-e9326c` (0c623db, passerelle servie par le moteur intégré) ;
- `claude/distracted-satoshi-342231` (de80489 et fadc9dc), qui n'est ni poussée ni fusionnée.

Les chemins partent de `C:\_22eme ARKANE\PROJETS\AIODOS`, sauf mention contraire. L'annexe les donne en chemins absolus. **À vérifier** signale ce que je n'ai pas pu confirmer sans rien lancer. Je n'avais pas d'accès réseau : les références d'OpenAI n'ont pas été revérifiées. C'est pourquoi les routes d'OpenAI dont la forme n'est pas sûre sont repoussées (§ 4.13).

Échelle de prix :
- **petit** : un fichier neuf de 100 à 200 lignes ;
- **moyen** : 2 à 4 fichiers, une migration, ou un module central touché ;
- **gros** : le protocole entre machines change.

**Vérifié dans le code pour cette synthèse**
- `db._ensure_schema` (l.348-362) n'applique que les migrations de numéro plus grand que `user_version`, puis réécrit ce numéro. Une version précédente qui ouvre une base passée en 19 ne plante pas et ne baisse pas le numéro.
- `jobs/executor._run_unit` (l.45) appelle `registry.run_local(task, payload)` sans `stop`, alors que `run_local(name, payload, stop=None)` le prend (registry.py l.127). C'est le point d'entrée de la vraie annulation.
- `leases.claim_next` ne confie que les unités à `u.local_only = 0` (l.41). `network/work.lease` décide unité par unité avec `acceptable()` (l.124) et `registry.friends_may_take`.
- `settings.SECRET_KEYS = {"mistral_key", "qwen_key"}` (l.16). `gateway_key` n'en fait pas partie.
- `llm/engine.py` : `MODELS_MAX` vaut 2, `CHAT_CONTEXT` 8192 et `EMBED_CONTEXT` 2048. `ctx-size` est écrit dans le préréglage du routeur (l.46-48).
- `network/pairing.py` compare un code à 6 chiffres.
- Pour faire revenir la fenêtre : `instance.wake_running`, et `POST /instance/show` dans `api/routes/system.py` (l.55).
- L'installeur ne contient pas Whisper (`tools/build_installer.py` : « 640 Mo de plus, à venir en option »), alors que `requirements.txt` liste faster-whisper. **Une copie installée ne transcrit donc rien aujourd'hui.** Le téléchargement de Whisper sans accord ne concerne que les copies où faster-whisper est installé.
- `requirements.txt` ne contient ni python-multipart ni Pillow. Pillow est pourtant importé par `imaging/thumbs.py` et `jobs/kinds/images.py` : à lister explicitement avant d'en dépendre pour les envois.
- `requirements-dev.txt` épingle openai 3.33.0 et mcp 2.3.0.
- `tools/installer/aiodos.iss` : AppId `{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}`, `DefaultDirName={autopf}\AIODOS`, `PrivilegesRequired=lowest`. C'est une installation par utilisateur, sous `%LOCALAPPDATA%\Programs\AIODOS`, avec sa clé de désinstallation sous HKCU.
- Branche upbeat-chaum : dans `gateway/engine.py`, `TIMEOUT = (10, 900)` est un délai de lecture entre deux morceaux, pas une durée totale.

---

## 0. En bref

### 0.1 Le principe

- **Une seule porte**, la passerelle sur `127.0.0.1:11435`, qui parle trois formes :
  - **OpenAI**, là où la forme est sûre et où une appli s'en sert : conversation, complétion, embeddings, liste des modèles, et création d'images (`/v1/images/generations`) ;
  - **Ollama et MCP**, comme aujourd'hui, filtrés par les droits ;
  - **une petite API `/aiodos/v1/`** pour le reste : découverte, accès, catalogue complet, créations (images et vidéos), travaux, fichiers, transcription, bibliothèques, machines.
- **Une appli, une clé.**
  - L'appli l'obtient par une demande. L'utilisateur l'accepte dans AIODOS, après avoir comparé un code à 6 chiffres.
  - Les droits se donnent par capacité. Le lieu de calcul a un plafond : ce PC, mes machines, ou mes amis aussi.
  - S'y ajoutent une priorité, un journal, et un retrait en un clic.
- **Rien de nouveau ne sort du PC sans un geste de l'utilisateur.** La passerelle reste fermée par défaut et n'appelle jamais le cloud. Rien ne part chez un ami sans accord explicite. Elle ne télécharge aucun modèle.
- **Deux bibliothèques clientes** : Python pour PANDORA, JavaScript pour ARKLIGHT.
  - Sous Apache-2.0, sans dépendance, à copier telles quelles.
  - Toute leur surface existe dès la première livraison. Ce qu'AIODOS ne sert pas encore lève `NotSupported`, si bien que le code des applis n'aura pas à changer ensuite.

### 0.2 Ce qui est livré en premier

**Livraison 1, « Branchable »** (étapes 0 à 4, § 9.1) :
0. Fusionner `upbeat-chaum`, puis `distracted-satoshi`, après l'avoir poussée.
1. **Des corrections utiles tout de suite**, même avec la clé générale :
   - un contexte réglable, 16 384 jetons par défaut si la mémoire vidéo le permet ;
   - des codes d'erreur stables ;
   - `X-Aiodos-Model` et `X-Aiodos-Machine` sur l'API d'Ollama ;
   - l'arrêt du calcul quand l'appli se déconnecte, même sans flux ;
   - un commentaire `: aiodos loading` pendant le chargement du modèle.
2. **L'accès par appli** :
   - migration 18 ;
   - demande d'accès et carte d'accord ;
   - droits, lieu de calcul, priorité, journal, limites ;
   - clés à la main, et clé générale protégée par DPAPI ;
   - refus `tools_unavailable` au lieu d'outils supprimés sans le dire.
3. **La découverte** : `gateway.json`, `GET /aiodos/v1/info` avec la preuve d'identité d'AIODOS, `--open`, et le catalogue enrichi.
4. **Les deux clients**, le contrat partagé, les faux serveurs, et le guide `docs/integrer-aiodos.md`.

Avec cela :
- l'assistant d'ARKLIGHT marche en entier : outils, vision, 16 k jetons ;
- PANDORA confie à AIODOS ses tâches de texte et de vision : translate, enhance, video_prompt, vision et conversations courtes.

**Ensuite** :
- **L2, « Créations »** : vraie annulation, migration 19, images, vidéos Wan 2.1, fichiers ;
- **L3, « Voix »** : la transcription, une fois le moteur choisi ;
- **L4** : bibliothèques, machines, image de départ et de fin pour H3.

### 0.3 Les contradictions tranchées

| Sujet | Positions en présence | Choix | Pourquoi |
|---|---|---|---|
| Forme des créations | Tout en OpenAI (`/v1/videos`, `/v1/audio/transcriptions`, vector stores) pour C1 ; une API AIODOS pour C2, C3 et les jurés 1 et 3 | OpenAI pour la conversation, les embeddings, les modèles et `/v1/images/generations`. API AIODOS pour les créations, les travaux et la transcription. Façades OpenAI plus tard (§ 4.13) | Les formes `/v1/videos` et vector stores n'ont pas pu être vérifiées. Celle de `/v1/videos` obligeait à montrer une annulation comme un échec. PANDORA et ARKLIGHT passeront par les clients. On garde ainsi une seule surface solide |
| Passerelle par défaut | « Guichet » ouvert (C2) ; fermée (C1, C3, jurés 1 et 2) | **Fermée** (invariant I2) | Rien n'écoute sans un geste. Le client propose « Ouvrir les réglages d'AIODOS » : un clic de plus, une fois. Reste une question (Q1) |
| Code à comparer | 7 caractères (C1), 4 chiffres (C2), 6 chiffres (C3) | **6 chiffres**, comme l'appairage | Même geste que `network/pairing.py`, et une collision plus improbable |
| Déroulé de la demande | Device flow RFC 8628 avec interrogation (C1) ; `claim` et attente longue (C2, C3) | **RFC 8628**, toutes les 2 s. Le `device_code` fait office de `claim` | Le vocabulaire est connu, les deux font la même chose, et 2 s d'attente suffisent |
| Droits en plus | Nouvelle clé (C1) ; même clé (C3) | **Même clé** | Les droits sont dans la base, la clé ne fait qu'identifier. L'argument de C1, « AIODOS n'a pas la clé en clair », ne tient pas |
| Forme de la clé | `aiodos-app-…` (C1) ; `aiodos_app_<id>_<secret>` (C2, C3) | **`aiodos_app_<id>_<secret>`** | L'`id` sert à la preuve, à distinguer « retirée » d'« inconnue », et à la recherche |
| Fetch Metadata | Tout `Sec-Fetch-*` refusé (C3) ; règle fine (C1) | **Règle fine**. `Sec-Fetch-Mode` seul passe | Le `fetch` de Node envoie toujours `sec-fetch-mode` (vérifié par le juré 2 dans undici 7.30, `lib/web/fetch/util.js` l.246) : la règle de C3 casserait ARKLIGHT |
| Liens de fichier | HMAC sans état (C1) ; jetons en mémoire à usages comptés (C2, C3) | **HMAC sans état, plus un compteur d'usages en mémoire** | Le lien survit à un redémarrage. Un redémarrage remet les compteurs à zéro, mais ne rend jamais valide un lien invalide |
| Où vont les créations | `data\gateway\files` 24 h (C1) ; galerie (C2) ; `data\gateway\out` (C3) | **`data\gateway\out\<appli>\`, 7 jours**, filtre dans la galerie et bouton « Garder » | Hors du dossier Images, peut-être synchronisé par OneDrive. 24 h, c'est court si l'appli plante pendant un long rendu. Reste une question (Q7) |
| Transcription | Multipart d'OpenAI (C1, demande python-multipart) ; JSON propre à AIODOS (C2, C3) | **`/aiodos/v1/audio/transcriptions` en JSON ou en corps brut**, avec une réponse au format d'OpenAI et l'alias `whisper-1` | Aucune dépendance non vérifiée en v1. La façade multipart restera mince à ajouter |
| Voie de transcription interactive | Second Whisper (C1, C2, jurés 2 et 3) ; préemption de l'indexation (C3) | **Second programme** lancé à la demande, avec `warmup` | Il ne touche pas à la file, et la touche F8 d'ARKLIGHT y gagne. Le moteur reste à choisir (Q8), puisque faster-whisper n'est pas dans l'installeur |
| Ordre des livraisons | Transcription avant les images (C1) ; créations d'abord (C2, C3, juré 1) | **Créations d'abord** | L'annulation est un défaut connu, et ce sont les créations qui rapportent le plus à PANDORA. ARKLIGHT a déjà sa voix avec whisper.cpp, et le moteur Whisper demande une étude |
| Client Python | Bibliothèque standard, `http.client` (C1) ; `requests` (C2, C3, juré 2) | **`http.client`** | Aucune dépendance, et un accès direct à la socket pour une annulation réelle. Pas de proxy lu dans l'environnement. Avec `requests`, couper un flux dépendrait des entrailles d'urllib3 |
| Conversation du client JS | `/api/chat` NDJSON (C3, rapport ARKLIGHT) ; `/v1` SSE (C1, C2, jurés 2 et 3) | **`/v1` en SSE** | Un seul format pour les deux clients. Les identifiants d'appels d'outils et les erreurs à code sont gardés |
| Catalogue | Modèles de médias dans `/v1/models` (C1) ; `/aiodos/v1/models` à part (C2, C3) | **`/v1/models` limité au texte et aux embeddings, enrichi à la façon d'OpenRouter ; `/aiodos/v1/models` complet** | Les clients génériques proposeraient sinon Z-Image dans leur liste de modèles de conversation |
| `max_tokens` trop grand | Refus, ou laisser faire | **Jamais de refus.** llama.cpp s'arrête à la fin du contexte, `finish_reason` vaut `length`, et le dernier morceau porte `aiodos.stopped_by: "context"` | La boucle de continuation de PANDORA y voit une troncature, et sait pourquoi |
| Réglages par requête | Objet `aiodos` dans le corps (C2) ; en-têtes (C1, C3) | **En-têtes `X-Aiodos-*`** | Les SDK les posent par `default_headers`. Le corps part tel quel au routeur. Une seule règle pour `/v1`, `/api` et `/aiodos` |
| Échappatoire des outils | `require_tools:false` dans le corps (C2) ; en-tête (C3) | **`X-Aiodos-Tools: optional`** | Pour la même raison |
| Contexte | Par modèle (C1) ; global (C2, C3) | **Global, avec une exception possible par modèle** | Simple par défaut, et plus de place pour un petit modèle |
| Migrations | Une seule migration 18 (C2) ; 18 puis 19 (C1, juré 1) | **18 en L1, 19 en L2** | On ne complète pas une migration déjà livrée |
| Lancer AIODOS | Commande lue dans le fichier (C1, C2) ; chemin fixe (C3) | **Commande calculée par le client** : registre `Uninstall\{AppId}_is1` (`InstallLocation`), sinon `%LOCALAPPDATA%\Programs\AIODOS` | Rien n'est jamais exécuté depuis un fichier modifiable |
| Cache de l'identité de l'appelant | Par port client pendant 30 s (C3) | **Vérification à chaque requête**. S'il faut un cache, par connexion, jamais par port | Un port éphémère peut être repris par un autre processus (correction du juré 2) |
| Preuve pour un `key_id` inconnu | `null` (C3) | **Preuve aléatoire** | Elle ne révèle pas si un identifiant existe (juré 2) |
| Identifiants de travail | `img_`, `video_`, `tr_` (C1) ; `job_` (C2) ; `j_` (C3) | **`job_`** pour tous, `file_` pour les fichiers | Un seul espace de noms, une seule route de suivi |
| Lier la clé au programme | Plus tard (C1) ; en option (C2) ; affichage seulement (C3) | **Toujours noter et alerter ; refus strict en option, décoché** | Peu coûteux, puisque l'appelant est de toute façon identifié à chaque requête pour le compte Windows |
| Nommage du lieu | `where` ou `reach` | **`where` dans l'API, colonne `reach` dans la base** | `where` est un mot réservé de SQL |
| Clé générale | Gardée (C1) ; plus créée d'office, clés à la main (C3) | **Gardée pour l'existant, avec ses droits. Plus créée d'office. Clés à la main par outil** | Rien ne casse, et le moindre privilège devient la norme |

---

## 1. Principe et périmètre

### 1.1 Cinq règles

1. **Le standard, quand il est sûr et utile.** Une route d'OpenAI n'est reprise que si sa forme est stable et connue sans doute, et qu'un outil ou une appli s'en sert.
   - On reprend alors le même chemin, le même corps, la même réponse et les mêmes statuts HTTP : les SDK lèvent leurs exceptions selon le statut.
   - En v1, cela donne `/v1/models`, `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` et `/v1/images/generations`.
   - Les autres routes d'OpenAI sont des façades prévues, posées plus tard sur les mêmes modules (§ 4.13).
2. **Des extensions sans danger.**
   - En entrée : des champs simples (`seed`, `negative_prompt`, `steps`), que les SDK envoient par `extra_body`.
   - En sortie : un objet `aiodos`, ou les champs qu'OpenRouter a déjà répandus (`context_length`, `architecture`, `supported_parameters`).
   - En-têtes `X-Aiodos-*`.
   - Une extension ne change jamais le sens d'un champ standard. Les réglages propres à une requête passent par les en-têtes, jamais par le corps.
3. **Les standards du web pour le reste.**
   - `Prefer: respond-async` (RFC 7240) ;
   - `Retry-After` et `Accept-Language` (RFC 9110) ;
   - `Idempotency-Key` ;
   - SSE ;
   - Fetch Metadata ;
   - le vocabulaire du device flow (RFC 8628) et l'esprit de la RFC 7009 pour le retrait.
4. **Ne jamais dire qu'on a fait ce qu'on n'a pas fait.**
   - Sous `/v1` : un paramètre standard qui changerait le résultat et qu'AIODOS ne traite pas donne un 400 `unsupported_parameter`. Un paramètre sans effet sur le résultat est ignoré, et la liste est documentée.
   - Sous `/aiodos/v1` : un champ inconnu donne un 400 `invalid_request`, pour attraper les fautes de frappe.
   - Les valeurs réellement appliquées sont toujours rendues : taille arrondie, durée réelle, machine qui a calculé.
5. **Les règles de l'auteur.**
   - Pas de serveur central : 127.0.0.1 seulement.
   - Rien chez un ami sans accord, rien dans le cloud.
   - Aucun téléchargement par la passerelle : elle répond 409 et renvoie vers AIODOS.
   - Un fichier par fonctionnalité.
   - Rien ne casse pour Open WebUI, Continue et les assistants MCP.

### 1.2 Invariants, chacun couvert par un essai (§ 6.5)

| # | Invariant |
|---|---|
| I1 | La passerelle n'écoute que sur 127.0.0.1. |
| I2 | Elle est fermée par défaut. Seule l'interface d'AIODOS l'ouvre. |
| I3 | Aucun appel au cloud, quelle que soit la clé : `cloud:` est refusé et le mode automatique ne le choisit jamais. |
| I4 | Rien ne part chez un ami sans le lieu `friends` accordé à l'appli **et** le réglage général correspondant (`auto_route_friends`, `images_from_friends`). |
| I5 | Une clé d'appli ne télécharge, ne supprime, ne charge ni ne décharge aucun modèle, et ne règle jamais le contexte. `keep_alive: 0`, le chargement à vide et `num_ctx` sont ignorés. |
| I6 | Aucune page web n'atteint la passerelle (détail ci-dessous). |
| I7 | Aucune appli ne fait lire un chemin de fichier à AIODOS : elle envoie des octets seulement. |
| I8 | Aucun contenu au journal : ni consigne, ni réponse, ni image, ni audio, ni passage. |
| I9 | Le nom de l'appli et sa clé ne quittent jamais ce PC. Les machines appairées ne savent pas pour qui elles calculent. |
| I10 | Une appli ne voit que ses propres travaux et fichiers. Ceux d'une autre répondent 404, jamais 403. |
| I11 | Un droit ne s'accorde que dans l'interface d'AIODOS, derrière le jeton de lancement. Aucune route de la passerelle n'en accorde. |
| I12 | Une requête peut resserrer le lieu ou baisser la priorité, jamais les élargir. |

Détail de I6 :
- l'en-tête `Host` doit être exact ;
- sous `/aiodos/v1/` et sur les routes sans clé, tout `Origin` est refusé ;
- ailleurs, un `Origin` non local est refusé, comme aujourd'hui ;
- `Sec-Fetch-Site: cross-site` ou `same-site` donne un 403 ;
- un `Sec-Fetch-Dest` présent et différent de `empty` donne un 403 ;
- `Sec-Fetch-Mode` seul est accepté ;
- pas de CORS, et un POST sans clé exige du JSON (sinon 415).

### 1.3 Contre qui on se défend

- **Les pages web**, d'un site quelconque ou servies sur localhost : défense complète (I6).
- **Une appli boguée ou trop gourmande** : droits, limites, priorité, garde-fou contre l'accaparement, annulation, retrait.
- **Un autre compte Windows du même PC**, qui partage 127.0.0.1 :
  - le compte de l'appelant est vérifié ;
  - les clés sont protégées par DPAPI ;
  - les clients vérifient qu'AIODOS est bien AIODOS (preuve, § 2.3) et qui tient le port.
- **Une clé qui fuit** (fichier copié, sauvegarde, capture) : les dégâts se limitent à une appli et à ses droits. Le journal signale un « programme inattendu », et l'accès se coupe en un clic.
- **Un programme malveillant du même compte : hors de portée.**
  - Il lit les fichiers, ouvre les secrets DPAPI de ce compte et peut cliquer à la place de l'utilisateur.
  - On le rend visible (programme affiché, journal), pas impuissant.
  - La documentation le dit franchement.

### 1.4 Dans la v1, hors de la v1

**Dans la v1** (L1 à L4) :
- découverte, lancement et réveil d'AIODOS ;
- accès par appli : demande, carte d'accord, droits, lieu de calcul, priorité, retrait, journal, limites ;
- clés à la main pour les outils ;
- conversation en flux, avec outils, images et réflexion, sans jamais retirer les outils en silence ;
- embeddings ;
- catalogue avec contexte réel, capacités et licences ;
- images (forme d'OpenAI et forme AIODOS) et vidéos Wan 2.1 ;
- travaux suivis en SSE, avec une annulation réelle et une reprise après redémarrage ;
- fichiers et liens signés ;
- transcription sur ce PC ;
- recherche dans les bibliothèques choisies ;
- liste des machines ;
- image de départ et de fin pour MiniMax H3 ;
- les deux clients, le contrat partagé, les faux serveurs, le guide.

**Hors de la v1** :
- la gestion des modèles par une appli (pull, create, delete, chargement) : jamais ;
- le cloud d'AIODOS (clés Mistral et Qwen) : jamais par la passerelle ;
- les conversations propres d'AIODOS (historique, recherche web, créations dans une conversation) ;
- `/v1/responses`, Realtime, WebSocket, `/v1/audio/speech`, Assistants, Batch, `POST /v1/files` ;
- la synthèse vocale, la musique, la vidéo vers vidéo, le masque de retouche, plusieurs images de référence, la sortie MP4 ;
- l'accès depuis une autre machine, un téléphone ou une page web (pas de CORS : un rendu Electron passe par son processus principal) ;
- les outils et les images envoyés à mes machines appairées (protocole de prêt : **gros**) ;
- l'écriture dans une bibliothèque ;
- l'approbation sur le téléphone, la signature Authenticode, la forme de l'API de sd-server.

---

## 2. Comment une appli trouve AIODOS

### 2.1 L'adresse

- L'adresse par défaut est `http://127.0.0.1:11435`, réglable dans AIODOS (`gateway_port`).
- Les clients écrivent `127.0.0.1`, jamais `localhost` : la passerelle n'écoute pas en IPv6.
- **Ordre de recherche de l'adresse** :
  1. le paramètre de l'appli ;
  2. `AIODOS_URL` ;
  3. le fichier de découverte ;
  4. l'adresse par défaut.
- **Ordre de recherche de la clé** :
  1. le coffre de l'appli (une fonction fournie par l'appli) ;
  2. `AIODOS_KEY`, réservé aux essais et au développement. La documentation rappelle qu'une variable d'environnement passe aux programmes enfants.
- Les clients refusent toute adresse dont l'hôte n'est pas `127.0.0.1`, `localhost` ou `[::1]`, **avant d'envoyer quoi que ce soit** (code client `non_local_url`). Ils ne suivent aucune redirection et ignorent les proxys.

### 2.2 Le fichier de découverte (`gateway/discovery.py`, petit)

**Emplacement**
- `%LOCALAPPDATA%\AIODOS\gateway.json`, quel que soit le dossier de données. Hors de Windows : `$XDG_STATE_HOME/aiodos/gateway.json`.
- `AIODOS_DISCOVERY_FILE` donne un autre chemin, et `-` coupe l'écriture. Les essais s'en servent pour ne jamais toucher le vrai fichier.

**Qui l'écrit**
- L'instance qui tient le port de la passerelle. Si le port est fermé, l'instance qui utilise le dossier de données par défaut.
- Jamais un nœud d'essai (`AIODOS_NO_DIALOG`), ni une instance lancée avec `AIODOS_DATA_DIR` ou `--data-dir`.

**Quand**
- au démarrage ;
- à chaque `Gateway.apply()` (ouverture, fermeture, changement de port) ;
- à l'arrêt, avec `running: false`.

L'écriture est atomique : un fichier temporaire, puis `os.replace`.

```json
{ "product": "aiodos", "format": 1, "version": "2.1.0", "api": 1,
  "pid": 12345, "instance": "Hk3v9QmZ", "running": true,
  "gateway": { "state": "open", "url": "http://127.0.0.1:11435",
               "apps": true, "requests": true, "approval_ui": true, "error": null },
  "updated": 1792500000.0 }
```

**Les champs**
- `gateway.state` vaut `open`, `closed` ou `error` (port déjà pris : `error` nomme alors le programme qui le tient, § 3.5).
- `instance` est tiré au hasard à chaque lancement.
- **Jamais dans ce fichier** : une clé, le port ou le jeton de l'interface, la `show_key` de `instance.json`, une commande de lancement, un nom de machine, le nom de l'utilisateur.

**Règles des clients**
- Ils ne retiennent que `url`, et seulement si son hôte est local.
- Ils n'exécutent jamais rien qui viendrait du fichier.
- Un `pid` mort veut dire « AIODOS arrêté ».
  - En Python, on le vérifie par `OpenProcess` et `GetExitCodeProcess`. **Jamais par `os.kill(pid, 0)` sous Windows : cela appelle `TerminateProcess` et tue AIODOS.**
  - En Node, `process.kill(pid, 0)` ne fait que tester.

### 2.3 `GET /aiodos/v1/info` et la preuve d'identité (`gateway/info.py`, petit)

**Sans clé, la route répond toujours 200**, jamais 401 :

```json
{ "object": "aiodos.info", "product": "aiodos", "version": "2.1.0", "api": 1,
  "instance": "Hk3v9QmZ",
  "features": ["access", "models", "chat", "embed", "open"],
  "access": { "apps": true, "requests": true, "approval_ui": true },
  "auth": "none" }
```

- `features` dit ce que **cette version** sert. La liste s'allonge à chaque livraison :
  - L2 ajoute `creations`, `images`, `videos`, `jobs` et `files` ;
  - L3 ajoute `transcribe` ;
  - L4 ajoute `libraries`, `machines` et `video_reference`.

  Un client lève `NotSupported` pour une fonction absente, sans rien essayer, et ne compare jamais des numéros de version.
- **Avec `Authorization`**, `auth` vaut `valid`, `revoked`, `expired` ou `invalid`. S'il vaut `valid`, la réponse ajoute :

  ```json
  "app": { "id": "k3j9m2qa", "name": "PANDORA", "kind": "app",
           "scopes": ["chat", "images", "videos"], "where": "mine",
           "priority": "interactive", "libraries": [], "expires": null },
  "limits": { "requests_per_minute": 120, "streams": 4, "active_jobs": 10,
              "max_image_bytes": 20971520, "max_audio_bytes": 26214400 }
  ```

- Toutes les réponses de la passerelle portent `X-Aiodos-Api: 1`, y compris « / ».
  - « / » répond toujours « Ollama is running », et `/api/version` toujours 0.40.5, pour les outils existants.
  - Une ancienne version d'AIODOS répond 401 au format d'AIODOS sur `/aiodos/v1/info`, alors qu'Ollama répond 404. Le client distingue ainsi `too_old` de `not_aiodos`.

**La preuve.** Si AIODOS est arrêté, n'importe quel programme peut écouter sur 11435 et recueillir les clés, même depuis un autre compte Windows.
1. Le client tire un défi, puis appelle `GET /aiodos/v1/info?key_id=<id>&challenge=<32 octets en base64url>`, **sans la clé**.
2. AIODOS répond `proof = base64url(HMAC-SHA256(clé=octets de key_hash, "aiodos-proof/1\n" + challenge))`. AIODOS garde `key_hash = sha256(clé)` : il sait répondre, un imposteur non.
3. **Pour un `key_id` inconnu, la preuve est aléatoire**, et ne se distingue pas d'une vraie. Pour une clé retirée, le haché est gardé et la preuve est juste. L'appel suivant, avec la clé, dira `revoked`.
4. Le client n'envoie sa clé qu'après une preuve juste. Il la refait :
   - quand `instance` change ;
   - après toute erreur de connexion ;
   - si la dernière preuve date de plus de 60 s.
5. Si la preuve est fausse, l'état est `untrusted` (§ 2.4) et la clé n'est pas envoyée.
6. La preuve ne vaut que pour les clés d'appli. Un outil branché avec une clé à la main, par un SDK d'OpenAI, n'en bénéficie pas : la documentation le dit.

**Qui tient le port.** Les clients vérifient en plus, par la table TCP de Windows, que le programme qui écoute sur le port est bien le `pid` de `gateway.json`. Le moment : avant une première demande d'accès, ou après une preuve fausse. Le fichier est dans le profil de l'utilisateur, où un autre compte ne peut pas écrire.
- En Python : `GetExtendedTcpTable` par ctypes.
- En Node : `netstat -ano -p tcp`, lancé sans shell.
- **À essayer.**

Cela empêche un faux AIODOS d'un autre compte d'« accorder » lui-même une clé à une appli qui vient de perdre la sienne.

### 2.4 Les états vus par l'appli

`status()` ne lève jamais d'erreur. Elle lit le fichier, tente une connexion TCP de 0,5 s, puis appelle `info`, la preuve, puis `info` avec la clé. Les textes en français et en anglais sont fournis par les clients (`texts.py`, `texts.js`). PANDORA les inscrit dans `core/i18n._FR_TO_EN` ; ARKLIGHT les passe à `t()`.

| `state` | Indice | Message conseillé | Action |
|---|---|---|---|
| `not_installed` | ni fichier, ni clé de désinstallation, ni `Programs\AIODOS` | « AIODOS n'est pas installé sur ce PC. » | « En savoir plus » ouvre la page du projet dans le navigateur ; rien n'est téléchargé |
| `not_running` | installé, connexion refusée ; `running: false` ou pid mort | « AIODOS n'est pas lancé. » | **[Lancer AIODOS]** |
| `gateway_closed` | pid vivant, `gateway.state: closed` | « AIODOS tourne, mais n'est pas ouvert aux applications. » | **[Ouvrir les réglages d'AIODOS]** |
| `gateway_error` | `gateway.state: error` | le message d'AIODOS (« port 11435 tenu par … ») | idem |
| `apps_closed` | `access.apps: false` (seule la clé générale est ouverte) | « AIODOS n'est ouvert qu'aux autres outils : ouvre-le aussi aux applications. » | idem |
| `not_aiodos` | le port répond, mais pas comme AIODOS | « Le port 11435 est pris par un autre programme. » | — |
| `too_old` | réponse d'un ancien AIODOS, ou `api` trop bas | « Mets AIODOS à jour. » | — |
| `untrusted` | preuve fausse, ou le port est tenu par un autre pid | « Le programme qui répond sur le port 11435 ne connaît pas la clé de PANDORA : elle n'a pas été envoyée. Vérifie qu'AIODOS est bien lancé. » | [Ouvrir AIODOS] |
| `headless` | `approval_ui: false`, pas de clé | « AIODOS tourne sans fenêtre : ouvre-le pour autoriser PANDORA. » | [Ouvrir AIODOS] |
| `requests_closed` | `access.requests: false`, pas de clé | « AIODOS n'accepte pas de nouvelle application en ce moment. » | [Ouvrir les réglages d'AIODOS] |
| `needs_access` | pas de clé, ou `auth: invalid` | « PANDORA n'a pas encore accès à AIODOS. » | **[Se connecter à AIODOS]** |
| `pending` | demande en cours | « Autorise PANDORA dans la fenêtre d'AIODOS. Code : 482 197 » | [Annuler] |
| `denied` | `access_denied` | « L'accès a été refusé dans AIODOS. » | [Redemander] |
| `revoked` | `auth: revoked` ou `expired` | « L'accès de PANDORA a été retiré (ou a expiré) dans AIODOS. » | [Se reconnecter] |
| `other_account` | 403 `other_account` | « AIODOS tourne sous un autre compte Windows. » | — |
| `ready` | `auth: valid` | « Connecté à AIODOS 2.1 », avec le modèle choisi et son contexte | [Gérer dans AIODOS] · [Se déconnecter] |

En `ready`, `status()` rend aussi `missing_scopes`. L'appli peut alors proposer « Demander aussi les vidéos » (§ 3.3).

### 2.5 Lancer AIODOS, ouvrir une page

- **Trouver l'installation.** Le client lit `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}_is1`, valeur `InstallLocation`, puis la même clé sous HKLM, sinon il prend `%LOCALAPPDATA%\Programs\AIODOS`. En Python par `winreg`, en Node par `reg query`.
- **`launch()`** lance `<racine>\runtime\pythonw.exe -m aiodos --background`, depuis `<racine>\app` :
  - sans shell, avec une liste d'arguments ;
  - seulement si ce fichier existe sous cette racine ;
  - seulement sur un clic de l'utilisateur ;
  - sans jamais rien installer.

  `wait_ready(60)` suit, puis la passerelle garde son réglage : fermée, elle reste fermée. Une copie de développement n'est pas lancée par un client : celui-ci affiche un message. **À vérifier** sur la copie installée : c'est bien ce que lance le raccourci du menu Démarrer (le lanceur `updates/launcher` choisit ensuite la version).
- **Nouvelle option `--open <page>`** (`__main__.py`, `instance.py`, `ui/static/js/app.js`). Pages : `applications`, `models`, `jobs`, `gallery`.
  - Si AIODOS tourne, `instance.wake_running(page)` fait revenir la fenêtre sur cette page (`POST /instance/show {page}`, avec la `show_key`).
  - Sinon, AIODOS démarre sur cette page.
  - C'est ainsi que « Ouvrir les réglages d'AIODOS » marche même passerelle fermée, sans nouvelle porte d'entrée.
- **`POST /aiodos/v1/open`**, avec une clé valide quelle qu'elle soit : `{"page": "models", "model": "minimax-h3"}`.
  - L'appli s'en sert pour « L'installer dans AIODOS » : l'utilisateur clique dans AIODOS, où la licence est affichée et acceptée.
  - Une fois par 10 s et par appli au plus. Réponse 202 `{"opened": true}`, ou `false` en mode sans fenêtre.
- **Premier plan.** Windows empêche de voler le premier plan.
  - Le client Python appelle `AllowSetForegroundWindow(pid)` avant de demander l'ouverture.
  - Pour Node, AIODOS fait clignoter son bouton dans la barre des tâches et montre une bulle (`ui/tray.py`).

### 2.6 AIODOS arrêté pendant un travail

- Les travaux sont dans la base, et `queue.recover()` remet en file au redémarrage les unités qui étaient en cours.
- Dans `watch()` ou `wait()`, le client passe à l'étape `unreachable` : « AIODOS s'est arrêté : le travail reprendra à son retour ». Il continue d'attendre jusqu'à son propre délai.
- Après un redémarrage de l'appli, `ai.job("job_…")` ou `GET /aiodos/v1/jobs?active=1` retrouve ses travaux.

---

## 3. L'accès

### 3.1 Les interrupteurs (Réglages → « Applications et autres outils »)

| Réglage | Défaut | Effet |
|---|---|---|
| `gateway_apps` : « Ouvrir AIODOS aux applications de ce PC » | **désactivé** | `info`, les clés d'appli et les clés à la main marchent |
| `gateway_requests` : « Accepter les nouvelles demandes d'accès » | activé, sous le premier | `POST /aiodos/v1/access/requests` est accepté |
| `gateway_enabled` (existant) : « Clé générale pour les autres outils » | désactivé, comme aujourd'hui | La clé générale et le pont MCP marchent |

- Le port écoute dès que `gateway_apps` ou `gateway_enabled` est vrai.
- Une clé d'appli présentée alors que `gateway_apps` est faux reçoit 403 `apps_closed`.

### 3.2 Une appli, une clé (`gateway/apps.py`, petit à moyen)

- **Table `gateway_apps`** (migration 18, § 6.3). Une ligne par accès, de quatre sortes :
  - `app` : obtenue par une demande ;
  - `manual` : créée à la main pour un outil ;
  - `general` : la clé générale d'aujourd'hui ;
  - `bridge` : la clé du pont MCP stdio.
- **Forme de la clé** : `aiodos_app_<id>_<secret>`.
  - `id` fait 8 caractères en base32. Il est public : on le voit au journal et dans l'interface.
  - `secret` vaut `token_urlsafe(32)`, soit 256 bits.
  - Le préfixe permet aux détecteurs de secrets de repérer une clé qui fuit. La clé générale garde sa forme actuelle (`aiodos-…`).
- **Rangement**
  - AIODOS ne garde que `key_hash = sha256(clé)`. Un hachage lent est inutile pour un secret aléatoire de 256 bits.
  - La clé n'est remise qu'une fois, à l'appli, ou montrée une fois pour une clé à la main.
  - Seules la clé générale et la clé du pont restent lisibles : elles sont dans les réglages (`gateway_key`, `gateway_bridge_key`), ajoutées à `SECRET_KEYS`, donc protégées par DPAPI (§ 3.12).
- **Contrôle**
  - `Guard` lit l'`id`, puis cherche l'appli dans un cache en mémoire, avec la base en secours, et compare les hachés par `secrets.compare_digest`.
  - Le cache est vidé à chaque modification ou retrait : l'effet est immédiat.
  - L'appli trouvée (`AppContext`) est rangée dans `scope["state"]["aiodos_app"]`. Chaque routeur déclare le droit qu'il exige par la dépendance `need("images")` (`gateway/scopes.py`).
- **Transport de la clé** : `Authorization: Bearer` ou `X-Api-Key`. **Jamais dans l'adresse** ; une clé dans l'adresse est ignorée. Aucun en-tête n'est journalisé.

### 3.3 La demande d'accès (`gateway/access_flow.py`, moyen)

```
Appli                                   Passerelle 127.0.0.1:11435              Interface d'AIODOS
 │ POST /aiodos/v1/access/requests ────► │ Host, pas d'Origin ni de Fetch Metadata,
 │                                       │ JSON, même compte Windows, limites ;
 │                                       │ programme appelant identifié (§ 3.5)
 │ ◄── 200 {device_code, user_code,      │── événement « gateway_access » ─────► carte d'accord,
 │          interval: 2, expires_in}     │                                       fenêtre ramenée (au plus 1 fois / 30 s),
 │ affiche « Code : 482 197 »            │                                       bulle si elle est cachée
 │ POST /aiodos/v1/access/token ───────► │ 400 authorization_pending
 │   (toutes les 2 s)                    │                                       l'utilisateur compare le code,
 │                                       │ ◄── POST /api/gateway/requests/{id}/approve (jeton de l'interface)
 │ POST /aiodos/v1/access/token ───────► │ 200 {access_token, scope, aiodos} : une seule fois
 │ range la clé (DPAPI, safeStorage)     │ seul le haché est gardé
```

**Demande** (sans clé) :

```http
POST /aiodos/v1/access/requests
Content-Type: application/json

{ "client_name": "PANDORA", "client_id": "arkane.pandora", "client_version": "3.2.0",
  "scope": "chat images videos", "optional_scope": "transcribe",
  "where": "mine", "libraries": false,
  "purpose": "Traduire les prompts, créer les moods et les animatiques." }
```

**Réponse 200** :

```json
{ "device_code": "<43 caractères>", "user_code": "482 197", "expires_in": 600,
  "interval": 2, "request_id": "req_8f3KqZ2m",
  "message": "Une demande d'accès t'attend dans AIODOS. Vérifie que le code est 482 197." }
```

**Validation de la demande**
- `client_name` : de 1 à 40 caractères imprimables, normalisés en NFC. Les caractères de contrôle et de sens d'écriture sont retirés (U+202A à U+202E, U+2066 à U+2069, U+200E, U+200F).
- `client_id` : `[a-z0-9.-]{2,40}`.
- `purpose` : 200 caractères au plus.
- `scope` et `where` : parmi les valeurs connues.
- Noms réservés : « AIODOS », « Autres outils », « Windows », « Microsoft ».

**Erreurs de la demande**

| Statut | Code | Cas |
|---|---|---|
| 403 | `requests_closed`, `apps_closed` | demandes ou passerelle aux applis fermées |
| 403 | `other_account` | la demande vient d'un autre compte Windows |
| 403 | `blocked_program` | programme bloqué par l'utilisateur |
| 403 | `origin_refused` | Origin ou Fetch Metadata présents, ou Host `host.docker.internal` |
| 503 | `approval_unavailable` | AIODOS tourne sans fenêtre (`--headless`) |
| 429 | `rate_limited` | trop de demandes |
| 415 | — | corps non JSON |
| 400 | `invalid_request` | champ invalide |

**Relève de la clé** :

```http
POST /aiodos/v1/access/token
{ "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "…" }
```

Les erreurs ont le format d'OAuth : `{"error", "error_description"}`, en statut 400.

| `error` | Sens |
|---|---|
| `authorization_pending` | pas encore de réponse |
| `slow_down` | l'appli interroge trop souvent ; elle ajoute 5 s à son intervalle |
| `access_denied` | refusé |
| `expired_token` | plus de 10 minutes, ou AIODOS a redémarré |
| `invalid_grant` | `device_code` inconnu, ou clé déjà relevée |

Succès :

```json
{ "access_token": "aiodos_app_k3j9m2qa_…", "token_type": "Bearer",
  "scope": "chat images",
  "aiodos": { "app_id": "k3j9m2qa", "where": "mine", "priority": "interactive",
              "libraries": [], "expires": null } }
```

`scope` donne **les droits accordés** : l'utilisateur peut en avoir décoché.

**Règles**
- **Les demandes ne vivent qu'en mémoire**, avec le haché du `device_code`. Un redémarrage les efface et l'appli redemande.
- **La clé en clair** n'existe qu'en mémoire, entre l'accord et la relève. Une appli approuvée qui ne relève pas sa clé en 10 minutes est supprimée.
- **Droits accordés ⊆ droits demandés** : l'utilisateur peut décocher, jamais ajouter. « Amis » n'est jamais coché d'avance.
- **Contre l'insistance**
  - 3 demandes en attente au plus en tout, et une par couple (nom déclaré, programme) ;
  - 10 par heure et par programme ;
  - 3 refus du même nom ou du même programme en une heure le mettent en sourdine pendant une heure, et la carte le dit (« PANDORA insiste : demandes mises en sourdine ») ;
  - une seule carte à la fois, les suivantes attendent ;
  - la fenêtre n'est ramenée qu'une fois toutes les 30 s au plus.
- **Droits en plus** : la même route, avec `Authorization: Bearer <clé actuelle>`.
  - La carte montre la différence : « PANDORA demande en plus : Créer des vidéos ».
  - À l'accord, **la même clé** reçoit les nouveaux droits. La relève répond `{"token_type": "Bearer", "scope": "…", "aiodos": {"upgraded": true}}`, sans `access_token`.
- **Clé perdue ou appli réinstallée.** Une nouvelle demande vient du même `client_id` et du même programme qu'un accès existant. La carte propose alors, coché, « Remplacer l'accès actuel de PANDORA (accordé le 12/10) ». L'ancienne clé est retirée au moment où la nouvelle est relevée.
- **L'appli se retire elle-même** : `POST /aiodos/v1/access/revoke`, avec la clé en `Authorization`. C'est l'esprit de la RFC 7009 : à appeler par le bouton « Se déconnecter d'AIODOS » ou à la désinstallation.

### 3.4 La carte d'accord (`ui/static/js/views/gateway-request.js`)

> **Une application demande à utiliser AIODOS**
>
> **PANDORA** 3.2.0. C'est le nom que donne le programme : AIODOS ne peut pas le vérifier.
> Programme vu par Windows : `C:\Users\moi\AppData\Local\Programs\PANDORA\PANDORA.exe`, sous ton compte Windows.
>
> **Code : 482 197**
> Il doit s'afficher en ce moment dans PANDORA. S'il est différent, ou si tu n'as rien demandé, refuse.
>
> Raison donnée par PANDORA : « Traduire les prompts, créer les moods et les animatiques. »
>
> **Ce que PANDORA pourra faire**
> - [x] Converser avec tes modèles (textes, images jointes, outils de l'application)
> - [x] Créer et retoucher des images
> - [x] Créer des vidéos
> - [ ] Transcrire de l'audio, sur ce PC seulement *(facultatif)*
> - [ ] Lire des passages de bibliothèques : *choisir lesquelles…* *(non demandé)*
>
> **Où ses calculs peuvent se faire**
> - ( ) Ce PC seulement
> - (•) Ce PC et mes machines (PC fixe, Portable)
> - ( ) Aussi chez mes amis qui me prêtent leurs modèles *(non demandé)*
>
> ▸ Plus de réglages
> - Ses conversations passent devant mes travaux de fond : [oui]
> - Durée de l'accès : (•) jusqu'à ce que je le retire · ( ) 30 jours · ( ) jusqu'à la fermeture d'AIODOS
> - [ ] Lier la clé à ce programme : refuser tout autre programme qui s'en servirait
>
> *AIODOS n'envoie rien dans le cloud. Ce que PANDORA fait ensuite de ce qu'elle reçoit (réponses, images, passages), par exemple l'envoyer à ses propres fournisseurs d'IA, ne dépend que d'elle.*
>
> [Refuser] · [Refuser et bloquer ce programme] · **[Autoriser]**, qui devient cliquable 1,5 s après l'apparition. Expire dans 9 min 41 s.

**Variantes**
- **Programme générique** (`python.exe`, `node.exe`, `electron.exe`), en ambre : « Programme générique (Python) : n'importe quel script peut s'en servir. » La case « Lier la clé » est grisée.
- **Programme introuvable** : un bandeau ambre « Programme inconnu ».
- **« Amis » coché** : en ambre, « Les textes, images et photos envoyés par PANDORA seront lus par la machine d'un ami le temps du calcul ». Une seconde confirmation suit.
- **Bibliothèques demandées** :
  - la liste des bibliothèques actives, aucune cochée d'avance ;
  - les bibliothèques « gardées sur ce PC » ne sont pas proposées, et la carte le dit ;
  - la phrase devient : « PANDORA pourra lire des passages de : Scénarios, Bible. Si elle les envoie à un service en ligne, ils quittent ce PC. »
- **Droits en plus** : les droits déjà accordés sont grisés, les nouveaux sont surlignés.
- **Accès existant** : la case cochée « Remplacer l'accès actuel de PANDORA (accordé le 12/10) ».

**Échappement.** Le nom et la raison passent par `esc` (`dom.js`), après le nettoyage du § 3.3. La fonction de rendu est pure et exportée, pour l'essai d'échappement.

### 3.5 L'identité d'une appli : ce qu'on vérifie, ce qu'on ne peut pas (`gateway/caller.py`, petit)

**Vérifié sous Windows, par ctypes, sans dépendance** :
1. la connexion vient de ce PC (bouclage) ;
2. le **pid** : `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_CONNECTIONS)` donne la ligne 127.0.0.1:port du client → 127.0.0.1:port de la passerelle ;
3. le **programme** : `QueryFullProcessImageNameW` ;
4. le **compte Windows** : `OpenProcessToken` puis `GetTokenInformation(TokenUser)`, comparé à celui d'AIODOS.

**Quand on vérifie**
- **À chaque requête** qui porte une clé d'appli, et à chaque demande d'accès. Le coût attendu est de l'ordre de la milliseconde, **à mesurer**.
- Pas de cache par port : un port éphémère peut passer à un autre processus. Si la mesure l'exige, cache par connexion, vidé à sa fermeture. **À vérifier** : comment uvicorn permet d'identifier une connexion.

**Ce qu'on en fait**
- **Compte différent ou illisible** : 403 `other_account`, pour les demandes et les clés d'appli (réglage `gateway_same_account`, vrai).
  - Pour les clés à la main : appliqué par défaut, sauf case « conteneur (Docker) ».
  - Pour la clé générale : noté seulement en v1, parce que Docker Desktop relaie les connexions par son propre programme (**à vérifier**), et rien ne doit casser.
- **Au démarrage**, si le port est pris, `caller.py` nomme le programme qui le tient. Exemple : « Le port 11435 est tenu par C:\…\ollama.exe ».
- **Le programme vu à l'accord est enregistré** (`exe_path`).
  - Si la clé sert depuis un autre programme : une ligne au journal, et un badge sur la carte de l'appli (« Clé de PANDORA utilisée par `curl.exe` le 21/10 à 14:02 »).
  - Si « Lier la clé à ce programme » est coché : 403 `program_mismatch`.
- **Hors de Windows** : identité « inconnue ». Linux lira `/proc` plus tard.

**Non vérifiable**
- Le nom, l'identifiant, la version et la raison : c'est l'appli qui les déclare.
- Le script que fait tourner un interpréteur générique.
- Que le programme est authentique : la signature Authenticode viendra plus tard.
- Ce que l'appli fait des résultats.
- **Qui se sert de la clé ensuite** : c'est un « bearer », valable pour qui le détient.
- Un programme malveillant du même compte : il lit la clé où qu'elle soit (§ 1.3).

Une clé par appli ne protège donc pas contre un logiciel malveillant du même compte. Elle sert à savoir qui fait quoi, à limiter les droits, à retirer une appli sans toucher les autres, et à se protéger des autres comptes, des pages web et du réseau.

### 3.6 Les droits

Ils sont découpés **par capacité, pas par modèle**. Les modèles changent, l'utilisateur doit comprendre chaque case, et la v1 n'a rien à écrire dans les bibliothèques.

| Droit | Routes | Remarque |
|---|---|---|
| `chat` | `/v1/chat/completions`, `/v1/completions`, `/api/chat`, `/api/generate`, `/api/show`, `/api/ps`, MCP `converser` | texte, images jointes, outils, réflexion |
| `embed` | `/v1/embeddings`, `/api/embed`, `/api/embeddings` | toujours sur ce PC |
| `images` | `/v1/images/generations`, `/aiodos/v1/creations` (`kind: image`) | |
| `videos` | `/aiodos/v1/creations` (`kind: video`) | |
| `transcribe` | `/aiodos/v1/audio/transcriptions`, `/aiodos/v1/audio/warmup` | toujours sur ce PC |
| `libraries` | `/aiodos/v1/libraries*`, MCP `lister_bibliotheques` et `rechercher_bibliotheques` | seulement les bibliothèques choisies à l'accord, gardées par paires (id, date de création) et revérifiées à chaque usage, car `collections.id` peut être réutilisé |

**Implicites**, pour toute clé valide :
- `info` ;
- `/v1/models`, `/api/tags` et `/aiodos/v1/models`, filtrés selon les droits et le lieu ;
- ses propres travaux et fichiers ;
- `open` et `access/revoke` ;
- `/aiodos/v1/machines`, selon le lieu.

**Jamais accordables** : les réglages, le réseau, les amis, les clés cloud, la gestion des modèles, les conversations d'AIODOS, la galerie des autres.

**Droit manquant** : 403 `scope_denied`, avec `detail.scope`. Le serveur MCP ne liste que les outils permis.

**Méta-essai** : chaque route montée déclare un droit ou figure dans la liste des routes publiques.

### 3.7 Où calculer (`where`) : ce qui peut partir chez un ami, et jamais dans le cloud

`where` vaut `here` (ce PC), `mine` (ce PC et mes machines, **par défaut**) ou `friends` (aussi mes amis).

- **Lieu effectif** : le plus étroit entre le plafond de l'appli, l'en-tête `X-Aiodos-Where` de la requête et les réglages généraux.
- Un en-tête plus large que le plafond donne 403 `where_denied`. AIODOS ne réduit pas en silence : l'appli doit le savoir.

| Capacité | `here` | `mine` | `friends` |
|---|---|---|---|
| Conversation, nom seul (automatique) | ce PC | `placement.plan` limité à mes machines | les amis aussi, seulement si `auto_route_friends` |
| `local:<m>` | oui | oui | oui |
| `peer:<ma machine>:<m>`, `reparti:<m>` | 403 | oui (pour `reparti:`, si tous les participants sont à moi, **à vérifier** dans `network/cluster`) | oui |
| `peer:<ami>:<m>` | 403 | 403 | oui |
| Outils ou images jointes | toujours ce PC. Sinon 400 `tools_unavailable` ou `local_only` | | |
| Embeddings, transcription | toujours ce PC | | |
| Images et vidéos (consigne, image de départ) | `units.local_only = 1` | `units.reach = 'mine'`, jamais confiée à un ami | les amis aussi, seulement si `images_from_friends` et si l'ami prête |
| Recherche dans les bibliothèques | les bibliothèques de ce PC choisies à l'accord, jamais celles des autres machines en v1 | | |
| **Cloud** | **jamais** | **jamais** | **jamais** |

- **Listes de modèles** : avec `mine`, elles ne montrent ni les modèles ni les noms de machines des amis.
- **Chaque réponse dit qui a calculé**, par trois en-têtes, et par l'objet `aiodos` du dernier morceau ou de chaque fichier :
  - `X-Aiodos-Model` : l'identifiant concret ;
  - `X-Aiodos-Machine` : le nom, en UTF-8 encodé pour cent ;
  - `X-Aiodos-Place` : `here`, `mine` ou `friend`.
- **Au journal**, tout calcul chez un ami est compté à part. La carte de l'appli l'affiche en clair : « calculé chez Paul : 3 ».

### 3.8 Priorité et garde-fou

- **Priorité de l'appli** : `interactive` par défaut, ou `background`.
  - **Conversation.** `answer.run`, puis `placement.run`, passent `interactive` à `_occupy(model, executor, interactive=…)`. En `background`, `executor.interactive_begin` n'est pas appelé : rien n'est suspendu, et sd-server n'est pas arrêté.
  - **Transcription** : interactive.
  - **Créations** : priorité normale de la file. `background` donne `LOW`.
- **`X-Aiodos-Priority: background`** baisse la priorité d'une requête, jamais l'inverse (I12). Les fonctions de lot des clients (`batch=True`) l'envoient. Le découpage de PANDORA ne gèle donc plus les images, les vidéos ni l'indexation.
- **Garde-fou contre l'accaparement.** Au-delà de 10 minutes de conversation interactive sur 15 pour une même appli, ses demandes passent en fond jusqu'à 2 minutes de pause. AIODOS affiche « PANDORA occupe la carte graphique depuis 10 min ». Valeurs à régler à l'usage.

### 3.9 Retrait, modification, expiration, blocage

- **Retirer**, depuis la carte de l'appli :
  - `revoked` est posé, et la ligne gardée pour que `info` réponde `revoked` ;
  - le cache est vidé : l'effet est immédiat ;
  - **les flux en cours sont coupés**, par des signaux d'arrêt propres à chaque appli, tenus par `apps.py` ;
  - **les travaux sont annulés**, par une case cochée par défaut (annulation réelle, L2) ;
  - les liens signés cessent de marcher.

  Texte de confirmation : « Retirer l'accès de PANDORA ? Ses demandes en cours s'arrêtent et ses travaux sont annulés. Pour revenir, PANDORA devra redemander l'accès. »
- **Modifier** les droits, le lieu, la priorité, les bibliothèques ou la liaison au programme : l'effet est immédiat, avec la même clé.
- **Expiration**, au choix lors de l'accord :
  - aucune (par défaut) ;
  - 30 jours ;
  - « jusqu'à la fermeture d'AIODOS ». Ces clés sont effacées au démarrage suivant.
- **Un accès inutilisé depuis 90 jours** reçoit un badge ambre « Le retirer ? ». Rien n'est retiré tout seul.
- **Bloquer un programme** : table `gateway_blocked`. Ses demandes suivantes sont refusées sans carte ; on le débloque depuis la liste.

### 3.10 Journal d'usage (`gateway/usage.py`, petit)

- **Jamais de contenu** (I8) : ni consigne, ni réponse, ni image, ni audio, ni nom de fichier envoyé, ni en-tête.
- **`gateway_usage`** : un agrégat par jour, par appli, par famille et par machine. Il est tenu en mémoire, écrit toutes les 30 s et gardé 90 jours. Colonnes :
  - famille : chat, embed, images, videos, transcribe, libraries ou mcp ;
  - lieu ;
  - requêtes et erreurs ;
  - jetons en entrée et en sortie ;
  - éléments produits ;
  - secondes de vidéo et d'audio ;
  - secondes de calcul.
- **`gateway_log`** : une ligne par requête terminée (pour un flux, à sa fin). Gardée 30 jours, 20 000 lignes au plus (`gateway_log_days`). Colonnes :
  - l'heure, l'appli, la route, le modèle concret, la machine, le lieu ;
  - le statut HTTP, le code d'erreur, la durée, `X-Request-Id` ;
  - les jetons et les octets ;
  - le programme, seulement s'il diffère de celui vu à l'accord.
- **Comment c'est rempli.** `Guard` enveloppe `send` pour relever le statut, la durée et les en-têtes `X-Aiodos-*`. Les routes ajoutent les jetons par `request.state.usage`.
- **Affichage sur la carte de l'appli** :
  - la dernière utilisation ;
  - les compteurs du jour et des 7 jours ;
  - les 50 dernières lignes ;
  - **en relief** : « calculé chez un ami : N », « programme inattendu », « refusé : N ».
- **Ailleurs dans AIODOS.** Le journal général ajoute « pour PANDORA », et la vue Travaux affiche « pour PANDORA ».
- **`X-Aiodos-App: Open WebUI`** n'est lu que pour la clé générale, et ne sert qu'au journal.

### 3.11 Limites (`gateway/limits.py`, petit ; réglables par appli plus tard)

| Limite | Défaut | Dépassement |
|---|---|---|
| Corps d'une requête | 32 Mo, vérifiés sur `Content-Length` puis comptés pendant la lecture | 413 `file_too_large` |
| Image envoyée | 20 Mo et 16 Mpx, dimensions lues **avant** décodage ; 4 par requête de conversation | 413 ou 400 `invalid_image` |
| Audio | 25 Mo en JSON ; 200 Mo en corps brut asynchrone ; 10 min en synchrone | 413 |
| Requêtes par minute et par appli | 120 (seau à jetons) | 429 `rate_limited` avec `Retry-After` |
| Requêtes de conversation et d'embeddings en cours | 4 par appli | 429 `busy` avec `Retry-After: 2` |
| Travaux actifs | 10 par appli ; 64 unités d'images en file ; 2 vidéos | 429 `quota_exceeded` |
| Transcriptions interactives | 1 en cours et 2 en attente, toutes applis confondues | 429 `busy` |
| Demandes d'accès | § 3.3 | 429 |
| Clés invalides | journalisées ; au-delà de 30 par minute, chaque réponse attend 1 s | 401 |
| `POST /aiodos/v1/open` | 1 par 10 s et par appli | 429 |

La clé générale garde des limites larges.

### 3.12 Clé générale, clés à la main, clé du pont MCP

- **Clé générale « Autres outils »** : rien ne casse pour Open WebUI, Continue et les assistants MCP.
  - Au démarrage, `apps.ensure_general()` crée sa ligne (`kind = 'general'`), avec le haché de la clé existante.
  - Elle garde ses droits d'aujourd'hui : `chat embed libraries`, sur toutes les bibliothèques actives non « gardées sur ce PC ».
  - Elle garde le lieu `friends` : `peer:<ami>` explicite, et l'automatique seulement si `auto_route_friends`.
  - **Elle est protégée par DPAPI** : `gateway_key` entre dans `SECRET_KEYS` et suit `settings.protect_secrets`, c'est-à-dire qu'elle n'est protégée qu'une fois la version jugée saine. **À vérifier** : `access._stored` lit aujourd'hui la valeur brute ; il doit passer par `settings`.
  - « Renouveler » ne touche qu'elle.
  - **Sur une nouvelle installation, elle n'est plus créée d'office** : un bouton « Créer une clé générale » la crée.
- **Clés à la main**, le remplaçant conseillé de la clé générale.
  - Réglages → Applications → « Ajouter un outil » : un nom, des droits, un lieu, et la case « conteneur (Docker) ».
  - La clé est montrée une seule fois, à copier (`kind = 'manual'`).
  - C'est aussi ce qu'utilise une appli sans bibliothèque cliente, par son fournisseur compatible OpenAI. Elle perd alors la preuve et l'accord par code.
- **Pont MCP stdio** (`mcp_stdio.py`) : il garde la clé générale (`access.existing()`). Si elle n'existe pas, une clé `bridge` est créée quand l'utilisateur copie la configuration stdio. Elle est rangée sous DPAPI dans `gateway_bridge_key`, puisque le pont doit la relire.

---

## 4. L'API

### 4.1 Conventions

**Base** : `http://127.0.0.1:<port>`.

**Routes sans clé**, liste exacte :
- `GET /` ;
- `GET /aiodos/v1/info` ;
- `POST /aiodos/v1/access/requests` (avec une clé, c'est une demande de droits en plus) ;
- `POST /aiodos/v1/access/token` ;
- `GET /aiodos/v1/files/{id}/content`, seulement avec `exp`, `n` et `sig` valides.

**En-têtes de requête**

| En-tête | Valeurs | Effet |
|---|---|---|
| `Authorization: Bearer` / `X-Api-Key` | la clé | |
| `X-Aiodos-Where` | `here`, `mine`, `friends` | resserre le lieu (§ 3.7) |
| `X-Aiodos-Priority` | `background` | baisse la priorité (§ 3.8) |
| `X-Aiodos-Tools` | `optional` | accepte une réponse sans outils (§ 4.5) |
| `Prefer: respond-async` | RFC 7240 | images et transcription : 202 et un travail, au lieu d'attendre |
| `Idempotency-Key` | 1 à 64 caractères | sur les POST qui créent un travail ; gardée 24 h par appli ; la même clé rend le même travail |
| `Accept-Language` | `fr`, `en` | langue de `error.message`, le français par défaut |
| `X-Aiodos-Client` | `python/0.1.0`, `js/0.1.0` | journal |
| `X-Aiodos-App` | texte | journal, clé générale seulement |

**En-têtes de réponse**
- `X-Aiodos-Api: 1` ;
- `X-Aiodos-Model`, `X-Aiodos-Machine`, `X-Aiodos-Place` ;
- `X-Request-Id` ;
- `Retry-After` sur 429 et 503 ;
- `Location` et `Preference-Applied: respond-async` sur 202.

**Formats**
- JSON en UTF-8. Les POST de `/aiodos/v1` exigent `Content-Type: application/json`, sauf les corps audio bruts.
- Dates en secondes Unix (`created_at`). Durées en secondes.
- Identifiants publics tirés au hasard, jamais réutilisés : `job_…`, `file_…`, `req_…`. Les numéros internes ne sortent jamais.

**Ordre des verrous dans `Guard`**
1. Host ;
2. Origin et Fetch Metadata (`gateway/web_guard.py`) ;
3. route publique, ou bien clé, puis appli ;
4. compte Windows et programme ;
5. limites ;
6. la route, avec son droit ;
7. le journal.

### 4.2 Erreurs et codes stables (`gateway/errors.py`, petit)

**Enveloppes**
- `/v1` et `/aiodos/v1` reçoivent l'enveloppe d'OpenAI, plus un objet `aiodos` :

  ```json
  { "error": { "message": "Le texte fait 9 412 jetons, ce modèle en accepte 8 192.",
               "type": "invalid_request_error", "param": "messages",
               "code": "context_length_exceeded",
               "aiodos": { "hint": "Raccourcis la demande, ou augmente le contexte dans AIODOS (Réglages → Moteur).",
                           "retryable": false, "detail": { "needed": 9412, "limit": 8192 } } } }
  ```

- `/api/*` : `{"error": "…", "code": "…", "hint": "…"}`. Ollama ne met que `error`, et ses clients ignorent le reste.
- `/mcp` : le code va dans `error.data.code`.
- `/aiodos/v1/access/token` : le format d'OAuth.
- Les messages sont en français et en anglais dans `errors.py`. Les applis traduisent d'après le `code`.

**Codes**

| HTTP | `code` | Quand | Ce que fait l'appli |
|---|---|---|---|
| 400 | `host_refused` | Host inattendu (rebinding DNS) | — |
| 400 | `context_length_exceeded` | la demande dépasse le contexte (`needed`, `limit`) | découper, ou raccourcir |
| 400 | `tools_unavailable` | outils demandés, aucune machine permise ne les prend | choisir un modèle d'ici, ou `X-Aiodos-Tools: optional` |
| 400 | `vision_unsupported` | images envoyées à un modèle qui ne les voit pas | choisir un modèle « vision » |
| 400 | `local_only` | la demande ne se calcule que sur ce PC, où le modèle n'est pas | installer le modèle ici |
| 400 | `unsupported_parameter`, `unsupported_value` | paramètre standard que l'on ne traite pas (`param`) | — |
| 400 | `invalid_request`, `invalid_size`, `invalid_image`, `invalid_audio` | `param` et les bornes dans `detail` | — |
| 401 | `invalid_api_key` | clé absente ou inconnue | demander l'accès |
| 401 | `key_revoked`, `key_expired` | clé retirée ou expirée | redemander une fois, après un clic |
| 403 | `scope_denied` | droit manquant (`detail.scope`) | demander le droit en plus |
| 403 | `where_denied` | lieu plus large que permis | — |
| 403 | `library_denied` | bibliothèque non autorisée | — |
| 403 | `program_mismatch` | clé liée à un autre programme | se reconnecter |
| 403 | `other_account` | autre compte Windows | — |
| 403 | `origin_refused` | page web | appeler depuis le processus principal |
| 403 | `apps_closed`, `requests_closed`, `blocked_program` | § 3.1, § 3.3 | ouvrir les réglages d'AIODOS |
| 404 | `model_not_found`, `job_not_found`, `file_not_found` | introuvable (y compris ce qui appartient à une autre appli) | — |
| 404 | `not_supported` | fonction absente de cette version (`/api/pull`…) | mettre AIODOS à jour, ou installer dans AIODOS |
| 409 | `model_not_installed` | modèle connu mais absent (`size_bytes`, `license`, `page`) | « L'installer dans AIODOS » (`open`) |
| 409 | `license_not_accepted` | licence restrictive non acceptée dans AIODOS (MiniMax H3) | idem |
| 409 | `engine_not_installed` | stable-diffusion.cpp ou Whisper absent | idem |
| 409 | `no_machine` | aucune machine permise n'a le modèle | — |
| 409 | `job_not_cancellable` | travail déjà terminé | — |
| 410 | `file_expired`, `link_expired` | fichier passé sa durée de garde, ou lien expiré ou épuisé | redemander un lien |
| 413 | `file_too_large` | — | — |
| 415 | `unsupported_media_type` | corps non JSON, ou format refusé | — |
| 429 | `rate_limited`, `busy`, `quota_exceeded` | voir `Retry-After` | attendre |
| 502 | `peer_failed`, `engine_failed` | échec sur la machine choisie | réessayer |
| 503 | `engine_unavailable`, `approval_unavailable` | moteur en panne ou en démarrage ; pas de fenêtre | réessayer |
| 504 | `timeout` | attente synchrone trop longue (`detail.job_id`) | suivre le travail |

**Codes propres aux clients**, qui portent la même enveloppe :
- `unreachable`, dont le message contient « Connexion refusée », pour que l'interface actuelle d'ARKLIGHT ouvre ses réglages ;
- `gateway_closed`, `apps_closed`, `not_aiodos`, `too_old`, `untrusted` ;
- `non_local_url`, `timeout` ;
- `cancelled`, dont le message est « Arrêté » ;
- `protocol` : réponse incomprise.

### 4.3 Toutes les routes

| Méthode et chemin | Clé | Droit | Forme | Livraison |
|---|---|---|---|---|
| `GET /` | non | — | Ollama, inchangé | existe |
| `GET /aiodos/v1/info` | facultative | — | AIODOS, avec la preuve | L1 |
| `POST /aiodos/v1/access/requests` | non (ou la clé, pour des droits en plus) | — | RFC 8628 | L1 |
| `POST /aiodos/v1/access/token` | non | — | RFC 8628, OAuth | L1 |
| `POST /aiodos/v1/access/revoke` | oui | — | esprit de la RFC 7009 | L1 |
| `GET /aiodos/v1/models?kind=` | oui | filtré | AIODOS | L1, médias en L2 et L3 |
| `POST /aiodos/v1/open` | oui | — | AIODOS | L1 |
| `GET /v1/models`, `GET /v1/models/{id}` | oui | filtré | OpenAI, avec les champs d'OpenRouter | existe, étendu en L1 |
| `POST /v1/chat/completions`, `POST /v1/completions` | oui | `chat` | OpenAI | existe, retouché en L1 |
| `POST /v1/embeddings` | oui | `embed` | OpenAI | existe |
| `/api/*` (tags, show, ps, chat, generate, embed, embeddings) | oui | `chat` / `embed` | Ollama, avec `code` | existe, retouché en L1 |
| `POST /mcp` | oui | `chat` / `libraries` | MCP, outils filtrés | existe, filtré en L1 |
| `POST /aiodos/v1/creations` | oui | `images` / `videos` | AIODOS, 202 | L2 |
| `POST /v1/images/generations` | oui | `images` | OpenAI, synchrone ou 202 | L2 |
| `GET /aiodos/v1/jobs?active=&status=&limit=&after=` | oui | les siens | AIODOS | L2 |
| `GET /aiodos/v1/jobs/{id}` | oui | le sien | AIODOS | L2 |
| `GET /aiodos/v1/jobs/{id}/events` | oui | le sien | SSE | L2 |
| `POST /aiodos/v1/jobs/{id}/cancel` | oui | le sien | AIODOS, 202 | L2 |
| `DELETE /aiodos/v1/jobs/{id}` | oui | le sien, terminé | 204 | L2 |
| `GET /aiodos/v1/files/{id}/content` | oui, ou lien signé | le sien | octets, `Range` | L2 |
| `POST /aiodos/v1/files/{id}/link` | oui | le sien | AIODOS | L2 |
| `DELETE /aiodos/v1/files/{id}` | oui | le sien | 204 | L2 |
| `POST /aiodos/v1/audio/transcriptions` | oui | `transcribe` | AIODOS (réponse au format d'OpenAI) | L3 |
| `POST /aiodos/v1/audio/warmup` | oui | `transcribe` | AIODOS | L3 |
| `GET /aiodos/v1/libraries`, `POST /aiodos/v1/libraries/search` | oui | `libraries` | AIODOS | L4 |
| `GET /aiodos/v1/machines` | oui | selon le lieu | AIODOS | L4 |

### 4.4 Le catalogue (`gateway/model_card.py` commun ; `gateway/models_api.py`)

Un seul module construit les fiches des modèles. Il sert `/v1/models`, `/api/tags`, `/api/show` et `/aiodos/v1/models`, si bien que les trois ne peuvent pas diverger.

**`GET /v1/models`**
- La forme d'OpenAI ne change pas. La liste ne contient que **les modèles de conversation et d'embedding** : les identifiants concrets (`local:`, `peer:`, `reparti:`, et le nom seul), filtrés selon les droits et le lieu.
- Chaque entrée gagne, à la façon d'OpenRouter que `discover_provider` de PANDORA lit déjà :

```json
{ "id": "qwen3.5-9b", "object": "model", "created": 1760000000, "owned_by": "PC-bureau",
  "name": "qwen3.5 9B · cette machine",
  "context_length": 16384,
  "architecture": { "input_modalities": ["text", "image"], "output_modalities": ["text"] },
  "supported_parameters": ["tools", "reasoning", "response_format", "max_tokens",
                           "temperature", "top_p", "stop", "seed"],
  "aiodos": { "kind": "chat", "machine": "PC-bureau", "here": true, "loaded": true,
              "installed": true, "size_bytes": 6100000000,
              "license": { "id": "apache-2.0", "restrictive": false } } }
```

- `context_length` est la valeur **réelle** du routeur. Pour une machine appairée, elle vaut `null` : le protocole ne la transmet pas.
- `/api/tags` et `/api/show` donnent le même contexte : la branche le fait déjà par `engine._context`.

**`GET /aiodos/v1/models?kind=chat|embed|image|video|transcribe`** : le catalogue complet.
- Les modèles sont regroupés par modèle logique, avec où ils se trouvent.
- Les modèles absents sont montrés aussi (`installed: false`), pour que l'appli propose « L'installer dans AIODOS ».

```json
{ "models": [
  { "id": "qwen3.5-9b", "kind": "chat", "label": "Qwen 3.5 9B", "installed": true,
    "where": [ { "id": "local:qwen3.5-9b", "machine": "PC fixe", "place": "here",
                 "loaded": true, "context": 16384 },
               { "id": "peer:Portable:qwen3.5-9b", "machine": "Portable", "place": "mine",
                 "context": null } ],
    "chat": { "context": 16384, "vision": true, "tools": true, "thinking": true,
              "tools_where": "here", "vision_where": "here" },
    "license": { "id": "apache-2.0", "restrictive": false } },
  { "id": "z-image-turbo", "kind": "image", "installed": true,
    "image": { "multiple": 64, "side": [256, 2048], "max_pixels": 2359296,
               "default_size": "1024x1024", "steps": { "default": 8, "max": 100 },
               "negative": true, "init_image": true, "strength": [0.05, 1.0], "max_count": 16 },
    "where": { "here": true, "mine": ["Portable"], "friends": [] },
    "typical_seconds": { "1024x1024": 11.2 },
    "license": { "id": "apache-2.0", "restrictive": false }, "size_bytes": 6100000000 },
  { "id": "wan2.1-t2v-1.3b", "kind": "video", "installed": true,
    "video": { "fps": 16, "frame_base": 1, "frame_step": 4, "max_frames": 161,
               "sizes": ["832x480", "480x832"], "seconds": { "min": 1, "max": 5 },
               "init_image": false, "end_image": false, "audio": false, "container": "webm" } },
  { "id": "minimax-h3", "kind": "video", "installed": false,
    "license": { "id": "minimax-h3-community", "restrictive": true, "accepted": false,
                 "url": "…", "notice_fr": "… exclut l'Union européenne …", "notice_en": "…" },
    "install": { "page": "models", "size_bytes": 0 } },
  { "id": "whisper-small", "kind": "transcribe", "installed": false, "aliases": ["whisper-1"],
    "transcribe": { "languages": ["auto", "fr", "en", "es"], "prompt": true,
                    "timestamps": ["segment", "word"] },
    "install": { "page": "models" } } ] }
```

- Les valeurs viennent de `imaging/catalog.py` et de `llm/engine` ; `typical_seconds` vient de `gallery.typical_seconds`. Les nombres de l'exemple sont à recopier depuis le code.
- Le catalogue permet à PANDORA de remplir `ENGINE_CAPS`, ses tables de résolutions et ses prix à 0 $ à partir des données.

### 4.5 Conversation : ce qui change (L1)

1. **Contexte.**
   - Il vient du réglage (§ 6.2, `llm/context.py`) ; `num_ctx` est ignoré.
   - Une demande trop longue donne 400 `context_length_exceeded`, avec les nombres tirés de l'erreur du routeur. **À vérifier** : le routeur donne-t-il `n_prompt_tokens` et `n_ctx` ?
   - `max_tokens` et `max_completion_tokens` sont acceptés tous les deux.
   - **Un `max_tokens` trop grand n'est jamais refusé** : llama.cpp s'arrête à la fin du contexte et rend `finish_reason: "length"`. Le dernier morceau (ou la réponse) porte `aiodos.stopped_by` (`context`, `max_tokens` ou `stop`) et `aiodos.context_length`.
2. **Outils jamais supprimés en silence.**
   - Pour une clé d'appli ou une clé à la main, ne comptent que les machines qui reçoivent les outils (en v1, ce PC). Sinon : 400 `tools_unavailable`. Exemple : « qwen3.5-9b n'est que sur PC fixe, qui ne reçoit pas encore les outils ».
   - `X-Aiodos-Tools: optional` rend le comportement d'aujourd'hui.
   - Pour la clé générale, le comportement est inchangé en v1, avec l'en-tête `X-Aiodos-Warning: tools-dropped` (Q6).
   - Images jointes : `local_only` si le modèle n'est pas ici, `vision_unsupported` s'il ne voit pas les images.
   - C'est une retouche de `answer.plan` : **petit**.
3. **Chargement.**
   - S'il n'y a qu'une machine possible, les en-têtes partent tout de suite, et `: aiodos loading <secondes>` part toutes les 5 s jusqu'au premier morceau. C'est un commentaire SSE, que les SDK ignorent ; les clients y voient `onLoading` et remettent leur délai d'inactivité à zéro.
   - S'il y a plusieurs machines possibles, les en-têtes attendent le premier morceau, pour garder la bascule d'une machine à l'autre. Le client règle alors son délai de chargement sur `aiodos.loaded`.
4. **Sans flux**, une appli qui se déconnecte arrête désormais le calcul : `request.is_disconnected()` est surveillé dans `engine.openai_chat`, ce qui lève la limite du § 8 de la doc. Les clients passent quand même toujours en flux.
5. **Réflexion.** `reasoning_effort: "none"` la coupe : c'est le réglage conseillé aux applis. `chat_template_kwargs` passe tel quel au routeur.
6. **Paramètres.**
   - Ignorés, et documentés : `user`, `store`, `metadata`, `service_tier`, `prompt_cache_key`, `safety_identifier`, `parallel_tool_calls`.
   - Refusés par 400 `unsupported_parameter` : `audio`, `modalities` avec `audio`, `prediction`, `web_search_options`.
7. **Fin.** Le dernier morceau, avant `[DONE]`, porte `"aiodos": {"model", "machine", "place", "stopped_by", "context_length"}`. Une erreur en cours de route arrive en `data: {"error": {…}}`, puis la connexion se ferme.
8. **Délais.** `TIMEOUT = (10, 900)` mesure l'inactivité entre deux morceaux : sans conséquence en flux. Une très longue réponse **sans flux** pourrait buter dessus : c'est documenté, et les clients passent toujours en flux.

### 4.6 Embeddings, Ollama, MCP

- **Embeddings** : inchangés, avec le droit `embed`, sur ce PC.
- **`/api/*`**
  - Les droits, `code` et `hint` dans les erreurs, et `X-Aiodos-Model`, `X-Aiodos-Machine`, `X-Aiodos-Place`.
  - Pour une clé d'appli, `keep_alive: 0` et le chargement à vide de `/api/generate` sont ignorés : une appli ne décharge pas le modèle d'une autre (I5).
  - `/api/pull`, `create` et `delete` répondent 404 `not_supported`, avec le conseil « installe le modèle dans AIODOS ».
  - Les fournisseurs « ollama » de PANDORA et d'ARKLIGHT, qui n'envoient pas de clé, **restent refusés**. Accepter des demandes sans clé ouvrirait la porte à tout programme du PC. La doc oriente vers les clients ou vers la forme d'OpenAI.
- **MCP** : `tools/list` ne montre que les outils permis (`converser` avec `chat`, les deux outils de bibliothèque avec `libraries`, limités à la liste choisie). La clé générale garde ce qu'elle a.

### 4.7 Créations : `POST /aiodos/v1/creations` (L2, `gateway/creations_api.py`)

```json
{ "kind": "video", "model": "wan2.1-t2v-1.3b",
  "prompt": "Travelling lent sur une table dressée, lumière de bougie…", "negative": "",
  "width": 832, "height": 480, "fit": "round", "seconds": 5,
  "count": 1, "steps": null, "seed": null,
  "init_image": null, "strength": null, "end_image": null,
  "label": "Animatique · Séq. 3 · Plan 7" }
```

**Les champs**
- **`fit`**
  - `round`, par défaut : AIODOS arrondit aux multiples du modèle, en gardant le rapport au plus près, puis ramène dans les bornes. La réponse donne `adjusted: [{"param": "height", "asked": 1080, "used": 1088}]`.
  - `exact` : 400 `invalid_size` si la taille n'est pas permise, avec les bornes dans `detail`.
- **`seconds` ou `frames`**, pour une vidéo : le nombre d'images valide le plus proche (4k+1 pour Wan, 17k+5 pour H3), signalé dans `adjusted`.
- **`init_image`** : PNG, JPEG ou WebP, en base64 ou en data-URL.
  - Le type est reconnu par les premiers octets, et les dimensions lues avant le décodage (16 Mpx au plus).
  - Pillow convertit l'image en PNG aux multiples du modèle, puis l'écrit dans `data\gateway\in\<aléatoire>`.
  - Le fichier est effacé à la fin du travail, ou au bout de 24 h au plus tard (nettoyage au démarrage aussi).
  - **Aucun chemin n'est accepté** (I7) : `init_path` disparaît de cette voie.
- **`end_image`** et `init_image` en vidéo : seulement pour H3, après la L4. Sinon, 400 `unsupported_parameter`.
- **`label`** : affiché dans la vue Travaux d'AIODOS, sous le nom de l'appli.
- **Tout champ inconnu** donne 400 `invalid_request`.

**En-têtes** : `X-Aiodos-Where`, `X-Aiodos-Priority`, `Idempotency-Key`.

**Réponse** : 202, avec `Location: /aiodos/v1/jobs/job_…`. Le corps est l'objet travail (§ 4.9).

**Refus avant toute mise en file** :
- `scope_denied`, `where_denied` ;
- `model_not_found` ;
- `model_not_installed`, si le lieu est `here` et le modèle absent ici ;
- `no_machine`, si aucune machine permise n'a le modèle (sinon l'unité attendrait à jamais) ;
- `license_not_accepted`, `engine_not_installed` ;
- `invalid_request`, `invalid_size`, `invalid_image` ;
- `quota_exceeded`.

Un modèle présent seulement sur une de mes machines hors ligne est accepté : le travail passe à l'étape `waiting_machine`.

**Aucun téléchargement** : la passerelle n'appelle jamais `download.start`, et un essai le vérifie.

`gateway/jobs_bridge.py` crée le travail. Il pose `owner_app`, `public_id`, `label`, `reach` (`local_only` pour `here`), la priorité et la clé d'idempotence. Il fait aussi le lien entre un travail et sa réponse standard.

### 4.8 Images au format d'OpenAI : `POST /v1/images/generations` (L2, `gateway/images_api.py`)

```json
{ "model": "z-image-turbo", "prompt": "…", "n": 2, "size": "1920x1080",
  "response_format": "url", "seed": 7, "negative_prompt": "texte, logo", "steps": 8 }
```

**Les champs**
- `model` absent : le modèle d'images par défaut d'AIODOS. Pas d'alias `dall-e-*` ni `gpt-image-*` : ce serait trompeur.
- `size` : « LxH » ou `auto`. Elle est arrondie comme avec `fit: round`, et la taille réelle est rendue.
- `n` : 16 au plus, dans la limite de l'appli.
- `response_format`
  - `b64_json`, par défaut ;
  - `url` : un **lien signé** valable 60 minutes et 3 usages (§ 4.10), téléchargeable sans en-tête. C'est ce qu'attend `image_call.subscribe` de PANDORA, qui fait `requests.get(url)`.
- Ignorés : `quality`, `moderation`, `style`, `user`.
- Refusés par 400 : `background: "transparent"`, `output_format` autre que `png`, `stream`, `partial_images`.

**Réponse**

```json
{ "created": 1760970000, "size": "1920x1088", "output_format": "png",
  "data": [ { "url": "http://127.0.0.1:11435/aiodos/v1/files/file_8g2…/content?exp=1760973600&n=3&sig=…",
              "aiodos": { "file_id": "file_8g2…", "width": 1920, "height": 1088, "seed": 7 } } ],
  "aiodos": { "job_id": "job_4m…", "model": "z-image-turbo", "machine": "PC fixe",
              "seconds": 11.4, "adjusted": [ { "param": "height", "asked": 1080, "used": 1088 } ] } }
```

**Synchrone ou asynchrone**
- **Par défaut**, la passerelle attend la fin du travail, 600 s au plus. Au-delà : 504 `timeout`, avec `detail.job_id` pour reprendre le suivi.
- **Si l'appli se déconnecte, le travail est annulé** : personne ne viendra le chercher.
- **Avec `Prefer: respond-async`** : réponse 202 avec `Location`, et le corps est l'objet travail. Son `result` contiendra exactement la réponse ci-dessus. Les clients passent toujours par là.

### 4.9 Travaux : état, avancement honnête, annulation réelle (L2)

**L'objet travail** (`gateway/jobs_api.py`)

```json
{ "id": "job_8Hk2Qp…", "object": "aiodos.job", "kind": "video", "model": "wan2.1-t2v-1.3b",
  "label": "Animatique · Séq. 3 · Plan 7",
  "status": "in_progress",
  "progress": { "stage": "running", "percent": 42, "estimated": true,
                "elapsed_s": 61, "eta_s": 84, "done": 0, "total": 1, "failed": 0,
                "position": null, "machine": { "name": "PC fixe", "place": "here" },
                "message": "Calcul de la vidéo sur PC fixe… environ 1 min 24 restante(s)" },
  "params": { "width": 832, "height": 480, "frames": 81, "steps": 20, "seed": 912733 },
  "adjusted": [ { "param": "frames", "asked": 80, "used": 81 } ],
  "files": [], "error": null, "cancelled_by": null, "result": null,
  "created_at": 1760950000, "started_at": 1760950003, "completed_at": null, "expires_at": null }
```

**`status`**, dans le vocabulaire d'OpenAI :

| `status` | Sens |
|---|---|
| `queued` | en file, ou en pause |
| `in_progress` | en cours |
| `cancelling` | annulation demandée, des unités s'arrêtent encore |
| `completed` | terminé |
| `failed` | échec, avec `error {code, message}` |
| `cancelled` | annulé, avec `cancelled_by` (`app`, `aiodos` ou `revoked`) |

**`progress.stage`**, plus fin, avec son message :

| Étape | Message |
|---|---|
| `queued` | « En file : 2 travaux avant celui-ci » |
| `waiting_machine` | « Aucune machine ne peut calculer Wan 2.1 en ce moment (Portable est hors ligne) ». La raison vient de `queue.not_here`, **à vérifier** |
| `paused` | « une conversation passe devant », « sur batterie » ou « mis en pause dans AIODOS » |
| `loading` | « Chargement de Wan 2.1 dans la carte graphique », tiré de l'état de `imaging/engine`, **à vérifier** |
| `running` | « Calcul… » |
| `saving` | « Enregistrement », ou « Réception depuis Portable » |
| `cancelling` | « Annulation… (Portable s'arrêtera dans 30 s au plus) » |
| `unreachable` | côté client seulement : AIODOS ne répond plus |

**Le pourcentage, honnêtement** (`gateway/progress.py`, une fonction pure)
- `percent = 100 × (unités faites + Σ p_unité en cours) / total`.
- `p_unité = min(0,95 ; temps écoulé / durée habituelle)`. La durée habituelle est la médiane des derniers calculs (`gallery.typical_seconds`, par modèle et par taille).
- `estimated: true` tant que c'est une estimation par le temps.
- Sans historique : `percent` et `eta_s` valent `null`, et le message dit « Première vidéo avec ce modèle : durée inconnue ; 1 min 12 écoulée ».
- **À vérifier** : si sd-server écrit ses pas (« 12/20 ») dans son journal, `progress.py` pourra donner les vrais pas (L4).

**Routes**
- `GET /aiodos/v1/jobs?active=1&status=&limit=&after=` : les travaux de l'appli seulement (`jobs.owner_app`).
- `GET /aiodos/v1/jobs/{id}` : 404 s'il appartient à une autre appli.
- `GET /aiodos/v1/jobs/{id}/events`, en SSE (`gateway/job_events.py`) :

  ```
  event: job
  data: {…objet travail…}

  event: file
  data: {"id": "file_…", "type": "video/webm", "width": 832, "height": 480, "frames": 81,
         "fps": 16, "seconds": 5.06, "bytes": 3418221, "seed": 912733, "machine": "PC fixe"}

  event: end
  data: {"status": "completed", "job": {…}}
  ```

  - Le premier événement est toujours l'état complet : pas d'historique à rejouer.
  - Puis un événement à chaque changement, 4 par seconde au plus.
  - Un `: ping` part toutes les 15 s, et le flux se ferme après `end`.
  - Le bus d'AIODOS est **filtré** par travail, et la propriété est vérifiée à l'abonnement. Le bus porte aussi les réglages, les conversations et le réseau : il n'est jamais relayé tel quel.
- `POST /aiodos/v1/jobs/{id}/cancel` :
  - répond 202 avec `status: cancelling`, sans effet si on le répète ;
  - l'événement `end` (`cancelled`) arrive quand le calcul est **vraiment** arrêté ;
  - 409 `job_not_cancellable` si le travail est déjà terminé.
- `DELETE /aiodos/v1/jobs/{id}` : oublie un travail terminé ; ses fichiers suivent leur propre durée de garde.

**Préalable : la vraie annulation** (étape 5 ; **moyen**, au cœur de la file)
1. `jobs/executor.py` : un `threading.Event` par unité en cours, passé à `registry.run_local(task, payload, stop)` (l.45). `compute_image` l'écoute en plus de `pacing.stopping()`, et `imaging/generate._once` appelle `_cancel` chez sd-server.
2. `jobs/queue.cancel_job` lève ces `Event`, en plus de marquer les unités en file. Il pose `cancelled_by`.
3. `jobs/leases.renew` répond faux pour un travail annulé : la machine appairée s'arrête au renouvellement suivant, donc dans les 30 s.
4. `leases.take_back` et `network/work.result` refusent le résultat d'un travail annulé, et `assimilate_image` ne range rien.
5. Les images déjà finies d'un lot restent dans `files`. Un travail annulé dans la vue Travaux d'AIODOS se termine avec `cancelled_by: "aiodos"`.

La correction profite aussi au bouton « Annuler » d'AIODOS, qui n'arrête aujourd'hui que les unités en file.

### 4.10 Fichiers produits et liens signés (L2, `gateway/files_api.py`, `gateway/links.py`, `gateway/retention.py`)

- **`GET /aiodos/v1/files/{id}/content`**, avec la clé de l'appli qui a créé le fichier, ou par lien signé.
  - `Content-Type` exact (`image/png`, `video/webm`), `Content-Disposition: attachment`, `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`.
  - `Range` accepté, pour reprendre une vidéo de 512 Mo.
  - `?size=` donne une vignette.
- **`POST /aiodos/v1/files/{id}/link`**, corps `{"ttl": 900, "uses": 3}` : 3600 s et 5 usages au plus. Réponse : `{"url": "http://127.0.0.1:11435/aiodos/v1/files/{id}/content?exp=…&n=3&sig=…", "expires_at": …}`.
- **Signature**
  - `sig` = HMAC-SHA256(`gateway_link_secret`, `file_id|app_id|exp|n`), tronqué à 128 bits.
  - Le secret est dans les réglages, dans `SECRET_KEYS`, donc sous DPAPI.
  - Le lien est sans état : il survit à un redémarrage.
  - Les usages sont comptés en mémoire, par signature. Un redémarrage remet le compteur à zéro, sans jamais rendre valide un lien invalide.
  - Le lien tombe quand l'appli est retirée ou que le fichier expire (410). Les verrous Host et Origin s'appliquent aussi à lui.
- **Où vont les fichiers.**
  - Dans `data\gateway\out\<app_id>\`, **hors du dossier Images**, qui peut être sauvegardé par OneDrive : des photos d'acteurs retouchées y partiraient dans le cloud sans accord.
  - Ils sont visibles dans la galerie, sous le filtre « Créations des applications ».
  - **Le bouton « Garder »** les déplace dans `Images\AIODOS` (geste de l'utilisateur) et efface `expires_at`.
- **Durée de garde** : 7 jours par défaut (`gateway_out_days`, Q7). Un nettoyage tourne toutes les 10 minutes et ne touche jamais une création de l'utilisateur.
- **Les vidéos restent en WebM.** AIODOS n'a pas ffmpeg, et VP8 et VP9 sont libres de brevets. PANDORA convertit déjà en MP4 avec son ffmpeg (`api/h3_local._to_mp4`).
- **`DELETE /aiodos/v1/files/{id}`** : seulement pour les fichiers de l'appli.

### 4.11 Transcription (L3, `gateway/audio_api.py`)

**`POST /aiodos/v1/audio/transcriptions`**, en deux formes :
- **JSON**, pour porter l'amorce :

  ```json
  { "audio": "data:audio/wav;base64,…", "model": "whisper-1", "language": "fr",
    "prompt": "SkyPanel, Titan, contre, débouchage, Face 3…",
    "response_format": "verbose_json", "timestamp_granularities": ["segment"],
    "temperature": 0, "hotwords": null, "vad_filter": true }
  ```

- **Corps brut**, pour un gros fichier : `Content-Type: audio/wav|mpeg|ogg|flac|webm|mp4`, avec `?language=fr&response_format=srt`. Il n'y a pas d'amorce dans l'adresse : elle contient des noms de projet, et une adresse finit plus facilement dans un journal.

**Les champs**
- `model` : `whisper-1` est l'alias du modèle réglé dans AIODOS. On peut aussi donner un identifiant du catalogue.
- `prompt` devient `initial_prompt`, coupé à environ 224 jetons comme dans Whisper.
- `response_format` : `json` (par défaut), `text`, `srt`, `vtt` ou `verbose_json`.

**Réponses**, aux formes d'OpenAI :
- `json` : `{"text": "Mets la Face 3 à 40 %"}`, en texte nu, sans l'en-tête « [Transcription de …] » de `media.transcribe` ;
- `verbose_json` :

  ```json
  { "task": "transcribe", "language": "french", "duration": 3.42,
    "text": "Mets la Face 3 à 40 %",
    "segments": [ { "id": 0, "start": 0.0, "end": 2.6, "text": "Mets la Face 3 à 40 %",
                    "avg_logprob": -0.21, "no_speech_prob": 0.01, "temperature": 0.0 } ],
    "words": [ { "word": "Mets", "start": 0.0, "end": 0.3 } ],
    "aiodos": { "model": "small", "machine": "PC fixe", "compute_s": 0.9 } }
  ```

- `srt` et `vtt` : en `text/plain` (`gateway/subtitles.py`).

**Fonctionnement**
- **Toujours sur ce PC** : l'amorce contient les noms du projet.
- **Voie interactive** : un second programme, lancé à la demande, à priorité normale, sans la cadence du travail de fond.
  - Une transcription à la fois et 2 en attente au plus, sinon 429 `busy`.
  - Il se ferme après 120 s sans demande.
  - L'indexation garde le sien, en priorité basse.
- **`POST /aiodos/v1/audio/warmup`** lance ce programme sans rien transcrire, et répond 202 `{"state": "loading" | "ready"}`. ARKLIGHT l'appelle quand F8 est enfoncée.
- **Le moteur reste à choisir** (Q8) : faster-whisper, absent de l'installeur, ou whisper.cpp (`whisper-server`), un programme enfant comme llama.cpp et sd-server. `services/transcription.py` le cache derrière une seule façade : l'API ne change pas.
- **Aucun téléchargement.**
  - faster-whisper : `WhisperModel(…, local_files_only=True)` (**à vérifier**) ; whisper.cpp : fichier présent.
  - Modèle absent : 409 `model_not_installed`, avec la taille et la page `models`.
  - L'installation passe par « Installer un modèle », sur accord, avec la licence (MIT pour Whisper, **à vérifier**).
  - Le même changement corrige l'indexation, qui télécharge aujourd'hui sans accord là où faster-whisper est installé.
- **Long fichier** (la piste d'un clip de PANDORA) : `Prefer: respond-async` rend un travail `job_…` de genre `transcription`, en `local_only`, dont `result` est la même réponse.
- **Si l'appli se déconnecte** en mode synchrone, la transcription s'arrête.
- **Fichier reçu** : écrit dans `data\gateway\in\`, effacé à la fin, même en cas d'échec.

### 4.12 Bibliothèques et machines (L4)

**`GET /aiodos/v1/libraries`**
- Réponse : `[{"id": "lib_<16 caractères de sha256(id:created)>", "name": "Bible PANDORA", "entries": 42, "updated_at": …}]`.
- Seulement les bibliothèques choisies à l'accord, actives et non « gardées sur ce PC ».
- Une bibliothèque supprimée puis recréée sous le même numéro change d'identifiant.

**`POST /aiodos/v1/libraries/search`**
- Corps : `{"query": "…", "libraries": ["lib_…"] | null, "count": 8, "min_score": null}`. `count` vaut 20 au plus ; sans `min_score`, c'est `rag_min_score`.
- Réponse : `{"passages": [{"text", "source", "library", "score"}]}`.
- `source` ne donne que le nom du fichier. **À vérifier** : `entries.source` contient-il un chemin complet du disque ? Le MCP le renvoie tel quel aujourd'hui (`mcp_server._find`, l.333).
- Un seul filtre pour le MCP et cette route : `knowledge/exposed.py`, qui reprend `mcp_server._active_libraries` et y ajoute la liste de l'appli.

**`GET /aiodos/v1/machines`**
- Réponse : `{"here": {"name", "busy": "video" | null}, "mine": [{"name", "online", "chat_models", "images"}]}`.
- `friends` (nom d'affichage et état en ligne seulement) n'apparaît qu'avec le lieu `friends`.

### 4.13 Façades standard prévues plus tard

Chaque façade est un fichier mince, posé sur les modules ci-dessus. Elle n'est ajoutée qu'une fois sa référence revérifiée, et quand un outil en a besoin :

| Façade | Posée sur | Condition |
|---|---|---|
| `POST /v1/audio/transcriptions` (multipart) | `audio_api` | python-multipart accepté (licence à vérifier, Q9) |
| `POST /v1/images/edits` (multipart) | `creations_api` | idem ; masque et `image[]` multiples refusés |
| `/v1/videos` (création, état, contenu, suppression) | `creations_api`, `jobs_api` | forme revérifiée (noms, `seconds` en chaîne, `input_reference`, `variant`). Une annulation y paraît en `failed` avec `error.code: "cancelled"` |
| `/v1/vector_stores` (liste, recherche) | `libraries_api` | forme de la réponse de `search` revérifiée |

---

## 5. Les bibliothèques clientes

### 5.1 Règles communes

- **Licence et forme**
  - Apache-2.0, dans le dépôt d'AIODOS : `clients/python/aiodos_client/` et `clients/js/aiodos/`, avec `LICENSE` et `NOTICE` à côté.
  - Chaque fichier porte `SPDX-License-Identifier: Apache-2.0` et la version du client.
  - On les **copie** dans l'appli. Une appli GPL-3.0 peut les inclure ; l'inverse est interdit : aucune ligne de PANDORA ou d'ARKLIGHT n'y entre (`ThinkFilter` et `h3_local` sont réécrits, pas copiés).
- **Aucune dépendance** : la bibliothèque standard de Python 3.10 à 3.14 ; Node 20.3 et plus (`fetch`, `AbortSignal.any`, `FormData`, `Blob`, `node:crypto`), sans `Promise.withResolvers` ni `Array.fromAsync`.
- **Transport**
  - 127.0.0.1, localhost ou [::1] seulement, vérifié avant tout envoi.
  - Sans proxy, sans redirection suivie.
  - Preuve (§ 2.3) avant d'envoyer la clé.
- **Toute la surface dès L1.** Les fonctions de médias, de travaux, de transcription et de bibliothèques existent et lèvent `NotSupported` tant que `features` ne les annonce pas.
- **Conversation toujours en flux.** Le délai compte l'inactivité, remis à zéro à chaque morceau et à chaque `: aiodos loading`. Avant le premier morceau, `load_timeout` (900 s) s'applique si le modèle n'est pas chargé (`aiodos.loaded`).
- **Médias toujours en asynchrone** (`Prefer: respond-async`, puis on suit le travail), avec `Idempotency-Key` et `Accept-Language`.
- **La clé**
  - lue à chaque appel par une fonction de l'appli ;
  - jamais écrite dans un journal ni dans un message d'erreur ;
  - masquée par `repr` et `toString`.
- **Rien n'est jamais téléchargé ni installé.** Un modèle absent lève `ModelNotInstalled`, avec `open_in_aiodos("models", model)`.
- **Erreurs** : `AiodosError(code, status, message, hint, retryable, detail)`. Ce sont les codes du § 4.2, plus ceux du client. Les messages sont en français par défaut, en anglais avec `lang="en"`. `texts.py` et `texts.js` donnent les textes des états et des codes.
- **Journal** : le logger `aiodos_client` avec un `NullHandler` en Python, ou une fonction `log` facultative en JS. Jamais `print`.

### 5.2 Python : `aiodos_client` (pour PANDORA)

**Choix techniques**
- `http.client`, directement : pas de proxy lu dans l'environnement (le cas du VPN est réglé), et un accès direct à la socket. Pour annuler un flux, un fil de surveillance appelle `sock.shutdown()`.
- Imports en tête de module : PyInstaller les voit sans `hiddenimports`.
- `__version__` est une constante : pas d'`importlib.metadata`.
- Sans Qt, bloquant, sûr entre threads : pas d'état global modifiable. Les rappels sont appelés dans le thread de travail.

| Fichier | Rôle |
|---|---|
| `__init__.py` | `Aiodos`, `Keystore`, `Cancel`, erreurs, `__version__`, `API_VERSION` |
| `_transport.py` | `http.client`, vérification de l'hôte, en-têtes, JSON, lecture SSE, garde d'annulation |
| `_discovery.py` | `gateway.json`, registre (`winreg`), `pid` par `OpenProcess`, propriétaire du port, `launch()`, `open_page()` |
| `_proof.py` | preuve HMAC |
| `errors.py` | `AiodosError` et ses sous-classes |
| `texts.py` | textes des états et des codes, en français et en anglais |
| `status.py` | `status()` (§ 2.4), `wait_ready()` |
| `access.py` | `request_access()`, `AccessRequest.wait()`, `upgrade()`, `revoke()` |
| `keystore.py` | DPAPI par ctypes, adapté de `node/vault.py` (déjà Apache-2.0), avec une entropie `aiodos-client/<app_id>` ; hors de Windows, un fichier en 0600 |
| `models.py` | `Model`, `models(kind=)`, `fit_size()`, `frames_for()` : le même arrondi qu'AIODOS |
| `chat.py` | `chat()` (voir ci-dessous) |
| `creations.py` | `submit_image()`, `submit_video()`, et les raccourcis `image()`, `video()` |
| `jobs.py` | `Job`, `Progress`, `watch()`, `wait()`, `cancel()`, `download()` ; `Aiodos.job(id)` |
| `files.py` | `download()`, `link()`, `delete()` |
| `transcribe.py` | `transcribe()`, `warmup()`, `Transcript.srt()` et `.vtt()` |
| `libraries.py` | `libraries()`, `search()` |
| `fake.py` | `FakeAiodos` : un `http.server` sur 127.0.0.1:0 qui sert `clients/contract/`, et sait simuler un refus, un retrait, une vidéo qui avance, une annulation |

`chat()` accepte les blocs d'Anthropic et ceux d'OpenAI, met les images en data-URL et retire la réflexion. Avant l'envoi, il vérifie la vision et estime la longueur : c'est une estimation, AIODOS reste juge.

**Sous-classes d'erreurs**
- `NotRunning`, `GatewayClosed`, `NotAiodos`, `TooOld`, `Untrusted` ;
- `NeedsAccess`, `AccessDenied`, `AccessExpired`, `Revoked`, `ScopeDenied`, `WhereDenied`, `ProgramMismatch`, `OtherAccount` ;
- `ModelNotFound`, `ModelNotInstalled`, `LicenseNotAccepted`, `EngineNotInstalled`, `NoMachine` ;
- `ContextTooLong` (`.needed`, `.limit`), `ToolsUnavailable`, `VisionUnsupported`, `LocalOnly`, `Unsupported`, `InvalidSize`, `FileTooLarge` ;
- `RateLimited` (`.retry_after`), `Busy`, `EngineUnavailable`, `PeerFailed` ;
- `Timeout`, `Cancelled`, `JobFailed`, `FileExpired`, `NotSupported`.

**Exemple pour PANDORA** : une fonction bloquante, appelée depuis un QThread.

```python
from aiodos_client import Aiodos, Keystore, errors

ai = Aiodos(app="PANDORA", app_id="arkane.pandora", version=VERSION, lang="fr",
            keystore=Keystore.dpapi(r"%LOCALAPPDATA%\PANDORA\aiodos.key"))  # hors de config.json

st = ai.status()                                   # ne lève jamais
if st.state == "needs_access":
    req = ai.request_access(scopes=["chat", "images", "videos"], optional=["transcribe"],
                            where="mine",
                            purpose="Traduire les prompts, créer les moods et les animatiques.")
    self.code.emit(req.user_code)                  # « 482 197 », affiché en grand dans PANDORA
    req.wait(cancel=self.isInterruptionRequested, timeout=600)   # range la clé dans le coffre

model = ai.models(kind="chat").best(needs=["vision"])
r = ai.chat(messages=msgs, model=model.id, system=SYS,      # blocs Anthropic acceptés
            max_tokens=min(4096, model.context // 3), think=False,
            on_text=self.chunk.emit, cancel=self.isInterruptionRequested)
r.text, r.truncated, r.finish_reason, r.stopped_by, r.usage, r.model, r.machine, r.place

job = ai.submit_video(prompt=p, model="wan2.1-t2v-1.3b", width=832, height=480,
                      seconds=params["duration"], seed=params.get("seed"),
                      label=f"Animatique · {plan}")
for step in job.watch(cancel=lambda: self._cancelled or self.isInterruptionRequested()):
    self.progress.emit(step.percent or 0, step.message)
webm = job.download(dest_dir, stem=plan)[0]        # PANDORA convertit ensuite en MP4

res = ai.image("…", model="z-image-turbo", width=1920, height=1080, seed=7,
               on_progress=…, cancel=…)
res.files[0].link()                                # URL sans clé, pour image_call.subscribe
```

- `watch(cancel)` appelle `job.cancel()` dès que `cancel()` est vrai. Il attend l'événement `end` 35 s au plus, puis lève `Cancelled`.
- `batch=True` sur `chat()` envoie `X-Aiodos-Priority: background`.

### 5.3 JavaScript : `clients/js/aiodos/` (pour ARKLIGHT PC)

C'est un module ES pour Node 20.3 et plus. Il tourne dans le serveur Node ou dans le processus principal d'Electron, jamais dans le rendu (refusé, I6).

| Fichier | Rôle |
|---|---|
| `index.js` | `createClient`, `pickModel`, `AiodosError`, `texts` |
| `transport.js` | `fetch`, relu à chaque appel (les essais le remplacent) ; refus des hôtes non locaux ; `redirect: 'error'` ; délai d'inactivité ; `signal` |
| `discovery.js` | `gateway.json` (`fs/promises`), `reg query`, `process.kill(pid, 0)`, propriétaire du port (`netstat -ano`), lancement détaché sans shell |
| `proof.js` | preuve par `node:crypto` |
| `errors.js`, `texts.js` | codes, messages en français et en anglais |
| `status.js`, `access.js` | état ; demande d'accès, avec `onKey` |
| `models.js`, `pick.js` | liste au format qu'ARKLIGHT attend déjà (`id`, `size`, `caps`, `params`), plus `machine`, `context`, `loaded`, `here` |
| `neutral.js` | format neutre d'ARKLIGHT ↔ OpenAI |
| `sse.js` | lecture SSE (lignes `data:`, commentaires) |
| `chat.js` | `/v1/chat/completions` en flux |
| `creations.js`, `jobs.js`, `files.js`, `transcribe.js`, `libraries.js` | médias, travaux, fichiers, voix, recherche |
| `fake.js` | `createFakeFetch()` et `startFakeServer()` (`node:http`), qui servent `clients/contract/` |

```js
import { createClient, pickModel } from './aiodos/index.js';

export const aiodos = createClient({
  app: 'ARKLIGHT PC', appId: 'arkane.arklight', version: APP_VERSION,
  key: { get: () => keys.get('aiodos'), set: (k) => keys.set('aiodos', k) },  // KeyStore (safeStorage)
  lang: () => config.lang, timeout: 240_000, loadTimeout: 900_000,
});

const st = await aiodos.status();
if (st.state === 'needs_access') {
  const req = await aiodos.requestAccess({ scopes: ['chat'], optional: ['transcribe'], where: 'here',
                                           purpose: 'Assistant lumière et commande vocale' });
  broadcast({ type: 'ai.aiodos.code', code: req.userCode });
  await req.wait({ signal });
}
const model = pickModel(await aiodos.models({ use: 'chat' }), { needs: ['tools'], vision: true, preferLocal: true });
const r = await aiodos.chat({ model: model.id, system, messages, tools, think: false,
                              temperature: 0.2, maxTokens: 4096, where: 'here',
                              signal, onLoading, onText });
// { text, toolCalls: [{ id, name, args }], finish, stoppedBy, usage, answeredBy, machine }
await aiodos.warmup();                                     // quand F8 est enfoncée (L3)
const t = await aiodos.transcribe({ audio: wav, type: 'audio/wav', language: 'fr',
                                    prompt: VOCABULARY + names, signal });
```

**Ce qu'elle fait**
- **Conversion des messages** (`neutral.js`) :
  - `images: [dataURL]` devient `image_url` ;
  - `toolCalls` devient `tool_calls`, avec les arguments en texte ;
  - `role: 'tool'` devient `tool_call_id`.
- **Flux** : les morceaux de `tool_calls` sont rassemblés par `index`, et les arguments analysés en JSON. Si l'analyse échoue, `toolCallsInText` d'ARKLIGHT reste le filet.
- **Réflexion** : `think: false` envoie `reasoning_effort: "none"`.
- **Outils** : `requireTools: false` envoie `X-Aiodos-Tools: optional`. Par défaut, un 400 `tools_unavailable` remonte en `AiodosError`.
- **`where: 'here'`** envoie `X-Aiodos-Where: here`.
- **Erreurs** : `unreachable` garde « Connexion refusée », et un arrêt donne « Arrêté » : l'interface actuelle d'ARKLIGHT réagit sans changement.

### 5.4 Contrat partagé et faux serveurs

- **`clients/contract/*.json`** : des paires requête-réponse par route, y compris les flux d'outils, les erreurs, les travaux, la transcription et l'accès.
- Elles sont rejouées :
  - par `tests/test_gateway_contract.py`, qui vérifie que la passerelle répond bien cela ;
  - par `clients/python/tests/` (pytest) et `clients/js/test/*.test.mjs` (`node:test`), qui vérifient que les clients les comprennent.
- Les trois ne peuvent donc pas diverger sans qu'un essai casse.
- **Les faux serveurs** ne touchent ni au réseau, ni à la vraie configuration, ni au vrai fichier de découverte. Ils servent aux harnais de PANDORA (`tools/test_cinema.py`, `tools/test_live.py`) et à `test/ai.test.mjs` d'ARKLIGHT.

### 5.5 Recettes pour PANDORA (dans le guide)

- **Coffre.** La clé va dans `%LOCALAPPDATA%\PANDORA\aiodos.key`, sous DPAPI. `config.json` ne garde que `aiodos_url` et `aiodos_where`, puisque `save_config` remplace tout le fichier. Compléter `core/config._DEFAULTS` et `data/config.clean.json`.
- **Texte.**
  - `core/aiodos.py`, sans Qt : client, état, catalogue en cache, licences.
  - Un moteur « aiodos » branché tâche par tâche (`ai_task_engines`) : translate, enhance, video_prompt, vision, conversations.
  - `max_tokens` et la taille des tranches de `api/decoupage_queue` (`budget_chars`) plafonnés d'après `context`.
  - Toujours en flux, avec `think=False`.
  - Les lots (découpage) en `batch=True`.
- **Erreurs.** `humanize_ai_error` branché sur `exc.code`. Le conseil devient « Réglages → Applications et autres outils », et non « Clés API » ou « lancez Ollama ».
- **Images.**
  - `core/aiodos_image.py` : un `subscribe` à la forme de fal, qui rend des liens signés.
  - Une branche `aiodos:` dans `core/image_call` et dans `needs_fal`.
  - **Ne jamais téléverser les références chez fal** pour un moteur AIODOS (`apercu._upload_ref_robust`).
  - Les libellés finissent par « $0 ».
- **Vidéo.**
  - `api/aiodos_video.py`, modelé sur `H3LocalWorker` : WebM, puis `_to_mp4`.
  - Il rend `local_file` **et** `local_path`, pour `core/download.download_result` (piège 1 du rapport).
  - Une ligne dans chaque registre (Cinéma et Live).
  - 0 $ dans `core/pricing`.
  - Le nom du signal (`finished` ou `done`) est à trancher dans PANDORA, selon ce que connectent les onglets.
- **Réseau.** Toujours 127.0.0.1. Les chemins non ASCII sont lus en octets.

### 5.6 Recettes pour ARKLIGHT (dans le guide)

- **Avant tout** :
  - commiter `server/ai/`, que git ne suit pas aujourd'hui ;
  - réserver `ai.*` aux clients de la machine (`req.socket.remoteAddress` local), sinon un appareil du réseau se servirait d'AIODOS à travers ARKLIGHT.
- **Brancher le client**
  - Copier `clients/js/aiodos/` vers `server/ai/aiodos/`.
  - Écrire `server/ai/provider-aiodos.js`, d'une trentaine de lignes.
  - Dans `providers.js`, une entrée `aiodos: { label: 'AIODOS (ce PC)', kind: 'aiodos', local: true, key: false }`, puisque la clé s'obtient par le bouton. Une branche dans `chat()` et une dans `listModels()`.
- **`server/index.js`**
  - `aiState()` ajoute `aiodos: await aiodosStatus()` ;
  - un message `ai.aiodos.connect` ;
  - `voice.engine: 'whispercpp' | 'aiodos'` (L3).
- **`web/js/ui/assistant.js`** : une carte `aiodosBox()` (état, bouton, code), sans téléchargement, et « AIODOS » dans l'expression qui ouvre les réglages.
- **Divers** : textes dans `shared/i18n/en.js`, et un essai dans `test/ai.test.mjs` avec `createFakeFetch()`.

---

## 6. Dans AIODOS : fichiers, migrations, essais

### 6.1 Fichiers neufs, par livraison (un par fonctionnalité)

| Fichier | Rôle | Prix | Liv. |
|---|---|---|---|
| `aiodos/llm/context.py` | contexte choisi par modèle (global et exceptions), borné par le contexte d'entraînement, estimation de la mémoire vidéo | petit | L1 |
| `aiodos/gateway/errors.py` | codes, statuts, messages en français et en anglais, enveloppes OpenAI, Ollama et MCP ; reprend `server.refusal()` | petit | L1 |
| `aiodos/gateway/web_guard.py` | Host, Origin, Fetch Metadata, JSON exigé sur les routes publiques | petit | L1 |
| `aiodos/gateway/apps.py` | table, clés, cache, sortes de clés, `ensure_general`, purge des clés de session, signaux d'arrêt par appli | petit à moyen | L1 |
| `aiodos/gateway/access_flow.py` | `/access/requests`, `/token`, `/revoke`, demandes en mémoire, code, limites, sourdine, événement du bus | moyen | L1 |
| `aiodos/gateway/caller.py` | pid, programme et compte de l'appelant, programme qui tient le port (ctypes) | petit | L1 |
| `aiodos/gateway/scopes.py` | droits, `need()`, lieu effectif, priorité, liste des routes publiques | petit | L1 |
| `aiodos/gateway/limits.py` | taille du corps, seaux à jetons, simultanéité, garde-fou d'accaparement | petit | L1 |
| `aiodos/gateway/usage.py` | compteurs, journal, purge, résumés pour l'interface | petit | L1 |
| `aiodos/gateway/discovery.py` | `gateway.json` | petit | L1 |
| `aiodos/gateway/info.py` | `/aiodos/v1/info` et la preuve | petit | L1 |
| `aiodos/gateway/model_card.py` | fiches des modèles, communes à tous les catalogues | petit | L1 |
| `aiodos/gateway/models_api.py` | `/aiodos/v1/models` | petit | L1 |
| `aiodos/gateway/open_page.py` | `POST /aiodos/v1/open` | petit | L1 |
| `aiodos/api/routes/gateway_apps.py` | routes de l'interface, derrière le jeton : applis, droits, retrait, journal, clés à la main, demandes en attente, `approve`, `deny`, `block` | petit | L1 |
| `aiodos/ui/static/js/views/gateway-apps.js` | liste des applis, modification, journal, « Ajouter un outil » | petit | L1 |
| `aiodos/ui/static/js/views/gateway-request.js` | la carte d'accord et le bandeau ; rendu pur, exporté pour l'essai | petit | L1 |
| `clients/python/aiodos_client/*`, `clients/js/aiodos/*`, `clients/contract/*` | § 5 | moyen chacun | L1, puis s'étoffent |
| `tools/gateway_openapi.py` | écrit `docs/api-applications.openapi.json` à partir de l'appli FastAPI (`openapi_url` reste `None` en marche) | petit | L1 |
| `aiodos/gateway/uploads.py` | octets reçus : base64, data-URL, corps brut ; type reconnu par les premiers octets ; dimensions avant décodage ; `data\gateway\in\` ; nettoyage | petit | L2 |
| `aiodos/gateway/jobs_bridge.py` | création d'un travail pour une appli, attente, annulation à la déconnexion, objet travail | petit | L2 |
| `aiodos/gateway/creations_api.py` | `POST /aiodos/v1/creations` | petit | L2 |
| `aiodos/gateway/images_api.py` | `POST /v1/images/generations` | petit | L2 |
| `aiodos/gateway/progress.py` | état interne → `progress` (fonction pure) | petit | L2 |
| `aiodos/gateway/jobs_api.py`, `aiodos/gateway/job_events.py` | travaux ; SSE filtré | petits | L2 |
| `aiodos/gateway/files_api.py`, `aiodos/gateway/links.py`, `aiodos/gateway/retention.py` | fichiers ; liens signés ; durée de garde | petits | L2 |
| `aiodos/gateway/audio_api.py`, `aiodos/gateway/subtitles.py` | transcription ; SRT et VTT | petits | L3 |
| `aiodos/services/transcription.py` | façade : voies interactive et de fond, choix du moteur, présence du modèle | petit | L3 |
| `aiodos/services/whisper_interactive.py` | le second programme : lancement, fermeture après 120 s, `warmup` | petit | L3 |
| `aiodos/services/whisper_models.py` | catalogue Whisper : tailles, licence, présence locale | petit | L3 |
| `aiodos/knowledge/exposed.py` | filtre commun des bibliothèques montrées (MCP et REST) | petit | L4 |
| `aiodos/gateway/libraries_api.py`, `aiodos/gateway/machines_api.py` | REST | petits | L4 |

### 6.2 Fichiers retouchés

**L1**

| Fichier | Changement |
|---|---|
| `aiodos/llm/engine.py` | `ctx-size` lu de `llm/context.py` dans `presets`. Le routeur repart quand l'empreinte des préréglages change |
| `aiodos/settings.py` | `chat_context` (16384 si la mémoire vidéo le permet, sinon 8192), `chat_context_overrides`, `gateway_apps`, `gateway_requests`, `gateway_same_account`, `gateway_log_days` ; `gateway_key` et `gateway_bridge_key` dans `SECRET_KEYS` |
| `aiodos/gateway/server.py` | `Guard` : `web_guard`, routes publiques, appli dans `scope["state"]`, compte et programme, limites, journal (enveloppe de `send`). `create_gateway_app` monte les routeurs neufs. `Gateway.apply` et l'arrêt appellent `discovery`. Plus de création d'office de la clé générale |
| `aiodos/gateway/access.py` | la clé générale lue par `settings` (DPAPI) ; sa ligne `general` |
| `aiodos/gateway/answer.py`, `aiodos/network/placement.py` | candidats filtrés par `where` ; `tools_unavailable` ; `_occupy(…, interactive=)` |
| `aiodos/gateway/catalog.py` | `peer:` et `reparti:` contrôlés selon `where` ; entrées par `model_card` |
| `aiodos/gateway/openai_api.py` | `/v1/models` enrichi et filtré ; `: aiodos loading` ; `X-Aiodos-Machine` et `X-Aiodos-Place` ; `max_completion_tokens` ; `stopped_by` ; codes |
| `aiodos/gateway/engine.py` (branche) | arrêt sur déconnexion sans flux ; `context_length_exceeded` avec les nombres |
| `aiodos/gateway/ollama_api.py` | droits, `code`, en-têtes, `keep_alive` et chargement à vide ignorés pour une clé d'appli |
| `aiodos/gateway/mcp_server.py` | outils filtrés selon les droits et les bibliothèques |
| `aiodos/db.py` | migration 18 |
| `aiodos/__main__.py`, `aiodos/instance.py`, `aiodos/api/routes/system.py`, `aiodos/ui/static/js/app.js` | `--open <page>`, `wake_running(page)`, `/instance/show` avec `page` |
| `aiodos/api/server.py`, `aiodos/api/routes/gateway.py` | monter `gateway_apps` ; interrupteurs |
| `aiodos/ui/static/js/views/gateway.js`, `settings.js` | la carte devient « Applications et autres outils » ; trois interrupteurs ; clé générale décrite comme un outil parmi d'autres |
| `aiodos/ui/static/js/views/models.js` | contexte global et par modèle, avec la mémoire vidéo estimée |
| `aiodos/ui/tray.py` | bulle « PANDORA demande l'accès à AIODOS » |

**L2**

| Fichier | Changement |
|---|---|
| `aiodos/jobs/executor.py` | un `Event` par unité, passé à `run_local` (l.45) |
| `aiodos/jobs/queue.py` | `create_job(…, owner_app, public_id, label, reach, idempotency_key)` ; `cancel_job` lève les `Event` et pose `cancelled_by` |
| `aiodos/jobs/kinds/images.py` | `compute_image(stop)` ; image de départ lue seulement depuis `data\gateway\in` ; sortie dans `data\gateway\out\<appli>` |
| `aiodos/imaging/generate.py` | `_once` regarde `stop` et appelle `_cancel` |
| `aiodos/jobs/leases.py` | `claim_next` respecte `units.reach` ; `renew` faux et `take_back` refusé pour un travail annulé |
| `aiodos/network/work.py` | `acceptable()` refuse à un ami une unité `reach = 'mine'`, sans changer le protocole ; `result` refuse un travail annulé |
| `aiodos/imaging/gallery.py` | `public_id`, `owner_app`, `expires_at`, filtre, « Garder » |
| `aiodos/db.py` | migration 19 |
| `aiodos/api/routes/jobs.py`, `aiodos/ui/…/jobs.js`, `gallery.js` | « pour PANDORA », `label`, `cancelled_by` ; filtre et bouton « Garder » |
| `aiodos/settings.py` | `gateway_link_secret` (dans `SECRET_KEYS`), `gateway_out_days` |
| `requirements.txt` | Pillow listé explicitement |

**L3**

| Fichier | Changement |
|---|---|
| `aiodos/services/whisper_child.py` | `language`, `initial_prompt`, `hotwords`, `task`, `word_timestamps`, segments horodatés, `local_files_only=True`. Ajouts compatibles au protocole JSON |
| `aiodos/services/media.py` | passe par `services/transcription.py` ; l'indexation garde son texte d'aujourd'hui |
| `aiodos/api/routes/model_install.py` et son catalogue | Whisper devient un modèle à installer sur accord, avec sa licence |

Si whisper.cpp est choisi, s'y ajoute `aiodos/services/whisper_server.py` (programme enfant), et son archive est épinglée comme celles de llama.cpp et de stable-diffusion.cpp.

**L4**

| Fichier | Changement |
|---|---|
| `aiodos/gateway/mcp_server.py` | passe par `exposed` |
| `aiodos/imaging/generate.video_body`, `aiodos/imaging/catalog.py`, `aiodos/jobs/kinds/images.normalize` | `init_image` et `end_image` pour H3, et les capacités du catalogue |

### 6.3 Migrations

Les numéros 18 et 19 sont libres sur toutes les branches : il faut les réserver d'ici là.

**Compatibilité vérifiée** (`db._ensure_schema`) : une version précédente qui rouvre la base ne plante pas et ne baisse pas le numéro. Les migrations ne font qu'ajouter, et `_replayable` saute les colonnes déjà présentes.

**Migration 18 (L1)**

```sql
CREATE TABLE IF NOT EXISTS gateway_apps (
    id           TEXT PRIMARY KEY,                  -- 8 caractères, dans la clé
    kind         TEXT NOT NULL DEFAULT 'app',       -- app | manual | general | bridge
    client_id    TEXT, name TEXT NOT NULL, version TEXT,
    key_hash     TEXT NOT NULL UNIQUE, key_hint TEXT,
    scopes       TEXT NOT NULL,                     -- JSON
    libraries    TEXT NOT NULL DEFAULT '[]',        -- JSON [[id, created], …]
    reach        TEXT NOT NULL DEFAULT 'mine',      -- here | mine | friends
    priority     TEXT NOT NULL DEFAULT 'interactive',
    limits       TEXT NOT NULL DEFAULT '{}',
    exe_path     TEXT, exe_pinned INTEGER NOT NULL DEFAULT 0,
    account_check INTEGER NOT NULL DEFAULT 1,
    created      REAL NOT NULL, last_used REAL,
    expires      REAL, session_only INTEGER NOT NULL DEFAULT 0, revoked REAL
);
CREATE TABLE IF NOT EXISTS gateway_blocked (exe_path TEXT PRIMARY KEY, name TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS gateway_usage (
    app_id TEXT NOT NULL, day TEXT NOT NULL, family TEXT NOT NULL,
    machine TEXT NOT NULL, place TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    items INTEGER NOT NULL DEFAULT 0, video_seconds REAL NOT NULL DEFAULT 0,
    audio_seconds REAL NOT NULL DEFAULT 0, compute_s REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, day, family, machine, place)
);
CREATE TABLE IF NOT EXISTS gateway_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, app_id TEXT,
    route TEXT NOT NULL, model TEXT, machine TEXT, place TEXT,
    status INTEGER NOT NULL, code TEXT, ms INTEGER, request_id TEXT,
    tokens_in INTEGER, tokens_out INTEGER, items INTEGER,
    bytes_in INTEGER, bytes_out INTEGER, program TEXT
);
CREATE INDEX IF NOT EXISTS ix_gateway_log_app ON gateway_log(app_id, at);
```

La ligne de la clé générale est créée **en Python** (`apps.ensure_general`), parce que SQLite n'a pas de sha256.

**Migration 19 (L2)**

SQLite refuse `UNIQUE` dans un `ADD COLUMN` : les index uniques se font à part.

```sql
ALTER TABLE jobs ADD COLUMN public_id TEXT;
ALTER TABLE jobs ADD COLUMN owner_app TEXT;
ALTER TABLE jobs ADD COLUMN label TEXT;
ALTER TABLE jobs ADD COLUMN reach TEXT;               -- NULL : les règles d'aujourd'hui
ALTER TABLE jobs ADD COLUMN idempotency_key TEXT;
ALTER TABLE jobs ADD COLUMN cancelled_by TEXT;
ALTER TABLE units ADD COLUMN reach TEXT;              -- copié du travail ; 'mine' : jamais chez un ami
ALTER TABLE images ADD COLUMN public_id TEXT;
ALTER TABLE images ADD COLUMN owner_app TEXT;
ALTER TABLE images ADD COLUMN expires_at REAL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_public ON jobs(public_id) WHERE public_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_idem ON jobs(owner_app, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_jobs_owner_app ON jobs(owner_app, created);
CREATE UNIQUE INDEX IF NOT EXISTS ux_images_public ON images(public_id) WHERE public_id IS NOT NULL;
```

Les numéros internes, des `INTEGER PRIMARY KEY` sans AUTOINCREMENT, ne sont plus jamais montrés aux applis. Le problème des numéros réutilisés disparaît sans reconstruire les tables.

### 6.4 Essais fonctionnels

**L1**
- `tests/test_llm_context.py` : contexte global et par modèle, borné ; le préréglage change ; le routeur repart.
- `tests/test_gateway_apps.py` :
  - seul le haché est gardé, et la clé n'est remise qu'une fois ;
  - un retrait est immédiat (401 `key_revoked`, flux coupé) ;
  - une modification est immédiate ;
  - expiration ; clés de session purgées au démarrage ;
  - clé générale inchangée dans ses droits et protégée par DPAPI sous Windows ;
  - clé à la main ; clé du pont MCP.
- `tests/test_gateway_access_flow.py` :
  - la suite `authorization_pending`, accord, clé relevée une seule fois, puis `invalid_grant` ;
  - `access_denied`, `expired_token`, `slow_down` ;
  - 3 demandes en attente au plus ; sourdine ; noms réservés ; programme bloqué ;
  - autre compte, avec `caller` simulé ;
  - droits en plus avec la même clé ; remplacement d'un accès existant ;
  - droits accordés ⊆ droits demandés.
- `tests/test_gateway_scopes.py` : méta-essai (chaque route montée a un droit ou est publique) ; matrice route × droit en 403 `scope_denied` ; `/v1/models` et `tools/list` filtrés.
- `tests/test_gateway_where.py` :
  - `here` donne `local_only = 1` ;
  - avec `mine`, aucune unité n'est louée par un ami (bail d'un ami simulé), et `peer:<ami>` donne 403 ;
  - un en-tête plus large que le plafond donne 403 ;
  - outils avec un modèle seulement chez une machine appairée : 400 `tools_unavailable`, ou réponse sans outils avec `X-Aiodos-Tools: optional`.
- `tests/test_gateway_priority.py` : `background` n'appelle pas `interactive_begin` ; garde-fou d'accaparement.
- `tests/test_gateway_limits.py` : 413 avant d'avoir tout lu ; 429 avec `Retry-After` ; simultanéité.
- `tests/test_gateway_errors.py` : un code stable par cas ; `Accept-Language` ; `code` côté Ollama ; `context_length_exceeded` avec les nombres ; `stopped_by: "context"`.
- `tests/test_gateway_models_ext.py` : `context_length` réel, `architecture`, `supported_parameters`, objet `aiodos` ; `/aiodos/v1/models` filtré ; licence de H3.
- `tests/test_gateway_discovery.py` : `open`, `closed`, `error`, `running: false` ; jamais de secret dans le fichier (recherche des préfixes de clé et du jeton) ; écriture atomique ; `AIODOS_DISCOVERY_FILE` respecté ; jamais écrit par un nœud d'essai.
- `tests/test_gateway_proof.py` : preuve juste ; fausse si le haché diffère ; aléatoire et différente à chaque appel pour un `id` inconnu.
- `tests/test_gateway_open.py` : `--open` et `wake_running(page)` ; limite de `POST /aiodos/v1/open`.
- `tests/test_gateway_contract.py` : la passerelle répond comme `clients/contract/`.
- `tests/test_gateway_openapi.py` : `docs/api-applications.openapi.json` est à jour, sinon l'essai échoue.
- **Avec le SDK officiel** (openai 3.33.0, dans `requirements-dev.txt`) : `client.models.list()`, `client.chat.completions.create(stream=True, tools=…)`, et les erreurs levées selon le statut.
- Essais existants à étendre : `tests/test_gateway.py`, `test_gateway_nodes.py`, `test_gateway_integrated.py`. La clé générale doit marcher comme avant.

**L2**
- `tests/test_jobs_cancel.py` :
  - une unité en cours ici reçoit `stop`, `_cancel` est appelé chez le faux sd-server, et rien n'entre dans la galerie ;
  - chez une machine appairée, `renew` répond faux et `take_back` refuse le résultat.
  - `tests/test_jobs_queue.py` et `tests/test_images_e2e.py` passent toujours.
- `tests/test_gateway_creations.py` : `fit: round` et `adjusted` ; `exact` ; `init_image` converti ; 409 pour un modèle absent ou une licence ; `no_machine` ; `Idempotency-Key`.
- `tests/test_gateway_images_api.py` (SDK `client.images.generate`) : b64 et url ; taille réelle ; `Prefer` donne 202 et `Location` ; une déconnexion annule.
- `tests/test_gateway_jobs.py` : étapes et pourcentage ; SSE (état complet d'abord, puis `end`) ; reprise après `recover()`.
- `tests/test_gateway_files.py` : clé, `Range`, liens (valide, expiré, épuisé, falsifié, détourné, appli retirée, plus de 3600 s refusé), garde et « Garder ».

**L3**
- `tests/test_gateway_audio.py` : `whisper_child` simulé ; langue, amorce et horodatage transmis ; les 5 formats ; alias `whisper-1` ; voie interactive pendant une indexation ; `warmup` ; 409 sans téléchargement.
- `tests/test_whisper_child.py`, étendu.

**L4**
- `tests/test_gateway_libraries.py` : liste de l'appli ; paires (id, date de création) ; bibliothèques « gardées sur ce PC » exclues ; `source` sans chemin.
- `tests/test_gateway_machines.py` : amis absents avec `mine`.

**Clients** : `clients/python/tests/` et `clients/js/test/` :
- rejouent le contrat ;
- refusent les hôtes non locaux et ne suivent pas les redirections ;
- ne laissent jamais la clé dans `repr` ni dans les messages ;
- exercent l'état `untrusted`, l'annulation réelle d'un flux et `NotSupported` ;
- plus un essai de bout en bout de chaque client contre une passerelle d'essai.

### 6.5 Essais de sécurité (les invariants)

| Invariant | Essai |
|---|---|
| I1 | `tests/test_gateway_bind.py` : l'adresse d'écoute est 127.0.0.1 |
| I2 | `tests/test_gateway_apps.py` : interrupteurs faux par défaut ; aucune route de la passerelle ne les change |
| I3 | `tests/test_gateway_no_cloud.py` : avec des clés cloud réglées, aucune route n'atteint un fournisseur cloud (catalogue, `cloud:`, `local:cloud:`, `converser` par défaut) |
| I4 | `tests/test_gateway_where.py` |
| I5 | `tests/test_gateway_no_download.py` : `download.start`, `model_install.start`, `WhisperModel` sans `local_files_only` et `snapshot_download` sont remplacés par des fonctions qui font échouer l'essai si la passerelle les appelle ; `keep_alive: 0` est sans effet pour une clé d'appli |
| I6 | `tests/test_gateway_web_attacks.py` (voir ci-dessous) |
| I7 | `tests/test_gateway_files.py` : `init_path`, `../`, `%2e%2e`, noms Windows réservés refusés |
| I8 | `tests/test_gateway_log.py` : un mot témoin dans la consigne, l'image et l'amorce ne se trouve ni dans `gateway_log`, ni dans `gateway_usage`, ni dans les octets du fichier de base, ni dans `aiodos.log` ; pas de clé dans les journaux (`caplog`) |
| I9 | `tests/test_gateway_nodes.py`, étendu : la charge envoyée à une machine appairée ne contient ni nom d'appli ni clé |
| I10 | `tests/test_gateway_jobs.py` et `tests/test_gateway_files.py` : 404 avec la clé d'une autre appli ; aucun numéro interne exposé ; le SSE ne laisse passer que ce travail |
| I11 | `tests/test_gateway_access_flow.py` : aucune route de la passerelle n'accorde |
| I12 | `tests/test_gateway_where.py` et `tests/test_gateway_priority.py` |

Détail de `tests/test_gateway_web_attacks.py` :
- Host forgé ;
- `Origin: null` ou une page localhost ;
- `Sec-Fetch-Site: cross-site` ou `same-site`, `Sec-Fetch-Dest: image` ou `document` : 403 ;
- **le `fetch` de Node** (`sec-fetch-mode: cors` seul), `http.client`, `requests` et les SDK d'OpenAI passent ;
- requête « simple » en `text/plain` : 415 ;
- clé dans l'adresse ignorée ; WebSocket refusé.

S'y ajoutent :
- **`tests/test_gateway_caller.py`** (Windows seulement) : une connexion à soi-même donne `pid == os.getpid()` et le bon programme ; un autre compte, simulé, est refusé ; un programme différent est noté, ou refusé si la clé est liée.
- **L'essai d'échappement** (Node, ignoré s'il est absent, comme `test_ui_syntax.py`) : `gateway-request.js` affiche échappé un nom comme `<img src=x onerror=…>`, et retire les caractères de contrôle et de sens d'écriture.
- **Les bombes** : un PNG qui annonce 50 000 × 50 000 est refusé avant décodage ; un base64 trop gros donne 413 ; un audio trop long donne 413 ; un exécutable renommé en `.png` est refusé.

---

## 7. La documentation pour les intégrateurs

| Fichier | Contenu |
|---|---|
| `docs/integrer-aiodos.md` (neuf, le principal) | le guide, plan ci-dessous |
| `docs/api-applications.md` (neuf) | la référence, route par route : champs pris, ignorés ou refusés ; réponses ; erreurs ; en-têtes ; un exemple `curl` ou PowerShell par route |
| `docs/api-applications.openapi.json` (neuf, produit) | `/aiodos/v1/*` et les extensions des routes `/v1`, avec un renvoi à la référence d'OpenAI pour les routes standard |
| `docs/autres-outils.md` (retouché) | la clé générale, les clés à la main, la carte « Applications et autres outils », les limites mises à jour (contexte, outils) |
| `clients/python/README.md`, `clients/js/README.md` | copie dans une appli, liste des fichiers, licence, exemples, harnais d'essai |

**Plan de `docs/integrer-aiodos.md`**, en français, avec un résumé en anglais en tête :
1. **Les garanties** : les invariants I1 à I12, et franchement, ce qu'AIODOS ne protège pas (un programme malveillant du même compte).
2. **En 5 minutes** :
   - `curl` ;
   - le SDK d'OpenAI en Python (`OpenAI(base_url="http://127.0.0.1:11435/v1", api_key=…, default_headers={"X-Aiodos-Where": "here"})`, `extra_body={"seed": 7}`) ;
   - le SDK en JS ;
   - les deux clients.
3. **Trouver AIODOS** : adresse, `gateway.json`, états et messages conseillés (§ 2.4), lancement, `--open`.
4. **Obtenir l'accès** :
   - le code à afficher en grand ;
   - demander au moment où l'utilisateur active la fonction, pas au premier lancement ;
   - droits en plus ; se déconnecter.
5. **Garder la clé** :
   - DPAPI ou `safeStorage` ;
   - jamais dans Documents ni OneDrive. ARKLIGHT écrit en clair (`raw:`) dans `Documents\ARKLIGHT` quand il est lancé par `node` seul ;
   - jamais dans un journal ni dans une adresse.
6. **Droits, lieu, priorité**, et le tableau « ce qui part où » (§ 3.7).
7. **Conversation** :
   - choisir un modèle d'après `context_length` et ses capacités ;
   - plafonner `max_tokens` et les tranches ;
   - outils, images, réflexion (`reasoning_effort: "none"`) ;
   - toujours en flux ; les lots en `background`.
8. **Créations et travaux** :
   - tailles et arrondis (`fit`, `adjusted`), durées ;
   - avancement estimé, annulation, reprise après redémarrage ;
   - `Idempotency-Key` ;
   - fichiers et liens signés ; WebM vers MP4.
9. **Transcription** : amorce, langue, horodatages, `warmup`.
10. **Bibliothèques** : les passages sont des données, pas des consignes.
11. **Erreurs** : la table des codes, ce que fait l'appli, et les textes conseillés en français et en anglais.
12. **Essayer sans AIODOS** : `FakeAiodos`, `fake.js`, le contrat.
13. **Licences** :
    - client Apache-2.0 dans un projet GPL-3.0 : permis, en gardant `LICENSE`, `NOTICE` et les en-têtes, mais pas l'inverse ;
    - licences des modèles : afficher `license.notice_*`, et faire accepter dans AIODOS.
14. **Versions** : lire `features`, ne jamais comparer de numéros.
15. **Liste de contrôle de l'intégrateur** :
    - 127.0.0.1, sans proxy, sans redirection ;
    - droits minimaux, et `here` pour les données sensibles (amorce, photos d'acteurs) ;
    - afficher qui a calculé et les licences restrictives ;
    - **ne pas devenir un relais ouvert vers le réseau local** ;
    - dire ce qui part vers le cloud de l'appli ;
    - annuler ce qu'on abandonne ;
    - sur `key_revoked`, une seule nouvelle demande, après un clic, jamais en boucle.
16. **Recettes PANDORA et ARKLIGHT** (§ 5.5 et § 5.6).

---

## 8. Risques et parades

### 8.1 Le tableau

| Risque | Parade | Ce qui reste |
|---|---|---|
| Une page web appelle la passerelle (CSRF, POST « simple ») | I6 : Origin, Fetch Metadata fin, JSON exigé, pas de CORS, clé jamais dans l'adresse | une balise `<img>` vers « / » peut deviner qu'AIODOS tourne : gêne faible |
| Rebinding DNS | Host exact (existe) | — |
| Un autre compte Windows du PC | compte vérifié à chaque requête et à chaque demande ; DPAPI ; preuve et propriétaire du port côté client | Docker et la clé générale : noté seulement en v1 |
| Un faux AIODOS sur le port (AIODOS arrêté) récolte les clés, ou en « accorde » | preuve HMAC ; le client vérifie qui tient le port ; preuve refaite (§ 2.3) ; AIODOS nomme qui tient son port | une fenêtre de 60 s au plus entre deux preuves ; un programme du même compte est hors de portée |
| Un faux « PANDORA » demande l'accès | code à 6 chiffres comparé ; programme et compte affichés ; « nom non vérifié » ; bouton actif après 1,5 s ; une carte à la fois ; sourdine ; blocage | un faux `PANDORA.exe` passe tant qu'Authenticode n'est pas vérifié |
| Une clé fuit | préfixe repérable ; montrée une fois ; un accès et ses droits seulement ; « programme inattendu » ; liaison au programme en option ; retrait immédiat | qui détient la clé « est » l'appli tant qu'elle n'est pas retirée |
| Un programme malveillant du même compte | rendu visible (programme, journal) ; dit dans la doc ; l'approbation sur le téléphone (v2) relèvera le seuil | hors de portée |
| Calcul chez un ami sans accord | `mine` par défaut ; « amis » jamais coché d'avance et confirmé deux fois ; minimum avec les réglages généraux ; `units.reach` ; 403 explicite ; compté et affiché | — |
| Données envoyées dans le cloud | aucune route vers le cloud (I3) ; liste de bibliothèques choisie ; créations hors du dossier Images | l'appli peut envoyer elle-même ce qu'elle reçoit : dit sur la carte |
| Lecture de fichiers arbitraires | des octets seulement, écrits dans `data\gateway\in\` (I7) | — |
| Suivre le travail d'un autre | `public_id` aléatoire, `owner_app`, 404 (I10) | — |
| Fuite par le bus d'événements | SSE filtré par travail et par appli | — |
| Bombes (PNG géant, audio interminable, corps énorme) | tailles bornées, dimensions lues avant décodage, durée de l'audio bornée, délais | — |
| Une appli accapare la carte graphique | limites, priorité `background`, garde-fou d'accaparement, quotas, annulation au retrait | des rechargements en alternance si deux applis utilisent deux modèles de conversation (`MODELS_MAX` 2) : documenté |
| Une appli décharge le modèle d'une autre, ou change le contexte | ignoré pour les clés d'appli (I5) | — |
| Fuite d'un lien signé | 60 min et 5 usages au plus ; un seul fichier ; 127.0.0.1 ; tombe au retrait ; `no-store` | sur la machine, qui détient le lien lit le fichier pendant sa durée de vie |
| Injection dans l'interface par le nom ou la raison | nettoyage, échappement, longueurs, essai dédié | — |
| Injection de consignes par les passages ou par MCP | doc : les passages sont des données ; `source` séparé du texte | dépend de l'appli |
| `gateway.json` falsifié | adresse locale seulement ; rien n'est exécuté depuis ce fichier ; preuve ; lancement par le registre ou un chemin fixe, sur un clic | même compte : hors de portée |
| Licence restrictive contournée | `license_not_accepted` ; l'acceptation se fait seulement dans AIODOS | — |
| Téléchargement sans accord (Whisper aujourd'hui) | `local_files_only`, installation sur accord, essai « aucun téléchargement » | — |
| sd-server sans clé sur un port local tiré au hasard (`imaging/engine.py`) | **à vérifier** : sd-server accepte-t-il une clé (`--api-key`) ? Si oui, la lui donner comme au routeur | sinon, un programme qui trouve le port peut faire calculer des images : gênant seulement sur un PC à plusieurs comptes |
| ARKLIGHT relaie AIODOS vers le réseau local (`/ws` sans authentification) | recette « réserver `ai.*` aux clients locaux » avant de brancher ; lieu `here` et droits limités pour ARKLIGHT | à corriger dans ARKLIGHT |
| Retour à une version précédente d'AIODOS | migrations par ajout (vérifié) ; secrets protégés une fois la version saine (`protect_secrets`) | une version d'avant ne connaît que la clé générale |

### 8.2 Ce qui n'a pas été vérifié

**Mécanismes Windows**
- Le coût et la fiabilité de `GetExtendedTcpTable` à chaque requête.
- Comment uvicorn identifie une connexion, s'il faut un cache.
- Le programme vu derrière `host.docker.internal`.
- `netstat -ano` côté Node.

**Moteurs**
- Les nombres dans l'erreur « exceeds the available context » du routeur.
- La mémoire vidéo d'un contexte de 16 k ou 32 k sur la carte de 8 Go, et l'effet d'un cache KV en q8_0.
- Les requêtes simultanées du routeur, et son cache KV partagé.
- Les pas dans le journal de sd-server ; `--api-key` de sd-server ; le décodage d'un JPEG par sd-server (contourné par la conversion en PNG).
- `local_files_only` de faster-whisper, et l'état du cache Hugging Face sur la machine.
- Les options de whisper.cpp (`whisper-server` : `--prompt`, `--language`, sortie horodatée, carte graphique) et sa licence.

**Dépôt d'AIODOS**
- `queue.not_here` comme source de la raison de `waiting_machine`.
- `entries.source` : un chemin complet du disque ?
- Les participants des gros modèles répartis (`network/cluster`).
- `access._stored`, qui lit la valeur brute.
- Comment Pillow arrive dans l'installeur.
- La commande du raccourci du menu Démarrer.

**Ailleurs**
- Les références d'OpenAI pour `/v1/videos` et `/v1/vector_stores`.
- La licence de python-multipart.
- Si le dossier Images est sous OneDrive sur les machines de l'auteur.

---

## 9. Réalisation et questions

### 9.1 Les étapes, dans l'ordre

| # | Étape | Contenu | Prix | Ce que l'auteur peut faire ensuite |
|---|---|---|---|---|
| 0 | **Préalable** | Pousser `distracted-satoshi`. Fusionner `upbeat-chaum`, puis `distracted-satoshi`. Repasser `tests/test_gateway*.py` | moyen | — |
| 1 | **Contexte et corrections immédiates** (L1) | `llm/context.py`, préréglages, choix dans l'interface, **mesure de la mémoire vidéo** à 16 k et 32 k ; `gateway/errors.py` et les codes stables ; `X-Aiodos-Model`, `X-Aiodos-Machine` et `X-Aiodos-Place` aussi sur `/api` ; `: aiodos loading` ; arrêt sur déconnexion sans flux ; `context_length_exceeded` avec les nombres ; `stopped_by` ; `X-Aiodos-Warning: tools-dropped` | moyen | Dès maintenant, avec la clé générale : ARKLIGHT par son fournisseur « custom », avec 16 k ; PANDORA par « Autre serveur OpenAI-compatible » sur les tâches courtes |
| 2 | **Accès par appli** (L1, le plus gros morceau) | Migration 18 ; `apps`, `access_flow`, `caller`, `scopes`, `web_guard`, `limits`, `usage` ; carte d'accord et liste des applis ; interrupteurs ; clé générale sous DPAPI ; clés à la main ; clé du pont ; lieu et priorité dans `answer`, `placement` et `catalog` ; `tools_unavailable` ; MCP filtré ; `keep_alive` ignoré ; garde-fou ; essais des invariants | moyen | — |
| 3 | **Découverte et catalogue** (L1) | `discovery`, `info` et la preuve ; `--open` et `POST /aiodos/v1/open` ; `model_card`, `/v1/models` enrichi, `/aiodos/v1/models` (texte) | petit à moyen | — |
| 4 | **Clients et guide** (L1) | Python et JS avec toute leur surface ; contrat ; faux serveurs ; `docs/integrer-aiodos.md` ; référence et OpenAPI | moyen | **Les intégrations texte.** ARKLIGHT : copier le client, `provider-aiodos.js`, `aiodosBox()` ; l'assistant marche en entier (outils, vision, 16 k). PANDORA : `core/aiodos.py`, le moteur « aiodos » pour translate, enhance, video_prompt, vision et les conversations, et les plafonds d'après `context` |
| 5 | **Vraie annulation et numéros publics** (L2) | Migration 19 ; `Event` par unité ; `cancel_job`, `renew`, `take_back`, `work.result` ; `units.reach` ; « pour PANDORA » et `cancelled_by` dans la vue Travaux | moyen, au cœur de la file | « Annuler » marche enfin, aussi dans AIODOS |
| 6 | **Images** (L2) | `uploads`, `jobs_bridge`, `creations_api` (images), `images_api`, `progress`, `jobs_api`, `job_events`, `files_api`, `links`, `retention` ; filtre et « Garder » dans la galerie ; les fonctions d'images des clients sont annoncées | moyen | PANDORA : `core/aiodos_image.py` et la branche `aiodos:` dans `image_call`, sans téléversement chez fal. Moods sans référence, Studio Images |
| 7 | **Vidéos Wan 2.1** (L2) | `creations_api` (vidéo), arrondi des images, avancement estimé | petit | PANDORA : `api/aiodos_video.py` et une ligne dans chaque registre ; animatiques en 480p |
| 8 | **Choix du moteur Whisper** (L3) | Étude courte : whisper.cpp ou faster-whisper ; licences ; options ; installation sur accord par « Installer un modèle » ; `local_files_only`, qui corrige aussi l'indexation | petit | — |
| 9 | **Transcription** (L3) | `audio_api`, `subtitles`, `services/transcription`, `whisper_interactive`, `warmup`, travail asynchrone pour les longs fichiers ; clients | moyen | ARKLIGHT : la voix par AIODOS en option (`voice.engine`), avec `warmup`. PANDORA : dictée, sous-titres SRT, vérification des répliques |
| 10 | **Compléments** (L4) | Bibliothèques en REST (`exposed`, liste par appli) ; `/aiodos/v1/machines` ; image de départ et de fin pour H3 (`video_body`, catalogue, licence) ; un délai de déchargement plus long quand une appli s'en sert ; les vrais pas de sd-server, s'ils existent | petits, H3 moyen | PANDORA : passages du scénario et de la bible ; storyboard en R2V par H3, si sa licence est acceptée |
| 11 | **Plus tard** | Façades OpenAI (§ 4.13) ; approbation sur le téléphone ; Authenticode ; ligne de commande des interpréteurs ; écriture dans une bibliothèque de l'appli ; limites par appli ; outils et images vers mes machines (**gros**) | — | — |

### 9.2 Questions à l'auteur, avec ma recommandation

1. **La passerelle reste-t-elle fermée par défaut, ou un « guichet » écoute-t-il d'office pour les demandes d'accès ?**
   *Recommandation : fermée* (I2). Le client propose « Ouvrir les réglages d'AIODOS », qui ouvre directement la bonne page (`--open applications`) : un clic de plus, une seule fois. Le guichet pourra devenir une option.
2. **Une clé par appli, des clés à la main par outil, et la clé générale gardée seulement pour l'existant ?**
   *Recommandation : oui.* La clé générale garde ses droits d'aujourd'hui, passe sous DPAPI, et n'est plus créée d'office sur une nouvelle installation.
3. **Où calculer par défaut ?**
   *Recommandation :* « mes machines » pour la conversation et les créations. Les amis jamais cochés d'avance, avec une seconde confirmation, et toujours soumis aux réglages généraux. Embeddings et transcription toujours sur ce PC. ARKLIGHT demandera `here`.
4. **Quel contexte, et quelle mémoire vidéo y mettre ?**
   *Recommandation :* un réglage global (8192, 16384 ou 32768), avec une exception possible par modèle. 16384 par défaut si l'estimation de la mémoire vidéo le permet, sinon 8192. 32768 sur demande pour PANDORA, avec l'estimation affichée. Jamais réglé par une requête. À mesurer sur la carte de 8 Go, en essayant un cache KV en q8_0 pour tenir 32 k.
5. **Priorité des applis, et garde-fou contre l'accaparement ?**
   *Recommandation :* conversation et transcription interactives par défaut, comme celles d'AIODOS ; les lots en fond (`batch=True`). Au-delà de 10 min sur 15, passage en fond. Valeurs à régler à l'usage.
6. **Les outils supprimés en silence : faut-il aussi refuser pour la clé générale ?**
   *Recommandation :* refus pour les clés d'appli et les clés à la main dès la v1. Pour la clé générale, un en-tête d'avertissement en v1, puis le refus à la version suivante si tu acceptes ce petit changement de comportement pour Open WebUI et Continue.
7. **Où vont les créations des applis, et combien de temps ?**
   *Recommandation :* dans `data\gateway\out\<appli>`, hors du dossier Images, visibles dans la galerie avec un filtre et un bouton « Garder ». Gardées **7 jours** : 24 h, c'est trop court si l'appli plante pendant un long rendu, et sans limite le disque se remplit. À confirmer : le dossier Images est-il synchronisé par OneDrive sur tes machines ?
8. **Quel moteur pour la transcription interactive ?**
   *Recommandation :* d'abord une étude courte de whisper.cpp (`whisper-server`). Il serait livré comme llama.cpp et stable-diffusion.cpp, pourrait utiliser la carte graphique, et ARKLIGHT s'en sert déjà. faster-whisper n'est pas dans l'installeur (640 Mo). Dans tous les cas : un second programme lancé à la demande, avec `warmup`, plutôt que la préemption de l'indexation.
9. **Ajouter python-multipart ?**
   *Recommandation : pas en v1.* Les routes AIODOS en JSON ou en corps brut suffisent aux deux applis. Plus tard, pour les façades `/v1/audio/transcriptions` et `/v1/images/edits`, une fois sa licence vérifiée (Apache-2.0 à ma connaissance).
10. **Vidéos : la forme AIODOS seulement ?**
    *Recommandation : oui en v1.* `/v1/videos` viendra plus tard, une fois sa référence revérifiée. Pas de forme calquée sur sd-server : PANDORA passe par le client.
11. **Sortie vidéo en WebM ?**
    *Recommandation : oui, documenté.* Ni ffmpeg ni H.264 dans AIODOS, et PANDORA convertit déjà.
12. **Lier la clé au programme ?**
    *Recommandation :* toujours noter le programme et alerter s'il change. Un refus strict en option, décoché par défaut, et impossible pour `python.exe` ou `node.exe` en développement.
13. **Refuser tout autre compte Windows ?**
    *Recommandation : oui* pour les demandes, les clés d'appli et les clés à la main (sauf la case « conteneur »). Pour la clé générale, noté seulement en v1 à cause de Docker, puis durci après essai.
14. **Expiration des accès ?**
    *Recommandation :* aucune par défaut, avec au choix 30 jours ou « jusqu'à la fermeture d'AIODOS ». Un badge après 90 jours sans usage, jamais de retrait automatique.
15. **Le retrait d'une appli coupe-t-il ses flux et annule-t-il ses travaux ?**
    *Recommandation : oui, les deux*, avec une case cochée par défaut pour les travaux.
16. **Bibliothèques ?**
    *Recommandation :* la lecture seulement, sur une liste choisie à l'accord, vide par défaut, en L4. Jamais les bibliothèques « gardées sur ce PC », jamais celles des autres machines. L'écriture en v2, dans une bibliothèque propre à l'appli, créée sur accord.
17. **Approbation des accès sur le téléphone ?**
    *Recommandation : en v2*, avec le mécanisme des mises à jour (`updates/approval.py`). C'est la seule vraie parade contre un programme qui cliquerait « Autoriser » à ta place.
18. **Clients copiés ou publiés, et sous quelle licence ?**
    *Recommandation :* copiés, sous Apache-2.0 seule, qui se reprend dans du GPL-3.0. Publication sur PyPI et npm quand l'API sera stable.
19. **Alias de modèles ?**
    *Recommandation :* `whisper-1`, oui. Aucun alias d'images (`dall-e-3`, `gpt-image-1`) : ce serait trompeur.
20. **Journal : quoi, et combien de temps ?**
    *Recommandation :* les métadonnées seulement, jamais le contenu. Le détail 30 jours (20 000 lignes au plus), les compteurs 90 jours.
21. **Garder l'imitation d'Ollama (« Ollama is running », `/api/version` 0.40.5) ?**
    *Recommandation : oui*, pour les outils existants. L'identité d'AIODOS passe par `/aiodos/v1/info` et `X-Aiodos-Api`.

---
