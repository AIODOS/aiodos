// SPDX-License-Identifier: Apache-2.0
// aiodos-client 0.2.0 — essais : status() dans tous les états du tableau § 6 de
// docs/api-applications.md, sans jamais rejeter ; waitReady().

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { texts } from '../src/index.js';
import { FAKE_KEY, createFakeClient, createFakeHome, writeFakeDiscovery } from '../src/fake.js';

async function deadPid() {
  const child = spawn(process.execPath, ['-e', ''], { stdio: 'ignore', windowsHide: true });
  await once(child, 'exit');
  return child.pid;
}

function expectState(st, state) {
  assert.equal(st.state, state, `${st.state} (${st.code}) au lieu de ${state}`);
  assert.equal(typeof st.message, 'string');
  assert.ok(st.message.length > 0);
}

test('not_installed : ni fichier de découverte, ni installation', async () => {
  const { client } = createFakeClient({ key: null, fake: { listening: false } });
  const st = await client.status();
  expectState(st, 'not_installed');
  assert.equal(st.installed, false);
  assert.equal(st.message, texts.state('not_installed'));
});

test('not_running : running false, pid mort, ou installé sans réponse', async () => {
  const stopped = await writeFakeDiscovery({ state: 'stopped' });
  const dead = await writeFakeDiscovery({ state: 'open', pid: await deadPid() });
  const home = await createFakeHome();
  try {
    for (const env of [stopped.env, dead.env, home.env]) {
      const { client } = createFakeClient({ key: null, fake: { listening: false }, env });
      const st = await client.status();
      expectState(st, 'not_running');
      assert.equal(st.installed, true);
      assert.equal(st.running, false);
      assert.equal(st.code, 'unreachable');
    }
  } finally {
    await stopped.remove();
    await dead.remove();
    await home.remove();
  }
});

test('gateway_closed et gateway_error : AIODOS tourne, la passerelle n’écoute pas', async () => {
  const closed = await writeFakeDiscovery({ state: 'closed' });
  const error = await writeFakeDiscovery({ state: 'error' });
  try {
    const a = await createFakeClient({ key: null, fake: { listening: false }, env: closed.env }).client.status();
    expectState(a, 'gateway_closed');
    assert.equal(a.gateway.state, 'closed');
    const b = await createFakeClient({ fake: { listening: false }, env: error.env }).client.status();
    expectState(b, 'gateway_error');
    assert.match(b.message, /Le port 11435 est déjà pris/); // le message d'AIODOS
  } finally {
    await closed.remove();
    await error.remove();
  }
});

test('apps_closed : ouverte à la seule clé générale', async () => {
  for (const key of [null, FAKE_KEY]) {
    const { client } = createFakeClient({ key, fake: { access: { apps: false } } });
    expectState(await client.status(), 'apps_closed');
  }
});

test('not_aiodos (Ollama sur le port) et too_old (ancien AIODOS)', async () => {
  const ollama = await createFakeClient({ fake: { mode: 'ollama' } }).client.status();
  expectState(ollama, 'not_aiodos');
  const old = await createFakeClient({ fake: { mode: 'old' } }).client.status();
  expectState(old, 'too_old');
  const api0 = await createFakeClient({ fake: { api: 0 } }).client.status();
  expectState(api0, 'too_old');
});

test('untrusted : la preuve est fausse, la clé n’est pas envoyée', async () => {
  const { client, fake } = createFakeClient({ fake: { wrongProof: true } });
  const st = await client.status();
  expectState(st, 'untrusted');
  assert.ok(fake.requests.length >= 2);
  for (const r of fake.requests) {
    assert.equal(r.headers.authorization, undefined);
    assert.equal(r.headers['x-api-key'], undefined);
  }
  // et aucune autre fonction ne l'envoie
  await assert.rejects(client.models(), (e) => e.code === 'untrusted' && e.name === 'Untrusted');
  await assert.rejects(client.chat({ model: 'qwen3.5:9b', messages: [{ role: 'user', content: 'Bonjour' }] }), (e) => e.code === 'untrusted');
  assert.ok(fake.requests.every((r) => r.headers.authorization === undefined));
});

test('headless, requests_closed, needs_access : sans clé', async () => {
  expectState(await createFakeClient({ key: null, fake: { access: { approval_ui: false } } }).client.status(), 'headless');
  expectState(await createFakeClient({ key: null, fake: { access: { requests: false } } }).client.status(), 'requests_closed');
  const st = await createFakeClient({ key: null }).client.status();
  expectState(st, 'needs_access');
  assert.deepEqual(st.features, ['access', 'models', 'chat', 'embed', 'open']);
  assert.equal(st.version, '2.1.0');
  // une clé qui n'a pas la forme d'une clé d'appli n'est pas envoyée
  const { client, fake } = createFakeClient({ key: `aiodos-${'x'.repeat(43)}` });
  const bad = await client.status();
  expectState(bad, 'needs_access');
  assert.equal(bad.code, 'bad_key');
  assert.ok(fake.requests.every((r) => r.headers.authorization === undefined));
});

