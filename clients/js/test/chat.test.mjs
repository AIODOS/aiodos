// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : la conversation (chat.json rejoué), les en-têtes, les outils,
// les délais, l'erreur en route et l'annulation réelle.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { createClient, memoryKeyStore } from '../src/index.js';
import { FAKE_KEY, createFakeClient, startFakeServer } from '../src/fake.js';

const contract = JSON.parse(readFileSync(new URL('../../contract/chat.json', import.meta.url), 'utf8'));
const byName = (name) => contract.interactions.find((i) => i.name === name);
const NO_INSTALL = { AIODOS_DISCOVERY_FILE: '-', AIODOS_HOME: path.join(tmpdir(), 'aiodos-client-aucune-installation') };

/** Chaque clé de `want` se retrouve, égale, dans `got` (le client envoie au moins le contrat). */
function assertIncludes(got, want, where = 'corps') {
  for (const [k, v] of Object.entries(want)) assert.deepEqual(got[k], v, `${where}.${k}`);
}

const chunk = (delta, finish = null, extra = {}) =>
  `data: ${JSON.stringify({ id: 'c', object: 'chat.completion.chunk', created: 1, model: 'qwen3.5:9b', choices: [{ index: 0, delta, finish_reason: finish }], ...extra })}`;

test('flux du contrat : chargement, réflexion, texte, usage, machine', async () => {
  const it = byName('conversation en flux : chargement, réflexion, texte');
  const { client, fake } = createFakeClient();
  const texts = [];
  const thoughts = [];
  const loading = [];
  const r = await client.chat({
    model: 'qwen3.5:9b',
    system: 'Réponds brièvement.',
    messages: [{ role: 'user', content: 'Bonjour' }],
    onText: (t) => texts.push(t),
    onThinking: (t) => thoughts.push(t),
    onLoading: (l) => loading.push(l.seconds),
  });
  assert.deepEqual(r, {
    text: 'Bonjour !',
    thinking: "L'utilisateur salue.",
    toolCalls: [],
    finish: 'stop',
    stoppedBy: 'stop',
    truncated: false,
    usage: { promptTokens: 21, completionTokens: 8, totalTokens: 29 },
    answeredBy: 'qwen3.5:9b',
    machine: 'PC-BUREAU',
    place: 'here',
    contextLength: 16384,
  });
  assert.deepEqual(texts, ['Bonjour', ' !']);
  assert.deepEqual(thoughts, ["L'utilisateur salue."]);
  // le catalogue dit « pas chargé » : prévenu d'abord (0), puis par le commentaire (5)
  assert.deepEqual(loading, [0, 5]);
  // ce qui est parti : exactement le corps du contrat, la clé après la preuve
  const [req] = fake.received('/v1/chat/completions');
  assert.deepEqual(req.json, it.request.json);
  assert.equal(req.headers.authorization, `Bearer ${FAKE_KEY}`);
  assert.equal(req.headers.accept, 'text/event-stream');
  for (const h of ['x-aiodos-where', 'x-aiodos-priority', 'x-aiodos-tools']) assert.equal(req.headers[h], undefined, h);
});

test("flux du contrat : appel d'outil rassemblé par index, arguments analysés", async () => {
  const it = byName("conversation en flux : appel d'outil");
  const { client, fake } = createFakeClient();
  const tool = it.request.json.tools[0].function;
  const r = await client.chat({
    model: 'qwen3.5:9b',
    messages: [{ role: 'user', content: 'Allume le projecteur 3 à 80 %.' }],
    tools: [{ name: tool.name, description: tool.description, parameters: tool.parameters }],
    think: false,
  });
  assert.deepEqual(r.toolCalls, [{ id: 'call_8yT2', name: 'set_light', args: { fixture: 3, level: 80 }, raw: '{"fixture": 3, "level": 80}' }]);
  assert.equal(r.text, '');
  assert.equal(r.finish, 'tool_calls');
  assert.equal(r.stoppedBy, 'tool_calls');
  const [req] = fake.received('/v1/chat/completions');
  assertIncludes(req.json, it.request.json); // think: false → reasoning_effort "none", outils au format d'OpenAI
  assert.equal(req.json.reasoning_effort, 'none');
});

test('flux du contrat : coupé à la fin du contexte (truncated, stoppedBy « context »)', async () => {
  const it = byName('conversation en flux : coupée à la fin du contexte');
  const { client, fake } = createFakeClient();
  fake.nextChat(it.name);
  const r = await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Écris un très long texte.' }], maxTokens: 100000 });
  assert.equal(r.text, 'Il était une fois');
  assert.equal(r.finish, 'length');
  assert.equal(r.stoppedBy, 'context');
  assert.equal(r.truncated, true);
  assert.equal(r.usage, null);
  assertIncludes(fake.received('/v1/chat/completions')[0].json, it.request.json);
});

