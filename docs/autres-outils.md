# Brancher d'autres outils sur AIODOS

AIODOS peut s'ouvrir aux autres outils d'IA de ta machine : Open WebUI, AnythingLLM, Continue
(dans VS Code ou JetBrains), un éditeur de code, un script, ou un assistant qui parle MCP
(Claude Desktop, Claude Code…). Ils y trouvent alors :

- **les modèles de cette machine et ceux que te prêtent tes machines appairées**, par une API
  compatible OpenAI et une API compatible Ollama, avec le même choix automatique de la machine
  qui répond que dans les conversations d'AIODOS ;
- **tes bibliothèques**, par un serveur MCP : l'assistant y cherche les passages utiles à sa
  réponse, ou fait répondre un modèle du cercle.

Les formats ont été vérifiés le 17/10/2026 dans la documentation d'OpenAI, celle d'Ollama
(0.40.5), la spécification MCP (2026-07-28) et le code d'Open WebUI 0.12.3, AnythingLLM 1.18,
Continue, et des clients officiels openai 3.33.0 et mcp 2.3.0, avec lesquels AIODOS est essayé
(`tests/test_gateway.py`, `tests/test_gateway_nodes.py`).

## 1. Ouvrir la passerelle

**Réglages → Autres outils** :

1. active **« Ouvrir AIODOS aux autres outils »** (désactivé par défaut) ;
2. copie la **clé d'accès** (bouton « Copier ») : c'est la « clé d'API » à donner à l'outil ;
3. copie l'**adresse** qui convient à l'outil :

| Pour un outil qui parle… | Adresse |
|---|---|
| l'API d'OpenAI | `http://127.0.0.1:11435/v1` |
| l'API d'Ollama | `http://127.0.0.1:11435` |
| MCP (Streamable HTTP) | `http://127.0.0.1:11435/mcp` |

Le port **11435** (celui d'Ollama + 1) est fixe : l'adresse ne change pas d'un lancement à
l'autre, contrairement à celle de l'interface d'AIODOS. Il se change dans la même carte, sans
relancer AIODOS.

**Sécurité.** La passerelle n'écoute que sur `127.0.0.1` : seuls les programmes de cette
machine la joignent, jamais une autre machine du réseau. Chaque requête doit porter la clé
(`Authorization: Bearer <clé>`, ou `X-Api-Key: <clé>`) ; seule la racine `/` répond sans elle
(« Ollama is running », pour les outils qui vérifient qu'un serveur répond). Une page web
ouverte dans un navigateur ne peut pas s'en servir : AIODOS refuse les requêtes venues d'un
autre site (en-tête `Origin`) et ne donne pas d'en-têtes CORS. La clé reste la même d'un
lancement à l'autre ; **« Renouveler »** en crée une nouvelle, et l'ancienne cesse aussitôt
de marcher.

**Ce qui part, et où.** Une conversation envoyée par un outil est calculée par cette machine
ou par une de tes machines (voir § 2). Elle ne passe chez un ami que si tu désignes sa machine
(`peer:…`) ou si tu as accepté ses machines en mode automatique : elle y passe alors le temps de
la réponse, sans y être enregistrée. Les passages qu'un assistant MCP trouve dans tes
bibliothèques partent vers lui, puis vers *son* modèle : si c'est un modèle dans le cloud
(Claude, GPT…), ces passages y partent aussi. C'est pourquoi les bibliothèques gardées sur ce
PC (le cadenas) ne sont jamais montrées aux assistants.

## 2. Les modèles et la machine qui répond

La liste des modèles (`/v1/models`, `/api/tags`) donne :

| Identifiant | Qui répond |
|---|---|
| `qwen3.5:9b` (le nom seul) | **choix automatique** : cette machine si elle a le modèle et qu'elle est libre (sauf s'il n'y tient pas en mémoire vidéo alors qu'une autre machine libre l'y fait tenir), sinon la moins occupée de tes machines qui le prêtent |
| `peer:<machine>:qwen3.5:9b` | cette machine appairée précisément (une des tiennes, ou celle d'un ami qui te prête ses modèles) |
| `local:qwen3.5:9b` | cette machine seulement (accepté, pas listé) |
| `reparti:qwen3.6` | un gros modèle réparti entre tes machines ([gros-modeles.md](gros-modeles.md)), tel quel |

Les machines d'un ami ne répondent d'office que si tu l'acceptes (Réglages → Conversation,
« faire aussi répondre les machines de mes amis »). Les modèles du cloud ne passent pas par
la passerelle : leur envoi se valide dans AIODOS, message par message.

Une demande calculée par **cette machine** est transmise telle quelle à Ollama : les outils
(*tool calling*), les images, un format de réponse imposé, la complétion de code y passent.
Une **machine appairée**, elle, ne reçoit que la conversation en texte. D'où :

- images, format imposé (`format`, `response_format`), complétion de code (`suffix`,
  `/v1/completions`), probabilités (`logprobs`) : **cette machine seulement** ;
- outils : cette machine d'abord ; si seule une machine appairée a le modèle, elle répond
  sans eux, en texte ;
- embeddings : **cette machine seulement** (son Ollama) ;
- réglages d'échantillonnage (température, `max_tokens`, `stop`…) : transmis ; ceux qui
  rechargeraient le modèle chez l'autre (`num_ctx`…) restent à ses valeurs.

## 3. Open WebUI

Le plus simple est de l'installer **directement sur la machine** (Python 3.11 conseillé, 3.12
possible, pas 3.13) :

```
pip install open-webui
open-webui serve
```

puis d'ouvrir http://localhost:8080.

**Dans Docker** : la passerelle n'écoute que sur `127.0.0.1`. Avec Docker Desktop (Windows,
macOS), un conteneur joint en principe les services de `127.0.0.1` de la machine par le nom
`host.docker.internal`, que la passerelle accepte : remplace alors `127.0.0.1` par
`host.docker.internal` dans les adresses ci-dessous (pas encore essayé : Docker n'est pas
installé sur la machine où AIODOS est mis au point). Avec Docker sous Linux, un conteneur ne
joint pas `127.0.0.1` de la machine : installe alors Open WebUI directement.

