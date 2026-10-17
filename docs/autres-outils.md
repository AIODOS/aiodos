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
ou par une de tes machines (voir § 2) : rien ne sort de chez toi. Mais les passages qu'un
assistant MCP trouve dans tes bibliothèques partent vers lui, puis vers *son* modèle : si
c'est un modèle dans le cloud (Claude, GPT…), ces passages y partent aussi.

## 2. Les modèles et la machine qui répond

La liste des modèles (`/v1/models`, `/api/tags`) donne :

| Identifiant | Qui répond |
|---|---|
| `qwen3.5:9b` (le nom seul) | **choix automatique** : cette machine si elle a le modèle et qu'elle est libre (sauf s'il n'y tient pas en mémoire vidéo alors qu'une autre machine libre l'y fait tenir), sinon la moins occupée de tes machines qui le prêtent |
| `peer:<machine>:qwen3.5:9b` | cette machine appairée précisément (une des tiennes, ou celle d'un ami qui te prête ses modèles) |
| `local:qwen3.5:9b` | cette machine seulement (accepté, pas listé) |

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

La passerelle n'écoute que sur `127.0.0.1` : un Open WebUI lancé **dans Docker** ne la joint
pas (pour un conteneur, `127.0.0.1` est le conteneur lui-même, et `host.docker.internal` ne
mène pas aux services liés à `127.0.0.1` de la machine). Installe-le donc **directement sur
la machine** (Python 3.11 ou 3.12) :

```
pip install open-webui
open-webui serve
```

puis ouvre http://localhost:8080. Ensuite, dans **Réglages administrateur → Connexions** :

- **API OpenAI** : ajoute une connexion, URL `http://127.0.0.1:11435/v1` (sans `/` à la fin),
  clé d'accès d'AIODOS ; ou
- **API Ollama** : URL `http://127.0.0.1:11435`, authentification « Bearer », clé d'accès
  d'AIODOS.

Les modèles du cercle apparaissent dans la liste de conversation ; un modèle d'une machine
appairée s'y lit « qwen3.6 · PC fixe ». Pour la recherche dans ses documents, Open WebUI peut
prendre le modèle d'embedding d'AIODOS (Réglages administrateur → Documents, moteur
« Ollama », même URL et même clé). Les « connexions directes » d'Open WebUI, faites depuis le
navigateur, ne marchent pas (pas de CORS, voir § 1).

## 4. AnythingLLM

AnythingLLM (version de bureau, ou serveur installé sur la machine — pas dans Docker, pour la
même raison qu'Open WebUI) :

- **Modèle de conversation** : Paramètres → AI Providers → LLM, fournisseur **« Generic
  OpenAI »** : Base URL `http://127.0.0.1:11435/v1`, API Key = la clé d'AIODOS, Chat Model
  Name = un identifiant du § 2, Token context window = celui du modèle (8192 par exemple).
  Ou fournisseur **« Ollama »** : URL `http://127.0.0.1:11435`, et la clé dans
  « Authentication Token » (sous « Advanced »).
- **Embeddings** : Paramètres → AI Providers → Embedder, « Ollama » (même URL, « Auth
  Token ») ou « Generic OpenAI » (Base URL `…/v1`), avec le modèle d'embedding de cette
  machine (embeddinggemma…).

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
| `rechercher_bibliotheques` | recherche par le sens dans tes bibliothèques actives : les passages les plus proches d'une question, avec leur source et un score (seuil de pertinence des Réglages) ; `bibliotheque` pour se limiter à une, `nombre` de passages (5 par défaut) |
| `lister_bibliotheques` | les bibliothèques actives, avec leur nombre d'éléments et de passages |
| `converser` | fait répondre un modèle du cercle (choix automatique de la machine, ou `peer:…`), sans historique |

Il parle la version actuelle de la spécification, **2026-07-28** (sans état : chaque requête
porte sa version, le serveur se présente par `server/discover`), et les versions à poignée de
main `initialize` : **2025-11-25**, **2025-06-18** et **2025-03-26**.

**Assistant qui appelle une adresse** (Streamable HTTP) : adresse `http://127.0.0.1:11435/mcp`,
en-tête `Authorization: Bearer <clé>`. Par exemple :

- Claude Code :
  ```
  claude mcp add --transport http aiodos http://127.0.0.1:11435/mcp --header "Authorization: Bearer aiodos-…"
  ```
- VS Code (`.vscode/mcp.json`) :
  ```json
  {"servers": {"aiodos": {"type": "http", "url": "http://127.0.0.1:11435/mcp",
                          "headers": {"Authorization": "Bearer aiodos-…"}}}}
  ```
- Cursor (`~/.cursor/mcp.json`) :
  ```json
  {"mcpServers": {"aiodos": {"url": "http://127.0.0.1:11435/mcp",
                             "headers": {"Authorization": "Bearer aiodos-…"}}}}
  ```

**Assistant qui lance un programme** (stdio : Claude Desktop…) : le pont
`python -m aiodos.gateway.mcp_stdio` relaie vers la passerelle. Il lit lui-même l'adresse et
la clé dans les réglages d'AIODOS, et relit une clé renouvelée. La configuration exacte, avec
le chemin de Python et celui d'AIODOS sur ta machine, est prête à copier dans **Réglages →
Autres outils** (« Assistant qui lance un programme ») ; pour Claude Desktop, elle va dans
`claude_desktop_config.json` (menu Réglages → Développeur → Modifier la configuration) :

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
- Pas de connexion depuis un conteneur Docker ou une autre machine : 127.0.0.1 seulement.
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
- Outils : [Open WebUI](https://docs.openwebui.com), [AnythingLLM](https://docs.anythingllm.com),
  [Continue](https://docs.continue.dev).