test('flux du contrat : panne en route → AiodosError, connexion fermée', async () => {
  const it = byName('conversation en flux : panne en route');
  const { client, fake } = createFakeClient();
  fake.nextChat(it.name);
  const seen = [];
  await assert.rejects(
    client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }], onText: (t) => seen.push(t) }),
    (e) => {
      assert.equal(e.code, 'engine_failed');
      assert.equal(e.name, 'EngineFailed');
      assert.equal(e.retryable, true);
      assert.equal(e.hint, 'Réessaie.');
      assert.equal(e.message, "Le moteur intégré n'a pas pu répondre : connexion perdue.");
      return true;
    },
  );
  assert.deepEqual(seen, ['Bon']);
});

test('réponse sans flux (contrat) : comprise aussi', async () => {
  const { client, fake } = createFakeClient();
  fake.respondNext(byName('conversation sans flux').response, { path: '/v1/chat/completions' });
  const r = await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }] });
  assert.equal(r.text, 'Bonjour !');
  assert.equal(r.finish, 'stop');
  assert.equal(r.machine, 'PC-BUREAU');
  assert.deepEqual(r.usage, { promptTokens: 9, completionTokens: 3, totalTokens: 12 });
});

test('outils demandés, modèle seulement sur une autre machine : ToolsUnavailable', async () => {
  const it = byName('outils demandés, modèle seulement sur une autre machine');
  const { client } = createFakeClient();
  await assert.rejects(
    client.chat({ model: 'grand:7b', messages: [{ role: 'user', content: 'Allume le projecteur 3.' }], tools: [{ name: 'set_light', parameters: { type: 'object', properties: {} } }] }),
    (e) => e.name === 'ToolsUnavailable' && e.code === 'tools_unavailable' && e.param === 'tools' && e.status === 400 && e.hint === it.response.json.error.aiodos.hint,
  );
});

test('les en-têtes : where, batch, requireTools ; lieu plus large que l’accord', async () => {
  const { client, fake } = createFakeClient();
  const base = { model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }] };
  await client.chat({ ...base, where: 'here', batch: true, requireTools: false });
  await client.chat({ ...base, where: ['here', 'mine'] });
  const [a, b] = fake.received('/v1/chat/completions');
  assert.equal(a.headers['x-aiodos-where'], 'here');
  assert.equal(a.headers['x-aiodos-priority'], 'background');
  assert.equal(a.headers['x-aiodos-tools'], 'optional');
  assert.equal(b.headers['x-aiodos-where'], 'here,mine');
  assert.equal(b.headers['x-aiodos-priority'], undefined);
  await assert.rejects(client.chat({ ...base, where: 'friends' }), (e) => e.code === 'where_denied' && e.name === 'WhereDenied' && e.detail.allowed.join() === 'here,mine');
  await assert.rejects(client.chat({ ...base, where: 'partout' }), (e) => e.code === 'invalid_request' && e.param === 'where');
});

test('les paramètres : réflexion, température, max, top_p, stop, seed ; le format neutre', async () => {
  const { client, fake } = createFakeClient();
  await client.chat({
    model: 'qwen3.5:9b',
    system: 'Consigne',
    messages: [
      { role: 'user', content: 'Regarde', images: ['data:image/png;base64,iVBORw0KGgo='] },
      { role: 'assistant', content: '', toolCalls: [{ id: 't1', name: 'get_scene', args: {} }] },
      { role: 'tool', toolCallId: 't1', name: 'get_scene', content: '{"ok":true}' },
    ],
    think: 'low',
    temperature: 0.2,
    maxTokens: 4096,
    topP: 0.9,
    stop: ['FIN'],
    seed: 7,
  });
  const body = fake.received('/v1/chat/completions')[0].json;
  assertIncludes(body, { reasoning_effort: 'low', temperature: 0.2, max_tokens: 4096, top_p: 0.9, stop: ['FIN'], seed: 7, stream: true, stream_options: { include_usage: true } });
  assert.deepEqual(body.messages, [
    { role: 'system', content: 'Consigne' },
    { role: 'user', content: [{ type: 'text', text: 'Regarde' }, { type: 'image_url', image_url: { url: 'data:image/png;base64,iVBORw0KGgo=' } }] },
    { role: 'assistant', content: null, tool_calls: [{ id: 't1', type: 'function', function: { name: 'get_scene', arguments: '{}' } }] },
    { role: 'tool', tool_call_id: 't1', content: '{"ok":true}' },
  ]);
  await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }], think: true });
  assert.equal(fake.received('/v1/chat/completions')[1].json.reasoning_effort, undefined);
});