test('pending puis ready : une demande en cours, le code', async () => {
  const { client, fake } = createFakeClient({ key: null, fake: { interval: 0.05 } });
  let seen = null;
  const result = await client.connect({
    scopes: ['chat'],
    where: ['here'],
    onCode: async (code) => {
      seen = await client.status();
      assert.equal(seen.userCode, code);
      setTimeout(() => fake.approve(), 30);
    },
  });
  expectState(seen, 'pending');
  assert.match(seen.message, new RegExp(`Code : ${seen.userCode}$`));
  assert.equal(result.upgraded, false);
  const st = await client.status();
  expectState(st, 'ready');
});

test('denied : l’utilisateur a refusé', async () => {
  const { client, fake } = createFakeClient({ key: null, fake: { interval: 0.05 } });
  await assert.rejects(
    client.connect({ scopes: ['chat'], onCode: () => setTimeout(() => fake.deny(), 20) }),
    (e) => e.code === 'access_denied',
  );
  expectState(await client.status(), 'denied');
});

test('revoked : clé retirée, ou expirée', async () => {
  for (const state of ['revoked', 'expired']) {
    const { client, fake } = createFakeClient();
    fake.setKeyState(FAKE_KEY, state);
    const st = await client.status();
    expectState(st, 'revoked');
    assert.equal(st.code, state === 'revoked' ? 'key_revoked' : 'key_expired');
  }
});

test('info.json « clé inconnue » (auth invalid) après une preuve juste : needs_access', async () => {
  const contract = JSON.parse(readFileSync(new URL('../../contract/info.json', import.meta.url), 'utf8'));
  const unknown = contract.interactions.find((i) => i.name === 'info avec une clé inconnue').response;
  const { client, fake } = createFakeClient();
  fake.respondNext(unknown, { path: '/aiodos/v1/info', when: (r) => Boolean(r.headers.authorization) });
  const st = await client.status();
  expectState(st, 'needs_access');
  assert.equal(st.code, 'invalid_api_key');
});

test('other_account : AIODOS sous un autre compte Windows', async () => {
  const { client, fake } = createFakeClient();
  fake.failNext('other_account', { path: '/aiodos/v1/info', message: 'Autre compte.' });
  expectState(await client.status(), 'other_account');
});

test('ready : l’appli, ses droits, son lieu, les limites', async () => {
  const st = await createFakeClient().client.status();
  expectState(st, 'ready');
  assert.equal(st.message, 'Connecté à AIODOS.');
  assert.deepEqual(st.app, { id: 'k3j7m2qa', name: 'PANDORA', kind: 'app', scopes: ['chat', 'embed'], where: ['here', 'mine'], priority: 'interactive', expires: null });
  assert.deepEqual(st.scopes, ['chat', 'embed']);
  assert.deepEqual(st.where, ['here', 'mine']);
  assert.deepEqual(st.limits, { requestsPerMinute: 120, streams: 4, maxBodyBytes: 67108864 });
  assert.equal(st.instance, 'Hk3v9QmZ');
  assert.equal(st.url, 'http://127.0.0.1:11435');
  const en = await createFakeClient({ lang: 'en' }).client.status();
  assert.equal(en.message, 'Connected to AIODOS.');
});

test('status() ne rejette jamais : coffre qui échoue, fetch qui lève n’importe quoi', async () => {
  const broken = createFakeClient({ key: { get: () => { throw new Error('coffre cassé'); }, set: () => {} } }).client;
  const a = await broken.status();
  expectState(a, 'needs_access');
  assert.equal(a.code, 'key_store');
  const weird = createFakeClient({ fetch: async () => { throw 42; } }).client; // eslint-disable-line no-throw-literal
  const b = await weird.status();
  assert.ok(['not_aiodos', 'not_installed', 'not_running'].includes(b.state), b.state);
  const ctrl = new AbortController();
  ctrl.abort();
  const c = await createFakeClient().client.status({ signal: ctrl.signal });
  assert.equal(typeof c.state, 'string');
});

test('waitReady : attend qu’AIODOS réponde, ou dépasse son délai', async () => {
  const home = await createFakeHome();
  try {
    const { client, fake } = createFakeClient({ key: null, fake: { listening: false }, env: home.env });
    setTimeout(() => {
      fake.listening = true;
    }, 150);
    const st = await client.waitReady({ interval: 40, timeout: 3000 });
    expectState(st, 'needs_access');
    // jamais prêt : délai dépassé
    const never = createFakeClient({ key: null, fake: { listening: false }, env: home.env }).client;
    await assert.rejects(never.waitReady({ interval: 30, timeout: 120 }), (e) => e.code === 'timeout' && e.detail.state === 'not_running');
    // pas installé : rien à attendre
    const none = createFakeClient({ key: null, fake: { listening: false } }).client;
    expectState(await none.waitReady({ interval: 30, timeout: 5000 }), 'not_installed');
    // until: 'ready'
    const ready = createFakeClient().client;
    expectState(await ready.waitReady({ until: 'ready', interval: 30, timeout: 1000 }), 'ready');
  } finally {
    await home.remove();
  }
});
