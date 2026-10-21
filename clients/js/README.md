<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- aiodos-client 0.2.0 -->

# aiodos-client (JavaScript)

**In English, briefly.** `aiodos-client` lets a Node.js application (or the **main process** of
an Electron app, never a renderer) use [AIODOS](https://github.com/AIODOS/aiodos), which shares
the computing power of your own machines. It finds AIODOS on this PC, asks the user for access
(a 6-digit code shown in the app and in AIODOS), stores the per-app key through your own key
store, and gives you the model catalogue, streamed chat with tools and images, embeddings, and
ComfyUI jobs (API-format graphs replayed by the ComfyUI of this PC and of your machines: inputs
sent once by their SHA-256, live progress with automatic reconnection, resumable and verified
downloads). No dependencies, Node 20.3+, ES module, Apache-2.0. It only talks to `127.0.0.1`, `localhost`
or `[::1]`, never follows redirects, refuses to go through a proxy, and never sends the key
before AIODOS has proven its identity. Install with `npm i --save-exact aiodos-client` and keep
your `package-lock.json`.

---

Le client JavaScript des applications d'AIODOS (ARKLIGHT PC d'abord). La référence de l'API
est [docs/api-applications.md](../../docs/api-applications.md) ; le contrat partagé avec la
passerelle et le client Python, [clients/contract/](../contract/).