test('appels d’outils : plusieurs, entrelacés ; arguments illisibles gardés dans raw', async () => {
  const { client, fake } = createFakeClient();
  fake.nextChat({
    sse: [
      chunk({ tool_calls: [{ index: 0, id: 'a', type: 'function', function: { name: 'get_scene', arguments: '' } }] }),
      chunk({ tool_calls: [{ index: 1, id: 'b', type: 'function', function: { name: 'set_light', arguments: '{"fixture": 3,' } }] }),
      chunk({ tool_calls: [{ index: 0, function: { arguments: '{}' } }] }),
      chunk({}, 'tool_calls'),
      'data: [DONE]',
    ],
  });
  const r = await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Vas-y' }], tools: [{ name: 'get_scene' }, { name: 'set_light' }] });
  assert.deepEqual(r.toolCalls, [
    { id: 'a', name: 'get_scene', args: {}, raw: '{}' },
    { id: 'b', name: 'set_light', args: null, raw: '{"fixture": 3,' },
  ]);
});

test('délai d’inactivité remis à zéro par « : aiodos loading », et dépassé sans signe de vie', async () => {
  const { client, fake } = createFakeClient();
  fake.nextChat({
    sse: [': aiodos loading 1', ': aiodos loading 2', ': aiodos loading 3', ': aiodos loading 4', chunk({ content: 'Prêt' }), chunk({}, 'stop'), 'data: [DONE]'],
    delayMs: 70,
  });
  const loading = [];
  const t0 = Date.now();
  // mistral:7b est chargé : le premier délai est `timeout`, 150 ms, plus court que le flux entier
  const r = await client.chat({ model: 'mistral:7b', messages: [{ role: 'user', content: 'Bonjour' }], timeout: 150, loadTimeout: 150, onLoading: (l) => loading.push(l.seconds) });
  assert.equal(r.text, 'Prêt');
  assert.deepEqual(loading, [1, 2, 3, 4]);
  assert.ok(Date.now() - t0 >= 400);

  fake.nextChat({ sse: [chunk({ content: 'Bon' }), chunk({ content: 'jour' }), chunk({}, 'stop'), 'data: [DONE]'], stallAfter: 1 });
  await assert.rejects(client.chat({ model: 'mistral:7b', messages: [{ role: 'user', content: 'Bonjour' }], timeout: 150 }), (e) => e.code === 'timeout' && e.name === 'Timeout');
  assert.equal(fake.closed, 1); // le client a fermé le flux
});

test('avant le premier morceau, loadTimeout si le modèle n’est pas chargé', async () => {
  const sse = [chunk({ content: 'Salut' }), chunk({}, 'stop'), 'data: [DONE]'];
  const { client, fake } = createFakeClient();
  // qwen3.5:9b n'est pas chargé (catalogue) : 600 ms laissés au premier morceau
  fake.nextChat({ sse, firstDelayMs: 300, delayMs: 0 });
  const r = await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }], timeout: 100, loadTimeout: 600 });
  assert.equal(r.text, 'Salut');
  // mistral:7b est chargé : 100 ms seulement
  fake.nextChat({ sse, firstDelayMs: 300, delayMs: 0 });
  await assert.rejects(client.chat({ model: 'mistral:7b', messages: [{ role: 'user', content: 'Bonjour' }], timeout: 100, loadTimeout: 600 }), (e) => e.code === 'timeout');
});

test('annulation par AbortSignal : « Arrêté », et le vrai serveur voit la connexion fermée', async () => {
  const srv = await startFakeServer({ chatDelayMs: 80 });
  try {
    srv.fake.nextChat({ sse: [chunk({ content: 'Un' }), chunk({ content: ' deux' }), chunk({ content: ' trois' }), chunk({ content: ' quatre' }), chunk({}, 'stop'), 'data: [DONE]'], delayMs: 80 });
    const client = createClient({ app: 'Essai', appId: 'arkane.essai', key: memoryKeyStore(FAKE_KEY), url: srv.url, env: NO_INSTALL });
    const ctrl = new AbortController();
    const seen = [];
    let closedSeen = false;
    srv.fake.on((kind) => {
      if (kind === 'closed') closedSeen = true;
    });
    await assert.rejects(
      client.chat({
        model: 'qwen3.5:9b',
        messages: [{ role: 'user', content: 'Compte' }],
        signal: ctrl.signal,
        onText: (t) => {
          seen.push(t);
          if (seen.length === 1) ctrl.abort(new Error('Arrêté par l’utilisateur'));
        },
      }),
      (e) => e.code === 'cancelled' && e.name === 'Cancelled' && e.message === 'Arrêté',
    );
    for (let i = 0; i < 50 && !closedSeen; i++) await new Promise((r) => setTimeout(r, 20));
    assert.equal(closedSeen, true, 'le serveur doit voir la connexion fermée avant la fin du flux');
    assert.equal(srv.fake.closed, 1);
    assert.deepEqual(seen, ['Un']);
  } finally {
    await srv.close();
  }
});

