# aiodos-client (Python)

<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- aiodos-client 0.1.0 -->

> **In English, in short.** `aiodos-client` is the zero-dependency Python library (3.10 to
> 3.14) that applications such as PANDORA use to talk to the local AIODOS gateway
> (`127.0.0.1:11435`). It finds AIODOS, reports a `status()` that never raises, gets a
> per-application key through the consent card shown in AIODOS (RFC 8628 device flow with a
> 6-digit code; through AIODOS's helper program when the gateway is closed), keeps it with
> DPAPI, checks AIODOS's identity proof before ever sending the key, and streams chat
> (OpenAI or Anthropic message formats, tools, images, reasoning) with real cancellation.
> Errors carry stable codes; messages come in French and English. `FakeAiodos` replays the
> shared contract for tests. Apache-2.0: it can be included in a GPL-3.0 application.

La bibliothèque cliente des applications pour la passerelle d'AIODOS. La référence de l'API
est [docs/api-applications.md](../../docs/api-applications.md) ; le contrat partagé avec la
passerelle et le client JavaScript, [clients/contract/](../contract/) ; le guide des
intégrateurs, [docs/integrer-aiodos.md](../../docs/integrer-aiodos.md).

## Installer

```
pip install aiodos-client==0.1.0
```

Aucune dépendance : la bibliothèque standard seulement. Pour PANDORA, épingler la version
**et l'empreinte** dans `requirements.txt`, puis installer avec `--require-hashes` :

```
aiodos-client==0.1.0 \
    --hash=sha256:<empreinte de la roue publiée sur PyPI>
```

L'empreinte se lit sur la page du paquet sur PyPI, ou se calcule sur la roue téléchargée
(`pip download aiodos-client==0.1.0 --no-deps -d roue`, puis `pip hash roue/*.whl`). On peut
aussi copier `src/aiodos_client/` dans l'appli, avec `LICENSE` et `NOTICE`.

Pour PyInstaller : tous les imports sont en tête de module, rien à ajouter dans
`hiddenimports`. Le faux serveur (`aiodos_client.fake`) n'est importé que par qui s'en sert.

## Exemple : PANDORA

Tout est bloquant : à appeler depuis un fil de travail (QThread). Les rappels (`on_text`,
`on_code`…) sont appelés dans ce fil-là ; seul `cancel` est interrogé par un fil de
surveillance (`QThread.isInterruptionRequested` et `threading.Event` sont sûrs entre threads).
Le client n'a pas d'état global et se partage entre threads.

```python
from aiodos_client import Aiodos, Keystore, errors

ai = Aiodos(app="PANDORA", app_id="arkane.pandora", version=VERSION, lang="fr",
            keystore=Keystore.dpapi(r"%LOCALAPPDATA%\PANDORA\aiodos.key"))  # hors de config.json

st = ai.status()                                     # ne lève jamais
show(st.message)                                     # message conseillé, en fr ou en en
if st.state == "not_running":                        # sur un clic de l'utilisateur seulement
    st = ai.launch()
if st.state != "ready":                              # bouton « Se connecter à AIODOS »
    ai.connect(scopes=["chat", "embed"], where=["here", "mine"],
               purpose="Traduire les prompts et décrire les images.",
               on_code=self.code.emit,               # « 482 197 », à afficher en grand
               cancel=self.isInterruptionRequested)

model = ai.models("chat").best(needs=["vision"])     # None si aucun ne convient
r = ai.chat(messages=msgs, model=model.id, system=SYS,          # blocs d'Anthropic acceptés
            max_tokens=min(4096, (model.context or 8192) // 3), think=False,
            on_text=self.chunk.emit, cancel=self.isInterruptionRequested)
r.text, r.truncated, r.finish_reason, r.stopped_by, r.usage, r.model, r.machine, r.place

vectors = ai.embed(["un chat noir", "un chien blanc"])            # list[list[float]]
ai.chat(lot, batch=True)                             # découpages : priorité basse
ai.disconnect()                                      # bouton « Se déconnecter d'AIODOS »
```

```python
try:
    r = ai.chat(...)
except errors.Cancelled:                  # l'utilisateur a arrêté (message « Arrêté »)
    ...
except errors.ContextTooLong as exc:      # exc.needed, exc.limit : découper
    ...
except errors.RateLimited as exc:         # Busy en est une sous-classe ; exc.retry_after
    ...
except errors.AiodosError as exc:         # exc.code, exc.message, exc.hint, exc.retryable
    humanize(exc.code)
```

## Le client

`Aiodos(app, app_id, version, keystore=None, lang="fr", url=None, timeout=240,
load_timeout=900)`

| Méthode | Rôle |
|---|---|
| `status()` | l'état (`Status`) ; ne lève jamais |
| `wait_ready(timeout=60, cancel=None)` | attend qu'AIODOS réponde ; rend l'état |
| `launch(wait=60, cancel=None)` | lance AIODOS en arrière-plan, sur un clic, puis attend qu'il réponde |
| `connect(scopes, where=None, purpose=None, on_code=None, cancel=None, timeout=600)` | demande l'accès, donne le code, relève et range la clé ; rend un `Access` |
| `request_access(scopes, where=None, purpose=None, cancel=None)` | la demande seule : `AccessRequest` (`user_code`, `wait(cancel, timeout)`) |
| `disconnect()` | retire la clé dans AIODOS, puis l'efface du coffre |
| `models(kind="chat")` | le catalogue (`ModelList` : `best(needs, prefer_local)`, `by_id`) ; `kind="embed"` |
| `chat(messages, model=None, …)` | une conversation, toujours en flux : `ChatResult` |
| `embed(texts, model=None)` | les vecteurs, sur ce PC |
| `open_in_aiodos(page, model=None)` | ramène la fenêtre d'AIODOS sur `applications`, `models`, `jobs` ou `gallery` |
| `comfy`, `jobs()`, `job(id)`, `inputs`, `engines()` | livraison 2 : `NotSupported` (voir plus bas) |

- **L'adresse** : `url`, puis `AIODOS_URL`, puis le fichier de découverte
  (`%LOCALAPPDATA%\AIODOS\gateway.json`), puis `http://127.0.0.1:11435`. Un hôte autre que
  `127.0.0.1`, `localhost` ou `[::1]` est refusé avant tout envoi (`NonLocalUrl`) ; aucun proxy
  n'est lu, aucune redirection n'est suivie.
- **La clé** : celle du coffre, sinon `AIODOS_KEY` (essais et développement seulement : une
  variable d'environnement passe aux programmes enfants ; le client la retire de
  l'environnement des programmes qu'il lance). Elle n'apparaît jamais dans un `repr`, un
  message d'erreur ou le journal.
- **La preuve d'identité** : avant d'envoyer la clé, le client fait signer un défi à AIODOS
  (§ 5.1 de la référence) ; fausse, l'état est `untrusted` et la clé n'est pas envoyée. Elle
  est refaite quand l'instance d'AIODOS change, après une erreur de connexion, et au-delà de
  60 s.
- **Journal** : le logger `aiodos_client`, avec un `NullHandler` ; jamais de `print`.

## Les états

`status().state`, avec `status().message` (le message conseillé) et, selon l'état, `version`,
`features`, `scopes`, `where`, `user_code`, `app_id`, `limits`.

| `state` | Quand | Message conseillé (fr) |
|---|---|---|
| `not_installed` | ni fichier de découverte, ni installation | AIODOS n'est pas installé sur ce PC. |
| `not_running` | installé, rien n'écoute ; `running: false` ; pid mort | AIODOS n'est pas lancé. |
| `gateway_closed` | AIODOS tourne, passerelle fermée | AIODOS tourne, mais n'est pas ouvert aux applications. |
| `gateway_error` | `gateway.state: error` | le message d'AIODOS |
| `apps_closed` | ouverte à la clé générale seulement | AIODOS n'est ouvert qu'aux autres outils. |
| `not_aiodos` | le port répond, mais pas comme AIODOS | Le port est pris par un autre programme. |
| `too_old` | ancienne version d'AIODOS | Mets AIODOS à jour. |
| `untrusted` | preuve fausse | Le programme qui répond ne connaît pas la clé : elle n'a pas été envoyée. |
| `headless` | AIODOS sans fenêtre, pas de clé | AIODOS tourne sans fenêtre : ouvre-le pour autoriser l'appli. |
| `requests_closed` | nouvelles demandes refusées, pas de clé | AIODOS n'accepte pas de nouvelle application en ce moment. |
| `needs_access` | pas de clé, ou clé inconnue | L'appli n'a pas encore accès à AIODOS. |
| `pending` | demande en cours | Autorise l'appli dans la fenêtre d'AIODOS. Code : 482 197 |
| `denied` | la demande a été refusée | L'accès a été refusé dans AIODOS. |
| `revoked` | clé retirée ou expirée | L'accès a été retiré (ou a expiré) dans AIODOS. |
| `other_account` | AIODOS sous un autre compte Windows | AIODOS tourne sous un autre compte Windows. |
| `ready` | clé valide | Connecté à AIODOS. |

Les textes sont dans `aiodos_client.texts` (`STATES`, `ERRORS`, `HINTS`, en `fr` et en `en`),
les mêmes que ceux du client JavaScript (un essai y veille). Une appli qui traduit elle-même
part de `state` et de `code`, jamais du texte.

## L'accès

- `connect()` passe par `POST /aiodos/v1/access/requests` quand la passerelle est ouverte aux
  applications, sinon (fermée, ouverte à la clé générale seulement, AIODOS arrêté) par le
  programme d'aide d'AIODOS : `<racine>\runtime\python.exe -m aiodos --app-request`, depuis
  `<racine>\app`, sans shell ni fenêtre, la demande sur son entrée standard. La racine vient
  de `AIODOS_HOME`, sinon du registre (`…\Uninstall\{8E1BB2BE-7BA8-4AF2-ACC7-463BA169FCF3}_is1`,
  `InstallLocation`, HKCU puis HKLM), sinon de `%LOCALAPPDATA%\Programs\AIODOS`. Une copie de
  développement (`AIODOS_HOME` qui contient `aiodos\__init__.py`) se lance avec
  `AIODOS_PYTHON` ; sans lui, rien n'est lancé.
- `on_code(code)` reçoit le code à 6 chiffres ; puis la relève interroge AIODOS toutes les
  `interval` secondes (5 s de plus à chaque `slow_down`), en tolérant la connexion refusée
  tant que la passerelle n'écoute pas, jusqu'à `expires_in`.
- L'`Access` rendu donne les droits et les cercles **accordés** (l'utilisateur a pu en
  décocher). Déjà connectée avec ces droits, `connect()` rend l'accès actuel sans rien
  demander ; avec des droits en plus, la même clé les reçoit (`Access.upgraded`).
