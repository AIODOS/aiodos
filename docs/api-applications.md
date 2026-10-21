# API des applications (version 1) : la référence

Ce que la passerelle d'AIODOS sert aux applications (PANDORA, ARKLIGHT PC…) et ce que les
bibliothèques clientes `aiodos-client` (Python et JavaScript, `clients/`) en attendent. C'est
la source commune des deux clients et des essais du serveur : `clients/contract/` en rejoue
les exemples des deux côtés. Le guide pour les intégrateurs est
[integrer-aiodos.md](integrer-aiodos.md) ; la conception, [conception/](conception/).

Livraison **L1** (« branchable, texte ») : découverte, accès par appli, catalogue,
conversation, embeddings. Les travaux de moteur (ComfyUI…), les fichiers et la file viennent
en L2 : les clients en ont déjà la surface, qui lève `NotSupported` tant que `features` ne
les annonce pas.

## 1. Trouver AIODOS

### 1.1 L'adresse

`http://127.0.0.1:<port>`, port 11435 par défaut (réglage `gateway_port`). Ordre de
recherche des clients : le paramètre de l'appli, `AIODOS_URL`, le fichier de découverte,
l'adresse par défaut. **Un client refuse toute adresse dont l'hôte n'est pas `127.0.0.1`,
`localhost` ou `[::1]`, avant d'envoyer quoi que ce soit** (code `non_local_url`) ; il ne
suit aucune redirection et n'utilise aucun proxy.

### 1.2 Le fichier de découverte

- Windows : `%LOCALAPPDATA%\AIODOS\gateway.json` ; ailleurs :
  `$XDG_STATE_HOME/aiodos/gateway.json` (`~/.local/state/aiodos/gateway.json`).
- `AIODOS_DISCOVERY_FILE` donne un autre chemin ; `-` coupe l'écriture (essais).
- Écrit par l'instance d'AIODOS qui utilise le dossier de données par défaut, jamais par un
  nœud d'essai ; au démarrage, à chaque ouverture, fermeture ou changement de port de la
  passerelle, et à l'arrêt (`running: false`). Écriture atomique.

```json
{ "product": "aiodos", "format": 1, "version": "2.1.0", "api": 1,
  "pid": 12345, "instance": "Hk3v9QmZ", "running": true,
  "gateway": { "state": "open", "url": "http://127.0.0.1:11435",
               "apps": true, "requests": true, "approval_ui": true, "error": null },
  "updated": 1792500000.0 }
```

- `gateway.state` : `open`, `closed` ou `error` (port déjà pris : `error` dit par quel
  programme). `url` est donnée même fermée : c'est là qu'elle s'ouvrira.
- `apps` : la passerelle accepte les clés d'appli ; `requests` : elle accepte de nouvelles
  demandes d'accès ; `approval_ui` : une fenêtre peut montrer la carte d'accord (faux pour
  un nœud sans fenêtre).
- **Jamais dans ce fichier** : une clé, le jeton ou le port de l'interface, une commande.
- Les clients ne retiennent que `url`, seulement si son hôte est local, et n'exécutent rien
  qui en viendrait. Un `pid` mort veut dire « AIODOS arrêté » (Windows : `OpenProcess` puis
  `GetExitCodeProcess`, **jamais `os.kill(pid, 0)`, qui tue le programme sous Windows** ;
  Node : `process.kill(pid, 0)`).

### 1.3 L'installation (pour lancer AIODOS ou se connecter passerelle fermée)

- La racine : `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}_is1`,
  valeur `InstallLocation`, puis la même clé sous HKLM, sinon `%LOCALAPPDATA%\Programs\AIODOS`.
  `AIODOS_HOME` en donne une autre (essais, copie de développement).
- Programmes : `<racine>\runtime\python.exe` (sans fenêtre : `pythonw.exe`), lancés avec
  `-m aiodos …` depuis `<racine>\app`, sans shell, avec une liste d'arguments.
