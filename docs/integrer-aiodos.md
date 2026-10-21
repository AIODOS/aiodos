# Intégrer AIODOS à une application

> **In English, in short.** AIODOS exposes a local gateway on `127.0.0.1:11435` (closed by
> default). Each application gets its own key through a consent card shown in AIODOS (an
> RFC 8628 device-flow: the app shows a 6-digit code, the user compares and approves). The app
> then talks OpenAI-compatible chat/embeddings (`/v1/*`) plus a small `/aiodos/v1/*` API, and
> AIODOS decides which machine computes — this PC, the user's paired machines, friends — within
> the circles the user granted. ComfyUI jobs (API-format graphs) are replayed on the ComfyUI of
> this PC and of the user's other machines, and the files are kept for the app. Use the
> zero-dependency clients `aiodos-client` (Python, npm), Apache-2.0. Reference:
> [api-applications.md](api-applications.md).

Ce guide s'adresse à qui branche une application sur AIODOS : PANDORA, ARKLIGHT PC, ou toute
autre. La référence, route par route, est [api-applications.md](api-applications.md) ; les
bibliothèques clientes sont dans `clients/` (leurs README donnent chaque fonction).

## 1. Les garanties

- **Rien n'écoute sans un geste de l'utilisateur.** La passerelle est fermée par défaut ;
  c'est le premier accord donné à une appli, dans AIODOS, qui l'ouvre aux applications.
- **127.0.0.1 seulement** : aucune autre machine, aucun téléphone, aucune page web ne l'atteint
  (l'hôte, l'origine et les en-têtes d'un navigateur sont refusés ; pas de CORS).
- **Une appli, une clé**, accordée dans AIODOS après comparaison d'un code à 6 chiffres. Seul
  son haché est gardé ; elle se retire d'un clic, avec effet immédiat.
- **Les droits par capacité** (conversation, embeddings, bibliothèques, travaux ComfyUI) et
  **les cercles** où
  ses calculs peuvent se faire (ce PC, mes machines, mes amis, les inconnus du réseau public) :
  l'utilisateur peut décocher, jamais l'appli ajouter. Une requête peut resserrer, jamais
  élargir.
- **Jamais le cloud** par la passerelle, quelle que soit la clé ; **aucun téléchargement** :
  un modèle absent renvoie vers AIODOS, où l'utilisateur l'installe et en accepte la licence.
- **Aucun contenu au journal** : il note qui a appelé quoi, quand, et qui a calculé.
- **Ce qu'AIODOS ne protège pas**, franchement : un programme malveillant qui tourne sous le
  même compte Windows lit les fichiers, et donc la clé d'une appli, où qu'elle soit. La clé
  par appli sert à savoir qui fait quoi, à limiter les droits, à retirer une appli sans
  toucher aux autres, et à fermer la porte aux autres comptes, aux pages web et au réseau.

## 2. En 5 minutes

**Avec le client Python** (`pip install aiodos-client`, sans dépendance) :

```python
from aiodos_client import Aiodos, Keystore

ai = Aiodos(app="Mon appli", app_id="moi.monappli", version="1.0.0",
            keystore=Keystore.dpapi(r"%LOCALAPPDATA%\MonAppli\aiodos.key"))
if ai.status().state != "ready":
    ai.connect(scopes=["chat"], purpose="Résumer mes notes",
               on_code=lambda code: print("Code à vérifier dans AIODOS :", code))
r = ai.chat(messages=[{"role": "user", "content": "Bonjour"}], think=False)
print(r.text, "— calculé par", r.machine)
```

**Avec le client JavaScript** (`npm i aiodos-client`, Node 20.3 et plus, jamais dans un rendu
de navigateur) :

```js
import { createClient } from 'aiodos-client';
const aiodos = createClient({ app: 'Mon appli', appId: 'moi.monappli', version: '1.0.0',
  key: { get: () => store.get('aiodos'), set: (k) => store.set('aiodos', k) } });
if ((await aiodos.status()).state !== 'ready')
  await aiodos.connect({ scopes: ['chat'], purpose: 'Assistant', onCode: (c) => show(c) });
const r = await aiodos.chat({ messages: [{ role: 'user', content: 'Bonjour' }], think: false });
```

**Avec le SDK d'OpenAI et une clé « à la main »** (Réglages → Applications et autres outils →
« Ajouter un outil » ; la clé est montrée une fois) :

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:11435/v1", api_key="aiodos_app_…",
                default_headers={"X-Aiodos-Where": "here"})
client.chat.completions.create(model="qwen3.5:9b", stream=True,
                               messages=[{"role": "user", "content": "Bonjour"}])