test('un rappel qui lève : son erreur remonte telle quelle, le flux est fermé', async () => {
  const { client, fake } = createFakeClient();
  fake.nextChat({ sse: [chunk({ content: 'a' }), chunk({ content: 'b' }), chunk({}, 'stop'), 'data: [DONE]'], delayMs: 20 });
  const boom = new Error('boum');
  await assert.rejects(
    client.chat({
      model: 'qwen3.5:9b',
      messages: [{ role: 'user', content: 'x' }],
      onText: () => {
        throw boom;
      },
    }),
    (e) => e === boom,
  );
  assert.equal(fake.closed, 1);
});

test('flux coupé : connexion perdue en route, ou fin sans [DONE] ni finish_reason', async () => {
  const { client, fake } = createFakeClient();
  fake.nextChat({ sse: [chunk({ content: 'Bon' }), chunk({ content: 'jour' })], cutAfter: 1 });
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] }), (e) => e.code === 'unreachable' && e.message.includes('Connexion refusée'));
  fake.nextChat({ sse: [chunk({ content: 'Bon' })] });
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] }), (e) => e.code === 'protocol' && /avant la fin/.test(e.message));
  fake.nextChat({ sse: ['data: {pas du JSON'] });
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] }), (e) => e.code === 'protocol');
});

test('après une connexion perdue en plein flux, la preuve est refaite avant de renvoyer la clé', async () => {
  const { client, fake } = createFakeClient();
  const proofs = () => fake.requests.filter((r) => r.path.includes('challenge=')).length;
  await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] });
  await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] });
  assert.equal(proofs(), 1); // moins de 60 s, même instance : une seule preuve
  fake.nextChat({ sse: [chunk({ content: 'Bon' }), chunk({ content: 'jour' })], cutAfter: 1 });
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] }), (e) => e.code === 'unreachable');
  const before = fake.requests.length;
  await client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] });
  assert.equal(proofs(), 2);
  const after = fake.requests.slice(before);
  const firstKeyed = after.findIndex((r) => r.headers.authorization);
  assert.ok(after.findIndex((r) => r.path.includes('challenge=')) < firstKeyed, 'la preuve avant la clé');
});

test('une autre instance d’AIODOS (fichier de découverte) : la preuve est refaite', async () => {
  const { writeFakeDiscovery } = await import('../src/fake.js');
  const disc = await writeFakeDiscovery({ state: 'open' });
  try {
    const { client, fake } = createFakeClient({ url: undefined, env: disc.env });
    const proofs = () => fake.requests.filter((r) => r.path.includes('challenge=')).length;
    await client.models();
    await client.models();
    assert.equal(proofs(), 1);
    fake.instance = 'Nouvelle1';
    await disc.write({ instance: 'Nouvelle1' });
    await client.models();
    assert.equal(proofs(), 2);
  } finally {
    await disc.remove();
  }
});

test('vérifié avant tout envoi : modèle, messages ; « chat » absent de features', async () => {
  const { client, fake } = createFakeClient();
  await assert.rejects(client.chat({ messages: [] }), (e) => e.code === 'invalid_request' && e.param === 'model');
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: 'Bonjour' }), (e) => e.param === 'messages');
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x', images: ['https://exemple.fr/a.png'] }] }), (e) => e.param === 'images');
  assert.equal(fake.requests.length, 0);
  const old = createFakeClient({ fake: { features: ['access', 'models'] } });
  await assert.rejects(old.client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] }), (e) => e.code === 'not_supported' && e.detail.feature === 'chat');
  assert.equal(old.fake.received('/v1/chat/completions').length, 0);
});

test('les erreurs avant le flux gardent leur forme (busy, Retry-After)', async () => {
  const { client, fake } = createFakeClient();
  fake.failNext('busy', { path: '/v1/chat/completions' });
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] }), (e) => e.code === 'busy' && e.name === 'Busy' && e.retryAfter === 2 && e.retryable === true && e.status === 429);
  fake.failNext('model_not_found', { path: '/v1/chat/completions' });
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'x' }] }), (e) => e.name === 'ModelNotFound' && e.param === 'model');
});