- Copie de développement (`AIODOS_HOME` contient `aiodos\__init__.py`) : l'interpréteur est
  `AIODOS_PYTHON` ; sans lui, le client ne lance rien et le dit.
- **Lancer AIODOS** (`launch()`, sur un clic seulement) : `pythonw.exe -m aiodos --background`.

## 2. Conventions

- Toutes les réponses portent `X-Aiodos-Api: 1`, « / » compris.
- La clé : `Authorization: Bearer <clé>` (ou `X-Api-Key`). **Jamais dans l'adresse** : une clé
  dans l'adresse est ignorée.
- JSON en UTF-8 ; les `POST` de `/aiodos/v1` exigent `Content-Type: application/json`
  (sinon 415). Dates en secondes Unix.
- Une requête fait au plus 64 Mio (413 `file_too_large` au-delà, sans tout lire).
- Les pages web n'atteignent pas la passerelle : `Host` exact ; sous `/aiodos/v1/` et sur les
  routes sans clé, tout `Origin` est refusé (403 `origin_refused`) ; ailleurs, un `Origin`
  non local l'est ; `Sec-Fetch-Site: cross-site` ou `same-site`, ou un `Sec-Fetch-Dest`
  autre que `empty`, donnent 403. `Sec-Fetch-Mode` seul passe (le `fetch` de Node l'envoie).

**En-têtes de requête**

| En-tête | Valeurs | Effet |
|---|---|---|
| `X-Aiodos-Where` | cercles séparés par des virgules : `here`, `mine`, `friends`, `public` | resserre le lieu de calcul ; plus large que l'accord : 403 `where_denied` |
| `X-Aiodos-Priority` | `background` | baisse la priorité (lots, découpages) ; jamais l'inverse |
| `X-Aiodos-Tools` | `optional` | accepte une réponse sans outils plutôt que 400 `tools_unavailable` |
| `Accept-Language` | `fr`, `en` | langue des messages d'erreur (français par défaut) |
| `X-Aiodos-Client` | `python/0.1.0`, `js/0.1.0` | journal |

**En-têtes de réponse** : `X-Aiodos-Api` ; `X-Aiodos-Model` (identifiant concret qui a
répondu), `X-Aiodos-Machine` (nom, encodé pour cent en UTF-8), `X-Aiodos-Place` (`here`,
`mine`, `friend`) ; `Retry-After` sur 429 et 503.

## 3. Les erreurs

`/aiodos/v1/*` et `/v1/*` : l'enveloppe d'OpenAI, plus un objet `aiodos`.

```json
{ "error": { "message": "« qwen3.5:9b » n'est que sur Portable, qui ne reçoit pas encore les outils.",
             "type": "invalid_request_error", "param": null, "code": "tools_unavailable",
             "aiodos": { "hint": "Choisis un modèle de ce PC, ou envoie X-Aiodos-Tools: optional.",
                         "retryable": false, "detail": {} } } }
```

`type` vaut `invalid_request_error` sous 500, `api_error` à partir de 500. `/api/*` (forme
d'Ollama) : `{"error": "…", "code": "…", "hint": "…"}`. `/mcp` : le code dans
`error.data.code`. `/aiodos/v1/access/token` : la forme d'OAuth (§ 4.3).

| HTTP | `code` | Quand |
|---|---|---|
| 400 | `host_refused` | `Host` inattendu |
| 400 | `invalid_request` | corps ou champ invalide (`param`) |
| 400 | `context_length_exceeded` | la demande dépasse le contexte (`detail.needed`, `detail.limit` quand ils sont connus) |
| 400 | `tools_unavailable` | outils demandés, aucune machine permise ne les reçoit |
| 400 | `vision_unsupported` | images envoyées à un modèle qui ne les voit pas |
| 400 | `local_only` | la demande ne se calcule que sur ce PC, qui n'a pas le modèle |
| 400 | `unsupported_parameter` | paramètre standard qui changerait le résultat et qu'AIODOS ne traite pas (`param`) |
| 401 | `invalid_api_key` | clé absente ou inconnue |
| 401 | `key_revoked`, `key_expired` | clé retirée, ou expirée |
| 403 | `scope_denied` | droit manquant (`detail.scope`) |
| 403 | `where_denied` | lieu plus large que l'accord |
| 403 | `apps_closed`, `requests_closed` | AIODOS fermé aux applis, ou aux nouvelles demandes |
| 403 | `blocked_program` | programme bloqué par l'utilisateur |
| 403 | `other_account` | la demande vient d'un autre compte Windows |
| 403 | `program_mismatch` | clé liée à un autre programme |
| 403 | `origin_refused` | page web, ou en-têtes d'un navigateur |
| 404 | `model_not_found` | modèle inconnu du cercle (ou hors du lieu permis) |
| 404 | `not_supported` | fonction absente de cette version |
| 409 | `no_machine` | aucune machine permise n'a le modèle en ce moment |
| 413 | `file_too_large` | corps trop gros |
| 415 | `unsupported_media_type` | corps non JSON |
| 429 | `rate_limited`, `busy` | voir `Retry-After` |
| 502 | `peer_failed`, `engine_failed` | échec de la machine choisie |
| 503 | `engine_unavailable`, `approval_unavailable` | moteur en panne ou en démarrage ; AIODOS sans fenêtre |

**Codes propres aux clients**, avec la même forme d'erreur : `unreachable` (son message
contient « Connexion refusée »), `gateway_closed`, `not_aiodos`, `too_old`, `untrusted`,
`non_local_url`, `timeout`, `cancelled` (message « Arrêté »), `protocol` (réponse incomprise),
`not_installed`, `not_running`, `needs_access`, `access_denied`, `expired_token`.

## 4. L'accès

### 4.1 Les interrupteurs (Réglages → « Applications et autres outils »)

| Réglage | Défaut | Effet |
|---|---|---|
| `gateway_apps` « Ouvrir AIODOS aux applications de ce PC » | désactivé | les clés d'appli marchent |
| `gateway_requests` « Accepter les nouvelles demandes d'accès » | activé | `POST /aiodos/v1/access/requests` est accepté |
| `gateway_enabled` « Clé générale pour les autres outils » | désactivé | la clé générale (`aiodos-…`) et le pont MCP marchent |

Le port écoute dès que `gateway_apps` ou `gateway_enabled` est vrai. **Le premier accord donné
à une appli ouvre la passerelle aux applications** (`gateway_apps` passe à vrai).

### 4.2 Une appli, une clé

- Forme : `aiodos_app_<id>_<secret>` ; `id` : 8 caractères `[a-z2-7]`, public (journal,
  interface, preuve) ; `secret` : 43 caractères base64url (256 bits). Une clé « à la main »
  pour un outil a la même forme.
- AIODOS ne garde que `sha256(clé)` ; la clé n'est remise qu'une fois.
- La clé générale garde sa forme (`aiodos-…`) et ses droits : `chat embed libraries`.

### 4.3 La demande d'accès (vocabulaire de la RFC 8628)

**`POST /aiodos/v1/access/requests`**, sans clé (avec une clé valide : demande de droits en
plus pour la même clé).

```json
{ "client_name": "PANDORA", "client_id": "arkane.pandora", "client_version": "3.2.0",
  "scope": "chat embed", "where": ["here", "mine", "friends", "public"],
  "purpose": "Traduire les prompts et décrire les images." }
```

- `client_name` : 1 à 40 caractères imprimables (contrôle et sens d'écriture retirés) ; pas
  « AIODOS », « Autres outils », « Windows », « Microsoft ». `client_id` : `[a-z0-9.-]{2,40}`.
  `client_version` : 40 caractères au plus. `purpose` : 200 au plus.
- `scope` : droits séparés par des espaces, parmi `chat`, `embed`, `libraries`. Un droit
  inconnu : 400 `invalid_request`.
- `where` : une liste (ou une chaîne séparée par des virgules) de cercles parmi `here`,
  `mine`, `friends`, `public` ; absent : les quatre. L'utilisateur peut en décocher, jamais en
  ajouter.

Réponse 200 :

```json
{ "device_code": "<43 caractères>", "user_code": "482 197", "expires_in": 600,
  "interval": 2, "request_id": "req_8f3KqZ2m",
  "message": "Une demande d'accès t'attend dans AIODOS. Vérifie que le code est 482 197." }
```

Erreurs : 403 `apps_closed` (aucune appli n'a encore été autorisée et la passerelle n'est
ouverte qu'à la clé générale : passer par § 4.4), `requests_closed`, `other_account`,
`blocked_program`, `origin_refused` ; 503 `approval_unavailable` ; 429 `rate_limited` (3
demandes en attente au plus, 10 par heure et par programme, sourdine d'une heure après 3
refus) ; 415 ; 400 `invalid_request`.

**`POST /aiodos/v1/access/token`**, sans clé, toutes les `interval` secondes :

```json
{ "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "…" }
```

Succès 200, **une seule fois** :

```json
{ "access_token": "aiodos_app_k3j7m2qa_…", "token_type": "Bearer", "scope": "chat embed",
  "aiodos": { "app_id": "k3j7m2qa", "where": ["here", "mine"], "priority": "interactive",
              "expires": null } }
```

`scope` et `where` sont **ceux accordés**. Pour des droits en plus, la réponse n'a pas
d'`access_token` et `aiodos.upgraded` vaut `true`. Erreurs, statut 400, forme d'OAuth
`{"error": "…", "error_description": "…"}` :

| `error` | Sens |
|---|---|
| `authorization_pending` | pas encore de réponse |
| `slow_down` | interrogé trop souvent : ajouter 5 s à l'intervalle |
| `access_denied` | refusé |
| `expired_token` | plus de 10 minutes, ou AIODOS a redémarré |
| `invalid_grant` | `device_code` inconnu, ou clé déjà relevée |

**`POST /aiodos/v1/access/revoke`**, avec la clé : l'appli se retire elle-même (bouton
« Se déconnecter d'AIODOS ») → 200 `{"revoked": true}`.

### 4.4 Se connecter quand la passerelle est fermée (ou AIODOS arrêté)

1. Sur un clic de l'utilisateur, le client lance le programme d'aide d'AIODOS :
   `<racine>\runtime\python.exe -m aiodos --app-request`, depuis `<racine>\app`, sans shell
   ni fenêtre, avec le corps de la demande (§ 4.3) **sur son entrée standard**, en une ligne.
2. Le programme d'aide lance AIODOS en arrière-plan s'il ne tourne pas (60 s au plus),
   transmet la demande à l'instance par son canal privé (jamais par la passerelle), avec le
   numéro du programme qui l'a lancé (l'appli), puis écrit **une ligne JSON** sur sa sortie
   standard et s'arrête (code 0) :

   ```json
   { "device_code": "…", "user_code": "482 197", "interval": 2, "expires_in": 600,
     "request_id": "req_…", "url": "http://127.0.0.1:11435" }
   ```

   En cas d'échec, code 1 et `{"error": {"code": "…", "message": "…"}}` (`not_running`,
   `approval_unavailable`, `blocked_program`, `rate_limited`, `invalid_request`…).
3. AIODOS montre la carte d'accord. **« Autoriser » ouvre la passerelle** aux applications.
4. Le client interroge `POST <url>/aiodos/v1/access/token` toutes les `interval` secondes ;
   tant que la passerelle n'écoute pas, la connexion est refusée et il continue, jusqu'à
   `expires_in`.

### 4.5 Les droits

| Droit | Routes |
|---|---|
| `chat` | `/v1/chat/completions`, `/v1/completions`, `/api/chat`, `/api/generate`, `/api/show`, `/api/ps`, MCP `converser` |
| `embed` | `/v1/embeddings`, `/api/embed`, `/api/embeddings` |
| `libraries` | MCP `lister_bibliotheques`, `rechercher_bibliotheques` (bibliothèques choisies à l'accord) |

Pour toute clé valide : `info`, `/v1/models`, `/api/tags`, `/aiodos/v1/models` (filtrés selon
les droits et le lieu), `open`, `access/revoke`. Droit manquant : 403 `scope_denied`. Jamais
accordables : les réglages, le réseau, le cloud, la gestion des modèles.

### 4.6 Où calculer (`where`)

| Cercle | Conversation (nom seul, choix automatique) | `peer:<machine>:<modèle>` | Outils, images jointes, embeddings |
|---|---|---|---|
| `here` | ce PC | — | toujours ce PC |
| `mine` | mes machines qui prêtent leurs modèles | une de mes machines | — |
| `friends` | mes amis aussi, si le réglage général `auto_route_friends` le permet | un ami | — |
| `public` | pas encore servi (confirmé à son arrivée) | — | — |

Lieu effectif : les cercles accordés à l'appli, resserrés par `X-Aiodos-Where`, et par les
réglages généraux. Jamais le cloud, quelle que soit la clé. Un modèle hors du lieu permis
n'apparaît pas dans les listes et répond 404 `model_not_found` ; un `peer:` hors du lieu, 403
`where_denied`.

### 4.7 Priorité

`interactive` par défaut (une conversation d'appli passe devant les travaux de fond, comme
celles d'AIODOS), ou `background`, choisi à l'accord ; `X-Aiodos-Priority: background` baisse
une requête.

## 5. Les routes

| Méthode et chemin | Clé | Droit |
|---|---|---|
| `GET /` | non | — (« Ollama is running ») |
| `GET /aiodos/v1/info` | facultative | — |
| `POST /aiodos/v1/access/requests`, `POST /aiodos/v1/access/token` | non | — |
| `POST /aiodos/v1/access/revoke` | oui | — |
| `GET /aiodos/v1/models?kind=chat\|embed` | oui | filtré |
| `POST /aiodos/v1/open` | oui | — |
| `GET /v1/models`, `GET /v1/models/{id}` | oui | filtré |
| `POST /v1/chat/completions`, `POST /v1/completions` | oui | `chat` |
| `POST /v1/embeddings` | oui | `embed` |
| `/api/*` (forme d'Ollama), `POST /mcp` | oui | `chat`, `embed`, `libraries` |

### 5.1 `GET /aiodos/v1/info`

Sans clé, toujours 200 :

```json
{ "object": "aiodos.info", "product": "aiodos", "version": "2.1.0", "api": 1,
  "instance": "Hk3v9QmZ", "features": ["access", "models", "chat", "embed", "open"],
  "access": { "apps": true, "requests": true, "approval_ui": true }, "auth": "none" }
```

- `features` dit ce que **cette version** sert ; un client ne compare jamais de numéros de
  version. L2 ajoutera `engines`, `engines.comfyui`, `jobs`, `jobs.pause`, `inputs`, `files`.
- Avec `Authorization`, `auth` vaut `valid`, `revoked`, `expired` ou `invalid` ; s'il vaut
  `valid` :

  ```json
  "app": { "id": "k3j7m2qa", "name": "PANDORA", "kind": "app", "scopes": ["chat", "embed"],
           "where": ["here", "mine"], "priority": "interactive", "expires": null },
  "limits": { "requests_per_minute": 120, "streams": 4, "max_body_bytes": 67108864 }
  ```

**La preuve d'identité.** AIODOS arrêté, n'importe quel programme pourrait écouter sur le port
et recueillir les clés. Avant d'envoyer sa clé, le client tire un défi :

1. `GET /aiodos/v1/info?key_id=<id>&challenge=<défi>`, **sans la clé** ; le défi : 32 octets
   aléatoires en base64url sans `=` (43 caractères).
2. La réponse ajoute `"proof": base64url_sans_égal(HMAC-SHA256(clé = SHA-256(clé d'accès en
   UTF-8), 32 octets bruts ; message = "aiodos-proof/1\n" + défi, en ASCII))`.
3. Le client calcule la même valeur avec sa clé et compare (en temps constant). Fausse :
   état `untrusted`, la clé n'est pas envoyée. Pour un `key_id` inconnu, AIODOS rend une
   preuve aléatoire (elle ne dit pas si l'identifiant existe) ; pour une clé retirée, la
   preuve est juste et l'appel suivant dira `revoked`.
4. Le client refait la preuve quand `instance` change, après une erreur de connexion, et si la
   dernière date de plus de 60 s.

### 5.2 `GET /aiodos/v1/models?kind=chat|embed`

Le catalogue par modèle logique, filtré selon les droits et le lieu de l'appli :

```json
{ "object": "list", "models": [
  { "id": "qwen3.5:9b", "kind": "chat", "label": "qwen3.5:9b", "installed": true,
    "context": 16384, "vision": true, "tools": true, "thinking": true,
    "size_bytes": 6600000000, "loaded": false, "here": true,
    "where": [ { "id": "local:qwen3.5:9b", "machine": "PC-BUREAU", "place": "here",
                 "loaded": false, "context": 16384, "tools": true, "vision": true },
               { "id": "peer:9c2e…:qwen3.5:9b", "machine": "Portable", "place": "mine",
                 "loaded": true, "context": null, "tools": false, "vision": false } ],
    "license": null } ] }
```

- `id` : à donner tel quel à `/v1/chat/completions` (la machine est alors choisie seule) ;
  `where[].id` : une machine précise.
- `tools` et `vision` au niveau du modèle : vrais si **ce PC** les sert (une machine appairée
  ne reçoit en v1 qu'une conversation en texte).
- `context` : le contexte réel du moteur de ce PC (null si inconnu).
- `kind=embed` : les modèles d'embedding de ce PC (`dimensions` quand il est connu).

### 5.3 `POST /aiodos/v1/open`

`{"page": "applications" | "models" | "jobs" | "gallery", "model": null}` → 202
`{"opened": true}` (la fenêtre d'AIODOS revient sur cette page ; `false` sans fenêtre). Une
fois par 10 s et par appli (429 sinon).

### 5.4 `/v1/models`

La forme d'OpenAI (`{"object": "list", "data": [...]}`), limitée aux modèles de conversation et
d'embedding permis, chaque entrée enrichie à la façon d'OpenRouter :

```json
{ "id": "qwen3.5:9b", "object": "model", "created": 1760000000, "owned_by": "aiodos",
  "name": "qwen3.5:9b", "context_length": 16384,
  "architecture": { "input_modalities": ["text", "image"], "output_modalities": ["text"] },
  "supported_parameters": ["tools", "reasoning", "response_format", "max_tokens",
                           "temperature", "top_p", "stop", "seed"],
  "aiodos": { "kind": "chat", "machine": null, "place": "here", "loaded": false } }
```

### 5.5 `POST /v1/chat/completions`

La forme d'OpenAI. Ce qu'AIODOS y ajoute :

- **Toujours conseillé en flux** (`stream: true`). Tant que le modèle se charge, la réponse
  peut porter des commentaires SSE `: aiodos loading <secondes>` (ignorés par les SDK) : un
  client y remet à zéro son délai d'inactivité.
- `reasoning_effort: "none"` coupe la réflexion ; `max_tokens` et `max_completion_tokens` sont
  pris tous les deux ; un `max_tokens` trop grand n'est jamais refusé (le moteur s'arrête à la
  fin du contexte, `finish_reason: "length"`).
- La réflexion arrive dans `delta.reasoning_content`.
- Outils (`tools`) et images (`image_url` en data-URL) : calculés par ce PC. Si le modèle n'y
  est pas : 400 `tools_unavailable` (outils) ou `local_only` (images), sauf
  `X-Aiodos-Tools: optional` (la conversation part alors en texte, sans outils).
- Le dernier morceau qui porte `finish_reason` porte aussi
  `"aiodos": {"model", "machine", "place", "stopped_by", "context_length"}`, `stopped_by`
  valant `stop`, `max_tokens`, `context` ou `tool_calls`. Sans flux : à la racine de la réponse.
- Une erreur en cours de route arrive en `data: {"error": {…}}`, puis le flux se ferme.
- Ignorés : `user`, `store`, `metadata`, `service_tier`, `prompt_cache_key`,
  `safety_identifier`, `parallel_tool_calls`. Refusés (400 `unsupported_parameter`) : `audio`,
  `modalities` avec `audio`, `prediction`, `web_search_options`.

### 5.6 `POST /v1/embeddings`

La forme d'OpenAI ; sur ce PC seulement ; `encoding_format: "base64"` pris.

## 6. Les bibliothèques clientes

- `aiodos-client` sur PyPI (module `aiodos_client`, Python 3.10 à 3.14) et sur npm (module
  ES, Node 20.3 et plus), sous Apache-2.0, **sans aucune dépendance**. Sources : `clients/`.
- Toute la surface existe dès la version 0.1.0 ; ce que `features` n'annonce pas lève
  `NotSupported` sans rien essayer.
- La clé : lue et rangée par des fonctions de l'appli (coffre DPAPI fourni en Python ; en JS,
  `safeStorage` d'Electron côté appli), jamais dans un journal, un message d'erreur, un
  `repr` ou un `toString`.
- Les états (`status()`, qui ne lève jamais) :

| `state` | Quand | Message conseillé |
|---|---|---|
| `not_installed` | ni fichier de découverte, ni installation | AIODOS n'est pas installé sur ce PC. |
| `not_running` | installé, rien n'écoute, ou `running: false`, ou pid mort | AIODOS n'est pas lancé. |
| `gateway_closed` | AIODOS tourne, passerelle fermée | AIODOS tourne, mais n'est pas ouvert aux applications. |
| `gateway_error` | `gateway.state: error` | le message d'AIODOS |
| `apps_closed` | ouverte à la clé générale seulement | AIODOS n'est ouvert qu'aux autres outils. |
| `not_aiodos` | le port répond, mais pas comme AIODOS | Le port est pris par un autre programme. |
| `too_old` | ancienne version (401 sans `X-Aiodos-Api`, ou `api` trop bas) | Mets AIODOS à jour. |
| `untrusted` | preuve fausse | Le programme qui répond ne connaît pas la clé : elle n'a pas été envoyée. |
| `headless` | `approval_ui: false`, pas de clé | AIODOS tourne sans fenêtre : ouvre-le pour autoriser l'appli. |
| `requests_closed` | `access.requests: false`, pas de clé | AIODOS n'accepte pas de nouvelle application en ce moment. |
| `needs_access` | pas de clé, ou `auth: invalid` | L'appli n'a pas encore accès à AIODOS. |
| `pending` | demande en cours | Autorise l'appli dans la fenêtre d'AIODOS. Code : 482 197 |
| `denied` | `access_denied` | L'accès a été refusé dans AIODOS. |
| `revoked` | `auth: revoked` ou `expired` | L'accès a été retiré (ou a expiré) dans AIODOS. |
| `other_account` | 403 `other_account` | AIODOS tourne sous un autre compte Windows. |
| `ready` | `auth: valid` | Connecté à AIODOS. |

- `connect(scopes, where, purpose)` : passerelle ouverte, `POST /aiodos/v1/access/requests` ;
  fermée ou AIODOS arrêté, le programme d'aide (§ 4.4) ; puis la relève (§ 4.3) et le
  rangement de la clé. Le code (`user_code`) est donné à l'appli, qui l'affiche en grand.
- La conversation passe toujours en flux ; son délai compte l'inactivité, remis à zéro à
  chaque morceau et à chaque `: aiodos loading`.
