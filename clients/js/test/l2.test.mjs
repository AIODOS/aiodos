// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — essais : la surface de L2 (comfy, jobs, inputs, engines) existe et
// rejette NotSupported tant que `features` ne l'annonce pas, sans rien essayer d'autre.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { NotSupported } from '../src/index.js';
import { createFakeClient } from '../src/fake.js';

function calls(client) {
  return [
    ['engines', 'engines', () => client.engines()],
    ['comfy.plan', 'engines.comfyui', () => client.comfy.plan({ graph: {} })],
    ['comfy.objectInfo', 'engines.comfyui', () => client.comfy.objectInfo('KSampler')],
    ['comfy.template', 'engines.comfyui', () => client.comfy.template('image_sdxl_t2i')],
    ['comfy.recipes', 'engines.comfyui', () => client.comfy.recipes()],
    ['comfy.recipeFor', 'engines.comfyui', () => client.comfy.recipeFor({})],
    ['comfy.tokenize', 'engines.comfyui', () => client.comfy.tokenize({}, {})],
    ['comfy.submit', 'engines.comfyui', () => client.comfy.submit({ graph: {} })],
    ['comfy.run', 'engines.comfyui', () => client.comfy.run({ graph: {} })],
    ['jobs', 'jobs', () => client.jobs({ active: true })],
    ['job', 'jobs', () => client.job('job_8Hk2Qp')],
    ['inputs.put', 'inputs', () => client.inputs.put(Buffer.from('png'))],
  ];
}

test('tant que features ne l’annonce pas : NotSupported, seulement GET /aiodos/v1/info', async () => {
  const { client, fake } = createFakeClient();
  for (const [name, feature, run] of calls(client)) {
    await assert.rejects(run(), (e) => {
      assert.ok(e instanceof NotSupported, name);
      assert.equal(e.code, 'not_supported', name);
      assert.deepEqual(e.detail, { feature, announced: false }, name);
      assert.match(e.message, /mets AIODOS à jour/, name);
      return true;
    });
  }
  assert.ok(fake.requests.length > 0);
  assert.ok(fake.requests.every((r) => r.method === 'GET' && r.path === '/aiodos/v1/info'));
  assert.ok(fake.requests.every((r) => r.headers.authorization === undefined));
});

test('annoncé par un AIODOS plus récent : NotSupported aussi, ce client ne connaît pas encore les routes', async () => {
  const { client, fake } = createFakeClient({ fake: { features: ['access', 'models', 'chat', 'embed', 'open', 'engines', 'engines.comfyui', 'jobs', 'inputs', 'files'] } });
  for (const [name, feature, run] of calls(client)) {
    await assert.rejects(run(), (e) => {
      assert.ok(e instanceof NotSupported, name);
      assert.deepEqual(e.detail, { feature, announced: true }, name);
      assert.match(e.message, /aiodos-client 0\.1\.0/, name);
      return true;
    });
  }
  assert.ok(fake.requests.every((r) => r.path === '/aiodos/v1/info'));
});

test('AIODOS injoignable : l’erreur de connexion, pas NotSupported', async () => {
  const { client } = createFakeClient({ fake: { listening: false } });
  await assert.rejects(client.jobs(), (e) => e.code === 'unreachable');
});
