# Contrat partagé de l'API des applications

<!-- SPDX-License-Identifier: Apache-2.0 -->

Des exemples de requêtes et de réponses de la passerelle d'AIODOS, rejoués des deux côtés :

- par `tests/test_gateway_contract.py` (AIODOS) : la vraie passerelle répond avec la même
  forme ;
- par les essais des clients (`clients/python/tests/`, `clients/js/test/`) : les clients
  comprennent ces réponses, et leurs faux serveurs (`FakeAiodos`, `fake.js`) les servent.

La référence lisible est `docs/api-applications.md` ; en cas de désaccord, c'est elle qui fait
foi, et le contrat est corrigé.

## Forme d'un fichier

```json
{ "interactions": [
  { "name": "info sans clé",
    "request": { "method": "GET", "path": "/aiodos/v1/info", "headers": {}, "json": null },
    "response": { "status": 200, "headers": { "X-Aiodos-Api": "1" }, "json": { } },
    "exact": ["object", "product", "api", "auth"] } ] }
```

- `request.headers` peut contenir `{key}` (la clé de l'appli d'essai) ; `request.path` et
  `request.json`, `{device_code}`, `{challenge}`, `{key_id}` : à remplacer par l'essai.
- `response` porte soit `json` (le corps), soit `sse` (les événements d'un flux, dans l'ordre :
  chaque chaîne est une ligne `data: …` ou un commentaire `: …`, suivie d'une ligne vide dans
  le flux réel), soit `ndjson` (lignes d'un flux de l'API d'Ollama).
- Comparaison par le serveur : chaque clé de l'exemple doit exister dans la vraie réponse avec
  le même type JSON, récursivement (pour une liste, son premier élément quand les deux en
  ont) ; les chemins de `exact` (notation pointée, `models.0.kind`) doivent être égaux.
  Les en-têtes de `response.headers` doivent être présents, avec la même valeur.
- `server` : `shape` (par défaut : comparé comme ci-dessus), `client` (seulement pour les
  clients : un cas qu'un essai du serveur ne sait pas produire à la demande, comme une panne
  en cours de flux).

## Fichiers

| Fichier | Contenu |
|---|---|
| `discovery.json` | le fichier `gateway.json` dans ses états, et la sortie du programme d'aide `--app-request` |
| `info.json` | `GET /aiodos/v1/info` sans clé, avec preuve, avec une clé valide ou retirée |
| `access.json` | demande, relève (attente, refus, expiration, succès, droits en plus), retrait |
| `models.json` | `/aiodos/v1/models` et `/v1/models` |
| `chat.json` | conversation en flux (texte, réflexion, chargement, outils, erreur en route) et sans flux |
| `embed.json` | `/v1/embeddings` |
| `errors.json` | une erreur par code stable |
| `proof.json` | un vecteur d'essai de la preuve (clé, défi, preuve attendue) |