Ensuite, dans **Settings → Admin → Connections** (Réglages → Admin → Connexions) :

- **API OpenAI** (« Manage OpenAI API Connections », « ➕ Add Connection ») : URL
  `http://127.0.0.1:11435/v1` (sans `/` à la fin), API Key = la clé d'AIODOS ; « Verify
  Connection » demande la liste des modèles ;
- ou **API Ollama** (« Manage Ollama API Connections ») : URL `http://127.0.0.1:11435`,
  Auth « Bearer », API Key = la clé d'AIODOS.

Les modèles du cercle apparaissent dans la liste de conversation ; un modèle d'une machine
appairée s'y lit « qwen3.6 · PC fixe ». Pour la recherche dans ses propres documents, Open WebUI
peut prendre le modèle d'embedding de cette machine (Admin → Documents, moteur « Ollama », même
URL et même clé). Open WebUI parle aussi MCP (§ 7). Ses « connexions directes », faites depuis
le navigateur, ne marchent pas (pas de CORS, voir § 1).

## 4. AnythingLLM

AnythingLLM Desktop, ou le serveur installé sur la machine (dans Docker, voir la remarque sur
Docker au § 3). Les libellés ci-dessous sont ceux de la version 1.18, en anglais même dans
l'interface française :

- **Modèle de conversation** : Settings → AI Providers → LLM (« Préférence LLM »), fournisseur
  **« Generic OpenAI »** : « Base URL » `http://127.0.0.1:11435/v1`, « API Key » = la clé
  d'AIODOS, « Selected Model » = un identifiant du § 2 (la liste vient de la passerelle),
  « Model context window » = celui du modèle (8192 par exemple), « Max Tokens ».
  Ou fournisseur **« Ollama »** : « Ollama Model », puis « Show advanced settings » : « Ollama
  Base URL » `http://127.0.0.1:11435` et « Authentication Token » = la clé d'AIODOS.
- **Embeddings** : AI Providers → Embedder (« Préférences d'intégration ») : « Ollama » (même
  URL, « Auth Token (optional) » = la clé) ou « Generic OpenAI » (« Base URL » `…/v1`,
  « Embedding Model », « API Key »), avec le modèle d'embedding de cette machine
  (embeddinggemma…).
- **MCP** : voir § 7.

## 5. Continue (VS Code, JetBrains) et les éditeurs de code

Dans `config.yaml` de Continue, le fournisseur **Ollama** est le plus complet : la
complétion de code (*autocomplete*) passe par `/api/generate` avec `suffix`, sur cette machine.

```yaml
models:
  - name: Qwen coder (AIODOS)
    provider: ollama
    model: qwen2.5-coder:7b
    apiBase: http://127.0.0.1:11435
    apiKey: aiodos-…            # la clé d'accès d'AIODOS
    roles: [chat, edit, autocomplete]
  - name: Embeddings (AIODOS)
    provider: ollama
    model: embeddinggemma
    apiBase: http://127.0.0.1:11435
    apiKey: aiodos-…
    roles: [embed]
```