```

Une clé à la main n'a ni la preuve d'identité ni l'accord par code : pour une application,
préférer les clients.

## 3. Trouver AIODOS

- L'adresse : `http://127.0.0.1:11435` par défaut ; le fichier de découverte
  `%LOCALAPPDATA%\AIODOS\gateway.json` dit si la passerelle est ouverte, fermée ou en erreur,
  et où elle s'ouvrira. Jamais de clé dedans, et les clients n'exécutent rien qui en vienne.
- `status()` ne lève jamais d'erreur et rend un état parmi ceux du § 6 de la référence
  (`not_installed`, `not_running`, `gateway_closed`, `needs_access`, `ready`…), avec un
  message conseillé en français et en anglais. À montrer tel quel, avec le bouton qui va avec.
- **Ne demandez jamais l'accès au premier lancement de votre appli** : faites-le au moment où
  l'utilisateur active la fonction qui en a besoin, par un bouton « Se connecter à AIODOS ».

## 4. Obtenir l'accès

1. `connect(scopes, where, purpose)` envoie la demande : directement si la passerelle est
   ouverte aux applis ; sinon par le programme d'aide d'AIODOS (`python -m aiodos
   --app-request`), qui lance AIODOS au besoin et transmet la demande par son canal privé.
2. Votre appli reçoit un **code à 6 chiffres** : affichez-le **en grand**. AIODOS montre une
   carte avec le même code, le nom de votre appli, le programme vu par Windows, les droits et
   les cercles demandés.
3. L'utilisateur autorise (en décochant ce qu'il veut) ou refuse. Le client relève la clé
   (une seule fois) et la range dans votre coffre.
- **Droits en plus** : le même `connect` avec la clé en place ; la carte montre la différence,
  la même clé reçoit les nouveaux droits.
- **Se déconnecter** : `disconnect()` retire l'accès dans AIODOS et efface la clé.
- **Sur `key_revoked`** : une seule nouvelle demande, après un clic de l'utilisateur, jamais
  en boucle (AIODOS met en sourdine une appli qui insiste).

## 5. Garder la clé

- Python : `Keystore.dpapi(chemin)` (chiffrée pour le compte Windows) ; ailleurs, un fichier
  lisible par soi seul. JavaScript (Electron) : `safeStorage`, dans le processus principal.
- Jamais dans Documents, OneDrive, un fichier de configuration partagé, un journal ou une
  adresse. Le client ne l'envoie qu'après la **preuve d'identité** d'AIODOS (un défi que seul
  le vrai AIODOS sait signer) : un faux serveur sur le port ne la reçoit pas.

## 6. Droits, cercles, priorité

| Cercle | Conversation | Outils, images jointes | Embeddings |
|---|---|---|---|
| `here` (ce PC) | oui | **oui, seulement ici** | **seulement ici** |
| `mine` (mes machines) | oui (prêt de modèles) | non : 400 `tools_unavailable` | — |
| `friends` (mes amis) | oui, si l'utilisateur l'a permis dans AIODOS | non | — |
| `public` (inconnus) | pas encore (confirmé à son arrivée) | — | — |

- Décision de l'auteur d'AIODOS (20 et 21/10/2026) : les quatre cercles sont proposés cochés à
  l'accord, avec la phrase qui dit que le contenu part chez la machine qui calcule, le temps du
  calcul. **Pour des données sensibles** (photos de personnes, amorces privées), demandez
  `where: ["here", "mine"]`, ou envoyez `X-Aiodos-Where: here` sur la requête.
- Chaque réponse dit qui a calculé : en-têtes `X-Aiodos-Model`, `X-Aiodos-Machine`,
  `X-Aiodos-Place`, et l'objet `aiodos` du dernier morceau. **Affichez-le** à l'utilisateur.
- Priorité : `interactive` par défaut (comme une conversation d'AIODOS) ; pour un lot
  (découpage, traduction de 200 lignes), `batch=True` / `X-Aiodos-Priority: background`.

## 7. Conversation

- Choisissez le modèle d'après le catalogue (`models(kind="chat")`) : `context` (le contexte
  réel du moteur, 16 384 jetons par défaut sur une carte de 8 Go et plus), `tools`, `vision`,
  `thinking`. Plafonnez `max_tokens` et la taille de vos tranches d'après `context`.
- Toujours en flux (les clients le font) ; `think=False` (`reasoning_effort: "none"`) pour
  une réponse sans réflexion, conseillé pour les tâches d'une appli.
- Outils et images : calculés par ce PC. Si le modèle n'y est pas, 400 `tools_unavailable` ou
  `local_only`, jamais une réponse sans outils en silence ; `require_tools=False`
  (`X-Aiodos-Tools: optional`) accepte une réponse en texte d'une autre machine.
