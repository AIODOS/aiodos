// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.1.0 — essais : les embeddings (embed.json) et openInAiodos.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { FAKE_KEY, createFakeClient } from '../src/fake.js';

const contract = JSON.parse(readFileSync(new URL('../../contract/embed.json', import.meta.url), 'utf8'));

test('embed() : le corps et la réponse du contrat', async () => {
  const it = contract.interactions.find((i) => i.name === 'embeddings');
  const { client, fake } = createFakeClient();
  const r = await client.embed({ input: it.request.json.input, model: it.request.json.model });
  assert.deepEqual(r, {
    embeddings: it.response.json.data.map((d) => d.embedding),
    model: 'embeddinggemma',
    dimensions: 2,
    usage: { promptTokens: 0, totalTokens: 0 },
  });
  const [req] = fake.received('/v1/embeddings');
  assert.deepEqual(req.json, it.request.json);
  assert.equal(req.headers.authorization, `Bearer ${FAKE_KEY}`);
});

test('embed() : un seul texte ; sans modèle, celui de ce PC', async () => {
  const { client, fake } = createFakeClient();
  const r = await client.embed({ input: 'un chat noir' });
  assert.equal(r.embeddings.length, 1);
  assert.equal(r.model, 'embeddinggemma');
  assert.equal(fake.received('/v1/embeddings')[0].json.input, 'un chat noir');
  assert.equal(fake.received('/aiodos/v1/models')[0].path, '/aiodos/v1/models?kind=embed');
  await assert.rejects(client.embed({ input: [] }), (e) => e.code === 'invalid_request' && e.param === 'input');
  await assert.rejects(client.embed({ input: [1, 2] }), (e) => e.param === 'input');
  fake.models.embed = [];
  await assert.rejects(client.embed({ input: 'x' }), (e) => e.code === 'model_not_found');
});

test('embed() : les vecteurs en base64 (float32) sont décodés', async () => {
  const { client, fake } = createFakeClient();
  const floats = Buffer.alloc(8);
  floats.writeFloatLE(0.5, 0);
  floats.writeFloatLE(-2, 4);
  fake.respondNext(
    { status: 200, headers: { 'X-Aiodos-Api': '1' }, json: { object: 'list', model: 'embeddinggemma', data: [{ object: 'embedding', index: 0, embedding: floats.toString('base64') }] } },
    { path: '/v1/embeddings' },
  );
  const r = await client.embed({ input: 'x', model: 'embeddinggemma' });
  assert.deepEqual(r.embeddings, [[0.5, -2]]);
});

test('embed() sans le droit : ScopeDenied (embed.json)', async () => {
  const it = contract.interactions.find((i) => i.name === 'embeddings sans le droit');
  const { client, fake } = createFakeClient();
  fake.keys.get(FAKE_KEY).scopes = ['chat'];
  await assert.rejects(client.embed({ input: 'un chat noir', model: 'embeddinggemma' }), (e) => {
    assert.equal(e.name, 'ScopeDenied');
    assert.equal(e.message, it.response.json.error.message);
    assert.equal(e.scope, 'embed');
    return true;
  });
});

test('openInAiodos : la page et le modèle ; une page inconnue est refusée', async () => {
  const { client, fake } = createFakeClient();
  assert.deepEqual(await client.openInAiodos({ page: 'models', model: 'minimax-h3' }), { opened: true });
  assert.deepEqual(fake.received('/aiodos/v1/open')[0].json, { page: 'models', model: 'minimax-h3' });
  assert.deepEqual(await client.openInAiodos(), { opened: true });
  assert.deepEqual(fake.received('/aiodos/v1/open')[1].json, { page: 'applications', model: null });
  await assert.rejects(client.openInAiodos({ page: 'reglages' }), (e) => e.code === 'invalid_request' && e.param === 'page');
  assert.equal(fake.received('/aiodos/v1/open').length, 2);
  // plus d'une fois par 10 s : 429
  fake.failNext('rate_limited', { path: '/aiodos/v1/open', retryAfter: 9 });
  await assert.rejects(client.openInAiodos({ page: 'models' }), (e) => e.name === 'RateLimited' && e.retryAfter === 9);
});
