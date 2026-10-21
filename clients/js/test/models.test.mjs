// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — essais : le catalogue (models.json) au format d'ARKLIGHT, et pickModel.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { pickModel } from '../src/index.js';
import { createFakeClient } from '../src/fake.js';
import { paramsOf } from '../src/models.js';

test('models() : le catalogue du contrat au format attendu par ARKLIGHT', async () => {
  const { client, fake } = createFakeClient();
  const list = await client.models();
  assert.deepEqual(list.map((m) => m.id), ['qwen3.5:9b', 'mistral:7b']);
  const [qwen, mistral] = list;
  // ce qu'ARKLIGHT lit déjà : id, size, caps, params
  assert.equal(qwen.size, 6600000000);
  assert.deepEqual(qwen.caps, ['completion', 'tools', 'vision', 'thinking']);
  assert.equal(qwen.params, '9B');
  assert.deepEqual(mistral.caps, ['completion', 'tools']);
  // et en plus : machine, context, loaded, here, where
  assert.equal(qwen.machine, 'PC-BUREAU');
  assert.equal(qwen.context, 16384);
  assert.equal(qwen.loaded, false);
  assert.equal(qwen.here, true);
  assert.equal(qwen.default, true);
  assert.equal(mistral.default, false);
  assert.equal(mistral.loaded, true);
  assert.deepEqual(qwen.where, [
    { id: 'local:qwen3.5:9b', machine: 'PC-BUREAU', place: 'here', loaded: false, context: 16384, tools: true, vision: true },
    { id: 'peer:9c2e41d0:qwen3.5:9b', machine: 'Portable', place: 'mine', loaded: true, context: null, tools: false, vision: false },
  ]);
  assert.equal(qwen.license, null);
  assert.equal(qwen.installed, true);
  // la requête : la clé, après la preuve ; kind=chat par défaut
  const [req] = fake.received('/aiodos/v1/models');
  assert.equal(req.path, '/aiodos/v1/models?kind=chat');
  assert.ok(req.headers.authorization);
  // « use » est un synonyme de « kind »
  assert.equal((await client.models({ use: 'chat' })).length, 2);
});

test('models({kind: "embed"}) : dimensions, et les valeurs inconnues à null', async () => {
  const { client, fake } = createFakeClient();
  const [gemma] = await client.models({ kind: 'embed' });
  assert.equal(gemma.id, 'embeddinggemma');
  assert.deepEqual(gemma.caps, ['embedding']);
  assert.equal(gemma.dimensions, 768);
  assert.equal(gemma.context, 2048);
  assert.equal(gemma.params, '');
  // AIODOS peut ne pas connaître dimensions et context ; license vaut null ou {id, restrictive}
  Object.assign(fake.models.embed[0], { dimensions: null, context: null, license: { id: 'apache-2.0', restrictive: false } });
  const [again] = await client.models({ kind: 'embed' });
  assert.equal(again.dimensions, null);
  assert.equal(again.context, null);
  assert.deepEqual(again.license, { id: 'apache-2.0', restrictive: false });
});

test('un genre inconnu, ou « models » absent de features : rien n’est demandé', async () => {
  const { client, fake } = createFakeClient();
  await assert.rejects(client.models({ kind: 'image' }), (e) => e.code === 'invalid_request' && e.param === 'kind');
  assert.equal(fake.requests.length, 0);
  const old = createFakeClient({ fake: { features: ['access', 'chat'] } });
  await assert.rejects(old.client.models(), (e) => e.code === 'not_supported' && e.name === 'NotSupported' && e.detail.feature === 'models');
  assert.equal(old.fake.received('/aiodos/v1/models').length, 0);
});

test('paramsOf : la taille du modèle d’après son nom', () => {
  assert.equal(paramsOf('qwen3.5:9b'), '9B');
  assert.equal(paramsOf('qwen3.6:35b-a3b'), '35B');
  assert.equal(paramsOf('gemma4:e4b'), '');
  assert.equal(paramsOf('local:mistral:7b'), '7B');
  assert.equal(paramsOf('smollm:360m'), '360M');
  assert.equal(paramsOf('embeddinggemma'), '');
});

test('pickModel : exigences, vision préférée, ce PC, celui d’AIODOS, déjà chargé, le plus léger', async () => {
  const list = await createFakeClient().client.models();
  // outils exigés : les deux conviennent ; celui d'AIODOS passe devant, puis le chargé
  assert.equal(pickModel(list, { needs: ['tools'] }).id, 'qwen3.5:9b');
  const plain = list.map((m) => ({ ...m, default: false }));
  assert.equal(pickModel(plain, { needs: ['tools'] }).id, 'mistral:7b');
  // la vision préférée (sans l'exiger)
  assert.equal(pickModel(list, { needs: ['tools'], vision: true, preferLocal: true }).id, 'qwen3.5:9b');
  // la vision exigée
  assert.equal(pickModel(list, { needs: ['vision'] }).id, 'qwen3.5:9b');
  assert.equal(pickModel(list, { needs: ['thinking', 'tools'] }).id, 'qwen3.5:9b');
  // un contexte trop petit écarte
  assert.equal(pickModel(list, { needs: ['tools'], minContext: 32768 }), null);
  assert.equal(pickModel([], { needs: ['tools'] }), null);
  assert.equal(pickModel(null), null);
  // un modèle d'une autre machine passe après ceux de ce PC
  const far = { ...list[1], id: 'peer:x:grand', here: false, loaded: true, size: 1 };
  assert.equal(pickModel([far, plain[0]], { needs: ['tools'] }).id, 'qwen3.5:9b');
  assert.equal(pickModel([far, plain[0]], { needs: ['tools'], preferLocal: false }).id, 'peer:x:grand');
  // sans préférence de lieu, celui d'AIODOS passe encore devant le chargé
  assert.equal(pickModel([far, list[0]], { needs: ['tools'], preferLocal: false }).id, 'qwen3.5:9b');
});

test('pickModel accepte aussi la liste d’Ollama d’ARKLIGHT ({id, size, caps})', () => {
  const ollama = [
    { id: 'qwen3:8b', size: 5_200_000_000, caps: ['completion', 'tools'] },
    { id: 'qwen3:4b', size: 2_600_000_000, caps: ['completion', 'tools'] },
    { id: 'llava:7b', size: 4_000_000_000, caps: ['completion', 'vision'] },
    { id: 'nomic-embed-text', size: 270_000_000, caps: ['embedding'] },
  ];
  assert.equal(pickModel(ollama, { needs: ['tools'] }).id, 'qwen3:4b');
  assert.equal(pickModel(ollama).id, 'qwen3:4b'); // jamais le modèle d'embedding
  assert.equal(pickModel(ollama, { needs: ['embedding'] }).id, 'nomic-embed-text');
});