- Un texte trop long : 400 `context_length_exceeded` avec `needed` et `limit` quand le moteur
  les donne ; une réponse coupée par le contexte finit par `finish_reason: "length"` et
  `stopped_by: "context"`.
- Pendant qu'un modèle se charge, le flux porte `: aiodos loading <s>` : les clients remettent
  leur délai d'inactivité à zéro.

## 8. Travaux ComfyUI (images, vidéos)

AIODOS ne remplace pas votre ComfyUI : il rejoue **vos graphes, au format API**, dans le
ComfyUI de ce PC et dans celui des autres machines de l'utilisateur qui l'ont (chacun accordé
dans AIODOS, Réglages → Moteurs des applications), plusieurs unités en même temps, et garde
les fichiers pour vous jusqu'à ce que vous les récupériez. Droit : `engines:comfyui`.

```python
ai.connect(scopes=["chat", "engines:comfyui"], purpose="Textes, moods et vidéos")
api, inputs = ai.comfy.tokenize(graph_api, {ref_node: png_bytes})    # @in: dans LoadImage
job = ai.comfy.submit(graph=api, inputs=inputs,
                      items=[{"seed": 1201 + i, "set": [[text_node, "text", p]]}
                             for i, p in enumerate(prompts)],
                      label=f"Mood · {plan}", client_ref=f"pandora:mood:{uid}")
for ev in job.watch(cancel=self.isInterruptionRequested):
    self.progress.emit(ev.job.progress.percent or 0, ev.job.progress.message)
paths = job.download(dest_dir, stem=plan)
```

- **Une unité par image** (`items`, ou `repeat` pour des variations) : AIODOS ne coupe pas
  un graphe, il répartit les unités entre les machines.
- **Les fichiers ne se donnent que par jeton** : `@in:<id>` (l'entrée envoyée à part, par
  empreinte), `@seed`, `@model:<clé>` ; par défaut AIODOS relève lui-même les noms de modèles.
  Un chemin, une adresse ou une annotation de dossier dans une valeur est refusé avant la mise
  en file (`forbidden_value`).
- **Retrouver ses travaux** après un redémarrage de l'appli ou d'AIODOS : `ai.jobs(active=None)`,
  ou le même `client_ref` (qui rend le même travail) ; récupérez les fichiers pas encore
  récupérés. Un fichier récupéré est gardé encore 7 jours ; un fichier jamais récupéré n'est
  jamais effacé seul.
- **Pause** après l'unité en cours (rien ne se perd) ou tout de suite ; **annulation** :
  AIODOS n'arrête dans ComfyUI que ce calcul-là, jamais ceux de l'utilisateur.
- **Où** : ce PC et mes machines dans cette version (les amis et les inconnus viendront, sur
  recettes) ; une autre machine ne reçoit que l'ordre de calcul, ni le nom de votre appli, ni
  le libellé, ni `client_ref`. Pour une photo de personne, `sensitive=True` (ce PC et mes
  machines seulement, aussi plus tard).
- Avant de déposer, `ai.comfy.plan(...)` dit où le travail peut tourner et ce qui manque
  (`missing` : un modèle absent, et la machine).

## 9. Les erreurs

Réagissez au **code**, jamais au message (fait pour l'utilisateur, en français ou en anglais
selon `Accept-Language`). La table complète est au § 3 de la référence ; les plus courants :