- **Node 20.3 et plus**, module ES, **aucune dépendance**.
- Il vit **dans le serveur Node ou dans le processus principal d'Electron**, jamais dans un rendu :
  importé dans une page, il lève une erreur claire (la passerelle refuse d'ailleurs les pages web).
- Il ne parle qu'à **ce PC** (`127.0.0.1`, `localhost`, `[::1]`), vérifié avant tout envoi.
- Il ne télécharge et n'installe **rien** ; il ne lance AIODOS que si l'appli le lui demande.

## Installation

```sh
npm i --save-exact aiodos-client
```

Gardez `package-lock.json` dans le dépôt : la version et l'empreinte du paquet y sont épinglées.
Le paquet n'a aucune dépendance, il n'ajoute donc rien d'autre à `node_modules`.

## Pour ARKLIGHT PC

Le client se crée une fois, dans le serveur (`server/ai/…`), que le processus principal
d'Electron charge. L'interface n'y touche pas : elle passe par les messages qu'elle envoie déjà
au serveur.

```js
import { createClient, pickModel } from 'aiodos-client';

// la clé est lue par get() à chaque appel et rangée par set() après l'accord :
// dans l'appli de bureau, le coffre de l'appli la chiffre par safeStorage d'Electron
export const aiodos = createClient({
  app: 'ARKLIGHT PC',
  appId: 'arkane.arklight',
  version: APP_VERSION,
  key: { get: () => keys.get('aiodos'), set: (k) => keys.set('aiodos', k) },
  lang: () => config.lang, // « fr » ou « en », relu à chaque appel
});

// l'état, pour la carte « AIODOS » des réglages ; ne rejette jamais
const st = await aiodos.status(); // { state, message, userCode, app, … }

// sur un clic de l'utilisateur seulement : se connecter
if (st.state !== 'ready') {
  await aiodos.connect({
    scopes: ['chat'],
    where: ['here'], // le calcul reste sur ce PC (outils et images s'y calculent de toute façon)
    purpose: 'Assistant lumière : comprendre les demandes et agir sur le plan de feu',
    onCode: (code) => broadcast({ type: 'ai.aiodos.code', code }), // affiché en grand
    signal,
  });
}

// le catalogue au format { id, size, caps, params } déjà connu, plus machine, context, loaded…
// pickModel préfère ce PC, puis le modèle qu'AIODOS prend lui-même (default), puis le chargé
const model = pickModel(await aiodos.models(), { needs: ['tools'], vision: true });

// un tour de l'agent : messages neutres { role, content, images?, toolCalls?, toolCallId }
const r = await aiodos.chat({
  model: model.id,
  system,
  messages,
  tools, // [{ name, description, parameters }]
  think: false, // conseillé : reasoning_effort « none »
  temperature: 0.2,
  where: 'here',
  signal, // l'arrêt ferme vraiment la connexion
  onLoading: ({ seconds }) => showModelLoading(seconds),
});
// r : { text, toolCalls: [{ id, name, args, raw }], finish, stoppedBy, truncated, usage,
//       answeredBy, machine, place, contextLength }

// « Se déconnecter d'AIODOS »
await aiodos.disconnect(); // retrait dans AIODOS, puis key.set(null)
```

- Une réponse arrive **toujours en flux** ; `timeout` (240 s) compte l'**inactivité**, remise à
  zéro à chaque morceau et à chaque `: aiodos loading N`. Avant le premier morceau,
  `loadTimeout` (900 s) vaut si le catalogue dit le modèle non chargé.
- `onText`, `onThinking` reçoivent les morceaux au fil de l'eau ; `onLoading({ seconds })`, le
  chargement du modèle.
- Un appel d'outil dont les arguments sont illisibles arrive avec `args: null` et le texte dans
  `raw` : le filet d'ARKLIGHT (appels écrits dans le texte) reste utile.
- `batch: true` baisse la priorité ; `requireTools: false` accepte une réponse sans outils
  plutôt qu'une erreur `tools_unavailable`.
- `embed({ input })` rend `{ embeddings, model, dimensions, usage }` ; sans `model`, celui des
  bibliothèques d'AIODOS. Garder `model` avec les vecteurs : ceux de deux modèles ne se
  comparent pas.
- Réserver les messages `ai.*` du serveur aux clients de la machine : sinon un appareil du
  réseau se servirait d'AIODOS à travers ARKLIGHT.

## Les états (`status()`)

`status()` ne rejette jamais. `message` est le texte conseillé, en français ou en anglais
(`texts.states` les donne tous) ; l'appli peut aussi traduire d'après `state`.

| `state` | Ce que l'appli propose |
|---|---|
| `not_installed` | « En savoir plus » (la page du projet) ; rien n'est téléchargé |
| `not_running` | **[Lancer AIODOS]** : `launch()`, puis `waitReady()` |
| `gateway_closed`, `apps_closed` | **[Se connecter à AIODOS]** : `connect()` passe par le programme d'aide d'AIODOS |
| `gateway_error` | le message d'AIODOS (port pris…) |
| `not_aiodos`, `too_old`, `other_account` | le message ; rien à faire depuis l'appli |
| `untrusted` | le programme qui répond ne connaît pas la clé : elle n'a pas été envoyée |
| `headless`, `requests_closed` | ouvrir AIODOS, ou ses réglages |
| `needs_access`, `denied`, `revoked` | **[Se connecter à AIODOS]** |
| `pending` | le code `userCode`, en grand ; [Annuler] (le `signal` de `connect()`) |
| `ready` | « Connecté à AIODOS » ; `app`, `scopes`, `where`, `limits` |

`connect()` choisit seul : passerelle ouverte, la demande `POST /aiodos/v1/access/requests` ;
fermée, AIODOS arrêté ou ouvert à la seule clé générale, le programme d'aide
(`python -m aiodos --app-request`, lancé sans shell ni fenêtre), qui démarre AIODOS au besoin et
lui transmet la demande. Puis la relève toutes les `interval` secondes (`slow_down` : 5 s de
plus ; tant que la passerelle n'écoute pas, la connexion refusée est tolérée), et la clé rangée
par `key.set()`. Avec une clé valide, c'est une demande de droits en plus pour **la même clé**
(`upgraded: true`). `requestAccess()` est l'étage du dessous (passerelle ouverte seulement).

## Les erreurs

Tout rejet est une `AiodosError` (ou une sous-classe : `Unreachable`, `NeedsAccess`,
`ToolsUnavailable`, `ContextTooLong`…), avec :

- `code` : stable, celui de la référence (§ 3) ; l'appli traduit d'après lui ;
- `message` (français par défaut, `lang: 'en'` pour l'anglais), `hint`, `retryable`, `detail`,
  `retryAfter` (secondes, sur 429 et 503), `status`, `param`.

Codes propres au client : `unreachable` (connexion refusée ou coupée ; **son message contient
« Connexion refusée »**, ce à quoi l'interface actuelle d'ARKLIGHT réagit en ouvrant ses
réglages), `cancelled` (message **« Arrêté »**), `timeout`, `non_local_url` (adresse non
locale, ou proxy en travers), `untrusted`, `not_aiodos`, `too_old`, `protocol`, `not_installed`,
`needs_access`, `access_denied`, `expired_token`, `invalid_grant`, `not_supported`,
`input_too_large` (une entrée de plus de 100 Mo, refusée avant l'envoi) et `job_failed`
(`comfy.run` : le travail a fini en échec, ou a été annulé ailleurs).

Les travaux ComfyUI ont leurs sous-classes : `GraphInvalid` (`graph_not_api` : le format de
l'éditeur ; `graph_invalid` ; `patch_invalid`), `GraphTooLarge`, `ForbiddenValue` (`node`,
`input`, `reason` : chemin, adresse ou annotation de dossier), `ForbiddenNode` (`nodeClass` :
un service en ligne), `InputMissing` (`input`), `InputHashMismatch`, `InputTooLarge`,
`EngineUnavailable` (aucune machine permise n'a ComfyUI ouvert et accordé, 409 ; moteur pas
prêt, 503), `ModelUnavailable` (`models` : `[{ folder, name }]`), `ModelUnresolved`,
`ModelAmbiguous` (`candidates`), `JobNotFound`, `FileNotFound`, `EngineNotFound`,
`InputNotFound`, `JobNotCancellable`, `JobNotPausable`, `QuotaExceeded` (`quota`) et
`JobFailed`. Un `HEAD` refusé n'a pas de corps : le client en dit la raison d'après l'info
avec la clé (clé retirée, droit `engines:comfyui` manquant, AIODOS fermé aux applis).

En anglais, ces messages sont en anglais (« Connection refused… », « Stopped ») : une appli qui
reconnaît le texte français doit soit garder `lang: 'fr'` pour les erreurs, soit (mieux) tester
`err.code`.

## Sécurité

- **La clé** n'est jamais gardée par le client, ni écrite dans un journal, un message d'erreur,
  un `toString` ou un `JSON.stringify` ; le `device_code` d'une demande non plus.
- **La preuve d'identité** (§ 5.1) : avant d'envoyer la clé, le client tire un défi et vérifie
  la réponse d'AIODOS (HMAC, comparé en temps constant). Fausse : état `untrusted`, la clé ne
  part pas. Elle est refaite quand l'instance d'AIODOS change, après une erreur de connexion,
  et au-delà de 60 s.
- **Ce PC seulement**, vérifié avant tout envoi ; aucune redirection suivie.
- **Aucun proxy** : si Node est réglé pour passer par un proxy (`NODE_USE_ENV_PROXY=1` ou
  `--use-env-proxy` avec `HTTP_PROXY`, ou un répartiteur à proxy), les requêtes vers
  127.0.0.1 passeraient par lui ; le client refuse alors (`non_local_url`, `detail.proxy`).
  Ajouter `127.0.0.1` à `NO_PROXY` règle la question.
- Du fichier de découverte, seule l'adresse est retenue (si elle est locale) ; rien n'en est
  exécuté. Les programmes lancés (`launch()`, le programme d'aide) sont calculés d'après
  l'installation (registre, `%LOCALAPPDATA%\Programs\AIODOS`, `AIODOS_HOME`), sans shell.

## Travaux ComfyUI

AIODOS ne remplace pas le ComfyUI de l'appli : il rejoue ses graphes, **au format API** et déjà
remplis (« Exporter (API) » dans ComfyUI), dans le ComfyUI accordé de ce PC et de mes machines
(Réglages → Moteurs des applications), plusieurs unités en même temps, et garde les fichiers
pour elle. Il faut le droit `engines:comfyui` (`connect({ scopes: ['chat', 'engines:comfyui'] })`).
Référence : [docs/api-applications.md](../../docs/api-applications.md) § 5.7.

```js
// un fichier ne se donne que par un jeton @in: : tokenize le met dans le LoadImage 10
const { graph, inputs } = await aiodos.comfy.tokenize(apiGraph, { 10: referencePng });

// les entrées hachées (SHA-256), celles qui manquent envoyées (HEAD, puis PUT), puis le travail
const job = await aiodos.comfy.submit({
  graph,
  inputs,
  items: prompts.map((p, i) => ({ label: `Mood ${i + 1}`, seed: 1200 + i, set: [['6', 'text', p]] })),
  label: 'Moods · Séq. 3',
  clientRef: `arklight:mood:${uid}`, // le même clientRef rend le même travail
  sensitive: hasPortrait, // ce PC et mes machines seulement
});

for await (const ev of job.watch({ signal })) {
  if (ev.type === 'job') showProgress(ev.job.progress.percent, ev.job.progress.message);
  if (ev.type === 'unreachable') showOffline(ev.retryIn); // reconnexion automatique
}
const paths = await job.download(destDir, { stem: 'mood' }); // mood-1.png, mood-2.png
```

- **`comfy.run({ …comme submit, destDir, stem, onFile, onEvent, signal })`** fait les trois
  d'un coup et rend les chemins. Arrêté par `signal`, le travail est aussi annulé dans AIODOS ;
  fini en échec, ou annulé ailleurs : `JobFailed` (`detail.units` : l'erreur de chaque unité).
- **`comfy.plan(…)`**, le pré-vol, prend le même corps et ne met rien en file :
  `{ ok, whereEffective, whereReason, machines, missing, units… }` (ses entrées sont envoyées,
  AIODOS en a besoin pour vérifier).
- **`comfy.tokenize(graph, { nœud: entrée })`** rend une copie : `@in:<id>` dans l'entrée de
  fichier de `LoadImage` et `LoadImageMask` (`image`), `LoadVideo` (`file`), `LoadAudio`
  (`audio`) ; `{ data | path, type, input }` donne le genre, ou l'entrée d'un autre nœud de
  chargement.
- **Une entrée** : des octets (`Buffer`, `Uint8Array`, `ArrayBuffer`), un chemin (ou une URL
  `file:`), `{ data | path, type }`, ou `{ sha256 }` déjà envoyée. **100 Mo au plus, vérifiés
  avant tout envoi** (`InputTooLarge`) ; toutes les entrées sont vérifiées avant la première
  envoyée, puis lues une à une. `inputs.put(data, { type })` envoie une entrée à part et rend
  son sha256.
- **Le lieu** : `where` et `sensitive` passent par `X-Aiodos-Where`, comme pour la
  conversation : ils resserrent l'accord de l'appli, jamais ne l'élargissent. `sensitive: true`
  garde le calcul sur ce PC et mes machines (l'accord est relu avec la clé). Les valeurs par
  défaut d'AIODOS (`priority: 'normal'`, `models: 'auto'`, `strict: false`) ne partent pas.
- **Un `Job`** suit le dernier état reçu : `id`, `status` (`queued`, `in_progress`, `pausing`,
  `paused`, `cancelling`, `completed`, `failed`, `cancelled`), `label`, `clientRef`,
  `progress` (`percent`, `message`, `machines`…), `units` (avec `error.code` :
  `engine_error`, `engine_timeout`, `output_invalid`, `output_too_large`, `no_machine`…),
  `error`, `cancelledBy`, `whereEffective`, `done`… ; **`job.state`** donne tout l'objet, dont
  `state.files` (les fichiers) et `state.pause` : `files()` et `pause()` sont des commandes.
- **`job.watch({ signal, timeout, idleTimeout })`** est un itérateur asynchrone :
  `{ type: 'job', job }` (l'état complet, d'abord puis à chaque changement), `{ type: 'file',
  file }` (chaque fichier pas encore récupéré, une fois), `{ type: 'end', status }`,
  `{ type: 'unreachable', error, attempt, retryIn }`. Il se reconnecte seul (l'état complet est
  relu, rien n'est perdu), rouvre un flux muet plus de 45 s, et réessaie de plus en plus
  lentement quand AIODOS ne répond plus. `signal` ou `break` ferment vraiment la connexion ;
  le travail, lui, continue dans AIODOS. `job.wait()` attend la fin et rend le travail, quel
  que soit son état final.
- **Les commandes** : `pause({ when: 'after_current' | 'now' })` (après l'unité en cours, par
  défaut : rien ne se perd), `resume()`, `cancel()` (ce calcul seulement, jamais ceux de
  l'utilisateur ; les fichiers rendus restent), `retryFailed()`, `delete({ files })` (un
  travail fini ; `files: true` efface aussi ses fichiers). Une appli n'agit que sur ses
  travaux : celui d'une autre est introuvable (`JobNotFound`).
- **Retrouver ses travaux** : `jobs()` rend ceux en cours ; avec `clientRef` ou `status`, tous
  (finis compris) ; `active: null` pour tous, `false` pour les finis ; `limit` (200 au plus).
  `job(id)` et `file(id)` pour un seul.
- **Les fichiers** : `job.files()` (ceux pas encore récupérés ; `{ unfetched: false }` pour
  tous), `job.download(dossier, { stem, onFile })` (tous, sous des noms stables
  `<stem>-<unité>[-<n>].<ext>`). Un `File` a sa fiche (`type`, `bytes`, `sha256`, `width`,
  `machine`, `place`, `fetched`…), `content()` (les octets), `save(chemin)` (au fil de l'eau
  dans `chemin.part`, puis renommé), `refresh()`, `delete()`. Un téléchargement coupé est
  repris par `Range` (3 essais par appel, puis à l'appel suivant), **l'empreinte SHA-256 est
  toujours vérifiée** et un fichier faux n'est jamais gardé ; un fichier déjà là, entier et
  juste, n'est pas redemandé. Servi jusqu'au dernier octet, il est « récupéré » : AIODOS le
  garde encore 7 jours.

Au lancement, l'appli reprend ce qu'AIODOS a calculé pendant son absence :

```js
for (const job of await aiodos.jobs({ active: null })) {
  if (job.clientRef?.startsWith('arklight:') && (await job.files()).length) await job.download(folderFor(job.clientRef));
}
```

Tout rejette `NotSupported` (`detail.announced: false`) sans rien essayer d'autre que
`GET /aiodos/v1/info` tant que l'AIODOS joint n'annonce pas `engines.comfyui` dans `features`
(et `engines`, `jobs`, `jobs.pause`, `inputs`, `files` pour ce que chaque fonction touche).
Ce qu'AIODOS ne sert pas encore rejette `NotSupported` même alors : `comfy.objectInfo()` et
`comfy.template()` (relais, `engines.comfyui.object_info`, `engines.comfyui.templates`),
`comfy.recipes()`, `comfy.recipeFor()` et `submit({ recipe })` (`recipes`), `file.link()`
(`files.link`). Si un AIODOS plus récent les annonce, cette version 0.2.0 rejette aussi
`NotSupported` (`detail.announced: true`) : il faut mettre à jour `aiodos-client`.

## Les faux serveurs (pour vos essais)

`aiodos-client/fake` sert le contrat partagé, sans réseau ni vraie installation :

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFakeClient } from 'aiodos-client/fake';

test('l’assistant répond par AIODOS', async () => {
  const { client, fake } = createFakeClient(); // clé d'essai déjà rangée ; key: null pour partir sans
  const r = await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }] });
  assert.equal(r.text, 'Bonjour !');
  assert.equal(fake.received('/v1/chat/completions').length, 1);
});
```

- `createFakeFetch(options)` : une fonction `fetch` (option `fetch` du client, ou à mettre dans
  `globalThis.fetch`) ; `startFakeServer(options)` : un vrai serveur `node:http` sur
  `127.0.0.1:0`.
- Le faux se pilote : `approve()`, `deny()`, `expire()`, `slowDown(n)`, `revokeKey()`,
  `failNext(code)`, `respondNext(réponse)`, `redirectNext()`, `nextChat(nom ou flux)`,
  `listening = false` (connexion refusée), `wrongProof = true` (un imposteur) ; `requests`,
  `received(chemin)` et `closed` disent ce qu'il a vu.
- Les travaux ComfyUI, en mémoire : par défaut, le faux annonce ce que dit l'info du contrat
  (sans les travaux) ; `features: FAKE_FEATURES` les annonce, et la clé d'essai a besoin du
  droit `engines:comfyui` (`scopes`). Il simule le ComfyUI de ce PC (`fake.comfy` : ses
  modèles, `enabled`), garde les entrées, les travaux et les fichiers par appli, et chaque
  sortie est un petit PNG (`FAKE_PNG`). Les travaux avancent seuls (`jobMode: 'auto'`, une
  étape toutes les 5 ms) ou à la main (`jobMode: 'manual'`, puis `step()`, `finish()`) ;
  `nextJob({ failUnits, filesPerUnit })` règle le prochain, `nextEvents({ cutAfter,
  stallAfter })` coupe ou fait taire le prochain flux d'événements, `cutStreams()` coupe ceux
  ouverts, `nextContent({ cutAt })` coupe le prochain contenu de fichier.

  ```js
  import { FAKE_FEATURES, createFakeClient } from 'aiodos-client/fake';
  const { client, fake } = createFakeClient({ fake: { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'] } });
  const job = await client.comfy.submit({ graph, inputs: { ref0: png } });
  await job.wait(); // completed : fake.jobs, fake.files, fake.received(…) disent le reste
  ```
- `writeFakeDiscovery({ state })` écrit un faux `gateway.json` dans un dossier temporaire (le
  vrai n'est jamais touché) ; `createFakeHome()` un faux `AIODOS_HOME` dont l'interpréteur est
  un script Node (faux programme d'aide).
- Les options `env` et `platform` du client remplacent `process.env` et `process.platform` pour
  les essais.

## Développer ce client

```sh
cd clients/js
npm test        # node --test : tous les fichiers test/*.test.mjs
npm pack --dry-run
```

`npm test` lance `node --test` sans argument : depuis Node 22, `node --test test/` prend le
dossier pour un fichier. `contract/` est une copie de `clients/contract/`, que le faux serveur
publié avec le paquet sert : `npm run sync-contract` la recopie à chaque changement du contrat
(un essai vérifie qu'elles sont identiques).

## Licence

Apache-2.0 (voir [LICENSE](LICENSE) et [NOTICE](NOTICE)). Une appli sous GPL-3.0, comme
ARKLIGHT PC, peut inclure ce client : Apache-2.0 est compatible avec la GPLv3, à condition de
garder les en-têtes de licence et le fichier NOTICE.
