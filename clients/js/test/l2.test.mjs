// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : la surface des travaux (comfy, jobs, inputs, engines, files)
// rejette NotSupported tant que `features` ne l'annonce pas, sans rien essayer d'autre ; ce
// qu'AIODOS ne sert pas encore (object_info et gabarits relayés, recettes, liens signés)
// rejette NotSupported même quand le reste est annoncé.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { NotSupported } from '../src/index.js';
import { FAKE_FEATURES, FAKE_PNG, createFakeClient } from '../src/fake.js';

const L1 = ['access', 'models', 'chat', 'embed', 'open'];
const GRAPH = { 9: { class_type: 'SaveImage', inputs: { filename_prefix: 'essai' } } };

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
    ['file', 'files', () => client.file('file_3Lq')],
    ['inputs.put', 'inputs', () => client.inputs.put(Buffer.from('png'))],
  ];
}

test('tant que features ne l’annonce pas : NotSupported, seulement GET /aiodos/v1/info', async () => {
  const { client, fake } = createFakeClient();
  assert.deepEqual(fake.features, L1); // l'info du contrat : AIODOS 2.1 sans les travaux
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

test('annoncé : ce qu’AIODOS ne sert pas encore rejette NotSupported (L2c, L4, liens signés)', async () => {
  const { client, fake } = createFakeClient({ fake: { features: FAKE_FEATURES, scopes: ['chat', 'engines:comfyui'], jobMode: 'manual' } });
  const later = [
    ['comfy.objectInfo', 'engines.comfyui.object_info', () => client.comfy.objectInfo('KSampler')],
    ['comfy.template', 'engines.comfyui.templates', () => client.comfy.template('image_sdxl_t2i')],
    ['comfy.recipes', 'recipes', () => client.comfy.recipes()],
    ['comfy.recipeFor', 'recipes', () => client.comfy.recipeFor({ name: 'image_sdxl_t2i' })],
    ['comfy.submit({ recipe })', 'recipes', () => client.comfy.submit({ recipe: 'comfy-templates/image_sdxl_t2i' })],
    ['comfy.plan({ recipe })', 'recipes', () => client.comfy.plan({ recipe: 'comfy-templates/image_sdxl_t2i' })],
    ['comfy.run({ recipe })', 'recipes', () => client.comfy.run({ recipe: 'comfy-templates/image_sdxl_t2i', destDir: '.' })],
  ];
  for (const [name, feature, run] of later) {
    await assert.rejects(run(), (e) => {
      assert.ok(e instanceof NotSupported, name);
      assert.deepEqual(e.detail, { feature, announced: false }, name);
      assert.match(e.message, new RegExp(`ne sert pas encore « ${feature.replaceAll('.', '\\.')} »`), name);
      return true;
    });
  }
  assert.ok(fake.requests.every((r) => r.path.startsWith('/aiodos/v1/info')), 'rien d’autre ne part');
  // le reste marche
  assert.equal((await client.engines())[0].id, 'comfyui');
  const job = await client.comfy.submit({ graph: GRAPH });
  assert.equal((await client.job(job.id)).id, job.id);
  assert.equal((await client.inputs.put(FAKE_PNG)).length, 64);
});

test('un AIODOS plus récent qui sert déjà les recettes : NotSupported aussi, ce client ne les connaît pas', async () => {
  const { client } = createFakeClient({ fake: { features: [...FAKE_FEATURES, 'recipes', 'engines.comfyui.object_info'] } });
  for (const run of [() => client.comfy.recipes(), () => client.comfy.objectInfo()]) {
    await assert.rejects(run(), (e) => {
      assert.ok(e instanceof NotSupported);
      assert.equal(e.detail.announced, true);
      assert.match(e.message, /aiodos-client 0\.2\.0 ne sait pas encore/);
      assert.equal(e.hint, 'Mets à jour aiodos-client.');
      return true;
    });
  }
});

test('AIODOS injoignable : l’erreur de connexion, pas NotSupported', async () => {
  const { client } = createFakeClient({ fake: { listening: false } });
  await assert.rejects(client.jobs(), (e) => e.code === 'unreachable');
  await assert.rejects(client.comfy.tokenize({}, {}), (e) => e.code === 'unreachable');
});