| Code | Que faire |
|---|---|
| `needs_access`, `invalid_api_key` | proposer « Se connecter à AIODOS » |
| `key_revoked`, `key_expired` | dire que l'accès a été retiré ; redemander sur un clic |
| `scope_denied` | demander le droit en plus (`connect` avec ce droit) |
| `where_denied` | resserrer `where`, ou le demander dans AIODOS |
| `tools_unavailable`, `local_only`, `vision_unsupported` | choisir un modèle de ce PC qui sait le faire |
| `model_not_found`, `no_machine` | proposer « L'installer dans AIODOS » (`open_in_aiodos("models")`) |
| `rate_limited`, `busy` | attendre `Retry-After` |
| `engine_unavailable`, `engine_failed`, `peer_failed` | réessayer un peu plus tard (travail ComfyUI : ComfyUI n'est ouvert et accordé sur aucune machine permise) |
| `model_unavailable` | dire quel modèle manque et où (`detail.models`, `detail.machine`) |
| `graph_not_api`, `graph_invalid`, `patch_invalid`, `forbidden_value`, `forbidden_node` | corriger le graphe (format API, jetons) : ce n'est pas à réessayer |
| `quota_exceeded` | récupérer ou supprimer des fichiers et des travaux finis |
| `unreachable` | « AIODOS n'est pas lancé » (son message contient « Connexion refusée ») |

## 10. Essayer sans AIODOS

Les deux clients ont un faux serveur qui sert le contrat partagé (`clients/contract/`) :
`FakeAiodos` en Python, `createFakeFetch()` et `startFakeServer()` en JavaScript. Ils simulent
l'accord, le refus, le retrait, un flux avec outils, une erreur en route. Ils ne touchent ni au
réseau, ni au vrai fichier de découverte.

## 11. Licences et versions

- Les clients sont sous **Apache-2.0** : une appli sous GPL-3.0 peut les inclure (en gardant
  `LICENSE`, `NOTICE` et les en-têtes) ; l'inverse est interdit (aucune ligne d'une appli GPL
  n'entre dans les clients).
- Les licences des modèles : affichez celle du catalogue ; une licence restrictive s'accepte
  dans AIODOS, par l'utilisateur.
- **Lisez `features`** (`GET /aiodos/v1/info`) : une fonction absente lève `NotSupported`.
  Ne comparez jamais de numéros de version. Les travaux ComfyUI (`engines.comfyui`, `jobs`,
  `inputs`, `files`) demandent un client 0.2.0 ou plus : le 0.1.0 lève `NotSupported`, avec le
  conseil de le mettre à jour.

## 12. Liste de contrôle

- [ ] `127.0.0.1` seulement, sans proxy ni redirection (les clients le font).
- [ ] Droits minimaux ; `here` pour les données sensibles.
- [ ] Le code à 6 chiffres affiché en grand pendant la demande ; un bouton « Annuler ».
- [ ] La clé dans un coffre (DPAPI, `safeStorage`), jamais ailleurs.
- [ ] Qui a calculé, affiché ; les licences restrictives aussi.
- [ ] **Ne pas devenir un relais ouvert** : si votre appli a un serveur (WebSocket…), ne
  laissez pas un appareil du réseau se servir d'AIODOS à travers elle.
- [ ] Dire à l'utilisateur ce que votre appli envoie, elle, à ses propres services en ligne.
- [ ] Annuler ce que l'utilisateur abandonne (les clients coupent vraiment le flux).
- [ ] Sur `key_revoked`, une seule nouvelle demande, après un clic.

## 13. Recettes

**PANDORA** (Python, PyQt) :
- `requirements.txt` : `aiodos-client==0.2.*`, épinglé par empreinte (`--require-hashes`).
- `core/aiodos.py`, sans Qt : le client, l'état en cache, `connect` appelé depuis un QThread
  (`on_code` émet un signal ; `cancel=self.isInterruptionRequested`).
- Un moteur « aiodos » pour translate, enhance, video_prompt, la vision et les conversations
  courtes ; `max_tokens` et les tranches du découpage plafonnés d'après `context` ; les lots en
  `batch=True` ; toujours `think=False`.
- La clé dans `%LOCALAPPDATA%\PANDORA\aiodos.key` (DPAPI) ; `config.json` ne garde que
  `aiodos_url` et `aiodos_where`.
- Les erreurs par `exc.code` ; le conseil devient « Réglages → Applications et autres outils ».
- Les images et vidéos de PANDORA restent calculées par ComfyUI : `core/comfy_transport.py`
  offre deux transports aux mêmes fonctions, « ComfyUI de ce PC » (celui d'aujourd'hui) et
  « Mes machines par AIODOS » (`comfy.submit`, § 8), choisi par un réglage. Le transport
  AIODOS corrige au passage trois pièges : plus d'interruption globale de ComfyUI, des noms de
  fichiers neutres, un clip de plus de 100 Mo refusé avant l'envoi. `sensitive=True` pour les
  fiches de casting et les visages de référence.

**ARKLIGHT PC** (Node.js, Electron) :
- D'abord, réserver les messages `ai.*` de son serveur aux clients de la machine elle-même
  (`req.socket.remoteAddress` local) : sinon un appareil du réseau se servirait d'AIODOS à
  travers ARKLIGHT.
- `npm i aiodos-client` (verrouillé par `package-lock.json`) ; un fournisseur « AIODOS (ce
  PC) » à côté des autres, dont la clé s'obtient par le bouton « Se connecter à AIODOS ».
- `connect({ scopes: ['chat'], where: ['here'] })` pour l'assistant lumière : outils et vision
  sur ce PC ; `pickModel(models, { needs: ['tools'], vision: true, preferLocal: true })`.
- La clé par `safeStorage` dans le processus principal ; jamais dans le rendu.