- Refus : `AccessDenied` ; demande expirée ou inconnue : `AccessExpired` ; arrêt :
  `Cancelled`. Après un `Revoked`, une seule nouvelle demande, sur un clic de l'utilisateur.
- **Les coffres** : `Keystore.dpapi(chemin)` (Windows ; chiffrée pour le compte de
  l'utilisateur, avec une entropie propre à l'appli), `Keystore.file(chemin)` (hors de
  Windows ; fichier en 0600), `Keystore.memory()`, ou tout objet à `get()`, `set(clé)`,
  `delete()`. Une clé illisible (autre compte, autre machine) se lit comme absente.

## La conversation

`chat(messages, model=None, system=None, tools=None, think=None, max_tokens=None,
temperature=None, top_p=None, stop=None, seed=None, response_format=None, where=None,
batch=False, require_tools=True, on_text=None, on_thinking=None, on_loading=None,
cancel=None, timeout=None)`

- **Toujours en flux.** `timeout` (240 s par défaut) compte l'inactivité, remise à zéro à
  chaque morceau et à chaque commentaire `: aiodos loading N` (qui appelle `on_loading(N)`).
  Avant le premier morceau, `load_timeout` (900 s) vaut si le modèle n'est pas connu comme
  chargé (d'après le dernier `models()`).
- **Annulation réelle** : dès que `cancel()` devient vrai, la connexion est fermée ; AIODOS
  voit l'appli partir et arrête le calcul ; `chat()` lève `Cancelled` dans les deux ou trois
  dixièmes de seconde, même pendant le chargement du modèle.
- **Messages** : le format d'OpenAI, ou les blocs d'Anthropic (`text`, `image` en base64,
  `tool_use`, `tool_result`, la réflexion étant retirée) ; `system` en texte ou en blocs. Les
  images en octets, en data-URL ou en base64 (`image_url`, bloc `image`, ou une liste
  `images` dans le message), jamais par adresse : rien n'est téléchargé. Les outils au format
  d'OpenAI ou d'Anthropic (`input_schema`).
- `model` absent : le meilleur modèle permis (`tools` si `tools`, `vision` s'il y a des
  images), sinon `ModelNotFound`. `think=False` coupe la réflexion (`reasoning_effort:
  "none"`) ; `where` resserre le lieu de calcul (`X-Aiodos-Where`) ; `batch=True` baisse la
  priorité ; `require_tools=False` accepte une réponse sans outils plutôt que
  `ToolsUnavailable`.
- `ChatResult` : `text`, `thinking`, `tool_calls` (`ToolCall(id, name, arguments, raw)`, les
  arguments rassemblés par index et analysés en JSON), `finish_reason`, `stopped_by` (`stop`,
  `max_tokens`, `context`, `tool_calls`), `usage`, `model`, `machine`, `place`, `truncated`,
  `context_length`. Une erreur en cours de route lève la bonne exception.

## Les erreurs

Toutes dérivent de `AiodosError(code, status, message, hint, retryable, detail,
retry_after)` ; `errors.from_response()` lit les trois formes de la passerelle (enveloppe
d'OpenAI avec son objet `aiodos`, forme d'Ollama, forme d'OAuth).

| Classe | Codes |
|---|---|
| `NotInstalled`, `NotRunning`, `Unreachable` (« Connexion refusée… ») | `not_installed`, `not_running`, `unreachable` |
| `GatewayClosed`, `AppsClosed` | `gateway_closed`, `apps_closed` |
| `NotAiodos`, `TooOld`, `Untrusted`, `NonLocalUrl`, `ProtocolError` | `not_aiodos`, `too_old`, `untrusted`, `non_local_url`, `protocol` |
| `Timeout`, `Cancelled` (« Arrêté ») | `timeout`, `cancelled` |
| `NeedsAccess`, `AccessDenied` (`BlockedProgram`), `AccessExpired`, `Revoked` | `needs_access`, `invalid_api_key`, `access_denied`, `blocked_program`, `expired_token`, `invalid_grant`, `key_revoked`, `key_expired` |
| `RequestsClosed`, `ApprovalUnavailable` | `requests_closed`, `approval_unavailable` |
| `ScopeDenied` (`.scope`), `WhereDenied` (`.allowed`), `ProgramMismatch`, `OtherAccount`, `OriginRefused`, `HostRefused` | `scope_denied`, `where_denied`, `program_mismatch`, `other_account`, `origin_refused`, `host_refused` |
| `InvalidRequest` : `ContextTooLong` (`.needed`, `.limit`), `ToolsUnavailable`, `VisionUnsupported`, `LocalOnly`, `Unsupported`, `UnsupportedMediaType` | `invalid_request`, `context_length_exceeded`, `tools_unavailable`, `vision_unsupported`, `local_only`, `unsupported_parameter`, `unsupported_media_type` |
| `ModelNotFound`, `NoMachine`, `FileTooLarge`, `NotSupported` | `model_not_found`, `no_machine`, `file_too_large`, `not_supported` |
| `RateLimited` (`Busy`), `EngineUnavailable`, `PeerFailed` (`EngineFailed`) | `rate_limited`, `busy`, `engine_unavailable`, `peer_failed`, `engine_failed` |

Un code inconnu de cette version donne un `AiodosError` qui le porte tel quel.

## La livraison 2

`ai.comfy` (`plan`, `object_info`, `template`, `recipes`, `recipe_for`, `tokenize`,
`submit`, `run`), `ai.jobs()`, `ai.job(id)` et `Job`, `File`, `ai.inputs.put()`,
`ai.engines()` existent déjà : tant que `features` n'annonce pas `engines.comfyui`, `jobs`,
`inputs`, `files` ou `engines`, ils lèvent `NotSupported` sans rien essayer. Le repli de
l'appli (le ComfyUI de ce PC) se branche sur cette erreur, et son code n'aura pas à changer.
Si AIODOS les annonce déjà, c'est ce client qui est à mettre à jour : `NotSupported` aussi,
avec ce conseil (`detail["client"]`).

## Le faux AIODOS, pour les essais

`aiodos_client.fake.FakeAiodos` : un `http.server` sur `127.0.0.1`, sur un port libre, dans
un fil, qui sert le contrat. Il écrit un faux `gateway.json` dans un dossier temporaire et ne
touche jamais au vrai, ni au réseau.

```python
import os
from aiodos_client import Aiodos
from aiodos_client.fake import FakeAiodos

with FakeAiodos(interval=0.05) as fake:
    os.environ.update(fake.environ())                # AIODOS_DISCOVERY_FILE
    ai = Aiodos(app="PANDORA", app_id="arkane.pandora", version="3.2.0")
    # dans un autre fil : fake.wait_request(), puis fake.approve() / deny() / expire()
    ...
    fake.fail_next("busy", retry_after=2)            # la prochaine requête : 429 busy
    fake.chat_script([": aiodos loading 3", 0.5, *lignes, "data: [DONE]"])
    fake.close_gateway(); fake.open_gateway()        # connexion refusée, puis rouverte
    home = fake.helper_home()                        # faux programme d'aide (AIODOS_HOME)
```

- `FakeAiodos(auto_approve=True)` autorise toute demande ; `impostor=True` ne sait pas
  prouver son identité ; `mode="old"` ou `"ollama"` imite une ancienne version ou Ollama ;
  `apps`, `requests`, `approval_ui`, `other_account`, `features` règlent le reste.
- `add_key()`, `revoke(clé)`, `expire_key(clé)` ; `override(chemin, réponse)` sert une
  réponse du contrat telle quelle.
- Il garde ce qu'il reçoit : `log`, `seen_keys` (même refusées), `token_polls`,
  `stream_closed` (le client a fermé un flux).

## Essais

```
py -3.14 -m pytest clients/python/tests -q
```

Ils rejouent le contrat (`clients/contract/`), le vecteur de la preuve, tous les états de
`status()`, `connect()` passerelle ouverte et fermée (avec un faux programme d'aide), les
refus d'hôtes non locaux et de redirection, l'annulation réelle d'un flux, l'absence de la
clé dans les `repr`, les messages et le journal, le coffre DPAPI (Windows) et le fichier. Ils
ne voient jamais le vrai AIODOS.

## Licence

Apache-2.0 (`LICENSE`, `NOTICE`), comme AIODOS. Une appli sous GPL-3.0, comme PANDORA, peut
l'inclure (l'Apache-2.0 est compatible avec la GPL-3.0, dans ce sens-là) : garder `LICENSE`
et `NOTICE` avec les fichiers copiés. L'inverse est exclu : aucune ligne de PANDORA ni
d'ARKLIGHT n'entre dans ce client.