Le fournisseur **OpenAI** marche aussi (`provider: openai`, `apiBase:
http://127.0.0.1:11435/v1`, `apiKey`) ; sa complétion de code passe par `/v1/completions`,
en texte brut, sur cette machine. Avec un nom de modèle qui commence par `o1`, `o3`… ou
`gpt-5`, Continue appellerait l'API « Responses » : ajoute alors `useResponsesApi: false`.

Tout éditeur ou greffon qui accepte « un serveur compatible OpenAI » (adresse de base + clé)
ou « un serveur Ollama » se règle de la même façon.

## 6. Un script

Avec le client officiel d'OpenAI pour Python (`pip install openai`) :

```python
from openai import OpenAI

aiodos = OpenAI(base_url="http://127.0.0.1:11435/v1", api_key="aiodos-…")
for model in aiodos.models.list():
    print(model.id, model.owned_by)
reply = aiodos.chat.completions.create(
    model="qwen3.5:9b", messages=[{"role": "user", "content": "Bonjour !"}])
print(reply.choices[0].message.content)
```

En-tête de la réponse `X-Aiodos-Model` : l'identifiant concret qui a répondu (le nom seul pour
cette machine, `peer:…` pour une autre).

Avec curl, à la façon d'Ollama :

```
curl -H "Authorization: Bearer aiodos-…" http://127.0.0.1:11435/api/chat -d "{\"model\": \"qwen3.5:9b\", \"messages\": [{\"role\": \"user\", \"content\": \"Bonjour !\"}]}"
```

La bibliothèque `ollama` pour Python : `Client(host="http://127.0.0.1:11435",
headers={"Authorization": "Bearer aiodos-…"})`.

## 7. Les assistants MCP

Le serveur MCP d'AIODOS donne trois outils :

| Outil | Ce qu'il fait |
|---|---|
| `rechercher_bibliotheques` | recherche par le sens dans tes bibliothèques actives, sauf celles gardées sur ce PC : les passages les plus proches d'une question, avec leur source et un score (seuil de pertinence des Réglages) ; `bibliotheque` pour se limiter à une, `nombre` de passages (5 par défaut) |
| `lister_bibliotheques` | ces mêmes bibliothèques, avec leur nombre d'éléments et de passages |
| `converser` | fait répondre un modèle du cercle (choix automatique de la machine, ou `peer:…`), sans historique |

Il parle la version actuelle de la spécification, **2026-07-28** (sans état : chaque requête
porte sa version, le serveur se présente par `server/discover`), et les versions à poignée de
main `initialize` : **2025-11-25**, **2025-06-18** et **2025-03-26**.

**Assistant qui appelle une adresse** (Streamable HTTP) : adresse `http://127.0.0.1:11435/mcp`,
en-tête `Authorization: Bearer <clé>`. Selon l'assistant :

- **Claude Code** :
  ```
  claude mcp add --transport http aiodos http://127.0.0.1:11435/mcp --header "Authorization: Bearer aiodos-…"
  ```
  ou, dans le `.mcp.json` d'un projet (`"type": "http"` est obligatoire) :
  ```json
  {"mcpServers": {"aiodos": {"type": "http", "url": "http://127.0.0.1:11435/mcp",
                             "headers": {"Authorization": "Bearer ${AIODOS_KEY}"}}}}
  ```
  (`${AIODOS_KEY}` : une variable d'environnement de ton choix, pour ne pas écrire la clé
  dans le fichier.)
- **VS Code** (`.vscode/mcp.json`, ou « MCP: Open User Configuration ») ; la clé est demandée
  une fois, sans être écrite dans le fichier :
  ```json
  {"inputs": [{"type": "promptString", "id": "aiodos-key", "description": "Clé d'AIODOS",
               "password": true}],
   "servers": {"aiodos": {"type": "http", "url": "http://127.0.0.1:11435/mcp",
                          "headers": {"Authorization": "Bearer ${input:aiodos-key}"}}}}
  ```
- **Cursor** (`~/.cursor/mcp.json`, ou `.cursor/mcp.json` dans un projet) et **LM Studio**
  (onglet « Program », Install → Edit mcp.json, même format) :
  ```json
  {"mcpServers": {"aiodos": {"url": "http://127.0.0.1:11435/mcp",
                             "headers": {"Authorization": "Bearer aiodos-…"}}}}
  ```
- **Continue** (`config.yaml`, mode agent seulement) :
  ```yaml
  mcpServers:
    - name: AIODOS
      type: streamable-http
      url: http://127.0.0.1:11435/mcp
      requestOptions:
        headers:
          Authorization: Bearer aiodos-…
  ```
- **Open WebUI** (0.6.31 ou plus) : Settings → Admin → Integrations → External Tool Servers →
  « + Add Connection », Type « MCP (Streamable HTTP) », URL ci-dessus, Auth « Bearer », Key =
  la clé d'AIODOS.
- **AnythingLLM** : dans `plugins/anythingllm_mcp_servers.json` de son dossier de stockage,
  `"type": "streamable"` (pas « http »), avec `url` et `headers` comme ci-dessus.

Pour VS Code, Continue, AnythingLLM et l'exemple `.mcp.json` de Claude Code, la configuration
est bâtie sur les clés que décrit leur documentation, qui n'a pas d'exemple tout fait de ce cas.

**Assistant qui lance un programme** (stdio : Claude Desktop…) : le pont
`python -m aiodos.gateway.mcp_stdio` relaie vers la passerelle. Il lit lui-même l'adresse et
la clé dans les réglages d'AIODOS, et relit une clé renouvelée. La configuration exacte, avec
le chemin de Python et celui d'AIODOS sur ta machine, est prête à copier dans **Réglages →
Autres outils** (« Assistant qui lance un programme »). Pour **Claude Desktop**, elle va dans
`%APPDATA%\Claude\claude_desktop_config.json` (Settings → Developer → « Edit Config »), puis
on relance Claude Desktop. Ce fichier n'accepte que ce genre de serveur : les « connecteurs »
distants de Claude Desktop passent par les serveurs d'Anthropic, qui ne joignent pas
`127.0.0.1`.

```json
{"mcpServers": {"aiodos": {
  "command": "C:\\…\\python.exe",
  "args": ["-m", "aiodos.gateway.mcp_stdio"],
  "env": {"PYTHONPATH": "C:\\…\\AIODOS", "PYTHONIOENCODING": "utf-8"}}}}
```

AIODOS doit tourner, la passerelle ouverte. Après une mise à jour d'AIODOS installée à côté de
l'ancienne version, recopie la configuration affichée : le chemin d'AIODOS a pu changer.

## 8. Limites

- Pas de gestion des modèles par la passerelle (`/api/pull`, `create`, `delete`…) : on les
  installe avec Ollama.
- Une machine appairée ne reçoit ni outils, ni images, ni format imposé (§ 2).
- Pas de connexion depuis une autre machine : 127.0.0.1 seulement (un conteneur Docker
  Desktop de cette machine passe en principe par `host.docker.internal`, § 3).
- Pas de CORS : un outil qui tourne dans le navigateur (extension, page web) ne peut pas
  l'utiliser.
- Le nombre de jetons d'une réponse venue d'une machine appairée n'est connu que si elle a
  une version d'AIODOS qui le donne ; sinon 0.

## 9. Où c'est dans le code

- `aiodos/gateway/server.py` : le serveur (127.0.0.1, port, ouverture et fermeture sans
  relancer), les trois verrous (Host, Origin, clé) ; `access.py` : la clé.
- `openai_api.py`, `ollama_api.py` : les deux API ; `catalog.py` : les modèles proposés ;
  `answer.py` : relais vers l'Ollama local ou conversation par une machine appairée.
- `mcp_server.py` : le serveur MCP ; `mcp_stdio.py` : le pont stdio.
- `aiodos/network/placement.py` : le choix automatique de la machine qui répond, commun aux
  conversations d'AIODOS et à la passerelle.

### Sources

- OpenAI : [référence de l'API](https://developers.openai.com/api/reference) (modèles,
  conversation, embeddings, codes d'erreur), [openai-openapi](https://github.com/openai/openai-openapi).
- Ollama : [documentation de l'API](https://docs.ollama.com/api) et son code (v0.40.5).
- MCP : [spécification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28),
  [changements](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
  [SDK Python](https://github.com/modelcontextprotocol/python-sdk) (licence MIT).
- Outils : [Open WebUI](https://docs.openwebui.com) (connexions, MCP),
  [AnythingLLM](https://docs.anythingllm.com/mcp-compatibility/overview),
  [Continue](https://docs.continue.dev/customize/deep-dives/mcp),
  [Claude Code](https://code.claude.com/docs/en/mcp),
  [Claude Desktop](https://modelcontextprotocol.io/docs/develop/connect-local-servers),
  [VS Code](https://code.visualstudio.com/docs/agents/reference/mcp-configuration),
  [Cursor](https://cursor.com/docs/mcp), [LM Studio](https://lmstudio.ai/docs/app/mcp),
  [Docker, réseau de l'hôte](https://docs.docker.com/engine/network/drivers/host/).
